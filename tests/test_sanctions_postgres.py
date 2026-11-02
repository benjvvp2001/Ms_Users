"""HU-45 on real PostgreSQL: constraints, one sanction in force, expiry by time, atomic audit and notices.
Runs only with USERS_TEST_DATABASE_URL (the isolated qa_tests database)."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg
import pytest

import app.repositories.postgres_user_repository as repository_module
from app.repositories.base import (
    DependencyUnavailableError,
    SanctionConflictError,
    SanctionReportError,
    SanctionTargetNotFoundError,
)
from app.schemas.audit import AuditQuery
from app.schemas.user import ReportCreate
from test_admin_audit_postgres import audit_db  # noqa: F401  (fixture: isolated schema with migrations 001-012)

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def apply(repo, user, admin, *, tipo="temporal", dias=3, now=NOW, report_id=None, motivo="Acoso reiterado"):
    return repo.apply_sanction(
        sanction_id=uuid4(), user_id=user, tipo=tipo, dias=dias if tipo == "temporal" else None, motivo=motivo,
        admin_id=admin, report_id=report_id, now=now, fin=now + timedelta(days=dias) if tipo == "temporal" else None)


def audits(repo, user):
    records, _ = repo.list_admin_audit(AuditQuery(recurso_tipo="user", recurso_id=str(user)))
    return [(r.action, r.motivo, r.metadata) for r in records]


def test_the_migration_is_repeatable_and_the_constraints_hold(audit_db):  # noqa: F811
    _, pool, add_user, _ = audit_db
    user = add_user()
    base = ("INSERT INTO sancion (id, usuario_id, tipo, motivo, inicio, fin, estado, levantada_en) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)")
    bad = [("temporal", None, "activa", None), ("temporal", NOW, "activa", None),
           ("temporal", NOW - timedelta(days=1), "activa", None), ("permanente", NOW + timedelta(days=1), "activa", None),
           ("otro", None, "activa", None), ("permanente", None, "suspendida", None), ("permanente", None, "levantada", None),
           ("permanente", None, "activa", NOW)]
    for tipo, fin, estado, levantada in bad:
        with pool.connection() as conn, pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(base, (uuid4(), user, tipo, "Motivo", NOW, fin, estado, levantada))
    with pool.connection() as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(base, (uuid4(), user, "permanente", "   ", NOW, None, "activa", None))
    with pool.connection() as conn:
        conn.execute(base, (uuid4(), user, "permanente", "Motivo", NOW, None, "activa", None))
    with pool.connection() as conn, pytest.raises(psycopg.errors.UniqueViolation):      # a second one in force
        conn.execute(base, (uuid4(), user, "permanente", "Motivo", NOW, None, "activa", None))
    with pool.connection() as conn:                                                      # a lifted one does not count
        conn.execute(base, (uuid4(), user, "permanente", "Motivo", NOW, None, "levantada", NOW))


def test_apply_blocks_a_second_one_audits_and_expires_by_time(audit_db):  # noqa: F811
    repo, pool, add_user, _ = audit_db
    user, admin = add_user(), add_user("admin")
    first = apply(repo, user, admin, dias=2)
    assert repo.get_active_sanction(user, NOW + timedelta(days=1)).id == first.id
    assert repo.get_active_sanction(user, NOW + timedelta(days=2)) is None               # `fin` reached: no job needed
    assert repo.get_active_sanction(add_user(), NOW) is None
    with pytest.raises(SanctionConflictError):
        apply(repo, user, admin, tipo="permanente", now=NOW + timedelta(days=1))
    assert [a for a, _, _ in audits(repo, user)] == ["SANCTION_APPLIED"]                 # the refused one left nothing

    later = NOW + timedelta(days=3)                                                      # the ended one closes by itself
    second = apply(repo, user, admin, tipo="permanente", now=later)
    stored = {s.id: s for s in repo.list_sanctions(None, user, 10, 0, later)}
    assert stored[first.id].estado == "levantada" and stored[first.id].motivo_levantamiento == "Vencimiento automático"
    assert stored[second.id].state_at(later) == "activa"
    metadata = [m for a, _, m in audits(repo, user) if a == "SANCTION_APPLIED"]
    assert {"tipo": "permanente", "dias": None, "reporte_id": None, "sancion_id": str(second.id)} in metadata


def test_apply_checks_the_account_and_the_origin_report(audit_db):  # noqa: F811
    repo, pool, add_user, _ = audit_db
    user, other, admin = add_user(), add_user(), add_user("admin")
    with pytest.raises(SanctionTargetNotFoundError):
        apply(repo, uuid4(), admin)

    def report(target, motivo="Spam o publicidad"):
        return repo.create_report(other, ReportCreate(
            tipo="usuario", objetivo_id=str(target), objetivo_nombre="Prueba T.", motivo=motivo,
            detalle="Me escribió de madrugada"), "player")
    pending, elsewhere = report(user), report(admin, "Lenguaje ofensivo")
    repo.review_report(elsewhere.id, "escalado", admin)
    for given, reason in ((uuid4(), "not_found"), (pending.id, "not_escalated"), (elsewhere.id, "other_target")):
        with pytest.raises(SanctionReportError) as raised:
            apply(repo, user, admin, report_id=given)
        assert raised.value.reason == reason
    repo.review_report(pending.id, "escalado", admin)
    assert apply(repo, user, admin, report_id=pending.id).reporte_id == pending.id
    assert audits(repo, user)[0][2]["reporte_id"] == str(pending.id)


def test_a_failing_audit_saves_no_sanction_and_a_lift_is_idempotent(audit_db, monkeypatch):  # noqa: F811
    repo, pool, add_user, _ = audit_db
    user, admin = add_user(), add_user("admin")

    def failing(conn, entry):
        raise psycopg.errors.OperationalError("audit table unavailable")
    monkeypatch.setattr(repository_module, "insert_admin_audit", failing)
    with pytest.raises(DependencyUnavailableError):
        apply(repo, user, admin)
    monkeypatch.undo()
    assert repo.list_sanctions(None, user, 10, 0, NOW) == [] and repo.get_active_sanction(user, NOW) is None

    created = apply(repo, user, admin)
    monkeypatch.setattr(repository_module, "insert_admin_audit", failing)
    with pytest.raises(DependencyUnavailableError):
        repo.lift_sanction(created.id, admin, "Apelación aceptada", NOW)
    monkeypatch.undo()
    assert repo.get_active_sanction(user, NOW).id == created.id                           # still in force

    lifted, changed = repo.lift_sanction(created.id, admin, "Apelación aceptada", NOW)
    again, changed_again = repo.lift_sanction(created.id, admin, "Otro motivo", NOW)
    assert (changed, changed_again) == (True, False) and again == lifted
    assert lifted.levantada_por == admin and lifted.motivo_levantamiento == "Apelación aceptada"
    assert [a for a, _, _ in audits(repo, user)] == ["SANCTION_LIFTED", "SANCTION_APPLIED"]
    assert repo.lift_sanction(uuid4(), admin, "No existe", NOW) is None
    assert repo.get_active_sanction(user, NOW) is None


def test_lifting_one_that_ended_changes_nothing_and_listing_filters(audit_db):  # noqa: F811
    repo, _, add_user, _ = audit_db
    a, b, c, admin = add_user(), add_user(), add_user(), add_user("admin")
    ended = apply(repo, a, admin, dias=1)
    permanent = apply(repo, b, admin, tipo="permanente")
    lifted = apply(repo, c, admin)
    later = NOW + timedelta(days=2)
    assert repo.lift_sanction(ended.id, admin, "Tarde", later)[1] is False
    assert repo.lift_sanction(lifted.id, admin, "Apelación", later)[1] is True
    ids = lambda estado, user=None, limit=10, offset=0: [
        s.id for s in repo.list_sanctions(estado, user, limit, offset, later)]
    assert ids("activa") == [permanent.id] and ids("vencida") == [ended.id] and ids("levantada") == [lifted.id]
    assert ids(None, b) == [permanent.id] and ids(None) == [lifted.id, permanent.id, ended.id]
    assert ids(None, limit=2, offset=2) == [ended.id]


def test_notices_are_saved_and_queued_for_push_only_when_the_inbox_exists(audit_db):  # noqa: F811
    repo, pool, add_user, _ = audit_db
    user = add_user()
    # The push inbox of the gateway lives in the schema `notifications`; the test points the repository at a table
    # of its own temporary schema instead, so it never creates (or leaves behind) anything shared.
    with pool.connection() as conn:
        schema = conn.execute("SELECT current_schema()").fetchone()[0]
    repo._push_inbox = f"{schema}.push_inbox"
    repo.add_notification(user, "sancion", "Tu cuenta fue suspendida", "Motivo: Acoso", {"tipo": "temporal"})
    with pool.connection() as conn:
        assert conn.execute("SELECT titulo, data->>'tipo' FROM notificacion WHERE usuario_id = %s", (user,)).fetchall() \
            == [("Tu cuenta fue suspendida", "temporal")]
        conn.execute("CREATE TABLE push_inbox (id uuid PRIMARY KEY, source text NOT NULL, "
                     "source_id bigint NOT NULL, user_id uuid NOT NULL, kind text NOT NULL, title text NOT NULL, "
                     "body text NOT NULL, data jsonb NOT NULL, created_at timestamptz NOT NULL, read_at timestamptz, "
                     "UNIQUE(source, source_id))")
    repo.add_notification(user, "sancion", "Se levantó la suspensión", "Ya puedes volver", {"evento": "levantada"})
    with pool.connection() as conn:
        assert conn.execute("SELECT count(*) FROM notificacion WHERE usuario_id = %s", (user,)).fetchone()[0] == 2
        assert conn.execute("SELECT kind, source, title FROM push_inbox WHERE user_id = %s", (user,)).fetchall() \
            == [("sancion", "users", "Se levantó la suspensión")]
    # Without an inbox the notice is still saved.
    repo._push_inbox = f"{schema}.missing_inbox"
    repo.add_notification(user, "sancion", "Otro aviso", "Texto", {})
    with pool.connection() as conn:
        assert conn.execute("SELECT count(*) FROM notificacion WHERE usuario_id = %s", (user,)).fetchone()[0] == 3
