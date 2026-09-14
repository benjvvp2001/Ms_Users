from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.security import get_token_subject
from app.repositories.base import StoredUser, UserRepository
from app.services.user_service import UserService

bearer_scheme = HTTPBearer(auto_error=False)


def get_repository(request: Request) -> UserRepository:
    return request.app.state.repository


def get_user_service(
    repository: UserRepository = Depends(get_repository),
) -> UserService:
    return UserService(repository)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    repository: UserRepository = Depends(get_repository),
) -> StoredUser:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
        )

    user_id = get_token_subject(credentials.credentials)
    user = repository.get_user(user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
        )
    return user
