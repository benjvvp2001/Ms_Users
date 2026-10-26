from __future__ import annotations

import os

os.environ["JWT_SECRET"] = "test-only-secret-that-is-long-enough"

from fastapi.testclient import TestClient

from app.main import create_app
from app.repositories.mock_user_repository import MockUserRepository
from fakes import REGISTER_PATH, VERIFY_CONFIRM_PATH, FakeEmailSender

LOGIN_PATH = "/api/v1/users/auth/login"
ACCOUNT = {
    "email": "cuenta@example.com",
    "password": "A-secure-test-password-1",
    "nombre": "Rocio",
    "apellido_paterno": "Soto",
}


def make_client() -> tuple[TestClient, MockUserRepository]:
    repository = MockUserRepository()
    return TestClient(create_app(repository=repository, email_sender=FakeEmailSender())), repository


def register_and_verify(client: TestClient, **extra: str) -> dict:
    payload = {**ACCOUNT, **extra}
    assert client.post(REGISTER_PATH, json=payload).status_code == 201
    code = client.app.state.email_sender.last_code(payload["email"])
    assert client.post(VERIFY_CONFIRM_PATH, json={"email": payload["email"], "code": code}).status_code == 200
    return payload


def login(client: TestClient, payload: dict) -> dict:
    response = client.post(LOGIN_PATH, json={"email": payload["email"], "password": payload["password"]})
    assert response.status_code == 200, response.text
    return response.json()


def test_registering_as_club_saves_the_role_and_login_points_to_the_web(monkeypatch) -> None:
    monkeypatch.setenv("SPORTMATCH_WEB_URL", "https://clubes.example.com")
    client, repository = make_client()
    payload = register_and_verify(client, tipo="club")
    assert repository.get_user_by_email(payload["email"]).role == "club_admin"
    body = login(client, payload)  # solo correo y contraseña
    assert body["user"]["role"] == "club_admin"
    assert body["frontend"] == "web" and body["web_url"] == "https://clubes.example.com"


def test_registering_as_user_sends_the_login_to_the_app(monkeypatch) -> None:
    monkeypatch.setenv("SPORTMATCH_WEB_URL", "https://clubes.example.com")
    client, repository = make_client()
    payload = register_and_verify(client, tipo="usuario")
    assert repository.get_user_by_email(payload["email"]).role == "player"
    body = login(client, payload)
    assert body["frontend"] == "app" and body["web_url"] is None


def test_the_type_defaults_to_user_and_rejects_other_values() -> None:
    client, repository = make_client()
    assert client.post(REGISTER_PATH, json=ACCOUNT).status_code == 201
    assert repository.get_user_by_email(ACCOUNT["email"]).role == "player"
    assert client.post(REGISTER_PATH, json={**ACCOUNT, "email": "otra@example.com", "tipo": "admin"}).status_code == 422


def test_the_email_confirmation_token_also_names_the_frontend(monkeypatch) -> None:
    monkeypatch.delenv("SPORTMATCH_WEB_URL", raising=False)
    client, _ = make_client()
    assert client.post(REGISTER_PATH, json={**ACCOUNT, "tipo": "club"}).status_code == 201
    code = client.app.state.email_sender.last_code(ACCOUNT["email"])
    confirmed = client.post(VERIFY_CONFIRM_PATH, json={"email": ACCOUNT["email"], "code": code}).json()
    assert confirmed["frontend"] == "web" and confirmed["web_url"] is None


def test_a_club_signs_in_from_the_app_or_the_web_with_the_same_token(monkeypatch) -> None:
    # El backend solo indica el frontend de destino; no impide usar el token en el otro.
    monkeypatch.setenv("SPORTMATCH_WEB_URL", "https://clubes.example.com")
    client, _ = make_client()
    payload = register_and_verify(client, tipo="club")
    body = login(client, payload)
    assert body["frontend"] == "web" and body["web_url"] == "https://clubes.example.com"
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    own_profile = client.get(f"/api/v1/users/{body['user']['user_id']}/profile", headers=headers)
    assert own_profile.status_code == 200


def test_a_player_gets_the_app_and_no_web_address(monkeypatch) -> None:
    monkeypatch.setenv("SPORTMATCH_WEB_URL", "https://clubes.example.com")
    client, _ = make_client()
    payload = register_and_verify(client, tipo="usuario")
    body = login(client, payload)
    assert body["frontend"] == "app" and body["web_url"] is None
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    assert client.get(f"/api/v1/users/{body['user']['user_id']}/profile", headers=headers).status_code == 200
