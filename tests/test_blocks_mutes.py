"""HU-51: block people (cutting contact) and mute them (chat preferences)."""
from __future__ import annotations

import os
from uuid import uuid4

os.environ["JWT_SECRET"] = "test-only-secret-that-is-long-enough"

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.repositories.mock_user_repository import MockUserRepository
from fakes import FakeEmailSender, register_and_verify

USERS = "/api/v1/users"


def make_client() -> tuple[TestClient, MockUserRepository]:
    repository = MockUserRepository()
    return TestClient(create_app(repository=repository, email_sender=FakeEmailSender())), repository


def athlete(client: TestClient, nombre: str) -> dict:
    tokens = register_and_verify(client, {
        "email": f"{nombre.lower()}@example.com", "password": "A-secure-test-password-1",
        "nombre": nombre, "apellido_paterno": "Torres",
    })
    return {"id": tokens["user"]["user_id"], "headers": {"Authorization": f"Bearer {tokens['access_token']}"}}


def trio():
    client, repository = make_client()
    return client, repository, athlete(client, "Ana"), athlete(client, "Beto"), athlete(client, "Carla")


def card(client, viewer, other):
    return client.get(f"{USERS}/athletes/{other['id']}", headers=viewer["headers"])


# -- blocking ----------------------------------------------------------------------------------------------


def test_post_block_creates_it_and_repeating_it_is_harmless():
    client, _, ana, beto, _ = trio()
    first = client.post(f"{USERS}/block", headers=ana["headers"], json={"blockedUserId": beto["id"]})
    assert first.status_code == 201, first.text
    body = first.json()
    assert (body["user_id"], body["nombre"], body["apellido_inicial"]) == (beto["id"], "Beto", "T.")
    assert body["fecha_bloqueo"]
    again = client.post(f"{USERS}/block", headers=ana["headers"], json={"blockedUserId": beto["id"]})
    assert again.status_code == 200
    assert again.json()["fecha_bloqueo"] == body["fecha_bloqueo"]  # the original date is kept
    assert len(client.get(f"{USERS}/blocks/me", headers=ana["headers"]).json()) == 1


def test_the_body_accepts_camel_and_snake_case_and_nothing_else():
    client, _, ana, beto, carla = trio()
    assert client.post(f"{USERS}/block", headers=ana["headers"], json={"blocked_user_id": beto["id"]}).status_code == 201
    assert client.post(f"{USERS}/block", headers=ana["headers"], json={"blockedUserId": carla["id"], "x": 1}).status_code == 422
    for body in ({}, {"blockedUserId": None}, {"blockedUserId": "no-es-uuid"}, {"blockedUserId": 7}):
        assert client.post(f"{USERS}/block", headers=ana["headers"], json=body).status_code == 422


def test_who_cannot_be_blocked_and_who_can_try():
    client, _, ana, *_ = trio()
    assert client.post(f"{USERS}/block", headers=ana["headers"], json={"blockedUserId": ana["id"]}).status_code == 422
    assert client.post(f"{USERS}/block", headers=ana["headers"], json={"blockedUserId": str(uuid4())}).status_code == 404
    assert client.post(f"{USERS}/block", json={"blockedUserId": str(uuid4())}).status_code == 401


def test_delete_block_unblocks_and_is_idempotent():
    client, _, ana, beto, _ = trio()
    client.post(f"{USERS}/block", headers=ana["headers"], json={"blockedUserId": beto["id"]})
    assert client.delete(f"{USERS}/block/{beto['id']}", headers=ana["headers"]).status_code == 204
    assert client.delete(f"{USERS}/block/{beto['id']}", headers=ana["headers"]).status_code == 204
    assert client.delete(f"{USERS}/block/{uuid4()}", headers=ana["headers"]).status_code == 204
    assert client.get(f"{USERS}/blocks/me", headers=ana["headers"]).json() == []
    assert client.delete(f"{USERS}/block/no-es-uuid", headers=ana["headers"]).status_code == 422
    assert client.delete(f"{USERS}/block/{beto['id']}").status_code == 401


