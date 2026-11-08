"""Where profile photos are kept (HU-61).

`PhotoStorage` is the only thing the rest of the service knows. Today it is a folder on a persistent Docker volume
(`LocalPhotoStorage`); moving to an object store (S3, GCS...) means writing another class with the same four methods
and building it in `create_app`. Nothing else changes.

A photo is addressed by its *key*: 32 random hex characters plus `.jpg` or `.png`. The key is generated here, never
derived from anything the client sent, so there is no original file name and no path to traverse. The key is also what
the `usuario.foto_perfil` column holds.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

_KEY = re.compile(r"^[0-9a-f]{32}\.(jpg|png)$")
PHOTO_URL_PREFIX = "/api/v1/users/photos/"
CONTENT_TYPES = {"jpg": "image/jpeg", "png": "image/png"}


class PhotoStorageError(RuntimeError):
    """The storage could not complete the operation (disk full, volume missing, permissions...)."""


def is_photo_key(value: object) -> bool:
    return isinstance(value, str) and _KEY.fullmatch(value) is not None


def new_photo_key(extension: str) -> str:
    return f"{secrets.token_hex(16)}.{extension}"


def content_type_of(key: str) -> str:
    return CONTENT_TYPES[key.rsplit(".", 1)[1]]


def photo_url(key: str | None) -> str | None:
    """What the API returns in `foto_perfil`: a path on the gateway, or None. Anything that is not a key this service
    generated (old free-text values, for example) is not served and reads as 'no photo'."""
    return f"{PHOTO_URL_PREFIX}{key}" if is_photo_key(key) else None


class PhotoStorage(Protocol):
    def save(self, key: str, data: bytes) -> None: ...

    def read(self, key: str) -> bytes | None: ...

    def delete(self, key: str) -> None: ...

    def keys(self) -> list[str]: ...


class LocalPhotoStorage:
    """Files in one flat folder. Writes are atomic (temp file + rename), so a crash never leaves half a photo."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def _path(self, key: str) -> Path:
        if not is_photo_key(key):
            raise PhotoStorageError("invalid photo key")
        return self._root / key

    def save(self, key: str, data: bytes) -> None:
        path = self._path(key)
        temp_name = None
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(dir=self._root, prefix=".tmp-")
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
            temp_name = None
        except OSError as error:
            raise PhotoStorageError(str(error)) from error
        finally:
            if temp_name is not None:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass

    def read(self, key: str) -> bytes | None:
        if not is_photo_key(key):
            return None
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise PhotoStorageError(str(error)) from error

    def delete(self, key: str) -> None:
        """Idempotent. A failure is logged, never raised: the profile is already updated and a leftover file is
        collected later by `purge_orphans`."""
        if not is_photo_key(key):
            return
        try:
            self._path(key).unlink(missing_ok=True)
        except OSError:
            logger.error("could not delete profile photo %s", key, exc_info=True)

    def keys(self) -> list[str]:
        try:
            return sorted(entry.name for entry in self._root.iterdir() if is_photo_key(entry.name))
        except FileNotFoundError:
            return []
        except OSError as error:
            raise PhotoStorageError(str(error)) from error
