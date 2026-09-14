from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import bcrypt
import jwt
from fastapi import HTTPException, status
from jwt import InvalidTokenError

from app.core.config import get_settings

JWT_ALGORITHM = "HS256"


def hash_password(password: str) -> bytes:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())


def verify_password(password: str, password_hash: bytes) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash)


def create_access_token(user_id: UUID, role: str = "player") -> tuple[str, int]:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(
        minutes=settings.jwt_access_token_minutes
    )
    expires_in_seconds = int(settings.jwt_access_token_minutes * 60)
    token = jwt.encode(
        {
            "sub": str(user_id), "exp": expires_at, "iat": now,
            "iss": settings.jwt_issuer, "aud": settings.jwt_audience,
            "token_type": "access", "roles": [role],
            "scope": "profile:read profile:write" + (" gateway:read" if role == "admin" else ""),
        },
        settings.jwt_secret,
        algorithm=JWT_ALGORITHM,
    )
    return token, expires_in_seconds


def get_token_subject(token: str) -> UUID:
    settings = get_settings()
    try:
        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=[JWT_ALGORITHM],
            issuer=settings.jwt_issuer, audience=settings.jwt_audience,
            options={"require": ["sub", "iss", "aud", "iat", "exp", "token_type"]},
        )
        if payload["token_type"] != "access":
            raise InvalidTokenError("Only access tokens are accepted")
        return UUID(str(payload["sub"]))
    except (InvalidTokenError, KeyError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or expired access token",
        ) from error
