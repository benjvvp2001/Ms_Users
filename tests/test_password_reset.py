from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone

os.environ["JWT_SECRET"] = "test-only-secret-that-is-long-enough"

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.repositories.mock_user_repository import MockUserRepository

from fakes import FakeEmailSender, register_and_verify

REQUEST_PATH = "/api/v1/users/auth/password-reset/request"
CONFIRM_PATH = "/api/v1/users/auth/password-reset/confirm"
NEW_PASSWORD = "A-brand-new-password-2"
ANA = {
    "email": "ana@example.com",
    "password": "A-secure-test-password-1",
    "nombre": "Ana",
    "apellido_paterno": "Torres",
}


class FailingEmailSender:
    def send(self, *, to: str, subject: str, text: str, html: str) -> None:
        raise OSError("smtp server unreachable")


@pytest.fixture(autouse=True)
def email_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PASSWORD_RESET_RESEND_SECONDS", "60")
    monkeypatch.setenv("PASSWORD_RESET_MAX_ATTEMPTS", "3")


def make_client(sender=None) -> tuple[TestClient, MockUserRepository, FakeEmailSender]:
    repository = MockUserRepository()
    sender = sender or FakeEmailSender()
    client = TestClient(create_app(repository=repository, email_sender=sender))
    if isinstance(sender, FakeEmailSender):
        register_and_verify(client, ANA)
        sender.sent.clear()  # each test starts with only its own mail
    else:
        client.post("/api/v1/users/auth/register", json=ANA)
    return client, repository, sender


def test_reset_code_is_emailed_to_the_address_the_user_typed() -> None:
    client, _, sender = make_client()

    response = client.post(REQUEST_PATH, json={"email": "ANA@example.com"})

    assert response.status_code == 202
    assert len(sender.sent) == 1
    assert sender.sent[0]["to"] == "ana@example.com"
    assert "Ana" in sender.sent[0]["text"]


def test_unknown_email_gets_the_same_answer_and_no_mail() -> None:
    client, _, sender = make_client()

    known = client.post(REQUEST_PATH, json={"email": "ana@example.com"})
    unknown = client.post(REQUEST_PATH, json={"email": "nobody@example.com"})

    assert unknown.status_code == known.status_code == 202
    assert unknown.json() == known.json()
    assert [mail["to"] for mail in sender.sent] == ["ana@example.com"]


def test_valid_code_changes_the_password_only_once() -> None:
    client, _, sender = make_client()
    client.post(REQUEST_PATH, json={"email": "ana@example.com"})
    code = sender.last_code()

    confirm = client.post(
        CONFIRM_PATH,
        json={"email": "ana@example.com", "code": code, "new_password": NEW_PASSWORD},
    )
    assert confirm.status_code == 200

    old_login = client.post(
        "/api/v1/users/auth/login",
        json={"email": "ana@example.com", "password": "A-secure-test-password-1"},
    )
    new_login = client.post(
        "/api/v1/users/auth/login", json={"email": "ana@example.com", "password": NEW_PASSWORD}
    )
    assert old_login.status_code == 401
    assert new_login.status_code == 200

    reused = client.post(
        CONFIRM_PATH,
        json={"email": "ana@example.com", "code": code, "new_password": "Yet-another-password-3"},
    )
    assert reused.status_code == 400


def test_code_is_blocked_after_too_many_wrong_attempts() -> None:
    client, _, sender = make_client()
    client.post(REQUEST_PATH, json={"email": "ana@example.com"})
    code = sender.last_code()
    wrong = "000000" if code != "000000" else "111111"

    for _ in range(3):
        response = client.post(
            CONFIRM_PATH,
            json={"email": "ana@example.com", "code": wrong, "new_password": NEW_PASSWORD},
        )
        assert response.status_code == 400

    blocked = client.post(
        CONFIRM_PATH,
        json={"email": "ana@example.com", "code": code, "new_password": NEW_PASSWORD},
    )
    assert blocked.status_code == 400


def test_expired_code_is_rejected() -> None:
    client, repository, sender = make_client()
    client.post(REQUEST_PATH, json={"email": "ana@example.com"})
    code = sender.last_code()
    for reset_id, reset in list(repository._password_resets.items()):
        repository._password_resets[reset_id] = replace(
            reset, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
        )

    response = client.post(
        CONFIRM_PATH,
        json={"email": "ana@example.com", "code": code, "new_password": NEW_PASSWORD},
    )
    assert response.status_code == 400


def test_repeated_requests_are_throttled() -> None:
    client, _, sender = make_client()

    client.post(REQUEST_PATH, json={"email": "ana@example.com"})
    second = client.post(REQUEST_PATH, json={"email": "ana@example.com"})

    assert second.status_code == 202
    assert len(sender.sent) == 1


def test_mail_failure_does_not_leak_to_the_client() -> None:
    client, _, _ = make_client(sender=FailingEmailSender())

    response = client.post(REQUEST_PATH, json={"email": "ana@example.com"})

    assert response.status_code == 202
    assert "smtp" not in response.text


@pytest.mark.parametrize(
    "payload",
    [
        {"email": "ana@example.com", "code": "12345", "new_password": NEW_PASSWORD},
        {"email": "ana@example.com", "code": "abcdef", "new_password": NEW_PASSWORD},
        {"email": "ana@example.com", "code": "123456", "new_password": "short"},
        {"email": "not-an-email", "code": "123456", "new_password": NEW_PASSWORD},
    ],
)
def test_confirm_validates_input(payload: dict[str, str]) -> None:
    client, _, _ = make_client()
    assert client.post(CONFIRM_PATH, json=payload).status_code == 422


def test_smtp_sender_uses_starttls_login_and_the_typed_recipient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import email_sender as module

    calls: list[tuple] = []

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: int) -> None:
            calls.append(("connect", host, port))

        def __enter__(self) -> FakeSMTP:
            return self

        def __exit__(self, *args: object) -> None:
            calls.append(("quit",))

        def starttls(self, context: object) -> None:
            calls.append(("starttls",))

        def login(self, username: str, password: str) -> None:
            calls.append(("login", username))

        def send_message(self, message) -> None:
            calls.append(("send", message["From"], message["To"], message["Subject"]))

    monkeypatch.setattr(module.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("EMAIL_BACKEND", "smtp")
    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USERNAME", "sportmatch@gmail.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")
    monkeypatch.delenv("EMAIL_FROM", raising=False)

    module.get_email_sender().send(
        to="user@outlook.com", subject="Asunto", text="texto", html="<p>texto</p>"
    )

    assert calls == [
        ("connect", "smtp.gmail.com", 587),
        ("starttls",),
        ("login", "sportmatch@gmail.com"),
        ("send", "sportmatch@gmail.com", "user@outlook.com", "Asunto"),
        ("quit",),
    ]
