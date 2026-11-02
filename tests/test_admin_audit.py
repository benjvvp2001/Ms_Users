"""HU-48: historial de acciones administrativas (repositorio simulado, sin base de datos)."""
from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

os.environ["JWT_SECRET"] = "test-only-secret-that-is-long-enough"

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.repositories.base import AuditEvent
from app.repositories.mock_user_repository import MockUserRepository
from app.schemas.audit import AdminAuditEntry, AuditAction, AuditResourceType
from app.services.audit_service import AuditService
from fakes import FakeEmailSender, register_and_verify

USERS = "/api/v1/users"
AUDIT = f"{USERS}/admin/audit-events"
INTERNAL = "/internal/v1/audit/events"
SERVICE_TOKEN = "service-to-service-secret-with-32-bytes-min"


def make_client() -> tuple[TestClient, MockUserRepository]:
    repository = MockUserRepository()
    return TestClient(create_app(repository=repository, email_sender=FakeEmailSender())), repository


def person(client: TestClient, nombre: str, role: str | None = None, repository=None) -> dict:
    tokens = register_and_verify(client, {
        "email": f"{nombre.lower()}@example.com", "password": "A-secure-test-password-1",
        "nombre": nombre, "apellido_paterno": "Torres",
    })
    who = {"id": tokens["user"]["user_id"], "headers": {"Authorization": f"Bearer {tokens['access_token']}"}}
    if role:
        uid = UUID(who["id"])
        repository._users[uid] = replace(repository._users[uid], role=role)
    return who


def setup():
    client, repository = make_client()
    admin = person(client, "Moderadora", "admin", repository)
    ana = person(client, "Ana")
    return client, repository, admin, ana


def file_report(client, who, target, motivo="Spam o publicidad") -> str:
    created = client.post(f"{USERS}/reports", headers=who["headers"], json={
        "tipo": "usuario", "objetivo_id": target, "objetivo_nombre": "Beto T.", "motivo": motivo,
        "detalle": "Me escribió de madrugada"})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def entry(actor, **changes) -> AdminAuditEntry:
    values = dict(actor_user_id=UUID(actor["id"]), action=AuditAction.USER_SUSPENDED,
                  resource_type=AuditResourceType.USER, resource_id=str(uuid4()), motivo="Acoso reiterado")
    return AdminAuditEntry(**{**values, **changes})


def items(client, admin, query="") -> list[dict]:
    response = client.get(f"{AUDIT}{query}", headers=admin["headers"])
    assert response.status_code == 200, response.text
    return response.json()["items"]


# -- registro automatico -------------------------------------------------------------------------

def test_reviewing_a_report_is_audited_automatically_with_every_field() -> None:
    client, _, admin, ana = setup()
    report_id = file_report(client, ana, str(uuid4()))
    before = datetime.now(timezone.utc)
    reviewed = client.patch(f"{USERS}/admin/reports/{report_id}", headers=admin["headers"],
                            json={"estado": "revisado", "motivo": "Confirmado: publicidad repetida"})
    assert reviewed.status_code == 200, reviewed.text

    [event] = items(client, admin)
    assert event["accion"] == "REPORT_REVIEWED" and event["resultado"] == "success"
    assert event["admin"] == {"id": admin["id"], "nombre": "Moderadora T."}      # Short name, no e-mail.
    assert (event["recurso_tipo"], event["recurso_id"]) == ("report", report_id)
    assert event["motivo"] == "Confirmado: publicidad repetida"
    assert before - timedelta(seconds=1) <= datetime.fromisoformat(event["fecha"]) <= datetime.now(timezone.utc)
    assert event["detalle"] == {"tipo": "usuario", "desde": "pendiente"}
    assert "example.com" not in client.get(AUDIT, headers=admin["headers"]).text


def test_without_a_motivo_the_report_reason_is_kept_and_a_discard_has_its_own_action() -> None:
    client, _, admin, ana = setup()
    report_id = file_report(client, ana, str(uuid4()), motivo="Lenguaje ofensivo")
    client.patch(f"{USERS}/admin/reports/{report_id}", headers=admin["headers"], json={"estado": "descartado"})
    [event] = items(client, admin)
    assert (event["accion"], event["motivo"]) == ("REPORT_DISCARDED", "Lenguaje ofensivo")


def test_a_failed_or_unknown_review_leaves_no_audit_row() -> None:
    client, _, admin, _ = setup()
    assert client.patch(f"{USERS}/admin/reports/{uuid4()}", headers=admin["headers"],
                        json={"estado": "revisado"}).status_code == 404
    assert items(client, admin) == []


