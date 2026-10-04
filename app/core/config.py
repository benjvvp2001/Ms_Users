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


@dataclass(frozen=True)
class EmailSettings:
    backend: str
    smtp_host: str | None
    smtp_port: int
    smtp_username: str | None
    smtp_password: str | None
    smtp_use_ssl: bool
    smtp_use_starttls: bool
    sender: str | None
    password_reset_code_minutes: int
    password_reset_max_attempts: int
    password_reset_resend_seconds: int
    email_verification_code_minutes: int
    email_verification_max_attempts: int
    email_verification_resend_seconds: int
    # Local development only: any 6-digit code activates the account.
    accept_any_verification_code: bool = False


def _positive_int(name: str, default: str) -> int:
    raw_value = os.getenv(name, default)
    try:
        value = int(raw_value)
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer") from error
    if value <= 0:
        raise RuntimeError(f"{name} must be greater than zero")
    return value


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def get_email_settings() -> EmailSettings:
    """Read the outgoing mail configuration used for password recovery and
    email verification.

    EMAIL_BACKEND=smtp (default) delivers through any SMTP provider (Gmail,
    Outlook, SendGrid...). EMAIL_BACKEND=console prints messages to stdout
    and must only be used in local development.
    """
    backend = os.getenv("EMAIL_BACKEND", "smtp").strip().lower()
    if backend not in {"smtp", "console"}:
        raise RuntimeError("EMAIL_BACKEND must be 'smtp' or 'console'")

    accept_any_code = _flag("EMAIL_VERIFICATION_ACCEPT_ANY_CODE", "false")
    if accept_any_code and backend != "console":
        raise RuntimeError(
            "EMAIL_VERIFICATION_ACCEPT_ANY_CODE is only allowed with EMAIL_BACKEND=console"
        )

    smtp_username = os.getenv("SMTP_USERNAME") or None
    return EmailSettings(
        backend=backend,
        smtp_host=os.getenv("SMTP_HOST") or None,
        smtp_port=_positive_int("SMTP_PORT", "587"),
        smtp_username=smtp_username,
        smtp_password=os.getenv("SMTP_PASSWORD") or None,
        smtp_use_ssl=_flag("SMTP_USE_SSL", "false"),
        smtp_use_starttls=_flag("SMTP_USE_STARTTLS", "true"),
        sender=os.getenv("EMAIL_FROM") or smtp_username,
        password_reset_code_minutes=_positive_int("PASSWORD_RESET_CODE_MINUTES", "15"),
        password_reset_max_attempts=_positive_int("PASSWORD_RESET_MAX_ATTEMPTS", "5"),
        password_reset_resend_seconds=_positive_int("PASSWORD_RESET_RESEND_SECONDS", "60"),
        email_verification_code_minutes=_positive_int("EMAIL_VERIFICATION_CODE_MINUTES", "30"),
        email_verification_max_attempts=_positive_int("EMAIL_VERIFICATION_MAX_ATTEMPTS", "5"),
        email_verification_resend_seconds=_positive_int(
            "EMAIL_VERIFICATION_RESEND_SECONDS", "60"
        ),
        accept_any_verification_code=accept_any_code,
    )


def get_database_url() -> str:
    """Read the PostgreSQL connection string without exposing it in source."""
    database_url = os.getenv("USERS_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("USERS_DATABASE_URL must be configured before starting the service")
    return database_url
