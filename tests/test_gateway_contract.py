from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
import pytest
from fastapi import HTTPException

from app.core.config import get_database_url
from app.core.security import create_access_token, get_token_subject

SECRET = "gateway-users-contract-secret-with-more-than-32-bytes"


def test_users_database_url_takes_precedence(monkeypatch):
    monkeypatch.setenv("USERS_DATABASE_URL", "postgresql://users@localhost/sportmach_users")
    monkeypatch.setenv("DATABASE_URL", "postgresql://legacy@localhost/old")
    assert get_database_url() == "postgresql://users@localhost/sportmach_users"


def test_issued_token_matches_gateway_requirements(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", SECRET)
    user_id = uuid4()
    token, lifetime = create_access_token(user_id, "admin")
    claims = jwt.decode(token, SECRET, algorithms=["HS256"], issuer="sportmatch-auth", audience="sportmatch-mobile", options={"require": ["sub", "iss", "aud", "iat", "exp", "token_type"]})
    assert lifetime > 0
    assert claims["sub"] == str(user_id)
    assert claims["token_type"] == "access"
    assert claims["roles"] == ["admin"]
    assert "gateway:read" in claims["scope"].split()
    assert get_token_subject(token) == user_id


@pytest.mark.parametrize("change", [{"aud": "other"}, {"iss": "other"}, {"token_type": "refresh"}, {"exp": 1}])
def test_service_rejects_wrong_gateway_claims(monkeypatch, change):
    monkeypatch.setenv("JWT_SECRET", SECRET)
    now = datetime.now(timezone.utc)
    claims = {"sub": str(uuid4()), "iss": "sportmatch-auth", "aud": "sportmatch-mobile", "iat": now, "exp": now + timedelta(minutes=5), "token_type": "access"}
    claims.update(change)
    token = jwt.encode(claims, SECRET, algorithm="HS256")
    with pytest.raises(HTTPException) as error:
        get_token_subject(token)
    assert error.value.status_code == 401
