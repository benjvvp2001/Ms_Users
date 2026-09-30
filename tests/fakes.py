"""Test doubles shared by the test modules (no real mail, no real database)."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

REGISTER_PATH = "/api/v1/users/auth/register"
VERIFY_REQUEST_PATH = "/api/v1/users/auth/email-verification/request"
VERIFY_CONFIRM_PATH = "/api/v1/users/auth/email-verification/confirm"


class FakeEmailSender:
    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    def send(self, *, to: str, subject: str, text: str, html: str) -> None:
        self.sent.append({"to": to, "subject": subject, "text": text, "html": html})

    def last_code(self, to: str | None = None) -> str:
        mails = [mail for mail in self.sent if to is None or mail["to"] == to.casefold()]
        assert mails, f"no email was sent to {to}"
        match = re.search(r"\b(\d{6})\b", mails[-1]["text"])
        assert match is not None
        return match.group(1)


def register_and_verify(client: TestClient, payload: dict) -> dict:
    """Register, then confirm the emailed code; returns the token response.

    The client must be built with create_app(..., email_sender=FakeEmailSender()).
    """
    registered = client.post(REGISTER_PATH, json=payload)
    assert registered.status_code == 201, registered.text
    code = client.app.state.email_sender.last_code(payload["email"])
    verified = client.post(VERIFY_CONFIRM_PATH, json={"email": payload["email"], "code": code})
    assert verified.status_code == 200, verified.text
    return verified.json()
