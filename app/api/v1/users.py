from __future__ import annotations

from uuid import UUID
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query, Response, status

from app.api.dependencies import get_current_user, get_user_service
from app.repositories.base import StoredUser
from app.schemas.user import (
    BlockCheck,
    BlockCreate,
    BlockedUser,
    ChatPreferences,
    ChatPreferencesUpdate,
    ConsentCreate,
    ConsentRead,
    DataExport,
    EmailVerificationConfirm,
    EmailVerificationRequest,
    LocationStatus,
    LocationUpdate,
    LoginRequest,
    MessageResponse,
    PasswordResetConfirm,
    PasswordResetRequest,
    PreferencesReplace,
    ProfileRead,
    ProfileReplace,
    RegisterRequest,
    RegisterResponse,
    ReportCreate,
    ReportRead,
    ReportReview,
    RoleRead,
    SuggestedUser,
    SuggestionFilters,
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
    filters: Annotated[SuggestionFilters, Query()],
    current_user: StoredUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> list[SuggestedUser]:
    return service.list_suggestions(current_user, filters.limit, filters)


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


@router.get("/me/blocks", response_model=list[BlockedUser])
def list_blocked(
    current_user: StoredUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> list[BlockedUser]:
    return service.list_blocked(current_user)


@router.put("/me/blocks/{user_id}", status_code=status.HTTP_204_NO_CONTENT,
            summary="Bloquear: ambos dejan de verse en Descubrir")
def block_user(
    user_id: UUID,
    current_user: StoredUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> Response:
    service.block(current_user, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/me/blocks/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def unblock_user(
    user_id: UUID,
    current_user: StoredUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> Response:
    service.unblock(current_user, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/block", response_model=BlockedUser, status_code=status.HTTP_201_CREATED,
             summary="Bloquear a una persona: ninguna puede escribirle ni verle a la otra")
def block_by_body(
    payload: BlockCreate,
    response: Response,
    current_user: StoredUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> BlockedUser:
    """201 when the block is new, 200 when it already existed."""
    if not service.block(current_user, payload.blocked_user_id):
        response.status_code = status.HTTP_200_OK
    return service.blocked_user(current_user, payload.blocked_user_id)


@router.delete("/block/{blocked_user_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Desbloquear (idempotente)")
def unblock_by_path(
    blocked_user_id: UUID,
    current_user: StoredUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> Response:
    service.unblock(current_user, blocked_user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/blocks/me", response_model=list[BlockedUser], summary="Personas que he bloqueado")
def my_blocks(
    current_user: StoredUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> list[BlockedUser]:
    return service.list_blocked(current_user)


@router.get("/blocks/check", response_model=BlockCheck,
            summary="¿Hay un bloqueo entre dos personas? (para otros servicios)",
            description="Solo lo puede consultar una de las dos personas (con su token) o un administrador. "
            "Responde si existe un bloqueo, nunca quién lo puso.")
def check_blocks(
    user_a: Annotated[UUID, Query(alias="userA")],
    user_b: Annotated[UUID, Query(alias="userB")],
    current_user: StoredUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> BlockCheck:
    return service.check_block(current_user, user_a, user_b)


@router.get("/chat-preferences", response_model=ChatPreferences, summary="Personas silenciadas")
def get_chat_preferences(
    current_user: StoredUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> ChatPreferences:
    return service.chat_preferences(current_user)


@router.patch("/chat-preferences", response_model=ChatPreferences,
              summary="Silenciar o dejar de silenciar personas",
              description="`muteUserIds` y `unmuteUserIds` (hasta 100 cada una, sin repetir ni cruzarse). "
              "Silenciar no impide que te escriban: solo apaga sus notificaciones de chat.")
def update_chat_preferences(
    payload: ChatPreferencesUpdate,
    current_user: StoredUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> ChatPreferences:
    return service.update_chat_preferences(current_user, payload)


@router.get("/location", response_model=LocationStatus, summary="¿Hay una ubicación guardada?",
            description="Solo indica si existe y los umbrales vigentes; nunca devuelve coordenadas.")
def get_location(
    current_user: StoredUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> LocationStatus:
    return service.get_location(current_user)


@router.put("/location", response_model=LocationStatus, summary="Actualizar mi ubicación actual",
            description="La app la envía al recorrer `min_distance_meters` desde la última enviada (se consulta con GET). "
            "Si el movimiento es menor, la posición guardada no cambia y `updated` es false. Se usa solo para recomendar "
            "personas cercanas; no se comparte ni se devuelve.")
def update_location(
    payload: LocationUpdate,
    current_user: StoredUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> LocationStatus:
    return service.update_location(current_user, payload)


@router.delete("/location", status_code=status.HTTP_204_NO_CONTENT, summary="Borrar mi ubicación actual")
def delete_location(
    current_user: StoredUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> Response:
    service.delete_location(current_user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/reports", response_model=ReportRead, status_code=status.HTTP_201_CREATED)
def create_report(
    payload: ReportCreate,
    current_user: StoredUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> ReportRead:
    return service.report(current_user, payload)


@router.get("/admin/reports", response_model=list[ReportRead], summary="Reportes para moderar (solo admin)")
def list_reports(
    estado: Literal["pendiente", "revisado", "descartado"] | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    current_user: StoredUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> list[ReportRead]:
    return service.list_reports(current_user, estado, limit)


@router.patch("/admin/reports/{report_id}", response_model=ReportRead)
def review_report(
    report_id: UUID,
    payload: ReportReview,
    current_user: StoredUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> ReportRead:
    return service.review_report(current_user, report_id, payload.estado)


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
