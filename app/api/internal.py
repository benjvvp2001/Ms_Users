"""Endpoints internos de Ms_Users (HU-48). Nunca se declaran en el gateway ni se publican fuera de Docker:
los otros servicios los llaman por la red interna, con el secreto compartido INTERNAL_SERVICE_TOKEN en
el encabezado X-Internal-Service-Token. Un token de usuario no sirve aquí."""
from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status

from app.api.dependencies import get_audit_service, get_repository
from app.core.config import get_internal_service_token
from app.repositories.base import UserRepository
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import get_sanction_service
from app.schemas.audit import AdminAuditEntry
from app.schemas.sanction import AccountStatus
from app.services.audit_service import AuditService
from app.services.sanction_service import SanctionService

router = APIRouter(prefix="/internal/v1/audit", tags=["internal"], include_in_schema=False)


def require_internal_service(
    x_internal_service_token: str | None = Header(default=None, max_length=256),
) -> None:
    try:
        expected = get_internal_service_token()
    except RuntimeError:  # secret configured but too weak: treat as not configured
        expected = None
    if expected is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "internal authentication is not configured")
    if x_internal_service_token is None or not hmac.compare_digest(
        x_internal_service_token.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "service authentication required")


@router.post("/events", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_internal_service)])
def record_event(
    entry: AdminAuditEntry,
    repository: UserRepository = Depends(get_repository),
    service: AuditService = Depends(get_audit_service),
) -> Response:
    """Another service reports an administrative action. The actor must be an existing administrator."""
    actor = repository.get_user(entry.actor_user_id)
    if actor is None or actor.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "the actor is not an administrator")
    service.record_entry(entry)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


accounts_router = APIRouter(prefix="/internal/v1/accounts", tags=["internal"], include_in_schema=False)


@accounts_router.get("/{user_id}/status", response_model=AccountStatus, dependencies=[Depends(require_internal_service)])
def account_status(user_id: UUID, service: SanctionService = Depends(get_sanction_service)) -> AccountStatus:
    """HU-45: the gateway asks, per account, whether it is suspended. Only the sanction's type, reason and end
    date: nothing about who reported or who sanctioned."""
    return service.account_status(user_id)


class AccountNamesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[UUID] = Field(max_length=100)


class AccountNames(BaseModel):
    names: dict[UUID, str]


@accounts_router.post("/names", response_model=AccountNames, dependencies=[Depends(require_internal_service)])
def account_names(
    payload: AccountNamesRequest, repository: UserRepository = Depends(get_repository)
) -> AccountNames:
    """HU-56: the short name ("Ana T.") of each account, so the administrator's plan screen can say who a
    subscription belongs to. Only the first name and the initial of the surname: never an e-mail or a RUT.
    Unknown accounts are left out."""
    names: dict[UUID, str] = {}
    for user_id in dict.fromkeys(payload.ids):
        user = repository.get_user(user_id)
        if user is not None and user.profile is not None:
            names[user_id] = f"{user.profile.nombre} {user.profile.apellido_paterno[:1].upper()}.".strip()
    return AccountNames(names=names)
