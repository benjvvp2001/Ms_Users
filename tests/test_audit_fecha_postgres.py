"""audit_events.fecha must be TIMESTAMPTZ on a base built by the real migration chain (migration 013).

Defect: the 001 migration created the column as TIMESTAMP and the 002 one (CREATE TABLE IF NOT EXISTS) never fixed it,
so GET /admin/audit-events answered 500 ("Input should have timezone info") with any row.
Runs only with USERS_TEST_DATABASE_URL (the isolated qa_tests database); each test uses its own schema, dropped at the end."""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg_pool import ConnectionPool

from app.database.migrate import MIGRATIONS, migration_names
from app.repositories.postgres_user_repository import PostgresUserRepository
from app.schemas.audit import AdminAuditEntry, AuditAction, AuditQuery, AuditResourceType
from app.services.audit_service import AuditService

DIRECTORY = Path(__file__).parents[1] / "db/migrations"
FECHA_INDEXES = {"audit_events_actor_fecha_idx", "audit_events_fecha_idx", "audit_events_action_fecha_idx"}
ADMIN = SimpleNamespace(role="admin")  # AuditService.list_events only looks at the role


def apply(conn, schema, name):
    """Run one migration file inside `schema` (the files name `public.` in three to_regclass checks)."""
    conn.execute((DIRECTORY / name).read_text(encoding="utf-8").replace("'public.", f"'{schema}."))


@pytest.fixture
def base():
    dsn = os.environ.get("USERS_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set USERS_TEST_DATABASE_URL for isolated PostgreSQL audit tests")
    schema = "audit_fecha_test_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as setup:
        setup.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            setup.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            yield setup, schema, dsn
        finally:
            setup.execute("RESET search_path")
            setup.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def column_type(conn, schema):
    return conn.execute("SELECT data_type FROM information_schema.columns WHERE table_schema = %s "
                        "AND table_name = 'audit_events' AND column_name = 'fecha'", (schema,)).fetchone()[0]


def entry(actor, **changes):
    values = dict(actor_user_id=actor, action=AuditAction.USER_SUSPENDED, resource_type=AuditResourceType.USER,
                  resource_id=str(uuid4()), motivo="Acoso reiterado")
    return AdminAuditEntry(**{**values, **changes})


def test_the_migration_list_covers_every_file_in_order():
    files = sorted(path.name for path in DIRECTORY.glob("*.sql"))
    assert migration_names(False) == files                       # a new file that is not in migrate.py fails here
    assert migration_names(True) == files[1:]
    assert MIGRATIONS[-1] == "014_reporte_revision_autor.sql"


def test_all_migrations_in_order_on_an_empty_base_give_a_listing_that_works(base):
    setup, schema, dsn = base
    for name in migration_names(False):
        apply(setup, schema, name)
    assert column_type(setup, schema) == "timestamp with time zone"
    pool = ConnectionPool(dsn, kwargs={"options": f"-c search_path={schema}"}, min_size=1, max_size=3)
    try:
        pool.wait()
        repo = PostgresUserRepository(pool)
        service = AuditService(repo)
        admin, other = uuid4(), uuid4()
        repo.record_audit(user_id=admin, action="login", resource="auth", result="success")      # old writer, no HU-48 columns
        repo.record_admin_audit(entry(admin, motivo="Primera"))
        repo.record_admin_audit(entry(other, action=AuditAction.PLAN_CREATED, resource_type=AuditResourceType.PLAN,
                                      resource_id="plan-1", motivo="Segunda"))
        with pool.connection() as conn:   # fixed instants, so the date filters have something exact to cut
            conn.execute("UPDATE audit_events SET fecha = %s WHERE motivo = 'Primera'", (datetime(2026, 3, 1, 10, tzinfo=timezone.utc),))
            conn.execute("UPDATE audit_events SET fecha = %s WHERE motivo = 'Segunda'", (datetime(2026, 3, 5, 10, tzinfo=timezone.utc),))

        def listing(**filters):
            page = service.list_events(ADMIN, AuditQuery(**filters))
            assert all(item.fecha.tzinfo is not None for item in page.items)
            return [item.motivo for item in page.items], page.total

        assert listing() == (["Segunda", "Primera"], 2)                      # this raised ValidationError (HTTP 500) before 013
        day = lambda n: datetime(2026, 3, n, tzinfo=timezone.utc)
        assert listing(desde=day(2)) == (["Segunda"], 1)
        assert listing(hasta=day(2)) == (["Primera"], 1)
        assert listing(desde=day(1), hasta=day(6)) == (["Segunda", "Primera"], 2)
        assert listing(desde=day(6)) == ([], 0)
        assert listing(hasta=day(1)) == ([], 0)
        assert listing(admin_id=other) == (["Segunda"], 1)
        assert listing(accion="USER_SUSPENDED", desde=day(1), hasta=day(2)) == (["Primera"], 1)
        # A filter given in another offset means the same instant: 2026-03-05 07:00 -03:00 = 10:00 UTC.
        assert listing(desde=datetime(2026, 3, 5, 7, tzinfo=timezone(timedelta(hours=-3)))) == (["Segunda"], 1)
        # A row written now (DEFAULT CURRENT_TIMESTAMP) falls inside "last minute".
        repo.record_admin_audit(entry(admin, motivo="Ahora"))
        now = datetime.now(timezone.utc)
        assert listing(desde=now - timedelta(minutes=1), hasta=now + timedelta(minutes=1)) == (["Ahora"], 1)
    finally:
        pool.close()


