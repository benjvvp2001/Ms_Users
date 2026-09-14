from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    jwt_secret: str
    jwt_access_token_minutes: int


def get_settings() -> Settings:
    """Read runtime-only configuration without exposing secrets in source."""
    jwt_secret = os.getenv("JWT_SECRET")
    if not jwt_secret:
        raise RuntimeError("JWT_SECRET must be configured before starting the service")

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
    )


def get_database_url() -> str:
    """Read the PostgreSQL connection string without exposing it in source."""
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL must be configured before starting the service")
    return database_url