def test_the_old_and_new_routes_share_the_same_blocks():
    client, _, ana, beto, carla = trio()
    client.put(f"{USERS}/me/blocks/{beto['id']}", headers=ana["headers"])  # the route the app uses today
    client.post(f"{USERS}/block", headers=ana["headers"], json={"blockedUserId": carla["id"]})
    assert {b["user_id"] for b in client.get(f"{USERS}/blocks/me", headers=ana["headers"]).json()} == {beto["id"], carla["id"]}
    assert {b["user_id"] for b in client.get(f"{USERS}/me/blocks", headers=ana["headers"]).json()} == {beto["id"], carla["id"]}
    client.delete(f"{USERS}/block/{beto['id']}", headers=ana["headers"])
    assert [b["user_id"] for b in client.get(f"{USERS}/me/blocks", headers=ana["headers"]).json()] == [carla["id"]]


def test_blocks_me_lists_only_my_own_blocks_newest_first():
    client, _, ana, beto, carla = trio()
    client.post(f"{USERS}/block", headers=ana["headers"], json={"blockedUserId": beto["id"]})
    client.post(f"{USERS}/block", headers=ana["headers"], json={"blockedUserId": carla["id"]})
    assert [b["nombre"] for b in client.get(f"{USERS}/blocks/me", headers=ana["headers"]).json()] == ["Carla", "Beto"]
    assert client.get(f"{USERS}/blocks/me", headers=beto["headers"]).json() == []  # being blocked is not listed
    assert client.get(f"{USERS}/blocks/me").status_code == 401


# -- the block cuts contact ------------------------------------------------------------------------------------


def test_a_block_withholds_the_athlete_card_both_ways():
    client, _, ana, beto, carla = trio()
    assert card(client, ana, beto).status_code == 200 and card(client, beto, ana).status_code == 200
    client.post(f"{USERS}/block", headers=ana["headers"], json={"blockedUserId": beto["id"]})
    # Matching and Activities ask for this card before any request, message or listing.
    assert card(client, ana, beto).status_code == 404
    assert card(client, beto, ana).status_code == 404  # also for the person who was blocked
    assert card(client, carla, ana).status_code == 200 and card(client, carla, beto).status_code == 200
    assert card(client, ana, ana).status_code == 200  # a person's own card is never withheld


def test_the_batch_endpoint_leaves_blocked_people_out_like_deactivated_accounts():
    client, _, ana, beto, carla = trio()
    client.post(f"{USERS}/block", headers=beto["headers"], json={"blockedUserId": ana["id"]})  # Beto blocked Ana
    ids = [ana["id"], beto["id"], carla["id"]]
    from_ana = client.post(f"{USERS}/athletes/cards", headers=ana["headers"], json=ids)
    assert {c["user_id"] for c in from_ana.json()} == {ana["id"], carla["id"]}
    from_beto = client.post(f"{USERS}/athletes/cards", headers=beto["headers"], json=ids)
    assert {c["user_id"] for c in from_beto.json()} == {beto["id"], carla["id"]}
    from_carla = client.post(f"{USERS}/athletes/cards", headers=carla["headers"], json=ids)
    assert {c["user_id"] for c in from_carla.json()} == set(ids)


def test_unblocking_restores_everything_and_a_block_in_either_direction_is_enough():
    client, _, ana, beto, _ = trio()
    client.post(f"{USERS}/block", headers=ana["headers"], json={"blockedUserId": beto["id"]})
    client.post(f"{USERS}/block", headers=beto["headers"], json={"blockedUserId": ana["id"]})  # both ways
    client.delete(f"{USERS}/block/{beto['id']}", headers=ana["headers"])
    assert card(client, ana, beto).status_code == 404  # Beto's block still stands
    client.delete(f"{USERS}/block/{ana['id']}", headers=beto["headers"])
    assert card(client, ana, beto).status_code == 200 and card(client, beto, ana).status_code == 200


# -- the check other services use ---------------------------------------------------------------------------------


def check(client, who, a, b):
    return client.get(f"{USERS}/blocks/check", headers=who["headers"], params={"userA": a["id"], "userB": b["id"]})


