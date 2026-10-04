from __future__ import annotations

from uuid import UUID
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query, Response, status

from app.api.dependencies import get_current_user, get_user_service
from app.repositories.base import StoredUser
from app.schemas.user import (
    ConsentCreate,
    ConsentRead,
    DataExport,
    EmailVerificationConfirm,
    EmailVerificationRequest,
    LoginRequest,
    MessageResponse,
    PasswordResetConfirm,
    PasswordResetRequest,
    PreferencesReplace,
    ProfileRead,
    ProfileReplace,
    RegisterRequest,
    RegisterResponse,
    RoleRead,
    SuggestedUser,
    TokenResponse,
)
from app.services.user_service import UserService

router = APIRouter(prefix="/api/v1/users", tags=["users"])


@router.post(
    "/auth/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
)
def register(
    payload: RegisterRequest,
    background_tasks: BackgroundTasks,
    service: UserService = Depends(get_user_service),
) -> RegisterResponse:
    return service.register(payload, background_tasks)


@router.post(
    "/auth/email-verification/request",
    response_model=MessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def request_email_verification(
    payload: EmailVerificationRequest,
    background_tasks: BackgroundTasks,
    service: UserService = Depends(get_user_service),
) -> MessageResponse:
    return service.request_email_verification(payload, background_tasks)


@router.post("/auth/email-verification/confirm", response_model=TokenResponse)
def confirm_email_verification(
    payload: EmailVerificationConfirm,
    service: UserService = Depends(get_user_service),
) -> TokenResponse:
    return service.confirm_email_verification(payload)


@router.post("/auth/login", response_model=TokenResponse)
def login(
    payload: LoginRequest,
    service: UserService = Depends(get_user_service),
) -> TokenResponse:
    return service.login(payload)


@router.post(
    "/auth/password-reset/request",
    response_model=MessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def request_password_reset(
    payload: PasswordResetRequest,
    background_tasks: BackgroundTasks,
    service: UserService = Depends(get_user_service),
) -> MessageResponse:
    return service.request_password_reset(payload, background_tasks)


@router.post("/auth/password-reset/confirm", response_model=MessageResponse)
def confirm_password_reset(
    payload: PasswordResetConfirm,
    service: UserService = Depends(get_user_service),
) -> MessageResponse:
    return service.confirm_password_reset(payload)


@router.get(
    "/suggestions",
    response_model=list[SuggestedUser],
    summary="Listar otros deportistas registrados",
    description="Cards públicas de deportistas activos y verificados (roles player y usuario). "
    "Excluye la cuenta que consulta, administradores y representantes de clubes.",
)
def list_suggestions(
    limit: int = Query(default=20, ge=1, le=50),
    current_user: StoredUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> list[SuggestedUser]:
    return service.list_suggestions(current_user, limit)


@router.get("/athletes/{user_id}", response_model=SuggestedUser)
def athlete_card(
    user_id: UUID,
    current_user: StoredUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> SuggestedUser:
    cards = service.athlete_cards(current_user, [user_id])
    if not cards:
        raise HTTPException(status_code=404, detail="Deportista no disponible.")
    return cards[0]


@router.post("/athletes/cards", response_model=list[SuggestedUser])
def athlete_cards(
    user_ids: Annotated[list[UUID], Body(max_length=100)],
    current_user: StoredUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> list[SuggestedUser]:
    return service.athlete_cards(current_user, user_ids)


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