def test_if_the_audit_cannot_be_written_the_review_is_not_saved_and_the_answer_is_503() -> None:
    client, repository, admin, ana = setup()
    report_id = file_report(client, ana, str(uuid4()))
    repository.audit_unavailable = True
    response = client.patch(f"{USERS}/admin/reports/{report_id}", headers=admin["headers"],
                            json={"estado": "revisado"})
    assert response.status_code == 503
    repository.audit_unavailable = False
    pending = client.get(f"{USERS}/admin/reports?estado=pendiente", headers=admin["headers"]).json()
    assert [r["id"] for r in pending] == [report_id]       # Still pending: nothing was half-done.
    assert items(client, admin) == []


def test_the_reusable_service_records_with_one_call_and_names_the_affected_user() -> None:
    client, repository, admin, ana = setup()
    AuditService(repository).record(
        actor_id=UUID(admin["id"]), action=AuditAction.USER_SUSPENDED, resource_type=AuditResourceType.USER,
        resource_id=ana["id"], motivo="Acoso reiterado", metadata={"dias": 7, "manual": True})
    [event] = items(client, admin)
    assert event["afectado"] == {"id": None, "nombre": "Ana T."}
    assert event["detalle"] == {"dias": 7, "manual": True}
    assert (event["recurso_tipo"], event["recurso_id"]) == ("user", ana["id"])


# -- consulta ------------------------------------------------------------------------------------

def seed(client, repository, admin, ana, count=5):
    other = person(client, "Otro", "admin", repository)
    service = AuditService(repository)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for n in range(count):
        actor = admin if n % 2 == 0 else other
        service.record(actor_id=UUID(actor["id"]),
                       action=AuditAction.USER_SUSPENDED if n % 2 == 0 else AuditAction.PLAN_CREATED,
                       resource_type=AuditResourceType.USER if n % 2 == 0 else AuditResourceType.PLAN,
                       resource_id=ana["id"] if n % 2 == 0 else f"plan-{n}", motivo=f"Motivo {n}",
                       result="success" if n != 2 else "failure")
        last = repository._audit_events[-1]
        repository._audit_events[-1] = replace(last, timestamp=base + timedelta(days=n))
    return other, base


def test_filters_by_date_range_admin_action_resource_type_resource_id_and_result() -> None:
    client, repository, admin, ana = setup()
    other, base = seed(client, repository, admin, ana)
    motivos = lambda query: [e["motivo"] for e in items(client, admin, query)]

    assert motivos("") == [f"Motivo {n}" for n in (4, 3, 2, 1, 0)]            # Newest first.
    assert motivos("?desde=2026-01-03T00:00:00Z") == ["Motivo 4", "Motivo 3", "Motivo 2"]
    assert motivos("?hasta=2026-01-02T00:00:00Z") == ["Motivo 1", "Motivo 0"]
    assert motivos("?desde=2026-01-02T00:00:00Z&hasta=2026-01-03T00:00:00Z") == ["Motivo 2", "Motivo 1"]
    assert motivos(f"?admin_id={other['id']}") == ["Motivo 3", "Motivo 1"]
    assert motivos("?accion=PLAN_CREATED") == ["Motivo 3", "Motivo 1"]
    assert motivos("?recurso_tipo=user") == ["Motivo 4", "Motivo 2", "Motivo 0"]
    assert motivos(f"?recurso_id={ana['id']}") == ["Motivo 4", "Motivo 2", "Motivo 0"]
    assert motivos("?recurso_id=plan-3") == ["Motivo 3"]
    assert motivos("?resultado=failure") == ["Motivo 2"]
    assert motivos("?accion=USER_SUSPENDED&resultado=success&recurso_tipo=user") == ["Motivo 4", "Motivo 0"]


def test_pagination_reports_totals_and_caps_the_page_size() -> None:
    client, repository, admin, ana = setup()
    seed(client, repository, admin, ana)
    page = client.get(f"{AUDIT}?por_pagina=2&pagina=2", headers=admin["headers"]).json()
    assert [e["motivo"] for e in page["items"]] == ["Motivo 2", "Motivo 1"]
    assert (page["pagina"], page["por_pagina"], page["total"], page["total_paginas"]) == (2, 2, 5, 3)
    assert client.get(f"{AUDIT}?por_pagina=2&pagina=9", headers=admin["headers"]).json()["items"] == []
    assert client.get(f"{AUDIT}?por_pagina=100", headers=admin["headers"]).status_code == 200
    for bad in ("por_pagina=101", "por_pagina=0", "pagina=0", "pagina=-1", "pagina=abc"):
        assert client.get(f"{AUDIT}?{bad}", headers=admin["headers"]).status_code == 422, bad