def test_check_reports_a_block_in_either_direction_and_never_who_placed_it():
    client, _, ana, beto, carla = trio()
    assert check(client, ana, ana, beto).json() == {"blocked": False}
    client.post(f"{USERS}/block", headers=ana["headers"], json={"blockedUserId": beto["id"]})
    for who in (ana, beto):  # either of the two can ask
        for a, b in ((ana, beto), (beto, ana)):
            response = check(client, who, a, b)
            assert response.status_code == 200 and response.json() == {"blocked": True}
    assert check(client, ana, ana, carla).json() == {"blocked": False}
    client.delete(f"{USERS}/block/{beto['id']}", headers=ana["headers"])
    assert check(client, ana, ana, beto).json() == {"blocked": False}


def test_check_is_only_for_the_people_involved_or_an_admin():
    client, repository, ana, beto, carla = trio()
    client.post(f"{USERS}/block", headers=ana["headers"], json={"blockedUserId": beto["id"]})
    outsider = check(client, carla, ana, beto)  # Carla cannot map who blocked whom
    assert outsider.status_code == 403
    assert outsider.json() == {"detail": "Solo puedes consultar bloqueos en los que participas."}
    repository.get_user_by_email("carla@example.com").role = "admin"
    admin = check(client, carla, ana, beto)
    assert admin.status_code == 200 and admin.json() == {"blocked": True}


def test_check_validates_its_parameters_and_needs_a_session():
    client, _, ana, beto, _ = trio()
    path = f"{USERS}/blocks/check"
    assert client.get(path, headers=ana["headers"]).status_code == 422
    assert client.get(path, headers=ana["headers"], params={"userA": ana["id"]}).status_code == 422
    assert client.get(path, headers=ana["headers"], params={"userA": ana["id"], "userB": "no-es-uuid"}).status_code == 422
    assert client.get(path, params={"userA": ana["id"], "userB": beto["id"]}).status_code == 401
    assert check(client, ana, ana, ana).json() == {"blocked": False}  # a person is never blocked from themselves


# -- muting -------------------------------------------------------------------------------------------------------


def chat_preferences(client, who):
    response = client.get(f"{USERS}/chat-preferences", headers=who["headers"])
    assert response.status_code == 200, response.text
    return response.json()


def test_mute_and_unmute_people():
    client, _, ana, beto, carla = trio()
    assert chat_preferences(client, ana) == {"muted_users": []}
    muted = client.patch(f"{USERS}/chat-preferences", headers=ana["headers"],
                         json={"muteUserIds": [beto["id"], carla["id"]]})
    assert muted.status_code == 200, muted.text
    assert {u["nombre"] for u in muted.json()["muted_users"]} == {"Beto", "Carla"}
    first = muted.json()["muted_users"][0]
    assert set(first) == {"user_id", "nombre", "apellido_inicial", "foto_perfil", "fecha_silencio"}
    unmuted = client.patch(f"{USERS}/chat-preferences", headers=ana["headers"], json={"unmuteUserIds": [beto["id"]]})
    assert [u["user_id"] for u in unmuted.json()["muted_users"]] == [carla["id"]]
    both = client.patch(f"{USERS}/chat-preferences", headers=ana["headers"],
                        json={"mute_user_ids": [beto["id"]], "unmute_user_ids": [carla["id"]]})  # snake_case works too
    assert [u["user_id"] for u in both.json()["muted_users"]] == [beto["id"]]
    assert chat_preferences(client, ana) == both.json()


def test_muting_is_idempotent_and_keeps_the_original_date():
    client, _, ana, beto, _ = trio()
    body = {"muteUserIds": [beto["id"]]}
    first = client.patch(f"{USERS}/chat-preferences", headers=ana["headers"], json=body).json()
    again = client.patch(f"{USERS}/chat-preferences", headers=ana["headers"], json=body).json()
    assert again == first and len(again["muted_users"]) == 1
    nobody = client.patch(f"{USERS}/chat-preferences", headers=ana["headers"], json={"unmuteUserIds": [str(uuid4())]})
    assert nobody.status_code == 200 and len(nobody.json()["muted_users"]) == 1  # unmuting a stranger changes nothing


