from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    jwt_secret: str
    jwt_access_token_minutes: int
    jwt_issuer: str
    jwt_audience: str


def get_settings() -> Settings:
    """Read runtime-only configuration without exposing secrets in source."""
    jwt_secret = os.getenv("JWT_SECRET")
    if not jwt_secret:
        raise RuntimeError("JWT_SECRET must be configured before starting the service")
    if len(jwt_secret.encode("utf-8")) < 32:
        raise RuntimeError("JWT_SECRET must contain at least 32 bytes")

    raw_minutes = os.getenv("JWT_ACCESS_TOKEN_MINUTES", "30")
    try:
        access_token_minutes = int(raw_minutes)
    except ValueError as error:
        raise RuntimeError("JWT_ACCESS_TOKEN_MINUTES must be an integer") from error

    if access_token_minutes <= 0:
        raise RuntimeError("JWT_ACCESS_TOKEN_MINUTES must be greater than zero")

    return Settings(
        jwt_secret=jwt_secret,
        jwt_access_token_minutes=access_token_minutes,
        jwt_issuer=os.getenv("JWT_ISSUER", "sportmatch-auth"),
        jwt_audience=os.getenv("JWT_AUDIENCE", "sportmatch-mobile"),
    )


def get_database_url() -> str:
    """Read the PostgreSQL connection string without exposing it in source."""
    database_url = os.getenv("USERS_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("USERS_DATABASE_URL must be configured before starting the service")
    return database_url
