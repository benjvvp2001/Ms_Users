"""Almacenamiento de archivos de usuario, detrás de una interfaz."""

from app.storage.photos import LocalPhotoStorage, PhotoStorage, PhotoStorageError, is_photo_key, photo_url

__all__ = ["LocalPhotoStorage", "PhotoStorage", "PhotoStorageError", "is_photo_key", "photo_url"]
