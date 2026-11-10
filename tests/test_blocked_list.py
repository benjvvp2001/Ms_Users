"""HU-66: ver y desbloquear a las personas bloqueadas (repositorio simulado, sin base de datos)."""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from test_blocks_mutes import USERS, athlete, card, make_client, trio
from test_sanctions import sanction, world

KEYS = {"user_id", "nombre", "apellido_inicial", "foto_perfil", "fecha_bloqueo"}
PHOTO_KEY = "0" * 32 + ".jpg"


def block(client, who, other):
    response = client.post(f"{USERS}/block", headers=who["headers"], json={"blockedUserId": other["id"]})
    assert response.status_code in (200, 201), response.text


def blocked(client, who, **query):
    response = client.get(f"{USERS}/blocks/me", headers=who["headers"], params=query)
    assert response.status_code == 200, response.text
    return response.json()


def people(client, count):
    return [athlete(client, f"Persona{chr(65 + i)}") for i in range(count)]


def test_an_empty_list_is_an_empty_array():
    client, _, ana, _, _ = trio()
    assert blocked(client, ana) == []
    assert client.get(f"{USERS}/me/blocks", headers=ana["headers"]).json() == []


def test_the_list_shows_id_name_and_photo_and_nothing_sensitive():
    client, repository, ana, beto, carla = trio()
    repository.set_photo(UUID(beto["id"]), PHOTO_KEY)
    block(client, ana, beto)
    block(client, ana, carla)
    by_name = {row["nombre"]: row for row in blocked(client, ana)}
    assert set(by_name) == {"Beto", "Carla"}
    assert all(set(row) == KEYS for row in by_name.values())
    assert by_name["Beto"]["user_id"] == beto["id"] and by_name["Beto"]["apellido_inicial"] == "T."
    assert by_name["Beto"]["foto_perfil"] == f"/api/v1/users/photos/{PHOTO_KEY}"
    assert by_name["Carla"]["foto_perfil"] is None                   # no photo: null, the app draws its default avatar
    text = client.get(f"{USERS}/blocks/me", headers=ana["headers"]).text
    for private in ("@example.com", "rut", "telefono", "password", "fecha_nacimiento", "Torres"):
        assert private not in text, private                          # not even the full last name


def test_newest_block_first_and_pages_do_not_overlap():
    client, _, ana, *_ = trio()
    others = people(client, 5)
    for other in others:
        block(client, ana, other)
    expected = [other["id"] for other in reversed(others)]
    assert [row["user_id"] for row in blocked(client, ana)] == expected
    pages = [blocked(client, ana, limit=2, offset=offset) for offset in (0, 2, 4, 6)]
    assert [len(page) for page in pages] == [2, 2, 1, 0]
    assert [row["user_id"] for page in pages for row in page] == expected


def test_the_page_size_has_a_ceiling_and_bad_values_are_refused():
    client, _, ana, *_ = trio()
    for query in ({"limit": 101}, {"limit": 0}, {"limit": -1}, {"offset": -1}, {"limit": "x"}):
        assert client.get(f"{USERS}/blocks/me", headers=ana["headers"], params=query).status_code == 422, query
        assert client.get(f"{USERS}/me/blocks", headers=ana["headers"], params=query).status_code == 422, query
    assert client.get(f"{USERS}/blocks/me", headers=ana["headers"], params={"limit": 100}).status_code == 200


def test_unblocking_removes_that_person_from_the_list_and_only_that_one():
    client, _, ana, beto, carla = trio()
    block(client, ana, beto)
    block(client, ana, carla)
    assert client.delete(f"{USERS}/block/{beto['id']}", headers=ana["headers"]).status_code == 204
    assert [row["nombre"] for row in blocked(client, ana)] == ["Carla"]


def test_unblocking_twice_or_someone_never_blocked_is_not_an_error():
    client, _, ana, beto, carla = trio()
    block(client, ana, beto)
    for _ in range(2):
        assert client.delete(f"{USERS}/block/{beto['id']}", headers=ana["headers"]).status_code == 204
    assert client.delete(f"{USERS}/block/{carla['id']}", headers=ana["headers"]).status_code == 204
    assert client.delete(f"{USERS}/me/blocks/{uuid4()}", headers=ana["headers"]).status_code == 204
    assert blocked(client, ana) == []


def test_only_the_one_who_blocked_can_unblock_and_it_touches_only_that_block():
    client, _, ana, beto, carla = trio()
    block(client, beto, carla)                                        # Beto blocked Carla
    assert client.delete(f"{USERS}/block/{carla['id']}", headers=ana["headers"]).status_code == 204  # Ana never did
    assert [row["user_id"] for row in blocked(client, beto)] == [carla["id"]]
    assert card(client, beto, carla).status_code == 404               # the block still stands
    assert client.delete(f"{USERS}/block/{beto['id']}", headers=carla["headers"]).status_code == 204  # Carla did not block Beto
    assert [row["user_id"] for row in blocked(client, beto)] == [carla["id"]]


