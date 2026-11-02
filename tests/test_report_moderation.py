"""HU-47: el moderador atiende los reportes (listado, estados, escalar) con auditoria HU-48. Repositorio simulado."""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

from test_admin_audit import AUDIT, USERS, file_report, items, person, setup

REPORTS = f"{USERS}/admin/reports"


def patch(client, who, report_id, estado, **extra):
    return client.patch(f"{REPORTS}/{report_id}", headers=who["headers"], json={"estado": estado, **extra})


def listed(client, who, query="") -> list[dict]:
    response = client.get(f"{REPORTS}{query}", headers=who["headers"])
    assert response.status_code == 200, response.text
    return response.json()


def states(client, admin) -> dict[str, str]:
    return {r["id"]: r["estado"] for r in listed(client, admin, "?estado=todos")}


# -- listado -------------------------------------------------------------------------------------

def test_the_list_defaults_to_pending_and_shows_reason_description_date_reporter_and_reported() -> None:
    client, _, admin, ana = setup()
    beto = person(client, "Beto")
    report_id = file_report(client, ana, beto["id"], motivo="Lenguaje ofensivo")
    patch(client, admin, file_report(client, ana, str(uuid4()), motivo="Spam o publicidad"), "descartado")

    [report] = listed(client, admin)                                   # No filter: only the pending one.
    assert report["id"] == report_id and report["estado"] == "pendiente"
    assert (report["motivo"], report["detalle"]) == ("Lenguaje ofensivo", "Me escribió de madrugada")
    assert report["fecha_creacion"] and report["fecha_revision"] is None
    assert (report["reportante"]["nombre"], report["reportante"]["apellido_inicial"]) == ("Ana", "T.")
    assert report["reportante_rol"] == "player"
    assert report["reportado"] == {"user_id": beto["id"], "nombre": "Beto", "apellido_inicial": "T."}
    assert report["objetivo_id"] == beto["id"]


def test_filter_by_state_and_todos_and_newest_first() -> None:
    client, _, admin, ana = setup()
    ids = [file_report(client, ana, str(uuid4()), motivo=m)
           for m in ("Spam o publicidad", "Lenguaje ofensivo", "Acoso o trato irrespetuoso", "Perfil falso o suplantación")]
    patch(client, admin, ids[0], "revisado")
    patch(client, admin, ids[1], "descartado")
    patch(client, admin, ids[2], "escalado")

    assert [r["id"] for r in listed(client, admin)] == [ids[3]]
    for estado, expected in (("pendiente", [ids[3]]), ("revisado", [ids[0]]), ("descartado", [ids[1]]),
                             ("escalado", [ids[2]])):
        assert [r["id"] for r in listed(client, admin, f"?estado={estado}")] == expected, estado
    assert [r["id"] for r in listed(client, admin, "?estado=todos")] == list(reversed(ids))


def test_pagination_and_the_page_size_cap() -> None:
    client, _, admin, ana = setup()
    ids = [file_report(client, ana, str(uuid4()), motivo=m) for m in
           ("Spam o publicidad", "Lenguaje ofensivo", "Acoso o trato irrespetuoso", "Perfil falso o suplantación")]
    newest = list(reversed(ids))
    assert [r["id"] for r in listed(client, admin, "?limit=3")] == newest[:3]
    assert [r["id"] for r in listed(client, admin, "?limit=3&pagina=2")] == newest[3:]
    assert listed(client, admin, "?limit=3&pagina=3") == []
    assert len(listed(client, admin, "?limit=100")) == 4
    for bad in ("limit=101", "limit=0", "pagina=0", "estado=otro", "estado=", "extra=1", "limit=abc"):
        assert client.get(f"{REPORTS}?{bad}", headers=admin["headers"]).status_code == 422, bad


