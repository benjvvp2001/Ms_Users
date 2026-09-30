"""Historia "Editar mi perfil (deportes, nivel, disponibilidad, objetivos y zona)".

Criterios de aceptación:
1. Permite editar deportes y nivel.
2. Permite editar disponibilidad y objetivos.
3. Permite definir zona/comuna.
"""

from __future__ import annotations

import os

os.environ["JWT_SECRET"] = "test-only-secret-that-is-long-enough"

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.repositories.mock_user_repository import MockUserRepository
from fakes import FakeEmailSender, register_and_verify

FULL_PREFERENCES = {
    "deportes": [
        {"deporte_codigo": "tennis", "nivel": 2},
        {"deporte_codigo": "running", "nivel": 4},
    ],
    "disponibilidad": [
        {"dia_semana": "lunes", "hora_inicio": "18:00", "hora_fin": "20:00"},
        {"dia_semana": "sabado", "hora_inicio": "09:00", "hora_fin": "11:30"},
    ],
    "objetivos": ["competir", "mejorar_condicion"],
    "zona": {"comuna": "Ñuñoa", "latitud": -33.45694, "longitud": -70.59750},
}


def signed_in(client: TestClient, email: str = "ana@example.com") -> tuple[str, dict[str, str]]:
    tokens = register_and_verify(
        client,
        {
            "email": email,
            "password": "A-secure-test-password-1",
            "nombre": "Ana",
            "apellido_paterno": "Torres",
        },
    )
    return tokens["user"]["user_id"], {"Authorization": f"Bearer {tokens['access_token']}"}


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(repository=MockUserRepository(), email_sender=FakeEmailSender()))


def put_preferences(client: TestClient, user_id: str, headers: dict, body: dict):
    return client.put(f"/api/v1/users/{user_id}/preferences", headers=headers, json=body)


def get_preferences(client: TestClient, user_id: str, headers: dict) -> dict:
    response = client.get(f"/api/v1/users/{user_id}/preferences", headers=headers)
    assert response.status_code == 200
    return response.json()


def test_new_account_starts_without_sports_goals_or_zone(client: TestClient) -> None:
    user_id, headers = signed_in(client)

    preferences = get_preferences(client, user_id, headers)

    assert preferences["deportes"] == []
    assert preferences["disponibilidad"] == []
    assert preferences["objetivos"] == []
    assert preferences["zona"] is None


def test_all_five_sections_are_saved_and_read_back(client: TestClient) -> None:
    user_id, headers = signed_in(client)

    saved = put_preferences(client, user_id, headers, FULL_PREFERENCES)

    assert saved.status_code == 200
    stored = get_preferences(client, user_id, headers)
    for field in ("deportes", "disponibilidad", "objetivos", "zona"):
        assert stored[field] == FULL_PREFERENCES[field], field


# -- 1. deportes y nivel -------------------------------------------------

def test_sports_and_levels_can_be_changed(client: TestClient) -> None:
    user_id, headers = signed_in(client)
    put_preferences(client, user_id, headers, FULL_PREFERENCES)

    edited = {**FULL_PREFERENCES, "deportes": [{"deporte_codigo": "tennis", "nivel": 5}]}
    assert put_preferences(client, user_id, headers, edited).status_code == 200

    assert get_preferences(client, user_id, headers)["deportes"] == [
        {"deporte_codigo": "tennis", "nivel": 5}
    ]


@pytest.mark.parametrize(
    "deportes",
    [
        [{"deporte_codigo": "tennis", "nivel": 0}],
        [{"deporte_codigo": "tennis", "nivel": 6}],
        [{"deporte_codigo": "tennis", "nivel": 3}, {"deporte_codigo": "TENNIS", "nivel": 1}],
        [{"deporte_codigo": "", "nivel": 3}],
    ],
)
def test_invalid_sports_are_rejected(client: TestClient, deportes: list) -> None:
    user_id, headers = signed_in(client)
    body = {**FULL_PREFERENCES, "deportes": deportes}
    assert put_preferences(client, user_id, headers, body).status_code == 422


# -- 2. disponibilidad y objetivos -----------------------------------------