def test_the_history_only_lists_administrative_actions() -> None:
    client, repository, admin, ana = setup()
    # register/login rows from the sign-ups above must not show up.
    assert any(e.action == "register" for e in repository._audit_events)
    assert items(client, admin) == []


# -- permisos ------------------------------------------------------------------------------------

def test_401_without_token_and_403_for_player_and_club_admin() -> None:
    client, repository, admin, ana = setup()
    club = person(client, "Rocio", "club_admin", repository)
    assert client.get(AUDIT).status_code == 401
    assert client.get(AUDIT, headers={"Authorization": "Bearer not-a-token"}).status_code == 401
    assert client.get(AUDIT, headers=ana["headers"]).status_code == 403
    assert client.get(AUDIT, headers=club["headers"]).status_code == 403
    assert client.get(AUDIT, headers=admin["headers"]).status_code == 200


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_there_is_no_route_to_create_edit_or_delete_audit_rows(method) -> None:
    client, _, admin, _ = setup()
    event_id = uuid4()
    for path in (AUDIT, f"{AUDIT}/{event_id}"):
        response = getattr(client, method)(path, headers=admin["headers"])
        assert response.status_code in (404, 405), (method, path, response.status_code)
    paths = {getattr(route, "path", "") for route in client.app.routes}
    assert not [p for p in paths if "audit" in p and p != AUDIT and p != INTERNAL]


# -- datos invalidos -----------------------------------------------------------------------------

@pytest.mark.parametrize("query", [
    "desde=ayer", "desde=2026-01-01T00:00:00", "hasta=nan", "admin_id=no-es-uuid", "accion=BORRAR_TODO",
    "recurso_tipo=otro", "recurso_id=a%00b", "recurso_id=" + "x" * 121, "recurso_id=%20", "resultado=ok",
    "desde=2026-02-01T00:00:00Z&hasta=2026-01-01T00:00:00Z", "extra=1",
])
def test_invalid_filters_are_422_and_never_echo_what_was_sent(query) -> None:
    client, _, admin, _ = setup()
    response = client.get(f"{AUDIT}?{query}", headers=admin["headers"])
    assert response.status_code == 422, response.text
    sent = query.split("=", 1)[-1].split("&")[0]
    assert len(sent) < 3 or sent not in response.text
    assert "input" not in response.text and "ctx" not in response.text


# -- datos sensibles -----------------------------------------------------------------------------

@pytest.mark.parametrize("changes", [
    {"motivo": "Escribió a ana@example.com"}, {"motivo": "RUT 12.345.678-5"}, {"motivo": "a\x00b"},
    {"motivo": "\ud800 roto"}, {"motivo": "ab"}, {"motivo": "x" * 301},
    {"metadata": {"password": "x"}}, {"metadata": {"api_token": "x"}}, {"metadata": {"rut": "1"}},
    {"metadata": {"nota": "contacto: ana@example.com"}}, {"metadata": {"n": float("nan")}},
    {"metadata": {"n": float("inf")}}, {"metadata": {f"k{i}": i for i in range(11)}},
    {"metadata": {"largo": "x" * 121}}, {"resource_id": "no-es-uuid"}, {"resource_id": "../etc"},
])
def test_sensitive_or_malformed_data_is_rejected_before_it_reaches_the_log(changes) -> None:
    client, repository, admin, _ = setup()
    with pytest.raises(ValueError):
        entry(admin, **changes)
    assert not [e for e in repository._audit_events if e.action == "USER_SUSPENDED"]


def test_a_review_motivo_with_an_email_is_a_422_and_nothing_is_saved() -> None:
    client, _, admin, ana = setup()
    report_id = file_report(client, ana, str(uuid4()))
    response = client.patch(f"{USERS}/admin/reports/{report_id}", headers=admin["headers"],
                            json={"estado": "revisado", "motivo": "ver ana@example.com"})
    assert response.status_code == 422
    assert items(client, admin) == []


