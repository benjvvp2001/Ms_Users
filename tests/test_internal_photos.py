"""Defecto 2 de la verificación de HU-61: ms-club necesita la foto VIGENTE de varios postulantes en una sola consulta.

`POST /internal/v1/accounts/photos` (solo red interna, con X-Internal-Service-Token): recibe ids y devuelve solo
`{id: ruta de la foto}` de las cuentas activas que tienen foto. Nada más de la persona.
"""
from __future__ import annotations

from uuid import UUID, uuid4

from test_admin_audit import SERVICE_TOKEN, setup

PHOTOS = "/internal/v1/accounts/photos"
KEY = "0123456789abcdef0123456789abcdef.jpg"
HEADERS = {"X-Internal-Service-Token": SERVICE_TOKEN}


def test_needs_the_service_secret(monkeypatch) -> None:
    client, _, admin, ana = setup()
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", SERVICE_TOKEN)
    body = {"ids": [ana["id"]]}
    assert client.post(PHOTOS, json=body).status_code == 401
    assert client.post(PHOTOS, json=body, headers=admin["headers"]).status_code == 401      # a user token is not the secret
    assert client.post(PHOTOS, json=body, headers={"X-Internal-Service-Token": "otro-secreto-distinto-de-32-bytes!!"}).status_code == 401
    assert client.post(PHOTOS, json=body, headers=HEADERS).status_code == 200
    monkeypatch.delenv("INTERNAL_SERVICE_TOKEN")
    assert client.post(PHOTOS, json=body, headers=HEADERS).status_code == 503


def test_gives_the_current_photo_and_only_the_photo(monkeypatch) -> None:
    client, repository, _, ana = setup()
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", SERVICE_TOKEN)
    repository.set_photo(UUID(ana["id"]), KEY)
    response = client.post(PHOTOS, json={"ids": [ana["id"], ana["id"]]}, headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.json() == {"photos": {ana["id"]: f"/api/v1/users/photos/{KEY}"}}
    assert "@" not in response.text and "Ana" not in response.text            # ni correo ni nombre


def test_a_person_without_photo_or_unknown_is_left_out(monkeypatch) -> None:
    client, repository, admin, ana = setup()
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", SERVICE_TOKEN)
    repository.set_photo(UUID(ana["id"]), KEY)
    desconocido = str(uuid4())
    assert client.post(PHOTOS, json={"ids": [admin["id"], desconocido]}, headers=HEADERS).json() == {"photos": {}}
    repository.set_photo(UUID(ana["id"]), None)                                # borró su foto
    assert client.post(PHOTOS, json={"ids": [ana["id"]]}, headers=HEADERS).json() == {"photos": {}}


def test_a_value_that_is_not_a_key_of_this_service_reads_as_no_photo(monkeypatch) -> None:
    client, repository, _, ana = setup()
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", SERVICE_TOKEN)
    repository.set_photo(UUID(ana["id"]), "https://evil.example.com/x.png")     # texto libre antiguo
    assert client.post(PHOTOS, json={"ids": [ana["id"]]}, headers=HEADERS).json() == {"photos": {}}


def test_validates_the_body(monkeypatch) -> None:
    client, *_ = setup()
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", SERVICE_TOKEN)
    assert client.post(PHOTOS, json={"ids": []}, headers=HEADERS).json() == {"photos": {}}
    assert client.post(PHOTOS, json={"ids": [str(uuid4()) for _ in range(101)]}, headers=HEADERS).status_code == 422
    assert client.post(PHOTOS, json={"ids": ["no-es-uuid"]}, headers=HEADERS).status_code == 422
    assert client.post(PHOTOS, json={"ids": [], "otro": 1}, headers=HEADERS).status_code == 422
    assert client.post(PHOTOS, json={}, headers=HEADERS).status_code == 422
