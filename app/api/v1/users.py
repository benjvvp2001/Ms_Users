from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from app.api.dependencies import get_current_user, get_user_service
from app.repositories.base import StoredUser
from app.schemas.user import (
    ConsentCreate,
    ConsentRead,
    DataExport,
    LoginRequest,
    PreferencesReplace,
    ProfileRead,
    ProfileReplace,
    RegisterRequest,
    RoleRead,
    TokenResponse,
)
from app.services.user_service import UserService

router = APIRouter(prefix="/api/v1/users", tags=["users"])


@router.post(
    "/auth/register",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
)
def register(
    payload: RegisterRequest,
    service: UserService = Depends(get_user_service),
) -> TokenResponse:
    return service.register(payload)


@router.post("/auth/login", response_model=TokenResponse)
def login(
    payload: LoginRequest,
    service: UserService = Depends(get_user_service),
) -> TokenResponse:
    return service.login(payload)


@router.get("/{user_id}/profile", response_model=ProfileRead)
def get_profile(
    user_id: UUID,
    current_user: StoredUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> ProfileRead:
    return service.get_profile(current_user, user_id)


@router.put("/{user_id}/profile", response_model=ProfileRead)
def replace_profile(
    user_id: UUID,
    payload: ProfileReplace,
    current_user: StoredUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> ProfileRead:
    return service.replace_profile(current_user, user_id, payload)


@router.get("/{user_id}/roles", response_model=RoleRead)
def get_roles(
    user_id: UUID,
    current_user: StoredUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> RoleRead:
    return service.get_roles(current_user, user_id)


@router.get("/{user_id}/preferences", response_model=PreferencesReplace)
def get_preferences(
    user_id: UUID,
    current_user: StoredUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> PreferencesReplace:
    return service.get_preferences(current_user, user_id)


@router.put("/{user_id}/preferences", response_model=PreferencesReplace)
def replace_preferences(
    user_id: UUID,
    payload: PreferencesReplace,
    current_user: StoredUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> PreferencesReplace:
    return service.replace_preferences(current_user, user_id, payload)


@router.get("/{user_id}/consents", response_model=list[ConsentRead])
def get_consents(
    user_id: UUID,
    current_user: StoredUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> list[ConsentRead]:
    return service.list_consents(current_user, user_id)


@router.post(
    "/{user_id}/consents",
    response_model=ConsentRead,
    status_code=status.HTTP_201_CREATED,
)
def grant_consent(
    user_id: UUID,
    payload: ConsentCreate,
    current_user: StoredUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> ConsentRead:
    return service.grant_consent(current_user, user_id, payload)


@router.delete("/{user_id}/consents/{consent_id}", response_model=ConsentRead)
def revoke_consent(
    user_id: UUID,
    consent_id: UUID,
    current_user: StoredUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> ConsentRead:
    return service.revoke_consent(current_user, user_id, consent_id)


@router.get("/{user_id}/exports", response_model=DataExport)
def export_personal_data(
    user_id: UUID,
    current_user: StoredUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> DataExport:
    return service.export_personal_data(current_user, user_id)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(
    user_id: UUID,
    current_user: StoredUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> Response:
    service.delete_account(current_user, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
