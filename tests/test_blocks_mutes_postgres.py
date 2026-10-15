"""HU-51 on real PostgreSQL: the SQL behind blocks, the block check and muted people."""
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg_pool import ConnectionPool

from app.repositories.postgres_user_repository import PostgresUserRepository

MIGRATIONS = ("001_users_schema.sql", "004_email_verification.sql", "005_profile_goals_zone.sql",
              "006_reports_blocks.sql", "007_chat_mutes.sql")


@pytest.fixture
def people():
    dsn = os.environ.get("USERS_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set USERS_TEST_DATABASE_URL for isolated PostgreSQL block and mute tests")
    schema = "blocks_test_" + uuid4().hex
    pool = None
    with psycopg.connect(dsn, autocommit=True) as setup:
        setup.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            setup.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            directory = Path(__file__).parents[1] / "db/migrations"
            for name in MIGRATIONS:
                setup.execute((directory / name).read_text())
            setup.execute((directory / "007_chat_mutes.sql").read_text())  # repeatable
            pool = ConnectionPool(dsn, kwargs={"options": f"-c search_path={schema}"}, min_size=1, max_size=4)
            pool.wait()

            def add():
                uid = uuid4()
                with pool.connection() as conn:
                    conn.execute("""INSERT INTO usuario(id,rol_id,nombre,apellido_paterno,email,password)
                        VALUES (%s,(SELECT id FROM rol WHERE nombre='player'),'Prueba','Temporal',%s,'unused')""",
                                 (uid, f"{uid}@example.com"))
                return uid

            yield PostgresUserRepository(pool), pool, add
        finally:
            if pool:
                pool.close()
            setup.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_block_reports_whether_it_is_new_and_keeps_the_first_date(people):
    repo, _, add = people
    ana, beto = add(), add()
    assert repo.block_user(ana, beto) is True
    first = repo.list_blocked(ana)[0][1]
    assert repo.block_user(ana, beto) is False
    assert repo.list_blocked(ana)[0][1] == first
    repo.unblock_user(ana, beto)
    repo.unblock_user(ana, beto)
    assert repo.list_blocked(ana) == []
    assert repo.block_user(ana, beto) is True  # new again after lifting it


def test_a_block_is_found_in_either_direction(people):
    repo, _, add = people
    ana, beto, carla = add(), add(), add()
    assert repo.blocked_between(ana, beto) is False
    repo.block_user(ana, beto)
    assert repo.blocked_between(ana, beto) is True and repo.blocked_between(beto, ana) is True
    assert repo.blocked_between(ana, carla) is False and repo.blocked_between(carla, beto) is False


def test_blocked_with_checks_many_people_in_one_query(people):
    repo, _, add = people
    ana, beto, carla, dani, elena = add(), add(), add(), add(), add()
    repo.block_user(ana, beto)  # Ana blocked Beto
    repo.block_user(carla, ana)  # Carla blocked Ana
    repo.block_user(dani, elena)  # unrelated to Ana
    assert repo.blocked_with(ana, [ana, beto, carla, dani, elena]) == {beto, carla}
    assert repo.blocked_with(beto, [ana, carla]) == {ana}
    assert repo.blocked_with(dani, [elena, ana]) == {elena}
    assert repo.blocked_with(ana, []) == set()
    assert repo.blocked_with(ana, [uuid4()]) == set()  # an id nobody has


def test_the_database_refuses_blocking_or_muting_oneself(people):
    _, pool, add = people
    ana = add()
    with pool.connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO bloqueo_usuario(bloqueador_id, bloqueado_id) VALUES (%s, %s)", (ana, ana))
    with pool.connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO silencio_usuario(silenciador_id, silenciado_id) VALUES (%s, %s)", (ana, ana))


def test_mutes_are_added_removed_listed_newest_first_and_idempotent(people):
    repo, _, add = people
    ana, beto, carla, dani = add(), add(), add(), add()
    repo.change_mutes(ana, [beto], [])
    repo.change_mutes(ana, [carla, dani], [])
    assert [muted for muted, _ in repo.list_muted(ana)][-1] == beto  # Beto, muted first, comes last
    assert {muted for muted, _ in repo.list_muted(ana)} == {beto, carla, dani}
    before = dict(repo.list_muted(ana))
    repo.change_mutes(ana, [beto, carla], [])  # again
    assert dict(repo.list_muted(ana)) == before  # original dates are kept
    repo.change_mutes(ana, [], [beto, uuid4()])  # an id that was never muted is ignored
    repo.change_mutes(ana, [beto], [carla])  # both in one call
    assert {muted for muted, _ in repo.list_muted(ana)} == {beto, dani}
    assert repo.list_muted(beto) == []  # lists are per person
    repo.change_mutes(ana, [], [])  # nothing to do is fine


def test_mute_changes_apply_together_or_not_at_all(people):
    repo, _, add = people
    ana, beto, carla = add(), add(), add()
    repo.change_mutes(ana, [beto], [])
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        repo.change_mutes(ana, [uuid4()], [beto])  # unmuting Beto happens first, then the unknown person fails
    assert {muted for muted, _ in repo.list_muted(ana)} == {beto}  # Beto is still muted


def test_removing_a_person_removes_their_blocks_and_mutes(people):
    repo, pool, add = people
    ana, beto = add(), add()
    repo.block_user(ana, beto)
    repo.block_user(beto, ana)
    repo.change_mutes(ana, [beto], [])
    repo.change_mutes(beto, [ana], [])
    with pool.connection() as conn:
        conn.execute("DELETE FROM usuario WHERE id = %s", (beto,))
    assert repo.list_blocked(ana) == [] and repo.list_muted(ana) == []
    assert repo.blocked_between(ana, beto) is False


def test_simultaneous_blocks_create_it_only_once(people):
    from concurrent.futures import ThreadPoolExecutor

    repo, _, add = people
    ana, beto = add(), add()
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: repo.block_user(ana, beto), range(8)))
    assert results.count(True) == 1 and results.count(False) == 7
    assert len(repo.list_blocked(ana)) == 1
