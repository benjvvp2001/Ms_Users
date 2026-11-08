"""HU-61 on real PostgreSQL: migration 015, the atomic swap of the photo key, account deletion and orphan files.
Runs only with USERS_TEST_DATABASE_URL (the isolated qa_tests database, one throwaway schema per test)."""
from __future__ import annotations

import io
import os
from pathlib import Path

os.environ.setdefault("JWT_SECRET", "test-only-secret-that-is-long-enough")
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.main import create_app
from app.storage.cleanup import purge_orphans
from app.storage.photos import LocalPhotoStorage, is_photo_key
from fakes import FakeEmailSender
from test_admin_audit_postgres import audit_db  # noqa: F401  (fixture: isolated schema with migrations 001-015)

KEY_A = "a" * 32 + ".jpg"
KEY_B = "b" * 32 + ".png"
MIGRATION = Path(__file__).parents[1] / "db/migrations/015_foto_perfil.sql"


def with_location_table(pool):
    """delete_user also clears the live position; the shared fixture does not create that table (migration 008)."""
    with pool.connection() as conn:
        conn.execute((Path(__file__).parents[1] / "db/migrations/008_current_location.sql").read_text(encoding="utf-8"))


def photo_of(pool, user):
    with pool.connection() as conn:
        return conn.execute("SELECT foto_perfil FROM usuario WHERE id = %s", (user,)).fetchone()[0]


def jpeg() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (32, 32), (10, 200, 10)).save(out, format="JPEG")
    return out.getvalue()


def test_the_migration_is_repeatable_and_cleans_old_free_text_values(audit_db):  # noqa: F811
    repository, pool, add_user, _ = audit_db
    kept, junk, none = add_user(), add_user(), add_user()
    with pool.connection() as conn:
        conn.execute("ALTER TABLE usuario DROP CONSTRAINT IF EXISTS usuario_foto_perfil_clave")
        conn.execute("UPDATE usuario SET foto_perfil = %s WHERE id = %s", (KEY_A, kept))
        conn.execute("UPDATE usuario SET foto_perfil = %s WHERE id = %s", ("file:///cache/avatar.jpg", junk))
    for _ in range(2):
        with pool.connection() as conn:
            conn.execute(MIGRATION.read_text(encoding="utf-8"))
    assert (photo_of(pool, kept), photo_of(pool, junk), photo_of(pool, none)) == (KEY_A, None, None)
    with pool.connection() as conn:
        assert conn.execute("SELECT count(*) FROM pg_constraint WHERE conname = 'usuario_foto_perfil_clave'").fetchone()[0] == 1
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE usuario SET foto_perfil = 'http://evil.example/x.png' WHERE id = %s", (kept,))


def test_set_photo_returns_the_previous_key_and_changes_only_the_photo(audit_db):  # noqa: F811
    repository, pool, add_user, _ = audit_db
    user = add_user(nombre="Ana")
    assert repository.set_photo(user, KEY_A) == (True, None)
    assert repository.set_photo(user, KEY_B) == (True, KEY_A)
    assert photo_of(pool, user) == KEY_B
    assert repository.get_user(user).profile.nombre == "Ana"
    assert repository.set_photo(user, None) == (True, KEY_B)
    assert photo_of(pool, user) is None
    assert repository.set_photo(uuid4(), KEY_A) == (False, None)


def test_a_deleted_account_cannot_get_a_photo_and_its_column_is_cleared(audit_db):  # noqa: F811
    repository, pool, add_user, _ = audit_db
    with_location_table(pool)
    user = add_user()
    repository.set_photo(user, KEY_A)
    assert repository.delete_user(user) is True
    assert photo_of(pool, user) is None
    assert repository.set_photo(user, KEY_B) == (False, None)


def test_photo_keys_lists_only_what_profiles_point_to(audit_db):  # noqa: F811
    repository, pool, add_user, _ = audit_db
    first, second, third = add_user(), add_user(), add_user()
    repository.set_photo(first, KEY_A)
    repository.set_photo(second, KEY_B)
    repository.set_photo(third, KEY_B)
    repository.set_photo(third, None)
    assert repository.photo_keys() == {KEY_A, KEY_B}


def test_two_uploads_at_the_same_time_leave_exactly_one_file(audit_db, tmp_path):  # noqa: F811
    from concurrent.futures import ThreadPoolExecutor
    repository, pool, add_user, _ = audit_db
    user = add_user()
    storage = LocalPhotoStorage(tmp_path / "p")
    client = TestClient(create_app(repository=repository, email_sender=FakeEmailSender(), photo_storage=storage))
    from app.core.security import create_access_token
    token, _ = create_access_token(user, "player", f"{user}@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    def put(_):
        return client.put(f"/api/v1/users/{user}/profile/photo", headers=headers,
                          files={"foto": ("a.jpg", jpeg(), "image/jpeg")}).status_code

    with ThreadPoolExecutor(6) as pool_:
        assert set(pool_.map(put, range(6))) == {200}
    assert storage.keys() == [photo_of(pool, user)]


def test_full_flow_through_the_api_with_postgres_and_no_orphans(audit_db, tmp_path):  # noqa: F811
    repository, pool, add_user, _ = audit_db
    with_location_table(pool)
    ana, beto = add_user(nombre="Ana"), add_user(nombre="Beto")
    for user in (ana, beto):
        with pool.connection() as conn:   # athletes must be verified to appear in cards
            conn.execute("UPDATE usuario SET is_active = TRUE WHERE id = %s", (user,))
    storage = LocalPhotoStorage(tmp_path / "p")
    client = TestClient(create_app(repository=repository, email_sender=FakeEmailSender(), photo_storage=storage))
    from app.core.security import create_access_token
    def headers(user):
        token, _ = create_access_token(user, "player", f"{user}@example.com")
        return {"Authorization": f"Bearer {token}"}

    # no photo: null
    assert client.get(f"/api/v1/users/{ana}/profile", headers=headers(ana)).json()["foto_perfil"] is None
    # upload, replace, the old file goes away
    first = client.put(f"/api/v1/users/{ana}/profile/photo", headers=headers(ana),
                       files={"foto": ("a.jpg", jpeg(), "image/jpeg")}).json()["foto_perfil"]
    second = client.put(f"/api/v1/users/{ana}/profile/photo", headers=headers(ana),
                        files={"foto": ("a.jpg", jpeg(), "image/jpeg")}).json()["foto_perfil"]
    assert first != second and client.get(first).status_code == 404 and client.get(second).status_code == 200
    # someone else sees it in the card
    card = client.post("/api/v1/users/athletes/cards", headers=headers(beto), json=[str(ana)])
    assert card.status_code == 200 and card.json()[0]["foto_perfil"] == second
    # 403 on someone else's account
    assert client.put(f"/api/v1/users/{ana}/profile/photo", headers=headers(beto),
                      files={"foto": ("a.jpg", jpeg(), "image/jpeg")}).status_code == 403
    # delete the account: the file is gone and nothing in storage is unreferenced
    assert client.delete(f"/api/v1/users/{ana}", headers=headers(ana)).status_code == 204
    assert storage.keys() == []
    assert purge_orphans(repository, storage, dry_run=True) == []
    assert all(is_photo_key(k) for k in repository.photo_keys())
