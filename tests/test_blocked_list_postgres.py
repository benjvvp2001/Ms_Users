"""HU-66 on real PostgreSQL: the query behind the block list (order, pages, deleted accounts, photos)."""
from datetime import datetime, timedelta, timezone

from test_blocks_mutes_postgres import people  # noqa: F401  (the fixture that builds an isolated schema)


def block_at(pool, blocker, blocked, when):
    with pool.connection() as conn:
        conn.execute("INSERT INTO bloqueo_usuario (bloqueador_id, bloqueado_id, fecha_creacion) VALUES (%s,%s,%s)",
                     (blocker, blocked, when))


def test_the_list_is_newest_first_with_name_photo_and_no_sensitive_columns(people):
    repo, pool, add = people
    ana, beto, carla = add(), add(), add()
    with pool.connection() as conn:
        conn.execute("UPDATE usuario SET foto_perfil = %s, nombre = 'Beto' WHERE id = %s", ("0" * 32 + ".jpg", beto))
    now = datetime.now(timezone.utc)
    block_at(pool, ana, beto, now - timedelta(hours=2))
    block_at(pool, ana, carla, now - timedelta(hours=1))
    rows = repo.list_blocked_people(ana, 10)
    assert [r.user_id for r in rows] == [carla, beto]
    assert rows[1].nombre == "Beto" and rows[1].foto_perfil == "0" * 32 + ".jpg" and rows[0].foto_perfil is None
    assert set(vars(rows[0])) == {"user_id", "nombre", "apellido_paterno", "foto_perfil", "fecha_bloqueo"}


def test_pages_cover_everything_once_even_with_the_same_block_date(people):
    repo, pool, add = people
    ana = add()
    others = [add() for _ in range(5)]
    same = datetime.now(timezone.utc)
    for other in others:
        block_at(pool, ana, other, same)                       # a tie: the order must still be stable
    seen = [r.user_id for offset in (0, 2, 4, 6) for r in repo.list_blocked_people(ana, 2, offset)]
    assert sorted(seen, key=str) == sorted(others, key=str) and len(set(seen)) == 5
    assert seen == [r.user_id for r in repo.list_blocked_people(ana, 10)]


def test_only_my_own_blocks_are_listed(people):
    repo, pool, add = people
    ana, beto, carla = add(), add(), add()
    repo.block_user(ana, beto)
    repo.block_user(beto, carla)
    assert [r.user_id for r in repo.list_blocked_people(ana, 10)] == [beto]
    assert [r.user_id for r in repo.list_blocked_people(carla, 10)] == []     # being blocked is not a list


def test_a_deactivated_account_is_left_out_and_pages_stay_full(people):
    repo, pool, add = people
    ana = add()
    others = [add() for _ in range(4)]
    base = datetime.now(timezone.utc)
    for index, other in enumerate(others):
        block_at(pool, ana, other, base - timedelta(minutes=index))
    with pool.connection() as conn:                             # what account deletion does (UserService.delete_account)
        conn.execute("UPDATE usuario SET is_active = FALSE WHERE id = %s", (others[1],))
    listed = [r.user_id for r in repo.list_blocked_people(ana, 10)]
    assert listed == [others[0], others[2], others[3]]
    assert [len(repo.list_blocked_people(ana, 2, offset)) for offset in (0, 2, 4)] == [2, 1, 0]
    repo.unblock_user(ana, others[1])                           # still harmless afterwards
    assert repo.list_blocked(ana) and others[1] not in [u for u, _ in repo.list_blocked(ana)]


def test_unblocking_only_removes_that_block_and_repeating_it_changes_nothing(people):
    repo, pool, add = people
    ana, beto, carla = add(), add(), add()
    repo.block_user(ana, beto)
    repo.block_user(ana, carla)
    repo.block_user(beto, ana)
    repo.unblock_user(ana, beto)
    repo.unblock_user(ana, beto)
    repo.unblock_user(carla, ana)                               # Carla never blocked Ana
    assert [r.user_id for r in repo.list_blocked_people(ana, 10)] == [carla]
    assert repo.blocked_between(ana, beto) is True              # Beto's own block of Ana is untouched
    repo.unblock_user(beto, ana)
    assert repo.blocked_between(ana, beto) is False
