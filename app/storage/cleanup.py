"""Remove photo files that no profile points to (HU-61).

Deleting an old photo after the profile changed is best effort: if the disk refuses at that moment, the file stays
behind. Run this now and then (or after a crash):

    python -m app.storage.cleanup            # list what would be removed
    python -m app.storage.cleanup --delete   # remove it

A file is only removed if it is not referenced by any profile row. Photos written in the last `--min-age` seconds are
left alone (an upload may be between "file saved" and "profile updated").
"""

from __future__ import annotations

import os
import sys
import time

from app.repositories.base import UserRepository
from app.storage.photos import LocalPhotoStorage, PhotoStorage


def purge_orphans(repository: UserRepository, storage: PhotoStorage, *, dry_run: bool = False) -> list[str]:
    referenced = repository.photo_keys()
    orphans = [key for key in storage.keys() if key not in referenced]
    if not dry_run:
        for key in orphans:
            storage.delete(key)
    return orphans


def main(argv: list[str]) -> int:
    from app.core.config import get_database_url, get_photo_settings
    from app.database.session import create_pool
    from app.repositories.postgres_user_repository import PostgresUserRepository

    delete = "--delete" in argv
    min_age = 3600
    if "--min-age" in argv:
        min_age = int(argv[argv.index("--min-age") + 1])
    settings = get_photo_settings()
    storage = LocalPhotoStorage(settings.storage_path)
    pool = create_pool(get_database_url())
    try:
        # The age filter goes through the file system: this command is for the local-disk storage.
        orphans = purge_orphans(PostgresUserRepository(pool), storage, dry_run=True)
        cutoff = time.time() - min_age
        old = [key for key in orphans if os.path.getmtime(os.path.join(settings.storage_path, key)) < cutoff]
        for key in old:
            if delete:
                storage.delete(key)
            print(("deleted " if delete else "orphan ") + key)
        print(f"{len(old)} orphan file(s)" + ("" if delete else " (use --delete to remove them)"))
    finally:
        pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
