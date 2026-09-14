from __future__ import annotations

from psycopg_pool import ConnectionPool


def normalize_dsn(database_url: str) -> str:
    """Accept the SQLAlchemy-style URL from .env.example and return a plain psycopg DSN."""
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


def create_pool(database_url: str) -> ConnectionPool:
    pool = ConnectionPool(
        conninfo=normalize_dsn(database_url),
        min_size=1,
        max_size=10,
        open=False,
    )
    pool.open(wait=True)
    return pool
