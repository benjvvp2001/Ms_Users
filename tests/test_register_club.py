from __future__ import annotations

import os

os.environ["JWT_SECRET"] = "test-only-secret-that-is-long-enough"

from fastapi.testclient import TestClient

from app.main import create_app
from app.repositories.mock_user_repository import MockUserRepository
from fakes import REGISTER_PATH, VERIFY_CONFIRM_PATH, FakeEmailSender

REGISTER_CLUB_PATH = "/api/v1/users/auth/register-club"
CLUB = {
    "email": "representante@club.example.com",
    "password": "A-secure-test-password-1",
    "nombre": "Rocio",
    "apellido_paterno": "Soto",
}


def make_client() -> tuple[TestClient, MockUserRepository]:
    repository = MockUserRepository()
    return TestClient(create_app(repository=repository, email_sender=FakeEmailSender())), repository


def test_club_registration_creates_a_club_admin_pending_verification() -> None:
    client, repository = make_client()
    response = client.post(REGISTER_CLUB_PATH, json=CLUB)
    assert response.status_code == 201
    assert response.json()["user"]["role"] == "club_admin"
    stored = repository.get_user_by_email(CLUB["email"])
    assert stored.role == "club_admin" and stored.email_verified is False


def test_verified_club_account_gets_a_club_admin_token() -> None:
    client, _ = make_client()
    assert client.post(REGISTER_CLUB_PATH, json=CLUB).status_code == 201
    credentials = {"email": CLUB["email"], "password": CLUB["password"]}
    assert client.post("/api/v1/users/auth/login", json=credentials).status_code == 403  # sin verificar
    code = client.app.state.email_sender.last_code(CLUB["email"])
    confirmed = client.post(VERIFY_CONFIRM_PATH, json={"email": CLUB["email"], "code": code})
    assert confirmed.status_code == 200
    assert confirmed.json()["user"]["role"] == "club_admin"


def test_regular_registration_still_creates_a_player() -> None:
    client, repository = make_client()
    assert client.post(REGISTER_PATH, json={**CLUB, "email": "jugador@example.com"}).status_code == 201
    assert repository.get_user_by_email("jugador@example.com").role == "player"


def test_club_registration_rejects_a_role_in_the_body() -> None:
    client, _ = make_client()
    response = client.post(REGISTER_CLUB_PATH, json={**CLUB, "role": "admin"})
    assert response.status_code == 422


def test_club_registration_rejects_a_duplicate_email() -> None:
    client, _ = make_client()
    assert client.post(REGISTER_CLUB_PATH, json=CLUB).status_code == 201
    assert client.post(REGISTER_CLUB_PATH, json=CLUB).status_code == 409
