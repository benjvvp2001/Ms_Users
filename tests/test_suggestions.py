from __future__ import annotations

import os
from datetime import date

os.environ["JWT_SECRET"] = "test-only-secret-that-is-long-enough"

from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from app.repositories.mock_user_repository import MockUserRepository
from fakes import REGISTER_PATH, FakeEmailSender, register_and_verify

SUGGESTIONS_PATH = "/api/v1/users/suggestions"


def make_client() -> tuple[TestClient, MockUserRepository]:
    repository = MockUserRepository()
    client = TestClient(create_app(repository=repository, email_sender=FakeEmailSender()))
    return client, repository


def player(email: str, nombre: str = "Ana", apellido: str = "Torres", **extra: object) -> dict:
    return {
        "email": email,
        "password": "A-secure-test-password-1",
        "nombre": nombre,
        "apellido_paterno": apellido,
        **extra,
    }


def signed_up(client: TestClient, email: str, deportes: list[str] = (), **extra: object) -> dict:
    """Register and verify a player, then declare their sports; returns
    {"user_id", "headers"}."""
    tokens = register_and_verify(client, player(email, **extra))
    user_id = tokens["user"]["user_id"]
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    if deportes:
        response = client.put(
            f"/api/v1/users/{user_id}/preferences",
            headers=headers,
            json={"deportes": [{"deporte_codigo": code, "nivel": 3} for code in deportes]},
        )
        assert response.status_code == 200
    return {"user_id": user_id, "headers": headers}


def test_requires_a_token() -> None:
    client, _ = make_client()
    assert client.get(SUGGESTIONS_PATH).status_code == 401


def test_lists_other_players_but_never_the_caller() -> None:
    client, _ = make_client()
    me = signed_up(client, "me@example.com")
    other = signed_up(client, "other@example.com", nombre="Diego", apellido="Araya")

    response = client.get(SUGGESTIONS_PATH, headers=me["headers"])

    assert response.status_code == 200
    ids = [card["user_id"] for card in response.json()]
    assert ids == [other["user_id"]]


def test_empty_list_when_there_are_no_other_athletes() -> None:
    client, _ = make_client()
    me = signed_up(client, "me@example.com")
    response = client.get(SUGGESTIONS_PATH, headers=me["headers"])
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.parametrize(
    ("role", "visible"),
    [("player", True), ("usuario", True), ("admin", False), ("club_admin", False)],
)
def test_only_athlete_roles_are_listed(role: str, visible: bool) -> None:
    client, repository = make_client()
    me = signed_up(client, "me@example.com")
    other = signed_up(client, "other@example.com")
    repository.get_user_by_email("other@example.com").role = role

    cards = client.get(SUGGESTIONS_PATH, headers=me["headers"]).json()
    assert [card["user_id"] for card in cards] == ([other["user_id"]] if visible else [])


def test_new_athlete_appears_after_verification_and_disappears_after_deletion() -> None:
    client, _ = make_client()
    me = signed_up(client, "me@example.com")
    registered = client.post(REGISTER_PATH, json=player("new@example.com"))
    assert registered.status_code == 201
    assert client.get(SUGGESTIONS_PATH, headers=me["headers"]).json() == []

    verified = client.post(
        "/api/v1/users/auth/email-verification/confirm",
        json={"email": "new@example.com", "code": client.app.state.email_sender.last_code("new@example.com")},
    )
    assert verified.status_code == 200
    tokens = verified.json()
    other_id = tokens["user"]["user_id"]
    cards = client.get(SUGGESTIONS_PATH, headers=me["headers"]).json()
    assert [card["user_id"] for card in cards] == [other_id]
    assert cards[0]["deportes"] == []  # Completing a sports profile is optional.

    deleted = client.delete(
        f"/api/v1/users/{other_id}",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert deleted.status_code == 204
    assert client.get(SUGGESTIONS_PATH, headers=me["headers"]).json() == []


def test_unverified_and_non_player_accounts_are_hidden() -> None:
    client, repository = make_client()
    me = signed_up(client, "me@example.com")
    client.post(REGISTER_PATH, json=player("pending@example.com"))
    admin = signed_up(client, "admin@example.com")
    repository._users[repository.get_user_by_email("admin@example.com").id].role = "admin"

    response = client.get(SUGGESTIONS_PATH, headers=me["headers"])

    assert response.json() == []
    assert admin["user_id"]


def test_cards_expose_only_public_fields() -> None:
    client, repository = make_client()
    me = signed_up(client, "me@example.com")
    other = signed_up(
        client, "diego@example.com", nombre="Diego", apellido="araya", rut="12345678-5"
    )
    stored = repository._users[repository.get_user_by_email("diego@example.com").id]
    stored.profile = stored.profile.model_copy(
        update={"telefono": "+56911112222", "fecha_nacimiento": date(2000, 1, 1)}
    )

    card = client.get(SUGGESTIONS_PATH, headers=me["headers"]).json()[0]

    assert card["user_id"] == other["user_id"]
    assert card["nombre"] == "Diego"
    assert card["apellido_inicial"] == "A."
    assert card["edad"] >= 26
    assert set(card) == {
        "user_id",
        "nombre",
        "apellido_inicial",
        "edad",
        "foto_perfil",
        "biografia",
        "deportes",
        "compatibilidad",
    }
    assert "diego@example.com" not in str(card)
    assert "12345678" not in str(card)


def test_ranked_by_shared_sports() -> None:
    client, _ = make_client()
    me = signed_up(client, "me@example.com", deportes=["tennis", "running"])
    same = signed_up(client, "same@example.com", deportes=["tennis", "running"])
    half = signed_up(client, "half@example.com", deportes=["tennis", "futbol"])
    none = signed_up(client, "none@example.com", deportes=["natacion"])
    empty = signed_up(client, "empty@example.com")

    cards = client.get(SUGGESTIONS_PATH, headers=me["headers"]).json()

    assert [(card["user_id"], card["compatibilidad"]) for card in cards] == [
        (same["user_id"], 100),
        (half["user_id"], 33),
        # Ties keep newest first.
        (empty["user_id"], 0),
        (none["user_id"], 0),
    ]


def test_limit_is_applied_and_validated() -> None:
    client, _ = make_client()
    me = signed_up(client, "me@example.com")
    for index in range(3):
        signed_up(client, f"p{index}@example.com")

    assert len(client.get(SUGGESTIONS_PATH, headers=me["headers"], params={"limit": 2}).json()) == 2
    for bad in (0, 51, "x"):
        response = client.get(SUGGESTIONS_PATH, headers=me["headers"], params={"limit": bad})
        assert response.status_code == 422