def test_unknown_fields_are_rejected_in_the_review_body() -> None:
    client, _, admin, ana = setup()
    report_id = file_report(client, ana, str(uuid4()))
    assert client.patch(f"{USERS}/admin/reports/{report_id}", headers=admin["headers"],
                        json={"estado": "revisado", "actor": "x"}).status_code == 422


# -- filas antiguas ------------------------------------------------------------------------------

def test_old_rows_keep_working_and_old_report_rows_show_up_with_catalog_names() -> None:
    client, repository, admin, ana = setup()
    report_id = str(uuid4())
    repository.record_audit(user_id=UUID(admin["id"]), action="report_revisado",
                            resource=f"reports/{report_id}", result="success")   # As written before HU-48.
    repository.record_audit(user_id=UUID(ana["id"]), action="login", resource="auth", result="success")
    [event] = items(client, admin)
    assert (event["accion"], event["recurso_tipo"], event["recurso_id"]) == ("REPORT_REVIEWED", "report", report_id)
    assert event["motivo"] is None and event["detalle"] is None
    assert [e["recurso_id"] for e in items(client, admin, "?accion=REPORT_REVIEWED&recurso_tipo=report")] == [report_id]
    assert any(isinstance(e, AuditEvent) and e.action == "login" for e in repository._audit_events)


# -- endpoint interno ----------------------------------------------------------------------------

def internal_body(admin) -> dict:
    return {"actor_user_id": admin["id"], "action": "PLAN_CREATED", "resource_type": "plan",
            "resource_id": "plan-1", "motivo": "Alta del plan mensual", "metadata": {"precio": 9990}}


def test_the_internal_endpoint_rejects_calls_without_service_authentication(monkeypatch) -> None:
    client, _, admin, ana = setup()
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", SERVICE_TOKEN)
    body = internal_body(admin)
    assert client.post(INTERNAL, json=body).status_code == 401
    assert client.post(INTERNAL, json=body, headers={"X-Internal-Service-Token": "wrong"}).status_code == 401
    # A normal user token, even an admin's, is not a service credential.
    assert client.post(INTERNAL, json=body, headers=admin["headers"]).status_code == 401
    assert client.post(INTERNAL, json=body, headers=ana["headers"]).status_code == 401
    assert items(client, admin) == []


def test_the_internal_endpoint_is_closed_when_no_service_secret_is_configured(monkeypatch) -> None:
    client, _, admin, _ = setup()
    monkeypatch.delenv("INTERNAL_SERVICE_TOKEN", raising=False)
    response = client.post(INTERNAL, json=internal_body(admin), headers={"X-Internal-Service-Token": SERVICE_TOKEN})
    assert response.status_code == 503
    assert items(client, admin) == []


def test_the_internal_endpoint_records_for_a_real_admin_only(monkeypatch) -> None:
    client, _, admin, ana = setup()
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", SERVICE_TOKEN)
    headers = {"X-Internal-Service-Token": SERVICE_TOKEN}
    assert client.post(INTERNAL, json=internal_body(admin), headers=headers).status_code == 204
    [event] = items(client, admin)
    assert (event["accion"], event["recurso_tipo"], event["detalle"]) == ("PLAN_CREATED", "plan", {"precio": 9990})
    assert client.post(INTERNAL, json=internal_body(ana), headers=headers).status_code == 403   # Not an admin.
    assert client.post(INTERNAL, json={**internal_body(admin), "actor_user_id": str(uuid4())},
                       headers=headers).status_code == 403
    bad = client.post(INTERNAL, json={**internal_body(admin), "motivo": "ana@example.com", "extra": 1},
                      headers=headers)
    assert bad.status_code == 422 and "ana@example" not in bad.text and "input" not in bad.text
    assert client.post(INTERNAL, json={**internal_body(admin), "action": "DROP_TABLE"}, headers=headers).status_code == 422
    assert len(items(client, admin)) == 1


def test_the_internal_endpoint_answers_503_when_the_audit_cannot_be_written(monkeypatch) -> None:
    client, repository, admin, _ = setup()
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", SERVICE_TOKEN)
    repository.audit_unavailable = True
    response = client.post(INTERNAL, json=internal_body(admin), headers={"X-Internal-Service-Token": SERVICE_TOKEN})
    assert response.status_code == 503


def test_a_too_short_service_secret_never_authenticates_and_is_a_503(monkeypatch) -> None:
    client, _, admin, _ = setup()
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "short")
    response = client.post(INTERNAL, json=internal_body(admin), headers={"X-Internal-Service-Token": "short"})
    assert response.status_code == 503
    assert items(client, admin) == []