def test_the_migration_converts_old_rows_without_moving_them_and_can_be_repeated(base):
    setup, schema, dsn = base
    for name in migration_names(False):
        if name != "013_audit_fecha_timestamptz.sql":
            apply(setup, schema, name)
    assert column_type(setup, schema) == "timestamp without time zone"      # the real situation of an existing base
    actor = uuid4()
    setup.execute("INSERT INTO audit_events (actor_user_id, action, resource, result, fecha) VALUES "
                  "(%s, 'login', 'auth', 'success', '2026-03-01 10:00:00'), "
                  "(%s, 'login', 'auth', 'success', '2026-10-28 23:59:59.123456')", (actor, actor))
    setup.execute("INSERT INTO audit_events (actor_user_id, action, resource, result, motivo, resource_type, resource_id, fecha) "
                  "VALUES (%s, 'USER_SUSPENDED', 'users/x', 'success', 'Antigua', 'user', %s, '2026-03-02 08:30:00')", (actor, str(uuid4())))
    before = setup.execute("SELECT id, extract(epoch FROM fecha AT TIME ZONE 'UTC') FROM audit_events ORDER BY id").fetchall()

    for _ in range(3):                                                         # applied on every startup
        apply(setup, schema, "013_audit_fecha_timestamptz.sql")

    assert column_type(setup, schema) == "timestamp with time zone"
    after = setup.execute("SELECT id, extract(epoch FROM fecha) FROM audit_events ORDER BY id").fetchall()
    assert after == before and len(after) == 3                                 # no row lost, no instant moved
    assert setup.execute("SELECT fecha FROM audit_events WHERE motivo = 'Antigua'").fetchone()[0] == datetime(2026, 3, 2, 8, 30, tzinfo=timezone.utc)
    indexes = {row[0] for row in setup.execute("SELECT i.indexrelid::regclass::text FROM pg_index i "
                                               "JOIN pg_class t ON t.oid = i.indrelid WHERE t.relname = 'audit_events' AND i.indisvalid")}
    assert FECHA_INDEXES <= {name.split(".")[-1].strip('"') for name in indexes}
    assert "CURRENT_TIMESTAMP" in setup.execute("SELECT column_default FROM information_schema.columns WHERE table_schema = %s "
                                                "AND table_name = 'audit_events' AND column_name = 'fecha'", (schema,)).fetchone()[0].upper()

    pool = ConnectionPool(dsn, kwargs={"options": f"-c search_path={schema}"}, min_size=1, max_size=2)
    try:
        pool.wait()
        service = AuditService(PostgresUserRepository(pool))
        page = service.list_events(ADMIN, AuditQuery(desde=datetime(2026, 3, 2, tzinfo=timezone.utc), hasta=datetime(2026, 3, 3, tzinfo=timezone.utc)))
        assert [item.motivo for item in page.items] == ["Antigua"] and page.items[0].fecha.utcoffset() == timedelta(0)
    finally:
        pool.close()


def test_the_migration_refuses_to_guess_when_the_base_is_not_in_utc(base):
    setup, schema, _ = base
    for name in migration_names(False):
        if name != "013_audit_fecha_timestamptz.sql":
            apply(setup, schema, name)
    setup.execute("INSERT INTO audit_events (actor_user_id, action, resource, result, fecha) "
                  "VALUES (gen_random_uuid(), 'login', 'auth', 'success', '2026-03-01 10:00:00')")
    setup.execute("SET timezone TO 'America/Santiago'")
    try:
        with pytest.raises(psycopg.errors.RaiseException, match="no es UTC"):
            apply(setup, schema, "013_audit_fecha_timestamptz.sql")
    finally:
        setup.execute("ROLLBACK")
        setup.execute("RESET timezone")
    assert column_type(setup, schema) == "timestamp without time zone"          # untouched
    assert setup.execute("SELECT fecha FROM audit_events").fetchone()[0] == datetime(2026, 3, 1, 10)

