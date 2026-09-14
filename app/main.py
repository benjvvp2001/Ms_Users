from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from app.api.v1.users import router
from app.core.config import get_database_url, get_settings
from app.database.session import create_pool
from app.repositories.base import UserRepository
from app.repositories.postgres_user_repository import PostgresUserRepository


def create_app(repository: UserRepository | None = None) -> FastAPI:
    """Create the service without routes outside the approved users prefix.

    With no repository given, the PostgreSQL connection pool (configured via
    USERS_DATABASE_URL) is opened on server startup, not at import time. Tests
    inject a MockUserRepository instead so they don't need a live database.
    """
    owns_pool = repository is None
    pool_holder: dict[str, object] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if owns_pool:
            get_settings()
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

    @app.get("/api/v1/users/health/ready", tags=["health"])
    def ready() -> dict[str, str]:
        pool = pool_holder.get("pool")
        if pool is not None:
            try:
                with pool.connection(timeout=2) as conn:
                    conn.execute("SELECT 1 FROM usuario LIMIT 1")
            except Exception as error:
                raise HTTPException(status_code=503, detail="users database unavailable") from error
        return {"status": "ready", "service": "ms_users"}

    return app


app = create_app()
