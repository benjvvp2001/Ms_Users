from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1.users import router
from app.core.config import get_database_url
from app.database.session import create_pool
from app.repositories.base import UserRepository
from app.repositories.postgres_user_repository import PostgresUserRepository


def create_app(repository: UserRepository | None = None) -> FastAPI:
    """Create the service without routes outside the approved users prefix.

    With no repository given, the PostgreSQL connection pool (configured via
    DATABASE_URL) is opened on server startup, not at import time. Tests
    inject a MockUserRepository instead so they don't need a live database.
    """
    owns_pool = repository is None
    pool_holder: dict[str, object] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if owns_pool:
            pool = create_pool(get_database_url())
            pool_holder["pool"] = pool
            app.state.repository = PostgresUserRepository(pool)
        try:
            yield
        finally:
            pool = pool_holder.get("pool")
            if pool is not None:
                pool.close()  # type: ignore[attr-defined]

    app = FastAPI(
        title="SportMatch users service",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    if repository is not None:
        app.state.repository = repository
    app.include_router(router)
    return app


app = create_app()
