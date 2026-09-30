from __future__ import annotations

import os
from uuid import uuid4

os.environ["JWT_SECRET"] = "test-only-secret-that-is-long-enough"

from fastapi.testclient import TestClient

from app.main import create_app
from app.repositories.mock_user_repository import MockUserRepository
from fakes import FakeEmailSender, register_and_verify


def make_client() -> TestClient:
    return TestClient(
        create_app(repository=MockUserRepository(), email_sender=FakeEmailSender())
    )


def register(client: TestClient, email: str = "ana@example.com") -> dict:
    return register_and_verify(
        client,
        {
            "email": email,
            "password": "A-secure-test-password-1",
            "nombre": "Ana",
            "apellido_paterno": "Torres",
        },
    )


def authorization_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_all_user_endpoints_follow_the_v1_contract() -> None:
    client = make_client()
    registration = register(client)
    user_id = registration["user"]["user_id"]
    headers = authorization_header(registration["access_token"])

    assert client.get(f"/api/v1/users/{user_id}/profile", headers=headers).status_code == 200

    profile = client.put(
        f"/api/v1/users/{user_id}/profile",
        headers=headers,
        json={
            "nombre": "Ana",
            "apellido_paterno": "Torres",
            "apellido_materno": "Diaz",
            "telefono": "+56911112222",
            "biografia": "Me gusta el tenis",
        },
    )
    assert profile.status_code == 200
    assert profile.json()["telefono"] == "+56911112222"

    preferences = client.put(
        f"/api/v1/users/{user_id}/preferences",
        headers=headers,
        json={
            "deportes": [{"deporte_codigo": "tennis", "nivel": 3}],
            "disponibilidad": [
                {"dia_semana": "lunes", "hora_inicio": "18:00", "hora_fin": "20:00"}
            ],
            "rango_distancia_km": "15",
            "disponibilidad_match": True,
        },
    )
    assert preferences.status_code == 200
    assert preferences.json()["deportes"][0]["deporte_codigo"] == "tennis"

    assert client.get(f"/api/v1/users/{user_id}/roles", headers=headers).json() == {
        "role": "player"
    }

    consent = client.post(
        f"/api/v1/users/{user_id}/consents",
        headers=headers,
        json={
            "type": "privacy",
            "purpose": "process matching preferences",
            "document_version": "2026-09",
            "method": "web",
        },
    )
    assert consent.status_code == 201
    consent_id = consent.json()["id"]
    assert client.get(f"/api/v1/users/{user_id}/consents", headers=headers).status_code == 200

    revoked = client.delete(
        f"/api/v1/users/{user_id}/consents/{consent_id}",
        headers=headers,
    )
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"

    export = client.get(f"/api/v1/users/{user_id}/exports", headers=headers)
    assert export.status_code == 200
    assert export.json()["profile"]["biografia"] == "Me gusta el tenis"

    deletion = client.delete(f"/api/v1/users/{user_id}", headers=headers)
    assert deletion.status_code == 204
    assert deletion.content == b""


def test_authentication_authorization_and_prefix_are_enforced() -> None:
    client = make_client()
    first = register(client, "first@example.com")
    second = register(client, "second@example.com")

    assert client.post(
        "/api/v1/users/auth/login",
        json={"email": "first@example.com", "password": "incorrect-password"},
    ).status_code == 401

    first_headers = authorization_header(first["access_token"])
    assert client.get(
        f"/api/v1/users/{second['user']['user_id']}/profile",
        headers=first_headers,
    ).status_code == 403

    assert client.get(f"/api/v1/users/{uuid4()}/profile").status_code == 401
    assert client.get("/users/auth/register").status_code == 404
