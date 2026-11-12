"""HU-54: deportistas con plan pagado aparecen destacados (es_destacado) y primero en las sugerencias."""
from __future__ import annotations

import json
from uuid import UUID

import httpx
from fastapi.testclient import TestClient

from test_suggestions import SUGGESTIONS_PATH, signed_up  # also sets JWT_SECRET

from app.main import create_app
from app.repositories.mock_user_repository import MockUserRepository
from app.services.entitlements_client import EntitlementsClient, EntitlementsSettings
from fakes import FakeEmailSender

TOKEN = "internal-token-for-tests-at-least-32-bytes-long"


class Payments:
    def __init__(self) -> None:
        self.pro: set[str] = set()
        self.down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("refused", request=request)
        assert request.url.path == "/internal/v1/entitlements/usuario/batch"
        assert request.headers["X-Internal-Service-Token"] == TOKEN
        ids = json.loads(request.content)["ids"]
        return httpx.Response(200, json={"items": [
            {"id": i, "plan": "PRO" if i in self.pro else "GRATIS",
             "beneficios": ["destacado"] if i in self.pro else [], "limites": {}} for i in ids]})


def make(payments: Payments | None):
    entitlements = None
    if payments is not None:
        entitlements = EntitlementsClient(EntitlementsSettings("http://pago.test", TOKEN, 1, 0), httpx.Client(
            base_url="http://pago.test", transport=httpx.MockTransport(payments.handler)))
    return TestClient(create_app(repository=MockUserRepository(), email_sender=FakeEmailSender(),
                                 entitlements=entitlements))


def test_paid_athletes_are_featured_and_listed_first():
    payments = Payments()
    client = make(payments)
    me = signed_up(client, "me@example.com", ["tenis"])
    ana = signed_up(client, "ana@example.com", ["tenis"], nombre="Ana")
    beto = signed_up(client, "beto@example.com", ["futbol"], nombre="Beto")
    payments.pro.add(beto["user_id"])
    cards = client.get(SUGGESTIONS_PATH, headers=me["headers"]).json()
    assert [(c["user_id"], c["es_destacado"]) for c in cards] == [(beto["user_id"], True), (ana["user_id"], False)]


def test_ms_pago_down_or_absent_features_nobody():
    payments = Payments()
    payments.down = True
    for client in (make(payments), make(None)):
        me = signed_up(client, "me@example.com")
        signed_up(client, "ana@example.com")
        cards = client.get(SUGGESTIONS_PATH, headers=me["headers"]).json()
        assert cards and all(c["es_destacado"] is False for c in cards)
        UUID(cards[0]["user_id"])
