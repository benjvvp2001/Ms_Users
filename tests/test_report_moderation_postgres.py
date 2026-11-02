"""HU-47 on real PostgreSQL: the 'escalado' state, row-locked transitions and the audit in the same transaction.
Runs only with USERS_TEST_DATABASE_URL (the isolated qa_tests database)."""
from uuid import uuid4

import psycopg
import pytest

import app.repositories.postgres_user_repository as repository_module
from app.repositories.base import DependencyUnavailableError, InvalidReportTransitionError
from app.schemas.audit import AuditQuery
from app.schemas.user import ReportCreate
from test_admin_audit_postgres import audit_db  # noqa: F401  (fixture: isolated schema with migrations 001-011)


def new_report(repo, add_user, motivo="Spam o publicidad"):
    reporter, target = add_user(), add_user()
    return repo.create_report(reporter, ReportCreate(
        tipo="usuario", objetivo_id=str(target), objetivo_nombre="Prueba T.", motivo=motivo,
        detalle="Me escribió de madrugada"), "player")


def audit_rows(repo, report_id):
    records, _ = repo.list_admin_audit(AuditQuery(recurso_tipo="report", recurso_id=str(report_id)))
    return [(r.action, r.motivo, r.metadata) for r in records]


def test_the_state_check_now_accepts_escalado_and_still_refuses_others(audit_db):  # noqa: F811
    repo, pool, add_user, _ = audit_db
    report = new_report(repo, add_user)
    with pool.connection() as conn:
        conn.execute("UPDATE reporte SET estado = 'escalado' WHERE id = %s", (report.id,))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE reporte SET estado = 'cerrado' WHERE id = %s", (report.id,))


def test_every_valid_transition_audits_in_the_same_transaction(audit_db):  # noqa: F811
    repo, _, add_user, _ = audit_db
    admin = add_user("admin")
    path = {"revisado": ["revisado", "escalado"], "descartado": ["descartado"], "escalado": ["escalado"]}
    for first, steps in path.items():
        report = new_report(repo, add_user, motivo={"revisado": "Spam o publicidad", "descartado": "Lenguaje ofensivo",
                                                    "escalado": "Acoso o trato irrespetuoso"}[first])
        for step in steps:
            assert repo.review_report(report.id, step, admin, "Decisión del moderador").estado == step
        actions = [a for a, _, _ in audit_rows(repo, report.id)]
        assert actions == [{"revisado": "REPORT_ESCALATED", "descartado": "REPORT_DISCARDED",
                            "escalado": "REPORT_ESCALATED"}[steps[-1]]] + (
            ["REPORT_REVIEWED"] if steps == ["revisado", "escalado"] else [])


def test_idempotent_repeat_invalid_transition_and_unknown_report(audit_db):  # noqa: F811
    repo, pool, add_user, _ = audit_db
    admin = add_user("admin")
    report = new_report(repo, add_user)
    first = repo.review_report(report.id, "descartado", admin)
    again = repo.review_report(report.id, "descartado", admin)
    assert again.fecha_revision == first.fecha_revision and len(audit_rows(repo, report.id)) == 1
    with pytest.raises(InvalidReportTransitionError):
        repo.review_report(report.id, "escalado", admin)
    assert repo.list_reports("descartado", 10)[0].id == report.id and len(audit_rows(repo, report.id)) == 1
    assert repo.review_report(uuid4(), "revisado", admin) is None


def test_a_failing_audit_rolls_the_escalation_back(audit_db, monkeypatch):  # noqa: F811
    repo, _, add_user, _ = audit_db
    admin = add_user("admin")
    report = new_report(repo, add_user)

    def failing(conn, entry):
        raise psycopg.errors.OperationalError("audit table unavailable")
    monkeypatch.setattr(repository_module, "insert_admin_audit", failing)
    with pytest.raises(DependencyUnavailableError):
        repo.review_report(report.id, "escalado", admin)
    monkeypatch.undo()
    assert [r.estado for r in repo.list_reports(None, 10)] == ["pendiente"] and audit_rows(repo, report.id) == []


def test_listing_filters_by_state_and_pages_newest_first(audit_db):  # noqa: F811
    repo, _, add_user, _ = audit_db
    admin = add_user("admin")
    reports = [new_report(repo, add_user, motivo=m) for m in
               ("Spam o publicidad", "Lenguaje ofensivo", "Acoso o trato irrespetuoso")]
    repo.review_report(reports[0].id, "escalado", admin)
    newest = [r.id for r in reversed(reports)]
    assert [r.id for r in repo.list_reports("pendiente", 10)] == [r for r in newest if r != reports[0].id]
    assert [r.id for r in repo.list_reports("escalado", 10)] == [reports[0].id]
    assert [r.id for r in repo.list_reports(None, 2)] == newest[:2]
    assert [r.id for r in repo.list_reports(None, 2, 2)] == newest[2:]
