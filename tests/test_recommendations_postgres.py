"""Exercise the actual search SQL in an isolated schema, including >200 candidates."""
import os
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg_pool import ConnectionPool
import pytest

from app.repositories.postgres_user_repository import PostgresUserRepository
from app.schemas.user import SuggestionFilters, UserSport, Zona


@pytest.fixture
def directory():
    dsn = os.environ.get('USERS_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('Set USERS_TEST_DATABASE_URL for isolated PostgreSQL recommendation tests')
    schema = 'recommendations_test_' + uuid4().hex
    pool = None
    with psycopg.connect(dsn, autocommit=True) as setup:
        setup.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        try:
            setup.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
            migrations = Path(__file__).parents[1] / 'db/migrations'
            for name in ('001_users_schema.sql', '004_email_verification.sql', '005_profile_goals_zone.sql',
                         '006_reports_blocks.sql'):
                setup.execute((migrations / name).read_text())
            pool = ConnectionPool(dsn, kwargs={'options': f'-c search_path={schema}'}, min_size=1, max_size=2)
            pool.wait()
            repo = PostgresUserRepository(pool)
            def add(lat=None, level=3, code='running', available=True):
                uid = uuid4()
                with pool.connection() as conn:
                    conn.execute("""INSERT INTO usuario(id,rol_id,nombre,apellido_paterno,email,password)
                        VALUES (%s,(SELECT id FROM rol WHERE nombre='player'),'Prueba','Temporal',%s,'unused')""", (uid, f'{uid}@example.com'))
                    conn.execute('INSERT INTO usuario_deporte(usuario_id,deporte_codigo,nivel) VALUES (%s,%s,%s)', (uid, code, level))
                    conn.execute('INSERT INTO preferencia_usuario(usuario_id,disponibilidad_match) VALUES (%s,%s)', (uid, available))
                    if lat is not None:
                        conn.execute("INSERT INTO preferencia_perfil(usuario_id,comuna,latitud,longitud) VALUES (%s,'Prueba',%s,0)", (uid, lat))
                return uid
            yield repo, pool, add
        finally:
            if pool:
                pool.close()
            setup.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def test_real_sql_filters_before_limit_and_preserves_nearest_older_athlete(directory):
    repo, pool, add = directory
    me = add(0)
    near = add(0.018, level=4)
    middle = add(0.063)
    add(0.108)
    add(None)
    add(0.001, available=False)
    with pool.connection() as conn:
        # Add 250 newer athletes who do not match. The old 200-candidate cap must not apply.
        conn.execute("""INSERT INTO usuario(rol_id,nombre,apellido_paterno,email,password)
            SELECT (SELECT id FROM rol WHERE nombre='player'),'Nuevo','Temporal',
                   gen_random_uuid()::text || '@example.com','unused' FROM generate_series(1,250)""")
    origin = Zona(comuna='Prueba', latitud=0, longitud=0)
    sports = [UserSport(deporte_codigo='running', nivel=3)]
    def search(**kwargs):
        return repo.list_suggestion_candidates(exclude_user_id=me, limit=50,
            filters=SuggestionFilters(**kwargs), origin=origin, sports=sports)
    assert [c.id for c in search(radius_km=5, shared_sports=True, level_tolerance=1)] == [near]
    rows = search(radius_km=10, sport='running', min_level=3, max_level=4)
    assert [c.id for c in rows] == [near, middle]
    assert rows[0].distancia_km == pytest.approx(2.0015, abs=0.01)
    assert rows[0].nivel_coincidente
    assert [c.id for c in search(radius_km=10, max_level=3)] == [middle]
    assert [c.id for c in repo.list_suggestion_candidates(exclude_user_id=me, limit=1,
        filters=SuggestionFilters(radius_km=10), origin=origin, sports=sports)] == [near]


def test_real_sql_requires_all_sport_filters_on_the_same_row(directory):
    repo, pool, add = directory
    me = add(None)
    mixed = add(None, level=5)
    with pool.connection() as conn:
        conn.execute("INSERT INTO usuario_deporte(usuario_id,deporte_codigo,nivel) VALUES (%s,'tennis',2)", (mixed,))
    valid = add(None, level=4, code='RUNNING')
    for filters in (SuggestionFilters(sport='running', min_level=2, max_level=4),
                    SuggestionFilters(shared_sports=True, level_tolerance=1)):
        rows = repo.list_suggestion_candidates(exclude_user_id=me, limit=50, filters=filters,
            origin=None, sports=[UserSport(deporte_codigo='running', nivel=3)])
        assert [c.id for c in rows] == [valid]
        assert rows[0].distancia_km is None


def test_real_sql_hides_blocked_people_both_ways_and_stores_reports(directory):
    from app.schemas.user import ReportCreate
    repo, pool, add = directory
    me, blocked, other = add(None), add(None), add(None)
    def search(user):
        return {c.id for c in repo.list_suggestion_candidates(exclude_user_id=user, limit=50,
            filters=SuggestionFilters(), origin=None, sports=[])}
    repo.block_user(me, blocked)
    repo.block_user(me, blocked)
    assert search(me) == {other}
    assert search(blocked) == {other}
    assert [user for user, _ in repo.list_blocked(me)] == [blocked]
    repo.unblock_user(me, blocked)
    assert search(me) == {blocked, other}

    body = ReportCreate(tipo='usuario', objetivo_id=str(blocked), objetivo_nombre='Prueba T.', motivo='Spam o publicidad')
    created = repo.create_report(me, body)
    assert created.estado == 'pendiente'
    assert repo.create_report(me, body) is None
    assert [r.id for r in repo.list_reports('pendiente', 10)] == [created.id]
    assert repo.review_report(created.id, 'descartado', other).estado == 'descartado'
    assert repo.list_reports('pendiente', 10) == []
