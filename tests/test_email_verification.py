from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone

os.environ["JWT_SECRET"] = "test-only-secret-that-is-long-enough"

import pytest
from fastapi.testclient import TestClient

from app.core.security import (
    hash_email_verification_code,
    hash_password_reset_code,
    verify_email_verification_code,
)
from app.main import create_app
from app.repositories.mock_user_repository import MockUserRepository
from fakes import (
    REGISTER_PATH,
    VERIFY_CONFIRM_PATH,
    VERIFY_REQUEST_PATH,
    FakeEmailSender,
    register_and_verify,
)

LOGIN_PATH = "/api/v1/users/auth/login"
ANA = {
    "email": "ana@example.com",
    "password": "A-secure-test-password-1",
    "nombre": "Ana",
    "apellido_paterno": "Torres",
}
CREDENTIALS = {"email": ANA["email"], "password": ANA["password"]}


class FailingEmailSender:
    def send(self, *, to: str, subject: str, text: str, html: str) -> None:
        raise OSError("smtp server unreachable")


@pytest.fixture(autouse=True)
def email_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMAIL_VERIFICATION_RESEND_SECONDS", "60")
    monkeypatch.setenv("EMAIL_VERIFICATION_MAX_ATTEMPTS", "3")


def make_client(sender=None) -> tuple[TestClient, MockUserRepository, FakeEmailSender]:
    repository = MockUserRepository()
    sender = sender or FakeEmailSender()
    return TestClient(create_app(repository=repository, email_sender=sender)), repository, sender


def register(client: TestClient) -> dict:
    response = client.post(REGISTER_PATH, json=ANA)
    assert response.status_code == 201
    return response.json()


def confirm(client: TestClient, code: str):
    return client.post(VERIFY_CONFIRM_PATH, json={"email": ANA["email"], "code": code})


def age_pending_code(repository: MockUserRepository, **changes: object) -> None:
    for user_id, pending in list(repository._email_verifications.items()):
        repository._email_verifications[user_id] = replace(pending, **changes)


def wrong_code(code: str) -> str:
    return "000000" if code != "000000" else "111111"


def test_register_emails_a_code_and_returns_no_token() -> None:
    client, _, sender = make_client()

    body = register(client)

    assert "access_token" not in body
    assert body["email_verification_required"] is True
    assert body["user"]["email"] == "ana@example.com"
    assert [mail["to"] for mail in sender.sent] == ["ana@example.com"]
    assert "verificar tu correo" in sender.sent[0]["subject"]
    assert "Ana" in sender.sent[0]["text"]


def test_login_is_blocked_until_the_email_is_verified() -> None:
    client, _, sender = make_client()
    register(client)

    blocked = client.post(LOGIN_PATH, json=CREDENTIALS)
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == "email not verified"

    # A wrong password still gets the generic 401, so the 403 reveals nothing
    # to someone who doesn't know the credentials.
    wrong = client.post(LOGIN_PATH, json={**CREDENTIALS, "password": "incorrect-password"})
    assert wrong.status_code == 401

    verified = confirm(client, sender.last_code())
    assert verified.status_code == 200
    assert verified.json()["access_token"]
    assert client.post(LOGIN_PATH, json=CREDENTIALS).status_code == 200


def test_verification_token_opens_the_users_own_profile() -> None:
    client, _, _ = make_client()

    tokens = register_and_verify(client, ANA)

    profile = client.get(
        f"/api/v1/users/{tokens['user']['user_id']}/profile",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert profile.status_code == 200


def test_code_works_only_once() -> None:
    client, _, sender = make_client()
    register(client)
    code = sender.last_code()

    assert confirm(client, code).status_code == 200
    reused = confirm(client, code)

    assert reused.status_code == 400
    assert reused.json()["detail"] == "invalid or expired verification code"


def test_code_is_blocked_after_too_many_wrong_attempts() -> None:
    client, _, sender = make_client()
    register(client)
    code = sender.last_code()

    for _ in range(3):
        assert confirm(client, wrong_code(code)).status_code == 400

    assert confirm(client, code).status_code == 400
    assert client.post(LOGIN_PATH, json=CREDENTIALS).status_code == 403


def test_expired_code_is_rejected() -> None:
    client, repository, sender = make_client()
    register(client)
    code = sender.last_code()
    age_pending_code(repository, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))

    assert confirm(client, code).status_code == 400


def test_a_new_code_replaces_a_blocked_or_expired_one() -> None:
    client, repository, sender = make_client()
    register(client)
    old_code = sender.last_code()
    for _ in range(3):
        confirm(client, wrong_code(old_code))
    age_pending_code(repository, sent_at=datetime.now(timezone.utc) - timedelta(minutes=5))

    resent = client.post(VERIFY_REQUEST_PATH, json={"email": "ANA@example.com"})
    assert resent.status_code == 202
    assert len(sender.sent) == 2
    new_code = sender.last_code()

    if new_code != old_code:
        assert confirm(client, old_code).status_code == 400
    assert confirm(client, new_code).status_code == 200


def test_resend_is_throttled() -> None:
    client, _, sender = make_client()
    register(client)

    again = client.post(VERIFY_REQUEST_PATH, json={"email": ANA["email"]})

    assert again.status_code == 202
    assert len(sender.sent) == 1


def test_resend_gives_the_same_answer_for_unknown_and_verified_accounts() -> None:
    client, _, sender = make_client()
    register_and_verify(client, ANA)
    sender.sent.clear()

    verified = client.post(VERIFY_REQUEST_PATH, json={"email": ANA["email"]})
    unknown = client.post(VERIFY_REQUEST_PATH, json={"email": "nobody@example.com"})

    assert verified.status_code == unknown.status_code == 202
    assert verified.json() == unknown.json()
    assert sender.sent == []


def test_codes_are_hashed_per_purpose() -> None:
    # The same six digits stored for a password reset must never match an
    # email-verification hash, and vice versa.
    assert hash_email_verification_code("123456") != hash_password_reset_code("123456")
    assert verify_email_verification_code("123456", hash_email_verification_code("123456"))
    assert not verify_email_verification_code("123456", hash_password_reset_code("123456"))


def test_mail_failure_does_not_break_registration() -> None:
    client, _, _ = make_client(sender=FailingEmailSender())

    response = client.post(REGISTER_PATH, json=ANA)

    assert response.status_code == 201
    assert "smtp" not in response.text


@pytest.mark.parametrize(
    "payload",
    [
        {"email": "ana@example.com", "code": "12345"},
        {"email": "ana@example.com", "code": "abcdef"},
        {"email": "not-an-email", "code": "123456"},
        {"email": "ana@example.com"},
    ],
)
def test_confirm_validates_input(payload: dict[str, str]) -> None:
    client, _, _ = make_client()
    assert client.post(VERIFY_CONFIRM_PATH, json=payload).status_code == 422
