"""HU-47 (panel web) en PostgreSQL real: la migración 014, quién resolvió un reporte con los mismos datos que la fila
de auditoría, la cuenta autora del contenido reportado y sancionar con un reporte de una actividad o un mensaje.
Corre solo con USERS_TEST_DATABASE_URL (la base aislada qa_tests)."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg
import pytest

from app.repositories.base import SanctionReportError
from app.schemas.audit import AuditQuery
from app.schemas.user import ReportCreate
from test_admin_audit_postgres import audit_db  # noqa: F401  (fixture: isolated schema with the migrations)


def new_report(repo, reporter, tipo="actividad", objetivo_id=None, motivo=None):
    motivo = motivo or {"mensaje": "Lenguaje ofensivo", "usuario": "Spam o publicidad"}.get(tipo, "Spam o publicidad")
    return repo.create_report(reporter, ReportCreate(
        tipo=tipo, objetivo_id=objetivo_id or str(uuid4()), objetivo_nombre="Contenido T.", motivo=motivo,
        detalle="Detalle suficiente del reporte"), "player")


def test_the_014_columns_exist_and_the_database_keeps_the_rules(audit_db):  # noqa: F811
    repo, pool, add_user, _ = audit_db
    reporter, author = add_user(), add_user()
    activity = new_report(repo, reporter)
    person = new_report(repo, reporter, "usuario", str(author))
    with pool.connection() as conn:
        columns = {c for (c,) in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'reporte'")}
        assert {"motivo_revision", "autor_id", "autor_consultado_en", "revisado_por", "fecha_revision"} <= columns
        with pytest.raises(psycopg.errors.CheckViolation):          # a report of a person has no separate author
            conn.execute("UPDATE reporte SET autor_id = %s WHERE id = %s", (author, person.id))
    with pool.connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):          # a note is at least 3 characters
            conn.execute("UPDATE reporte SET motivo_revision = 'ab' WHERE id = %s", (activity.id,))
    with pool.connection() as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):     # an author is a real account
            conn.execute("UPDATE reporte SET autor_id = %s WHERE id = %s", (uuid4(), activity.id))


def test_resolving_keeps_actor_date_and_note_and_the_audit_row_has_the_same_data(audit_db):  # noqa: F811
    repo, pool, add_user, _ = audit_db
    admin, reporter, target = add_user("admin", "Moderadora"), add_user(), add_user()
    report = new_report(repo, reporter, "usuario", str(target))
    done = repo.review_report(report.id, "revisado", admin, "Se revisó y no hay falta")
    assert (done.revisado_por, done.motivo_revision) == (admin, "Se revisó y no hay falta") and done.fecha_revision
    records, _ = repo.list_admin_audit(AuditQuery(recurso_tipo="report", recurso_id=str(report.id)))
    [event] = records
    assert event.admin_id == admin and event.motivo == done.motivo_revision
    assert event.fecha == done.fecha_revision                       # same transaction: the very same instant
    # Without a note the audit still uses the reason of the report, and the note stays empty.
    other = new_report(repo, reporter, "usuario", str(add_user()), motivo="Lenguaje ofensivo")
    bare = repo.review_report(other.id, "descartado", admin)
    assert bare.motivo_revision is None and bare.revisado_por == admin
    [event2], _ = repo.list_admin_audit(AuditQuery(recurso_tipo="report", recurso_id=str(other.id)))
    assert event2.motivo == "Lenguaje ofensivo"
    # Resolving again with the same state changes nothing (the first actor and note stay).
    again = repo.review_report(report.id, "revisado", add_user("admin"), "Otra nota distinta")
    assert (again.revisado_por, again.motivo_revision) == (admin, "Se revisó y no hay falta")


def test_the_author_is_remembered_listed_without_author_and_used_to_sanction(audit_db):  # noqa: F811
    repo, pool, add_user, _ = audit_db
    admin, reporter, author, stranger = add_user("admin"), add_user(), add_user(), add_user()
    activity = new_report(repo, reporter)
    message = new_report(repo, reporter, "mensaje", f"{uuid4()}:9")
    person = new_report(repo, reporter, "usuario", str(author))
    assert {r.id for r in repo.list_reports_without_author("pendiente", 100)} == {activity.id, message.id}   # not the person's
    now = datetime.now(timezone.utc)
    saved = repo.set_report_author(activity.id, author, now)
    assert saved.autor_id == author and saved.autor_consultado_en == now
    assert repo.set_report_author(person.id, author, now) is None                       # only activity and message
    gone = repo.set_report_author(message.id, None, now)                                 # content that does not exist
    assert gone.autor_id is None and gone.autor_consultado_en == now
    assert repo.set_report_author(message.id, uuid4(), now).autor_id is None            # an unknown account is never stored
    assert [r.id for r in repo.list_reports_without_author("pendiente", 100)] == [message.id]
    assert repo.set_report_author(uuid4(), author, now) is None
    # Sanction rules (HU-45): escalated first, and the report must be about that account.
    from app.services.sanction_service import utcnow
    t = utcnow()

    def apply(user, rid):
        return repo.apply_sanction(sanction_id=uuid4(), user_id=user, tipo="temporal", dias=7, motivo="Conducta reiterada",
                                   admin_id=admin, report_id=rid, now=t, fin=t + timedelta(days=7))

    with pytest.raises(SanctionReportError) as not_escalated:
        apply(author, activity.id)
    assert not_escalated.value.reason == "not_escalated"
    repo.review_report(activity.id, "escalado", admin, "Pasa a sanción")
    with pytest.raises(SanctionReportError) as other:
        apply(stranger, activity.id)
    assert other.value.reason == "other_target"
    repo.review_report(message.id, "escalado", admin, "Pasa a sanción")
    with pytest.raises(SanctionReportError) as unknown_author:
        apply(author, message.id)                                                          # no author known for that message
    assert unknown_author.value.reason == "other_target"
    done = apply(author, activity.id)
    assert done.usuario_id == author and done.reporte_id == activity.id
    records, _ = repo.list_admin_audit(AuditQuery(recurso_tipo="user", recurso_id=str(author)))
    assert any(r.action == "SANCTION_APPLIED" for r in records)


def test_the_014_migration_is_a_noop_when_repeated_and_keeps_old_reports(audit_db):  # noqa: F811
    repo, pool, add_user, _ = audit_db
    from pathlib import Path
    reporter = add_user()
    report = new_report(repo, reporter)
    admin = add_user("admin")
    repo.review_report(report.id, "descartado", admin, "Nota previa")
    text = (Path(__file__).parents[1] / "db/migrations/014_reporte_revision_autor.sql").read_text()
    with pool.connection() as conn:
        for _ in range(3):
            conn.execute(text)
    same = repo.list_reports(None, 10)[0]
    assert (same.id, same.estado, same.motivo_revision, same.revisado_por) == (report.id, "descartado", "Nota previa", admin)


def test_a_listing_with_more_than_100_pending_reports_pages_and_keeps_the_limit(audit_db):  # noqa: F811
    repo, pool, add_user, _ = audit_db
    reporter = add_user()
    for _ in range(130):
        new_report(repo, reporter)
    first = repo.list_reports("pendiente", 100, 0)
    second = repo.list_reports("pendiente", 100, 100)
    assert len(first) == 100 and len(second) == 30
    assert not {r.id for r in first} & {r.id for r in second}
    assert len(repo.list_reports_without_author("pendiente", 100)) == 100