def test_availability_and_goals_can_be_changed_and_cleared(client: TestClient) -> None:
    user_id, headers = signed_in(client)
    put_preferences(client, user_id, headers, FULL_PREFERENCES)

    edited = {
        **FULL_PREFERENCES,
        "disponibilidad": [{"dia_semana": "Domingo", "hora_inicio": "08:00", "hora_fin": "10:00"}],
        "objetivos": ["  socializar  "],
    }
    assert put_preferences(client, user_id, headers, edited).status_code == 200
    stored = get_preferences(client, user_id, headers)
    assert stored["disponibilidad"] == [
        {"dia_semana": "domingo", "hora_inicio": "08:00", "hora_fin": "10:00"}
    ]
    assert stored["objetivos"] == ["socializar"]

    cleared = {**FULL_PREFERENCES, "disponibilidad": [], "objetivos": []}
    assert put_preferences(client, user_id, headers, cleared).status_code == 200
    stored = get_preferences(client, user_id, headers)
    assert stored["disponibilidad"] == []
    assert stored["objetivos"] == []


@pytest.mark.parametrize(
    "change",
    [
        {"disponibilidad": [{"dia_semana": "feriado", "hora_inicio": "08:00", "hora_fin": "09:00"}]},
        {"disponibilidad": [{"dia_semana": "lunes", "hora_inicio": "20:00", "hora_fin": "18:00"}]},
        {"disponibilidad": [{"dia_semana": "lunes", "hora_inicio": "25:00", "hora_fin": "26:00"}]},
        {"objetivos": ["a", "b", "c", "d", "e", "f"]},
        {"objetivos": ["x"]},
        {"objetivos": ["y" * 51]},
        {"objetivos": ["competir", "Competir"]},
    ],
)
def test_invalid_availability_or_goals_are_rejected(client: TestClient, change: dict) -> None:
    user_id, headers = signed_in(client)
    assert put_preferences(client, user_id, headers, {**FULL_PREFERENCES, **change}).status_code == 422


# -- 3. zona / comuna -------------------------------------------------------

@pytest.mark.parametrize(
    "zona",
    [
        {"comuna": "Pedro Aguirre Cerda"},
        {"comuna": "O'Higgins", "latitud": -34.17, "longitud": -70.74},
        {"comuna": "Til-Til"},
    ],
)
def test_zone_can_be_set_with_or_without_coordinates(client: TestClient, zona: dict) -> None:
    user_id, headers = signed_in(client)

    assert put_preferences(client, user_id, headers, {**FULL_PREFERENCES, "zona": zona}).status_code == 200

    stored = get_preferences(client, user_id, headers)["zona"]
    assert stored == {"latitud": None, "longitud": None, **zona}


def test_zone_can_be_removed(client: TestClient) -> None:
    user_id, headers = signed_in(client)
    put_preferences(client, user_id, headers, FULL_PREFERENCES)

    assert put_preferences(client, user_id, headers, {**FULL_PREFERENCES, "zona": None}).status_code == 200

    assert get_preferences(client, user_id, headers)["zona"] is None


@pytest.mark.parametrize(
    "zona",
    [
        {"comuna": "X"},
        {"comuna": "Santiago 123"},
        {"comuna": "<script>"},
        {"comuna": "Ñuñoa", "latitud": -33.4},
        {"comuna": "Ñuñoa", "latitud": -95, "longitud": -70},
        {"comuna": "Ñuñoa", "latitud": -33, "longitud": 190},
        {"latitud": -33, "longitud": -70},
    ],
)
def test_invalid_zone_is_rejected(client: TestClient, zona: dict) -> None:
    user_id, headers = signed_in(client)
    assert put_preferences(client, user_id, headers, {**FULL_PREFERENCES, "zona": zona}).status_code == 422


# -- seguridad y privacidad --------------------------------------------------

def test_nobody_else_can_edit_my_profile(client: TestClient) -> None:
    my_id, _ = signed_in(client, "ana@example.com")
    _, other_headers = signed_in(client, "diego@example.com")

    response = put_preferences(client, my_id, other_headers, FULL_PREFERENCES)

    assert response.status_code == 403


def test_zone_and_goals_never_show_up_on_other_players_cards(client: TestClient) -> None:
    my_id, my_headers = signed_in(client, "ana@example.com")
    put_preferences(client, my_id, my_headers, FULL_PREFERENCES)
    _, other_headers = signed_in(client, "diego@example.com")

    card = client.get("/api/v1/users/suggestions", headers=other_headers).json()[0]

    assert card["user_id"] == my_id
    assert "zona" not in card and "latitud" not in str(card) and "Ñuñoa" not in str(card)


def test_my_data_export_includes_goals_and_zone(client: TestClient) -> None:
    user_id, headers = signed_in(client)
    put_preferences(client, user_id, headers, FULL_PREFERENCES)

    export = client.get(f"/api/v1/users/{user_id}/exports", headers=headers).json()

    assert export["preferences"]["objetivos"] == FULL_PREFERENCES["objetivos"]
    assert export["preferences"]["zona"]["comuna"] == "Ñuñoa"
