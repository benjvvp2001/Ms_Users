from __future__ import annotations

from uuid import UUID
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials
from starlette.datastructures import UploadFile
from starlette.concurrency import run_in_threadpool

from app.api.dependencies import (bearer_scheme, get_audit_service, get_current_user, get_repository,
                                  get_sanction_service, get_user_service)
from app.api.internal import require_internal_service
from app.repositories.base import StoredUser, UserRepository
from app.schemas.audit import AuditPage, AuditQuery
from app.schemas.sanction import SanctionCreate, SanctionLift, SanctionListQuery, SanctionRead
from app.schemas.user import (
    BLOCK_LIST_MAX,
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
    ReportListQuery,
    ReportReview,
    RoleRead,
    SuggestedUser,
    SuggestionFilters,
    TokenResponse,
)
from app.services.audit_service import AuditService
from app.services.sanction_service import SanctionService
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
    "/auth/register-club",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Registrar la cuenta de un representante de club",
    description="Igual que /auth/register, pero la cuenta queda con el rol club_admin. "
    "El club mismo se crea después en ms-club (POST /api/v1/clubs), ya con sesión.",
)
def register_club(
    payload: RegisterRequest,
    background_tasks: BackgroundTasks,
    service: UserService = Depends(get_user_service),
) -> RegisterResponse:
    return service.register(payload, background_tasks, role="club_admin")


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
    limit: Annotated[int, Query(ge=1, le=BLOCK_LIST_MAX)] = BLOCK_LIST_MAX,
    offset: Annotated[int, Query(ge=0)] = 0,
    current_user: StoredUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> list[BlockedUser]:
    return service.list_blocked(current_user, limit, offset)


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


@router.get("/blocks/me", response_model=list[BlockedUser], summary="Personas que he bloqueado",
            description="HU-66. Solo la lista propia, la más reciente primero, de a `limit` (máximo 100) desde `offset`.")
