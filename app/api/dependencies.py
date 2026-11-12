from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.security import get_token_subject
from app.repositories.base import StoredUser, UserRepository
from app.services.audit_service import AuditService
from app.services.sanction_service import SanctionService, ensure_not_suspended
from app.services.email_sender import EmailSender, get_email_sender
from app.services.user_service import UserService

bearer_scheme = HTTPBearer(auto_error=False)


def get_repository(request: Request) -> UserRepository:
    return request.app.state.repository


def get_mailer(request: Request) -> EmailSender:
    # Tests inject a fake sender through create_app(); otherwise use EMAIL_BACKEND.
    return getattr(request.app.state, "email_sender", None) or get_email_sender()


def get_content_authors(request: Request):
    # Tests inject a fake through create_app(); in production it is built at startup. None = no way to ask.
    return getattr(request.app.state, "content_authors", None)


def get_entitlements(request: Request):
    # HU-54: ms-pago, to feature paid athletes in suggestions. None = nobody is featured.
    return getattr(request.app.state, "entitlements", None)


def get_photo_storage(request: Request):
    # Built in create_app (a folder on a persistent volume); tests inject their own.
    return getattr(request.app.state, "photo_storage", None)


def get_user_service(
    repository: UserRepository = Depends(get_repository),
    email_sender: EmailSender = Depends(get_mailer),
    content_authors=Depends(get_content_authors),
    photo_storage=Depends(get_photo_storage),
    entitlements=Depends(get_entitlements),
) -> UserService:
    return UserService(repository, email_sender, content_authors=content_authors, photo_storage=photo_storage,
                       entitlements=entitlements)


def get_audit_service(repository: UserRepository = Depends(get_repository)) -> AuditService:
    return AuditService(repository)


def get_sanction_service(
    repository: UserRepository = Depends(get_repository),
    email_sender: EmailSender = Depends(get_mailer),
) -> SanctionService:
    return SanctionService(repository, email_sender)


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
    if user.role != "admin":  # HU-45: a sanction applies to a token that already exists, at once
        ensure_not_suspended(repository, user.id)
    return user