def test_a_wrong_id_changes_nothing():
    client, _, ana, beto, _ = trio()
    response = client.patch(f"{USERS}/chat-preferences", headers=ana["headers"],
                            json={"muteUserIds": [beto["id"], str(uuid4())]})
    assert response.status_code == 404
    assert chat_preferences(client, ana) == {"muted_users": []}  # Beto was not muted either
    own = client.patch(f"{USERS}/chat-preferences", headers=ana["headers"], json={"muteUserIds": [beto["id"], ana["id"]]})
    assert own.status_code == 422
    assert chat_preferences(client, ana) == {"muted_users": []}


@pytest.mark.parametrize(
    "body",
    [
        {}, {"muteUserIds": []}, {"muteUserIds": [], "unmuteUserIds": []},
        {"muteUserIds": ["a", "b"]}, {"muteUserIds": ["no-es-uuid"]}, {"muteUserIds": None},
        {"muteUserIds": "x"}, {"foo": "bar", "muteUserIds": []},
    ],
)
def test_invalid_changes_are_422(body):
    client, _, ana, *_ = trio()
    assert client.patch(f"{USERS}/chat-preferences", headers=ana["headers"], json=body).status_code == 422


def test_lists_cannot_repeat_people_overlap_or_be_too_long():
    client, _, ana, beto, _ = trio()
    path = f"{USERS}/chat-preferences"
    assert client.patch(path, headers=ana["headers"], json={"muteUserIds": [beto["id"], beto["id"]]}).status_code == 422
    assert client.patch(path, headers=ana["headers"], json={"unmuteUserIds": [beto["id"], beto["id"]]}).status_code == 422
    assert client.patch(path, headers=ana["headers"],
                        json={"muteUserIds": [beto["id"]], "unmuteUserIds": [beto["id"]]}).status_code == 422
    assert client.patch(path, headers=ana["headers"], json={"unmuteUserIds": [str(uuid4()) for _ in range(101)]}).status_code == 422
    assert client.patch(path, headers=ana["headers"], json={"unmuteUserIds": [str(uuid4()) for _ in range(100)]}).status_code == 200


def test_muted_people_are_private_and_need_a_session():
    client, _, ana, beto, carla = trio()
    client.patch(f"{USERS}/chat-preferences", headers=ana["headers"], json={"muteUserIds": [beto["id"]]})
    assert chat_preferences(client, beto) == {"muted_users": []}  # Beto does not learn he was muted
    assert chat_preferences(client, carla) == {"muted_users": []}
    assert client.get(f"{USERS}/chat-preferences").status_code == 401
    assert client.patch(f"{USERS}/chat-preferences", json={"muteUserIds": [beto["id"]]}).status_code == 401


def test_muting_is_not_blocking():
    client, _, ana, beto, _ = trio()
    client.patch(f"{USERS}/chat-preferences", headers=ana["headers"], json={"muteUserIds": [beto["id"]]})
    assert card(client, ana, beto).status_code == 200 and card(client, beto, ana).status_code == 200
    assert check(client, ana, ana, beto).json() == {"blocked": False}
    assert client.get(f"{USERS}/blocks/me", headers=ana["headers"]).json() == []
    # and the two lists are independent
    client.post(f"{USERS}/block", headers=ana["headers"], json={"blockedUserId": beto["id"]})
    client.delete(f"{USERS}/block/{beto['id']}", headers=ana["headers"])
    assert [u["user_id"] for u in chat_preferences(client, ana)["muted_users"]] == [beto["id"]]


def test_the_changes_are_audited():
    client, repository, ana, beto, _ = trio()
    client.post(f"{USERS}/block", headers=ana["headers"], json={"blockedUserId": beto["id"]})
    client.patch(f"{USERS}/chat-preferences", headers=ana["headers"], json={"muteUserIds": [beto["id"]]})
    actions = [event.action for event in repository._audit_events if str(event.user_id) == ana["id"]]
    assert "block_user" in actions and "update_chat_preferences" in actions
