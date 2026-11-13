"""`POST /internal/v1/accounts/{id}/contactable`: de una lista de personas, a cuáles puede contactar quien pregunta.

Lo usa ms-machine para que `totalUnread` no cuente chats de personas bloqueadas, suspendidas o de cuentas
desactivadas (las mismas que `athletes/cards` deja fuera de la lista de chats). Solo red interna.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from test_admin_audit import SERVICE_TOKEN, person, setup
from test_sanctions import SANCTIONS

HEADERS = {"X-Internal-Service-Token": SERVICE_TOKEN}


def path(user):
    return f"/internal/v1/accounts/{user['id']}/contactable"


def test_needs_the_service_secret(monkeypatch):
    client, _, admin, ana = setup()
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", SERVICE_TOKEN)
    body = {"ids": [ana["id"]]}
    assert client.post(path(ana), json=body).status_code == 401
    assert client.post(path(ana), json=body, headers=admin["headers"]).status_code == 401
    assert client.post(path(ana), json=body, headers=HEADERS).status_code == 200
    monkeypatch.delenv("INTERNAL_SERVICE_TOKEN")
    assert client.post(path(ana), json=body, headers=HEADERS).status_code == 503


def test_leaves_out_blocked_deactivated_suspended_unknown_and_non_athletes(monkeypatch):
    client, repository, admin, ana = setup()
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", SERVICE_TOKEN)
    ok, blocked, blocked_me, gone, suspended, club = (person(client, n) for n in
                                                      ("Okey", "Bloqueado", "Bloqueador", "Baja", "Suspendido", "Club"))
    repository.get_user_by_email("club@example.com").role = "club_admin"
    client.post("/api/v1/users/block", headers=ana["headers"], json={"blockedUserId": blocked["id"]})
    client.post("/api/v1/users/block", headers=blocked_me["headers"], json={"blockedUserId": ana["id"]})
    repository.delete_user(UUID(gone["id"]))  # cuenta desactivada
    client.post(SANCTIONS, headers=admin["headers"], json={
        "usuario_id": suspended["id"], "tipo": "permanente", "motivo": "Acoso reiterado a otros"})
    ids = [ok, blocked, blocked_me, gone, suspended, club]
    asked = [p["id"] for p in ids] + [str(uuid4())]
    response = client.post(path(ana), json={"ids": asked}, headers=HEADERS)
    assert response.status_code == 200, response.text
    assert set(response.json()["ids"]) == {ok["id"]}