def test_the_listing_exposes_no_email_rut_or_photo_of_anyone() -> None:
    client, repository, admin, ana = setup()
    beto = person(client, "Beto")
    file_report(client, ana, beto["id"])
    text = client.get(REPORTS, headers=admin["headers"]).text
    for secret in ("example.com", "@", "rut", "password", "foto_perfil"):
        assert secret not in text.lower().replace("reportante", ""), secret


def test_only_admins_see_reports_the_reported_person_and_the_reporter_included() -> None:
    client, repository, admin, ana = setup()
    beto, club = person(client, "Beto"), person(client, "Rocio", "club_admin", repository)
    report_id = file_report(client, ana, beto["id"])
    assert client.get(REPORTS).status_code == 401
    assert client.get(REPORTS, headers={"Authorization": "Bearer nope"}).status_code == 401
    for who in (ana, beto, club):
        response = client.get(REPORTS, headers=who["headers"])
        assert response.status_code == 403 and ana["id"] not in response.text and "Ana" not in response.text
        assert patch(client, who, report_id, "revisado").status_code == 403
    assert states(client, admin) == {report_id: "pendiente"}


# -- transiciones --------------------------------------------------------------------------------

VALID = [("pendiente", "revisado"), ("pendiente", "descartado"), ("pendiente", "escalado"), ("revisado", "escalado")]
INVALID = [("revisado", "descartado"), ("descartado", "revisado"), ("descartado", "escalado"),
           ("escalado", "revisado"), ("escalado", "descartado")]


def move(client, admin, report_id, path: list[str]) -> None:
    for step in path:
        assert patch(client, admin, report_id, step).status_code == 200


@pytest.mark.parametrize("origin,target", VALID)
def test_valid_transitions(origin, target) -> None:
    client, _, admin, ana = setup()
    report_id = file_report(client, ana, str(uuid4()))
    move(client, admin, report_id, [] if origin == "pendiente" else [origin])
    response = patch(client, admin, report_id, target)
    assert response.status_code == 200, response.text
    assert response.json()["estado"] == target and response.json()["fecha_revision"] is not None
    assert states(client, admin)[report_id] == target


@pytest.mark.parametrize("origin,target", INVALID)
def test_invalid_transitions_are_409_and_change_nothing(origin, target) -> None:
    client, _, admin, ana = setup()
    report_id = file_report(client, ana, str(uuid4()))
    move(client, admin, report_id, [origin])
    audited = len(items(client, admin))
    response = patch(client, admin, report_id, target)
    assert response.status_code == 409, response.text
    assert states(client, admin)[report_id] == origin
    assert len(items(client, admin)) == audited                          # No audit row for a refused change.


def test_pending_is_not_a_valid_target_and_unknown_states_are_422() -> None:
    client, _, admin, ana = setup()
    report_id = file_report(client, ana, str(uuid4()))
    for estado in ("pendiente", "cerrado", "", None, 1):
        assert patch(client, admin, report_id, estado).status_code == 422, estado


@pytest.mark.parametrize("estado", ["revisado", "descartado", "escalado"])
def test_repeating_a_transition_is_idempotent_and_audits_once(estado) -> None:
    client, _, admin, ana = setup()
    report_id = file_report(client, ana, str(uuid4()))
    first = patch(client, admin, report_id, estado)
    again = patch(client, admin, report_id, estado)
    assert first.status_code == again.status_code == 200
    assert again.json() == first.json()                                  # Nothing moved, not even the date.
    assert len(items(client, admin)) == 1


def test_escalating_only_marks_the_report_and_applies_no_sanction() -> None:
    client, repository, admin, ana = setup()
    beto = person(client, "Beto")
    report_id = file_report(client, ana, beto["id"])
    before = {u.id: u.role for u in repository._users.values()}
    assert patch(client, admin, report_id, "escalado").status_code == 200
    assert {u.id: u.role for u in repository._users.values()} == before
    assert client.get(f"{USERS}/{beto['id']}/profile", headers=beto["headers"]).status_code == 200   # Still active.
    assert {e["accion"] for e in items(client, admin)} == {"REPORT_ESCALATED"}


