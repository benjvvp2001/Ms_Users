from __future__ import annotations

import math
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.internal import accounts_router as internal_accounts_router
from app.api.internal import router as internal_router
from app.api.v1.users import router
from app.core.config import (
    get_content_author_settings, get_database_url, get_email_settings, get_photo_settings, get_settings,
)
from app.database.session import create_pool
from app.repositories.base import DependencyUnavailableError, UserRepository
from app.repositories.postgres_user_repository import PostgresUserRepository
from app.services.content_authors import ContentAuthors, InternalContentAuthors
from app.services.email_sender import EmailSender
from app.services.entitlements_client import EntitlementsClient, settings_from_env
from app.services.sanction_service import AccountSuspendedError
from app.storage.photos import LocalPhotoStorage, PhotoStorage


def _finite_or_text(value: float):
    """A number such as 1e999 reaches validation as infinity, which JSON cannot carry: the error
    message that echoes the received value would fail to serialize and answer 500 instead of 422."""
    return value if math.isfinite(value) else str(value)


_NO_ECHO_PREFIXES = ("/api/v1/users/admin/audit-events", "/api/v1/users/admin/reports",
                     "/api/v1/users/admin/sanctions", "/internal/")


def create_app(
    repository: UserRepository | None = None, email_sender: EmailSender | None = None,
    content_authors: ContentAuthors | None = None, photo_storage: PhotoStorage | None = None,
    entitlements: EntitlementsClient | None = None,
) -> FastAPI:
    """Create the service without routes outside the approved users prefix.

    With no repository given, the PostgreSQL connection pool (configured via
    USERS_DATABASE_URL) is opened on server startup, not at import time. Tests
    inject a MockUserRepository instead so they don't need a live database,
    and a fake email_sender so password recovery never sends real mail.
    """
    owns_pool = repository is None
    pool_holder: dict[str, object] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if owns_pool:
            get_settings()
            get_email_settings()
            pool = create_pool(get_database_url())
            pool_holder["pool"] = pool
            app.state.repository = PostgresUserRepository(pool)
        if content_authors is None:
            # HU-47: who made a reported activity (Ms_Activities) or message (Ms_Matching), over the internal network.
            settings = get_content_author_settings()
            app.state.content_authors = InternalContentAuthors(
                settings.activities_url, settings.matching_url, os.getenv("INTERNAL_SERVICE_TOKEN"), settings.timeout_seconds)
        own_entitlements = None
        if entitlements is None and owns_pool:
            # HU-54: featured athletes (paid plan) from ms-pago; without PAYMENTS_SERVICE_URL nobody is featured.
            entitlement_settings = settings_from_env()
            if entitlement_settings is not None:
                own_entitlements = EntitlementsClient(entitlement_settings)
                app.state.entitlements = own_entitlements
        try:
            yield
        finally:
            if own_entitlements is not None:
                own_entitlements.close()
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
    if content_authors is not None:
        app.state.content_authors = content_authors
    if entitlements is not None:
        app.state.entitlements = entitlements
    # HU-61: profile photos. A folder on a persistent volume by default; another PhotoStorage can be injected.
    app.state.photo_storage = photo_storage or LocalPhotoStorage(get_photo_settings().storage_path)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        if request.url.path.startswith(_NO_ECHO_PREFIXES):
            # Audit routes: say which field failed and why, never what was sent.
            return JSONResponse(status_code=422, content={"detail": [
                {"type": e["type"], "loc": jsonable_encoder(e["loc"]),
                 "msg": e["msg"] if e["type"] == "value_error" else "Valor inválido."}
                for e in error.errors()]})
        # The same 422 body as FastAPI's own, but able to carry an infinite number.
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(
            error.errors(), custom_encoder={float: _finite_or_text})})
    @app.exception_handler(AccountSuspendedError)
    async def account_suspended(_, error: AccountSuspendedError) -> JSONResponse:
        return JSONResponse(status_code=403, content=error.body())

    @app.exception_handler(DependencyUnavailableError)
    async def dependency_unavailable(_, error: DependencyUnavailableError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": "service temporarily unavailable"})
    if email_sender is not None:
        app.state.email_sender = email_sender
    app.include_router(router)
    app.include_router(internal_router)
    app.include_router(internal_accounts_router)

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
