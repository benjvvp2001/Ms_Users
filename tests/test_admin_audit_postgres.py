"""HU-48 on real PostgreSQL: the migration over existing rows, the SQL filters and the atomic review.
Runs only with USERS_TEST_DATABASE_URL (the isolated qa_tests database); each test uses its own schema."""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg_pool import ConnectionPool

import app.repositories.postgres_user_repository as repository_module
from app.repositories.base import DependencyUnavailableError
from app.repositories.postgres_user_repository import PostgresUserRepository
from app.schemas.audit import AdminAuditEntry, AuditAction, AuditQuery, AuditResourceType
from app.schemas.user import ReportCreate

MIGRATIONS = ("001_users_schema.sql", "004_email_verification.sql", "005_profile_goals_zone.sql",
              "006_reports_blocks.sql", "009_reporte_rol.sql")


@pytest.fixture
def audit_db():
    dsn = os.environ.get("USERS_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set USERS_TEST_DATABASE_URL for isolated PostgreSQL audit tests")
    schema = "audit_test_" + uuid4().hex
    pool = None
    with psycopg.connect(dsn, autocommit=True) as setup:
        setup.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            setup.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            directory = Path(__file__).parents[1] / "db/migrations"
            for name in MIGRATIONS:
                setup.execute((directory / name).read_text())
            # Rows written before HU-48 (old columns only), then the migration, applied twice.
            legacy_actor = uuid4()
            setup.execute("INSERT INTO audit_events (id, actor_user_id, action, resource, result) VALUES "
                          "(gen_random_uuid(), %s, 'login', 'auth', 'success'), "
                          "(gen_random_uuid(), %s, 'report_revisado', 'reports/' || gen_random_uuid()::text, 'success')",
                          (legacy_actor, legacy_actor))
            for _ in range(2):
                setup.execute((directory / "010_audit_admin.sql").read_text())
            for _ in range(2):
                setup.execute((directory / "011_reporte_escalado.sql").read_text())
            for _ in range(2):
                setup.execute((directory / "012_sanciones.sql").read_text())
            for _ in range(2):   # audit_events.fecha becomes TIMESTAMPTZ, as on a base that went through the whole chain
                setup.execute((directory / "013_audit_fecha_timestamptz.sql").read_text())
            for _ in range(2):   # HU-47: who resolved a report, the note and the author of the reported content
                setup.execute((directory / "014_reporte_revision_autor.sql").read_text())
            for _ in range(2):   # HU-61: profile photo keys only
                setup.execute((directory / "015_foto_perfil.sql").read_text())
            pool = ConnectionPool(dsn, kwargs={"options": f"-c search_path={schema}"}, min_size=1, max_size=4)
            pool.wait()

            def add_user(role="player", nombre="Prueba"):
                uid = uuid4()
                with pool.connection() as conn:
                    conn.execute("""INSERT INTO usuario(id,rol_id,nombre,apellido_paterno,email,password)
                        VALUES (%s,(SELECT id FROM rol WHERE nombre=%s),%s,'Temporal',%s,'unused')""",
                                 (uid, role, nombre, f"{uid}@example.com"))
                return uid

            yield PostgresUserRepository(pool), pool, add_user, legacy_actor
        finally:
            if pool:
                pool.close()
            setup.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def entry(actor, **changes):
    values = dict(actor_user_id=actor, action=AuditAction.USER_SUSPENDED, resource_type=AuditResourceType.USER,
                  resource_id=str(uuid4()), motivo="Acoso reiterado")
    return AdminAuditEntry(**{**values, **changes})


def test_the_migration_is_repeatable_and_keeps_old_rows(audit_db):
    repo, pool, _, legacy_actor = audit_db
    with pool.connection() as conn:
        rows = conn.execute("SELECT action, motivo, resource_type, metadata FROM audit_events ORDER BY action").fetchall()
    assert rows == [("login", None, None, None), ("report_revisado", None, None, None)]
    repo.record_audit(user_id=legacy_actor, action="login", resource="auth", result="success")   # Old writer still works.
    # Old report rows appear in the history under the catalog name; plain logins do not.
    records, total = repo.list_admin_audit(AuditQuery())
    assert total == 1 and records[0].action == "REPORT_REVIEWED" and records[0].resource_type == "report"