def my_blocks(
    limit: Annotated[int, Query(ge=1, le=BLOCK_LIST_MAX)] = BLOCK_LIST_MAX,
    offset: Annotated[int, Query(ge=0)] = 0,
    current_user: StoredUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> list[BlockedUser]:
    return service.list_blocked(current_user, limit, offset)


@router.get("/blocks/check", response_model=BlockCheck,
            summary="¿Hay un bloqueo entre dos personas? (para otros servicios)",
            description="Solo un administrador (con su token) o otro servicio (con X-Internal-Service-Token). "
            "Ni siquiera las dos personas involucradas: la bloqueada se enteraría. "
            "Responde si existe un bloqueo, nunca quién lo puso.")
def check_blocks(
    user_a: Annotated[UUID, Query(alias="userA")],
    user_b: Annotated[UUID, Query(alias="userB")],
    x_internal_service_token: Annotated[str | None, Header(max_length=256)] = None,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    repository: UserRepository = Depends(get_repository),
    service: UserService = Depends(get_user_service),
) -> BlockCheck:
    if x_internal_service_token is not None:
        require_internal_service(x_internal_service_token)
        return service.check_block(None, user_a, user_b)
    return service.check_block(get_current_user(credentials, repository), user_a, user_b)


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


@router.get(
    "/admin/reports", response_model=list[ReportRead], summary="Reportes para moderar (solo admin)",
    description="Más recientes primero. estado: pendiente (por defecto), revisado, descartado, escalado o todos. "
    "Paginado con pagina (desde 1) y limit (máximo 100).",
)
def list_reports(
    query: Annotated[ReportListQuery, Query()],
    current_user: StoredUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> list[ReportRead]:
    estado = None if query.estado == "todos" else query.estado
    return service.list_reports(current_user, estado, query.limit, query.pagina)


@router.patch("/admin/reports/{report_id}", response_model=ReportRead)
def review_report(
    report_id: UUID,
    payload: ReportReview,
    current_user: StoredUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> ReportRead:
    return service.review_report(current_user, report_id, payload.estado, payload.motivo)


@router.post(
    "/admin/sanctions", response_model=SanctionRead, status_code=status.HTTP_201_CREATED,
    summary="Suspender o sancionar una cuenta (solo admin)",
    description="tipo temporal (dias de 1 a 365) o permanente. reporte_id es opcional; si se envía debe ser un "
    "reporte escalado de esa misma cuenta. Avisa a la persona (motivo y duración) y le restringe el acceso al instante.",
)
def apply_sanction(
    payload: SanctionCreate,
    current_user: StoredUser = Depends(get_current_user),
    service: SanctionService = Depends(get_sanction_service),
) -> SanctionRead:
    return service.apply(current_user, payload)


@router.get("/admin/sanctions", response_model=list[SanctionRead], summary="Sanciones (solo admin)",
            description="Más recientes primero. Filtros: estado (activa, levantada, vencida), usuario_id; "
            "pagina (desde 1) y limit (máximo 100).")
def list_sanctions(
    query: Annotated[SanctionListQuery, Query()],
    current_user: StoredUser = Depends(get_current_user),
    service: SanctionService = Depends(get_sanction_service),
) -> list[SanctionRead]:
    return service.list(current_user, query)


@router.get("/admin/sanctions/{sanction_id}", response_model=SanctionRead, summary="Una sanción (solo admin)")
def get_sanction(
    sanction_id: UUID,
    current_user: StoredUser = Depends(get_current_user),
    service: SanctionService = Depends(get_sanction_service),
) -> SanctionRead:
    return service.get(current_user, sanction_id)


@router.post("/admin/sanctions/{sanction_id}/lift", response_model=SanctionRead,
             summary="Levantar una sanción (solo admin)",
             description="Idempotente: levantar una ya levantada o ya vencida devuelve la sanción sin cambios.")
def lift_sanction(
    sanction_id: UUID,
    payload: SanctionLift,
    current_user: StoredUser = Depends(get_current_user),
    service: SanctionService = Depends(get_sanction_service),
) -> SanctionRead:
    return service.lift(current_user, sanction_id, payload)


@router.get(
    "/admin/audit-events", response_model=AuditPage,
    summary="Historial de acciones administrativas (solo admin, solo lectura)",
    description="Más reciente primero. Filtros: desde, hasta, admin_id, accion, recurso_tipo, recurso_id, "
    "resultado. Paginado: pagina y por_pagina (máximo 100). No hay endpoints para editar ni borrar.",
)
def list_audit_events(
    query: Annotated[AuditQuery, Query()],
    current_user: StoredUser = Depends(get_current_user),
    service: AuditService = Depends(get_audit_service),
) -> AuditPage:
    return service.list_events(current_user, query)


# HU-61. Declared before /{user_id}/... so "photos" is never taken for a user id.
PHOTO_FIELD = "foto"
MULTIPART_OVERHEAD = 64 * 1024  # boundaries and headers around the file


@router.get(
    "/photos/{key}", include_in_schema=True,
    summary="Entregar una foto de perfil",
    description="Pública: la clave es un valor aleatorio de 128 bits que solo reciben quienes pueden ver ese perfil "
    "(foto_perfil), y cambia cada vez que la persona cambia su foto. Solo JPG o PNG, con nosniff.",
)
def get_photo(key: str, service: UserService = Depends(get_user_service)) -> Response:
    found = service.read_photo(key)
    if found is None:
        raise HTTPException(status_code=404, detail="Foto no encontrada.")
    data, content_type = found
    return Response(content=data, media_type=content_type, headers={
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Content-Disposition": "inline",
        # The key changes with every new photo, so a stored copy is never stale; it is private to the person's browser.
        "Cache-Control": "private, max-age=86400, immutable",
    })


@router.put(
    "/{user_id}/profile/photo", response_model=ProfileRead,
    summary="Subir o reemplazar mi foto de perfil",
    description="multipart/form-data con el campo `foto` (JPG o PNG, hasta PHOTO_MAX_BYTES). Solo la propia cuenta. "
    "El contenido se verifica por su firma, se reprocesa (sin EXIF ni ubicación) y se reduce a PHOTO_MAX_SIDE. "
    "400 vacía, dañada o nombre engañoso; 413 demasiado grande; 415 no es JPG ni PNG.",
)
async def upload_photo(
    user_id: UUID,
    request: Request,
    current_user: StoredUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> ProfileRead:
    if current_user.id != user_id:  # before reading a single byte of the body
        raise HTTPException(status_code=403, detail="not authorized to access this resource")
    limit = service.photo_limits().max_bytes
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit + MULTIPART_OVERHEAD:
        raise HTTPException(status_code=413, detail="La imagen supera el tamaño máximo permitido.")
    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        raise HTTPException(status_code=415, detail="Formato no permitido. Usa una imagen JPG o PNG.")
    try:
        form = await request.form(max_files=1, max_fields=2)
    except HTTPException:
        raise
    except Exception as error:  # malformed multipart
        raise HTTPException(status_code=400, detail="La solicitud no es válida.") from error
    try:
        upload = form.get(PHOTO_FIELD)
        if not isinstance(upload, UploadFile):
            raise HTTPException(status_code=400, detail="Falta la imagen.")
        if upload.size is not None and upload.size > limit:
            raise HTTPException(status_code=413, detail="La imagen supera el tamaño máximo permitido.")
        data = await upload.read(limit + 1)
        return await run_in_threadpool(
            service.replace_photo, current_user, user_id,
            filename=upload.filename, content_type=upload.content_type, data=data,
        )
    finally:
        await form.close()


@router.delete(
    "/{user_id}/profile/photo", response_model=ProfileRead,
    summary="Quitar mi foto de perfil",
    description="Solo la propia cuenta. Idempotente. Borra el archivo; el perfil queda sin foto (foto_perfil null).",
)
def delete_photo(
    user_id: UUID,
    current_user: StoredUser = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
) -> ProfileRead:
    return service.remove_photo(current_user, user_id)


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
