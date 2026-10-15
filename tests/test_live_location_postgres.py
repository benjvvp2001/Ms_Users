"""Live position on real PostgreSQL: the table, the upsert and the search SQL that prefers a recent position."""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg_pool import ConnectionPool

from app.repositories.postgres_user_repository import PostgresUserRepository
from app.schemas.user import SuggestionFilters, UserSport, Zona

MIGRATIONS = ("001_users_schema.sql", "004_email_verification.sql", "005_profile_goals_zone.sql",
              "006_reports_blocks.sql", "008_current_location.sql")
SPORTS = [UserSport(deporte_codigo="running", nivel=3)]


@pytest.fixture
def people():
    dsn = os.environ.get("USERS_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set USERS_TEST_DATABASE_URL for isolated PostgreSQL location tests")
    schema = "location_test_" + uuid4().hex
    pool = None
    with psycopg.connect(dsn, autocommit=True) as setup:
        setup.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            setup.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            directory = Path(__file__).parents[1] / "db/migrations"
            for name in MIGRATIONS:
                setup.execute((directory / name).read_text(encoding="utf-8"))
            setup.execute((directory / "008_current_location.sql").read_text(encoding="utf-8"))  # repeatable
            pool = ConnectionPool(dsn, kwargs={"options": f"-c search_path={schema}"}, min_size=1, max_size=4)
            pool.wait()

            def add(zone_lat=None):
                uid = uuid4()
                with pool.connection() as conn:
                    conn.execute("""INSERT INTO usuario(id,rol_id,nombre,apellido_paterno,email,password)
                        VALUES (%s,(SELECT id FROM rol WHERE nombre='player'),'Prueba','Temporal',%s,'unused')""",
                                 (uid, f"{uid}@example.com"))
                    conn.execute("INSERT INTO usuario_deporte(usuario_id,deporte_codigo,nivel) VALUES (%s,'running',3)", (uid,))
                    if zone_lat is not None:
                        conn.execute("INSERT INTO preferencia_perfil(usuario_id,comuna,latitud,longitud) "
                                     "VALUES (%s,'Prueba',%s,0)", (uid, zone_lat))
                return uid

            yield PostgresUserRepository(pool), pool, add
        finally:
            if pool:
                pool.close()
            setup.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def search(repo, me, origin, fresh_since=None, **filters):
    fresh_since = fresh_since or datetime.now(timezone.utc) - timedelta(days=1)
    return repo.list_suggestion_candidates(exclude_user_id=me, limit=50, filters=SuggestionFilters(**filters),
                                           origin=origin, sports=SPORTS, fresh_since=fresh_since)


def test_save_replaces_the_single_row_and_returns_plain_numbers(people):
    repo, pool, add = people
    ana = add()
    assert repo.get_location(ana) is None
    repo.save_location(ana, -33.45694, -70.64827, 15)
    first = repo.get_location(ana)
    assert (first.latitud, first.longitud, first.precision_m) == (-33.45694, -70.64827, 15)
    assert isinstance(first.latitud, float)
    repo.save_location(ana, -33.5, -70.7, None)
    second = repo.get_location(ana)
    assert (second.latitud, second.precision_m) == (-33.5, None) and second.actualizada_en >= first.actualizada_en
    with pool.connection() as conn:
        assert conn.execute("SELECT count(*) FROM ubicacion_actual").fetchone()[0] == 1  # no history


def test_touch_renews_the_date_without_moving_and_delete_reports_whether_it_existed(people):
    repo, pool, add = people
    ana = add()
    repo.save_location(ana, 1.0, 2.0, None)
    with pool.connection() as conn:
        conn.execute("UPDATE ubicacion_actual SET actualizada_en = actualizada_en - interval '1 hour'")
    old = repo.get_location(ana)
    repo.touch_location(ana)
    renewed = repo.get_location(ana)
    assert renewed.actualizada_en > old.actualizada_en and (renewed.latitud, renewed.longitud) == (1.0, 2.0)
    assert repo.delete_location(ana) is True and repo.delete_location(ana) is False
    repo.touch_location(ana)  # nothing to renew, not an error
    assert repo.get_location(ana) is None


def test_the_table_refuses_impossible_coordinates(people):
    repo, pool, add = people
    ana = add()
    for lat, lon in ((91, 0), (-91, 0), (0, 181), (0, -181)):
        with pytest.raises(psycopg.errors.DatabaseError), pool.connection() as conn:
            conn.execute("INSERT INTO ubicacion_actual(usuario_id,latitud,longitud) VALUES (%s,%s,%s)", (ana, lat, lon))
    assert repo.get_location(ana) is None


def test_search_uses_the_live_position_when_recent_and_the_zone_when_not(people):
    repo, pool, add = people
    me = add(0)
    mover = add(0.018)     # zone ~2 km from me
    still = add(0.063)     # zone ~7 km from me
    origin = Zona(comuna="Prueba", latitud=0, longitud=0)
    assert [(c.id, round(c.distancia_km, 1)) for c in search(repo, me, origin, radius_km=10)] == [(mover, 2.0), (still, 7.0)]

    repo.save_location(mover, 0.5, 0, None)                      # moved ~55 km away
    assert [c.id for c in search(repo, me, origin, radius_km=10)] == [still]
    assert {c.id for c in search(repo, me, origin)} == {mover, still}

    repo.save_location(still, 0.0, 0.0, None)                    # now right next to me
    ranked = search(repo, me, origin, radius_km=10)
    assert [c.id for c in ranked] == [still] and ranked[0].distancia_km == pytest.approx(0.0, abs=0.01)

    # An old position is ignored and the zone is used again.
    with pool.connection() as conn:
        conn.execute("UPDATE ubicacion_actual SET actualizada_en = CURRENT_TIMESTAMP - interval '2 days'")
    assert [(c.id, round(c.distancia_km, 1)) for c in search(repo, me, origin, radius_km=10)] == [(mover, 2.0), (still, 7.0)]


def test_a_person_with_only_a_live_position_and_no_zone_can_be_found(people):
    repo, _, add = people
    me, nomad = add(0), add(None)
    origin = Zona(comuna="Prueba", latitud=0, longitud=0)
    assert search(repo, me, origin, radius_km=10) == []
    repo.save_location(nomad, 0.018, 0.0, None)
    assert [c.id for c in search(repo, me, origin, radius_km=10)] == [nomad]


def test_deleting_the_account_removes_the_position(people):
    repo, pool, add = people
    ana = add()
    repo.save_location(ana, 1.0, 2.0, None)
    assert repo.delete_user(ana) is True
    assert repo.get_location(ana) is None
    with pool.connection() as conn:
        assert conn.execute("SELECT count(*) FROM ubicacion_actual").fetchone()[0] == 0