def test_filters_pagination_order_and_names(audit_db):
    repo, pool, add_user, _ = audit_db
    admin, other, victim = add_user("admin", "Moderadora"), add_user("admin", "Otro"), add_user("player", "Ana")
    start = datetime(2026, 3, 1, tzinfo=timezone.utc)
    for n in range(5):
        repo.record_admin_audit(entry(admin if n % 2 == 0 else other,
                                      action=AuditAction.USER_SUSPENDED if n % 2 == 0 else AuditAction.PLAN_CREATED,
                                      resource_type=AuditResourceType.USER if n % 2 == 0 else AuditResourceType.PLAN,
                                      resource_id=str(victim) if n % 2 == 0 else f"plan-{n}",
                                      motivo=f"Motivo {n}", result="failure" if n == 2 else "success"))
    with pool.connection() as conn:   # Spread the dates so ordering and ranges are testable.
        for n, motivo in enumerate(f"Motivo {i}" for i in range(5)):
            conn.execute("UPDATE audit_events SET fecha = %s WHERE motivo = %s", (start + timedelta(days=n), motivo))

    def motivos(**filters):
        records, _ = repo.list_admin_audit(AuditQuery(**filters))
        return [r.motivo for r in records if r.motivo]

    assert motivos() == [f"Motivo {n}" for n in (4, 3, 2, 1, 0)]
    assert motivos(desde=start + timedelta(days=3)) == ["Motivo 4", "Motivo 3"]
    assert motivos(hasta=start + timedelta(days=1)) == ["Motivo 1", "Motivo 0"]
    assert motivos(admin_id=other) == ["Motivo 3", "Motivo 1"]
    assert motivos(accion="PLAN_CREATED") == ["Motivo 3", "Motivo 1"]
    assert motivos(recurso_tipo="user") == ["Motivo 4", "Motivo 2", "Motivo 0"]
    assert motivos(recurso_id=str(victim)) == ["Motivo 4", "Motivo 2", "Motivo 0"]
    assert motivos(resultado="failure") == ["Motivo 2"]
    records, total = repo.list_admin_audit(AuditQuery(por_pagina=2, pagina=2, recurso_tipo="user"))
    assert total == 3 and [r.motivo for r in records] == ["Motivo 0"]
    first = repo.list_admin_audit(AuditQuery(recurso_id=str(victim)))[0][0]
    assert (first.admin_nombre, first.afectado_nombre) == ("Moderadora T.", "Ana T.")


def test_the_database_refuses_values_outside_the_catalog_shape(audit_db):
    _, pool, add_user, _ = audit_db
    admin = add_user("admin")
    for column, value in (("resource_type", "database"), ("motivo", "   "), ("metadata", '["lista"]'),
                          ("metadata", '{"x": "' + "a" * 3000 + '"}')):
        with pool.connection() as conn, pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(f"INSERT INTO audit_events (actor_user_id, action, resource, result, {column}) "
                         f"VALUES (%s, 'USER_SUSPENDED', 'x', 'success', %s)", (admin, value))


def test_reviewing_a_report_saves_decision_and_audit_together_or_neither(audit_db, monkeypatch):
    repo, pool, add_user, _ = audit_db
    admin, ana, beto = add_user("admin"), add_user(), add_user()
    body = ReportCreate(tipo="usuario", objetivo_id=str(beto), objetivo_nombre="Beto T.", motivo="Spam o publicidad",
                        detalle="Me escribió de madrugada")
    report = repo.create_report(ana, body, "player")

    def failing(conn, entry):
        raise psycopg.errors.OperationalError("audit table unavailable")
    monkeypatch.setattr(repository_module, "insert_admin_audit", failing)
    with pytest.raises(DependencyUnavailableError):
        repo.review_report(report.id, "revisado", admin)
    monkeypatch.undo()
    assert [r.estado for r in repo.list_reports(None, 10)] == ["pendiente"]       # Rolled back.

    assert repo.review_report(report.id, "revisado", admin, "Confirmado").estado == "revisado"
    event = repo.list_admin_audit(AuditQuery(accion="REPORT_REVIEWED"))[0][0]       # Newest first.
    assert (event.motivo, event.resource_id, event.admin_id) == ("Confirmado", str(report.id), admin)
    assert repo.review_report(uuid4(), "revisado", admin) is None
    assert repo.list_admin_audit(AuditQuery())[1] == 2    # The old legacy row plus this one.