# -- auditoria -----------------------------------------------------------------------------------

def test_every_state_change_is_audited_with_the_decision_motivo_or_the_reports_own() -> None:
    client, _, admin, ana = setup()
    with_motivo, without = file_report(client, ana, str(uuid4())), file_report(client, ana, str(uuid4()), "Lenguaje ofensivo")
    patch(client, admin, with_motivo, "escalado", motivo="Reincidente, pasa a sanción")
    patch(client, admin, without, "revisado")
    patch(client, admin, without, "escalado")

    by_report = {}
    for event in items(client, admin):
        by_report.setdefault(event["recurso_id"], []).append((event["accion"], event["motivo"], event["detalle"]))
    assert by_report[with_motivo] == [("REPORT_ESCALATED", "Reincidente, pasa a sanción",
                                       {"tipo": "usuario", "desde": "pendiente"})]
    assert by_report[without] == [("REPORT_ESCALATED", "Lenguaje ofensivo", {"tipo": "usuario", "desde": "revisado"}),
                                  ("REPORT_REVIEWED", "Lenguaje ofensivo", {"tipo": "usuario", "desde": "pendiente"})]
    escalated = items(client, admin, "?accion=REPORT_ESCALATED&recurso_tipo=report")
    assert len(escalated) == 2 and all(e["admin"]["nombre"] == "Moderadora T." for e in escalated)


@pytest.mark.parametrize("estado", ["revisado", "descartado", "escalado"])
def test_if_the_audit_fails_the_change_is_not_saved_and_the_answer_is_503(estado) -> None:
    client, repository, admin, ana = setup()
    report_id = file_report(client, ana, str(uuid4()))
    repository.audit_unavailable = True
    assert patch(client, admin, report_id, estado).status_code == 503
    repository.audit_unavailable = False
    assert states(client, admin) == {report_id: "pendiente"}
    assert items(client, admin) == []
    assert patch(client, admin, report_id, estado).status_code == 200      # And it works once the audit is back.


# -- 404 y datos invalidos -----------------------------------------------------------------------

def test_an_unknown_report_is_404_for_admins_and_403_for_everyone_else() -> None:
    client, repository, admin, ana = setup()
    missing = uuid4()
    assert patch(client, admin, missing, "revisado").status_code == 404
    assert patch(client, ana, missing, "revisado").status_code == 403       # No hint about which reports exist.
    assert client.patch(f"{REPORTS}/{missing}", json={"estado": "revisado"}).status_code == 401


@pytest.mark.parametrize("body", [
    {"estado": "revisado", "motivo": "a\x00b"}, {"estado": "revisado", "motivo": "\ud800 roto"},
    {"estado": "revisado", "motivo": "ver ana@example.com"}, {"estado": "revisado", "motivo": "ab"},
    {"estado": "revisado", "motivo": "x" * 301}, {"estado": "revisado", "motivo": 5},
    {"estado": "revisado", "otro": 1}, {"motivo": "sin estado"}, {"estado": "revisado", "motivo": float("nan")},
])
def test_invalid_bodies_are_422_without_echoing_what_was_sent(body) -> None:
    client, _, admin, ana = setup()
    report_id = file_report(client, ana, str(uuid4()))
    response = client.patch(f"{REPORTS}/{report_id}", headers={**admin["headers"], "Content-Type": "application/json"},
                            content=json.dumps(body, allow_nan=True).encode())
    assert response.status_code == 422, response.text
    assert "input" not in response.text and "example.com" not in response.text and "roto" not in response.text
    assert states(client, admin) == {report_id: "pendiente"}


def test_a_report_id_that_is_not_a_uuid_is_422_without_echo() -> None:
    client, _, admin, _ = setup()
    response = client.patch(f"{REPORTS}/no-es-uuid-secreto", headers=admin["headers"], json={"estado": "revisado"})
    assert response.status_code == 422 and "secreto" not in response.text