def test_each_person_only_ever_sees_their_own_list():
    client, _, ana, beto, carla = trio()
    block(client, ana, beto)
    assert blocked(client, beto) == [] and blocked(client, carla) == []   # the blocked one learns nothing from it
    # there is no way to ask for another person's list: the id comes from the token and the parameter is ignored
    assert blocked(client, carla, user_id=ana["id"], bloqueador_id=ana["id"]) == []
    for path in (f"{USERS}/{ana['id']}/blocks", f"{USERS}/blocks/{ana['id']}"):
        assert client.get(path, headers=carla["headers"]).status_code in (404, 405, 422)


def test_the_list_and_unblock_need_a_session():
    client, _, ana, beto, _ = trio()
    block(client, ana, beto)
    assert client.get(f"{USERS}/blocks/me").status_code == 401
    assert client.get(f"{USERS}/me/blocks").status_code == 401
    assert client.delete(f"{USERS}/block/{beto['id']}").status_code == 401
    assert client.delete(f"{USERS}/me/blocks/{beto['id']}").status_code == 401
    assert client.get(f"{USERS}/blocks/me", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert len(blocked(client, ana)) == 1


def test_a_deleted_account_is_left_out_without_breaking_the_list_or_the_pages():
    client, repository, ana, beto, carla = trio()
    others = people(client, 3)
    for other in (beto, *others, carla):
        block(client, ana, other)
    assert client.delete(f"{USERS}/{beto['id']}", headers=beto["headers"]).status_code == 204
    names = [row["nombre"] for row in blocked(client, ana)]
    assert "Beto" not in names and len(names) == 4
    assert [len(blocked(client, ana, limit=2, offset=o)) for o in (0, 2, 4)] == [2, 2, 0]   # pages stay full
    assert client.delete(f"{USERS}/block/{beto['id']}", headers=ana["headers"]).status_code == 204  # still harmless


def test_a_suspended_account_is_listed_like_any_other_with_nothing_that_says_so():
    client, repository, admin, ana, victim = world()
    block(client, ana, victim)
    assert sanction(client, admin, victim, tipo="permanente").status_code == 201
    rows = blocked(client, ana)
    assert [row["nombre"] for row in rows] == ["Beto"] and set(rows[0]) == KEYS
    text = client.get(f"{USERS}/blocks/me", headers=ana["headers"]).text.lower()
    for hint in ("suspend", "sancion", "permanente", "account_suspended"):
        assert hint not in text, hint
    assert client.delete(f"{USERS}/block/{victim['id']}", headers=ana["headers"]).status_code == 204
    assert blocked(client, ana) == []


def test_after_unblocking_both_can_reach_each_other_again():
    client, _, ana, beto, _ = trio()
    assert card(client, ana, beto).status_code == 200 and card(client, beto, ana).status_code == 200
    block(client, ana, beto)
    assert card(client, ana, beto).status_code == 404 and card(client, beto, ana).status_code == 404
    client.delete(f"{USERS}/block/{beto['id']}", headers=ana["headers"])
    assert card(client, ana, beto).status_code == 200 and card(client, beto, ana).status_code == 200
    ids = {c["user_id"] for c in client.post(f"{USERS}/athletes/cards", headers=beto["headers"],
                                              json=[ana["id"]]).json()}
    assert ids == {ana["id"]}


def test_the_blocked_person_gets_no_sign_of_it():
    client, _, ana, beto, _ = trio()
    block(client, ana, beto)
    unknown = client.get(f"{USERS}/athletes/{uuid4()}", headers=beto["headers"])
    seen = card(client, beto, ana)
    assert seen.status_code == unknown.status_code == 404 and seen.json() == unknown.json()   # same as "no such person"
    assert blocked(client, beto) == []
    assert client.get(f"{USERS}/chat-preferences", headers=beto["headers"]).json() == {"muted_users": []}
    assert ana["id"] not in client.get(f"{USERS}/me/blocks", headers=beto["headers"]).text


def test_unblocking_is_audited_with_the_resource_and_nothing_else():
    client, repository, ana, beto, _ = trio()
    block(client, ana, beto)
    client.delete(f"{USERS}/block/{beto['id']}", headers=ana["headers"])
    events = [e for e in repository._audit_events if e.action == "unblock_user"]
    assert [(str(e.user_id), e.resource, e.result) for e in events] == [(ana["id"], f"users/{beto['id']}", "success")]
