from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from fastapi import BackgroundTasks, HTTPException, status

from app.core.config import get_email_settings, get_settings
from app.core.config import EmailSettings, LocationSettings, get_location_settings
from app.core.security import (
    create_access_token,
    generate_one_time_code,
    hash_email_verification_code,
    hash_password,
    hash_password_reset_code,
    verify_email_verification_code,
    verify_password,
    verify_password_reset_code,
)
from app.repositories.base import (
    ATHLETE_ROLES,
    LocationRecord,
    NewEmailVerification,
    ReportRecord,
    StoredUser,
    SuggestionCandidate,
    UserRepository,
)
from app.schemas.user import (
    AuthenticatedUser,
    BlockCheck,
    BlockedUser,
    ChatPreferences,
    ChatPreferencesUpdate,
    MutedUser,
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
    PublicAthlete,
    ReportCreate,
    ReportRead,
    RegisterRequest,
    RegisterResponse,
    RoleRead,
    SuggestedUser,
    SuggestionFilters,
    TokenResponse,
    UserSport,
)
from app.core.recommendations import compatibility, distance_km, similar_level
from app.services.email_sender import (
    EmailSender,
    deliver_safely,
    email_verification_message,
    password_reset_message,
)

# Same answer whether or not the email exists, so the endpoint cannot be used
# to discover which addresses are registered.
PASSWORD_RESET_REQUESTED = (
    "Si el correo está registrado, recibirás un código para restablecer tu contraseña."
)
INVALID_RESET_CODE = "invalid or expired reset code"
REGISTERED_PENDING_VERIFICATION = (
    "Cuenta creada. Te enviamos un código a tu correo para activarla."
)
EMAIL_VERIFICATION_REQUESTED = (
    "Si la cuenta está pendiente de verificación, recibirás un nuevo código en tu correo."
)
INVALID_VERIFICATION_CODE = "invalid or expired verification code"
EMAIL_NOT_VERIFIED = "email not verified"


# Rol de las cuentas de club (se elige "club" al registrarse); el resto usa "player".
CLUB_ROLE = "club_admin"

class UserService:
    """Business logic for the users domain: registration, profile, preferences,
    roles, consents, exports and account deletion.

    Sits between the HTTP controllers (app.api) and the data-access layer
    (app.repositories): it enforces owner-or-admin authorization and records
    audit events, so routes stay thin HTTP adapters.
    """

    def __init__(
        self, repository: UserRepository, email_sender: EmailSender | None = None,
        location: LocationSettings | None = None,
    ) -> None:
        self._repository = repository
        self._email_sender = email_sender
        self._location = location or get_location_settings()

    # -- auth -----------------------------------------------------------

    def register(
        self,
        payload: RegisterRequest,
        background_tasks: BackgroundTasks,
        role: str | None = None,
    ) -> RegisterResponse:
        """Create the account inactive and email it a code; it gets a token
        only after confirm_email_verification. Club sign-up passes club_admin."""
        if role is None:
            role = CLUB_ROLE if payload.tipo == "club" else "player"
        sender = self._require_email_sender()
        settings = get_email_settings()
        code = generate_one_time_code()
        try:
            user = self._repository.create_user(
                email=str(payload.email),
                password_hash=hash_password(payload.password),
                rut=payload.rut,
                profile=ProfileReplace(
                    nombre=payload.nombre,
                    apellido_paterno=payload.apellido_paterno,
                    apellido_materno=payload.apellido_materno,
                    fecha_nacimiento=None,
                    telefono=None,
                    foto_perfil=None,
                    biografia=None,
                ),
                preferences=PreferencesReplace(
                    deportes=[],
                    disponibilidad=[],
                    rango_distancia_km=None,
                    rango_edad_min=None,
                    rango_edad_max=None,
                    mismo_nivel=None,
                    disponibilidad_match=True,
                ),
                email_verification=NewEmailVerification(
                    code_hash=hash_email_verification_code(code),
                    expires_at=self._verification_expiry(settings),
                ),
                role=role,
            )
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(error),
            ) from error

        self._repository.record_audit(
            user_id=user.id,
            action="register_club" if role == CLUB_ROLE else "register",
            resource="users",
            result="success",
        )
        self._send_verification_code(sender, user, code, settings, background_tasks)
        return RegisterResponse(
            detail=REGISTERED_PENDING_VERIFICATION,
            user=self._as_authenticated_user(user),
        )

    def login(self, payload: LoginRequest) -> TokenResponse:
        user = self._repository.get_user_by_email(str(payload.email))
        if user is None or not verify_password(payload.password, user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid email or password",
            )
        # Checked after the password, so it reveals nothing to someone who
        # doesn't already know the credentials.
        if not user.email_verified:
            self._repository.record_audit(
                user_id=user.id,
                action="login",
                resource="auth",
                result="failure",
            )
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=EMAIL_NOT_VERIFIED)

        self._repository.record_audit(
            user_id=user.id,
            action="login",
            resource="auth",
            result="success",
        )
        return self._issue_token(user)

    # -- email verification -----------------------------------------------

    def request_email_verification(
        self, payload: EmailVerificationRequest, background_tasks: BackgroundTasks
    ) -> MessageResponse:
        """Send a fresh code to an unverified account. Same answer for unknown,
        already verified or throttled addresses, so nothing is revealed."""
        sender = self._require_email_sender()
        settings = get_email_settings()
        user = self._repository.get_user_by_email(str(payload.email))
        pending = None if user is None else self._repository.get_pending_email_verification(user.id)
        if user is None or pending is None:
            return MessageResponse(detail=EMAIL_VERIFICATION_REQUESTED)

        if datetime.now(timezone.utc) - pending.sent_at < timedelta(
            seconds=settings.email_verification_resend_seconds
        ):
            return MessageResponse(detail=EMAIL_VERIFICATION_REQUESTED)

        code = generate_one_time_code()
        if not self._repository.restart_email_verification(
            user_id=user.id,
            code_hash=hash_email_verification_code(code),
            expires_at=self._verification_expiry(settings),
        ):
            return MessageResponse(detail=EMAIL_VERIFICATION_REQUESTED)

        self._repository.record_audit(
            user_id=user.id,
            action="request_email_verification",
            resource="auth/email-verification",
            result="success",
        )
        self._send_verification_code(sender, user, code, settings, background_tasks)
        return MessageResponse(detail=EMAIL_VERIFICATION_REQUESTED)

    def confirm_email_verification(self, payload: EmailVerificationConfirm) -> TokenResponse:
        """Activate the account and log the user in with the same response as login."""
        settings = get_email_settings()
        user = self._repository.get_user_by_email(str(payload.email))
        pending = None if user is None else self._repository.get_pending_email_verification(user.id)
        if user is None or pending is None:
            raise self._invalid_verification_code()

        # Count the attempt before checking the code so parallel guesses
        # cannot exceed the limit; expired codes fail here too.
        if not self._repository.register_email_verification_attempt(
            user.id, settings.email_verification_max_attempts
        ) or not (
            settings.accept_any_verification_code
            or verify_email_verification_code(payload.code, pending.code_hash)
        ):
            self._repository.record_audit(
                user_id=user.id,
                action="confirm_email_verification",
                resource="auth/email-verification",
                result="failure",
            )
            raise self._invalid_verification_code()

        if not self._repository.complete_email_verification(
            user_id=user.id, code_hash=pending.code_hash
        ):
            raise self._invalid_verification_code()

        self._repository.record_audit(
            user_id=user.id,
            action="confirm_email_verification",
            resource="auth/email-verification",
            result="success",
        )
        return self._issue_token(user)

    # -- password recovery ------------------------------------------------

    def request_password_reset(
        self, payload: PasswordResetRequest, background_tasks: BackgroundTasks
    ) -> MessageResponse:
        """Email a one-time code to the address the user typed, if it belongs
        to an active account. The mail is sent after the response so neither
        the answer nor its timing reveals whether the account exists."""
        sender = self._require_email_sender()
        settings = get_email_settings()
        user = self._repository.get_user_by_email(str(payload.email))
        if user is None:
            return MessageResponse(detail=PASSWORD_RESET_REQUESTED)

        now = datetime.now(timezone.utc)
        latest = self._repository.get_active_password_reset(user.id)
        if latest is not None and now - latest.created_at < timedelta(
            seconds=settings.password_reset_resend_seconds
        ):
            # Throttle: the previous code is still fresh, don't flood the inbox.
            return MessageResponse(detail=PASSWORD_RESET_REQUESTED)

        code = generate_one_time_code()
        self._repository.create_password_reset(
            user_id=user.id,
            code_hash=hash_password_reset_code(code),
            expires_at=now + timedelta(minutes=settings.password_reset_code_minutes),
        )
        self._repository.record_audit(
            user_id=user.id,
            action="request_password_reset",
            resource="auth/password-reset",
            result="success",
        )

        subject, text, html = password_reset_message(
            nombre=user.profile.nombre,
            code=code,
            minutes=settings.password_reset_code_minutes,
        )
        background_tasks.add_task(
            deliver_safely, sender, to=user.email, subject=subject, text=text, html=html
        )
        return MessageResponse(detail=PASSWORD_RESET_REQUESTED)

    def confirm_password_reset(self, payload: PasswordResetConfirm) -> MessageResponse:
        settings = get_email_settings()
        user = self._repository.get_user_by_email(str(payload.email))
        reset = None if user is None else self._repository.get_active_password_reset(user.id)
        if user is None or reset is None:
            raise self._invalid_reset_code()

        # Count the attempt before checking the code so parallel guesses
        # cannot exceed the limit.
        if not self._repository.register_password_reset_attempt(
            reset.id, settings.password_reset_max_attempts
        ) or not verify_password_reset_code(payload.code, reset.code_hash):
            self._repository.record_audit(
                user_id=user.id,
                action="confirm_password_reset",
                resource="auth/password-reset",
                result="failure",
            )
            raise self._invalid_reset_code()

        if not self._repository.complete_password_reset(
            reset_id=reset.id, user_id=user.id, password_hash=hash_password(payload.new_password)
        ):
            raise self._invalid_reset_code()

        self._repository.record_audit(
            user_id=user.id,
            action="confirm_password_reset",
            resource="auth/password-reset",
            result="success",
        )
        return MessageResponse(detail="Contraseña actualizada. Ya puedes iniciar sesión.")

    # -- profile ----------------------------------------------------------

    def get_profile(self, current_user: StoredUser, user_id: UUID) -> ProfileRead:
        self._authorize(current_user, user_id)
        user = self._get_user_or_404(user_id)

        self._repository.record_audit(
            user_id=current_user.id,
            action="read_profile",
            resource=f"users/{user_id}/profile",
            result="success",
        )
        return self._as_profile(user)

    def replace_profile(
        self, current_user: StoredUser, user_id: UUID, payload: ProfileReplace
    ) -> ProfileRead:
        self._authorize(current_user, user_id)
        self._get_user_or_404(user_id)

        user = self._repository.replace_profile(user_id, payload)
        self._repository.record_audit(
            user_id=current_user.id,
            action="replace_profile",
            resource=f"users/{user_id}/profile",
            result="success",
        )
        return self._as_profile(user)

    # -- suggestions ------------------------------------------------------

    def athlete_cards(self, current_user: StoredUser, user_ids: list[UUID]) -> list[SuggestedUser]:
        """Public directory for matching, including profiles outside the suggestions window.

        A person with a block against the caller (either way) is left out, exactly like a deactivated
        account. Matching and Activities ask for this card before any request, message or listing, so
        this is what stops two people who blocked each other from reaching each other."""
        if current_user.role not in ATHLETE_ROLES or not current_user.email_verified:
            raise HTTPException(status_code=403, detail="Solo deportistas verificados pueden hacer match.")
        cards = []
        ids = list(dict.fromkeys(user_ids))
        blocked = self._repository.blocked_with(current_user.id, ids)
        for user_id in ids:
            if user_id in blocked:
                continue
            user = self._repository.get_user(user_id)
            if user is None or user.role not in ATHLETE_ROLES or not user.email_verified:
                continue
            cards.append(self._as_suggestion(SuggestionCandidate(
                id=user.id, nombre=user.profile.nombre,
                apellido_paterno=user.profile.apellido_paterno,
                fecha_nacimiento=user.profile.fecha_nacimiento,
                foto_perfil=user.profile.foto_perfil, biografia=user.profile.biografia,
                deportes=user.preferences.deportes,
            ), current_user.preferences.deportes))
        return cards

    def list_suggestions(self, current_user: StoredUser, limit: int, filters: SuggestionFilters | None = None) -> list[SuggestedUser]:
        filters = filters or SuggestionFilters(limit=limit)
        fresh_since = self._fresh_since()
        # Where the person is now if the app reported it recently; otherwise the fixed zone of the profile.
        live = self._repository.get_location(current_user.id)
        origin = live if live is not None and live.actualizada_en >= fresh_since else current_user.preferences.zona
        if filters.radius_km is not None and (origin is None or origin.latitud is None):
            raise HTTPException(422, "Activa tu ubicación en el perfil para filtrar por distancia.")
        candidates = self._repository.list_suggestion_candidates(
            exclude_user_id=current_user.id, limit=limit, filters=filters,
            origin=origin, sports=current_user.preferences.deportes, fresh_since=fresh_since,
        )
        ranked = [self._as_suggestion(candidate, current_user.preferences.deportes) for candidate in candidates]
        self._repository.record_audit(
            user_id=current_user.id,
            action="list_suggestions",
            resource="users/suggestions",
            result="success",
        )
        return ranked[:limit]

    # -- live location --------------------------------------------------------

    def _fresh_since(self) -> datetime:
        return datetime.now(timezone.utc) - timedelta(minutes=self._location.max_age_minutes)

    def _location_status(self, record: LocationRecord | None, updated: bool | None = None) -> LocationStatus:
        return LocationStatus(
            has_location=record is not None, updated=updated,
            updated_at=record.actualizada_en if record else None,
            min_distance_meters=self._location.min_move_meters, max_age_minutes=self._location.max_age_minutes,
        )

    @staticmethod
    def _require_athlete(user: StoredUser) -> None:
        if user.role not in ATHLETE_ROLES:
            raise HTTPException(403, "Solo los deportistas comparten su ubicación.")

    def get_location(self, user: StoredUser) -> LocationStatus:
        """Whether a position is saved and the thresholds; the coordinates are never returned."""
        self._require_athlete(user)
        return self._location_status(self._repository.get_location(user.id))

    def update_location(self, user: StoredUser, payload: LocationUpdate) -> LocationStatus:
        """Keeps one position per person. A move shorter than the minimum distance leaves it as it was
        (it only renews its date now and then, so a person who stays still does not expire)."""
        self._require_athlete(user)
        current = self._repository.get_location(user.id)
        moved_meters = None if current is None else 1000 * distance_km(current, payload)
        if current is None or moved_meters >= self._location.min_move_meters:
            self._repository.save_location(user.id, payload.latitud, payload.longitud, payload.precision_m)
            updated = True
        else:
            updated = False
            if datetime.now(timezone.utc) - current.actualizada_en >= TOUCH_AFTER:
                self._repository.touch_location(user.id)
        return self._location_status(self._repository.get_location(user.id), updated)

    def delete_location(self, user: StoredUser) -> None:
        self._require_athlete(user)
        self._repository.delete_location(user.id)

    # -- blocks and reports ---------------------------------------------------

    def block(self, current_user: StoredUser, user_id: UUID) -> bool:
        """Cuts contact both ways: they vanish from each other in Discover and neither one's athlete
        card is served to the other, so neither can send requests or messages. Matches, messages and
        activities are kept and come back when the block is lifted. True when the block is new."""
        if user_id == current_user.id:
            raise HTTPException(422, "No puedes bloquearte a ti mismo.")
        self._get_user_or_404(user_id)
        created = self._repository.block_user(current_user.id, user_id)
        self._repository.record_audit(user_id=current_user.id, action="block_user",
                                      resource=f"users/{user_id}", result="success")
        return created

    def blocked_user(self, current_user: StoredUser, user_id: UUID) -> BlockedUser:
        """The person as the block list shows them."""
        blocked_at = dict(self._repository.list_blocked(current_user.id))[user_id]
        return BlockedUser(**self._as_public(self._get_user_or_404(user_id)).model_dump(), fecha_bloqueo=blocked_at)

    def check_block(self, current_user: StoredUser, user_a: UUID, user_b: UUID) -> BlockCheck:
        """For other services to ask before letting two people interact. It only answers whether a block
        exists, never who placed it, and only to one of the two people (or an admin): anyone else could
        otherwise map who blocked whom."""
        if current_user.role != "admin" and current_user.id not in (user_a, user_b):
            raise HTTPException(403, "Solo puedes consultar bloqueos en los que participas.")
        return BlockCheck(blocked=user_a != user_b and self._repository.blocked_between(user_a, user_b))

    def unblock(self, current_user: StoredUser, user_id: UUID) -> None:
        self._repository.unblock_user(current_user.id, user_id)
        self._repository.record_audit(user_id=current_user.id, action="unblock_user",
                                      resource=f"users/{user_id}", result="success")

    def list_blocked(self, current_user: StoredUser) -> list[BlockedUser]:
        blocked = []
        for user_id, blocked_at in self._repository.list_blocked(current_user.id):
            user = self._repository.get_user(user_id)
            if user is not None:
                blocked.append(BlockedUser(**self._as_public(user).model_dump(), fecha_bloqueo=blocked_at))
        return blocked

    # -- muted people (chat preferences) -------------------------------------------

    def chat_preferences(self, current_user: StoredUser) -> ChatPreferences:
        muted = []
        for user_id, muted_at in self._repository.list_muted(current_user.id):
            user = self._repository.get_user(user_id)
            if user is not None:
                muted.append(MutedUser(**self._as_public(user).model_dump(), fecha_silencio=muted_at))
        return ChatPreferences(muted_users=muted)

    def update_chat_preferences(self, current_user: StoredUser, payload: ChatPreferencesUpdate) -> ChatPreferences:
        """Mute and unmute together. Everyone to mute is checked first, so a wrong id changes nothing."""
        if current_user.id in payload.mute_user_ids:
            raise HTTPException(422, "No puedes silenciarte a ti mismo.")
        for user_id in payload.mute_user_ids:
            self._get_user_or_404(user_id)
        self._repository.change_mutes(current_user.id, payload.mute_user_ids, payload.unmute_user_ids)
        self._repository.record_audit(user_id=current_user.id, action="update_chat_preferences",
                                      resource="users/me/chat-preferences", result="success")
        return self.chat_preferences(current_user)

    def report(self, current_user: StoredUser, payload: ReportCreate) -> ReportRead:
        if payload.tipo == "usuario" and payload.objetivo_id == str(current_user.id):
            raise HTTPException(422, "No puedes reportarte a ti mismo.")
        record = self._repository.create_report(current_user.id, payload)
        if record is None:
            raise HTTPException(409, "Ya enviaste este reporte. Lo estamos revisando.")
        return self._as_report(record)

    def list_reports(self, current_user: StoredUser, estado: str | None, limit: int) -> list[ReportRead]:
        self._require_admin(current_user)
        return [self._as_report(record) for record in self._repository.list_reports(estado, limit)]

    def review_report(self, current_user: StoredUser, report_id: UUID, estado: str) -> ReportRead:
        self._require_admin(current_user)
        record = self._repository.review_report(report_id, estado, current_user.id)
        if record is None:
            raise HTTPException(404, "Reporte no encontrado.")
        self._repository.record_audit(user_id=current_user.id, action=f"report_{estado}",
                                      resource=f"reports/{report_id}", result="success")
        return self._as_report(record)

    def _require_admin(self, current_user: StoredUser) -> None:
        if current_user.role != "admin":
            raise HTTPException(403, "Solo un administrador puede revisar reportes.")

    def _as_public(self, user: StoredUser) -> PublicAthlete:
        return PublicAthlete(user_id=user.id, nombre=user.profile.nombre,
                             apellido_inicial=f"{user.profile.apellido_paterno[:1].upper()}.",
                             foto_perfil=user.profile.foto_perfil)

    def _as_report(self, record: ReportRecord) -> ReportRead:
        reporter = None if record.reportante_id is None else self._repository.get_user(record.reportante_id)
        return ReportRead(
            id=record.id, tipo=record.tipo, objetivo_id=record.objetivo_id,
            objetivo_nombre=record.objetivo_nombre, motivo=record.motivo, detalle=record.detalle,
            reportante=None if reporter is None else self._as_public(reporter),
            estado=record.estado, fecha_creacion=record.fecha_creacion, fecha_revision=record.fecha_revision,
        )

    # -- roles --------------------------------------------------------------

    def get_roles(self, current_user: StoredUser, user_id: UUID) -> RoleRead:
        self._authorize(current_user, user_id)
        user = self._get_user_or_404(user_id)
        return RoleRead(role=user.role)

    # -- preferences ----------------------------------------------------

    def get_preferences(self, current_user: StoredUser, user_id: UUID) -> PreferencesReplace:
        self._authorize(current_user, user_id)
        user = self._get_user_or_404(user_id)
        return user.preferences

    def replace_preferences(
        self, current_user: StoredUser, user_id: UUID, payload: PreferencesReplace
    ) -> PreferencesReplace:
        self._authorize(current_user, user_id)
        self._get_user_or_404(user_id)

        user = self._repository.replace_preferences(user_id, payload)
        self._repository.record_audit(
            user_id=current_user.id,
            action="replace_preferences",
            resource=f"users/{user_id}/preferences",
            result="success",
        )
        return user.preferences

    # -- consents -------------------------------------------------------

    def list_consents(self, current_user: StoredUser, user_id: UUID) -> list[ConsentRead]:
        self._authorize(current_user, user_id)
        self._get_user_or_404(user_id)
        return self._repository.list_consents(user_id)

    def grant_consent(
        self, current_user: StoredUser, user_id: UUID, payload: ConsentCreate
    ) -> ConsentRead:
        self._authorize(current_user, user_id)
        self._get_user_or_404(user_id)

        consent = self._repository.add_consent(user_id, payload)
        self._repository.record_audit(
            user_id=current_user.id,
            action="grant_consent",
            resource=f"users/{user_id}/consents/{consent.id}",
            result="success",
        )
        return consent

    def revoke_consent(
        self, current_user: StoredUser, user_id: UUID, consent_id: UUID
    ) -> ConsentRead:
        self._authorize(current_user, user_id)
        self._get_user_or_404(user_id)

        consent = self._repository.revoke_consent(user_id, consent_id)
        if consent is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="consent not found"
            )

        self._repository.record_audit(
            user_id=current_user.id,
            action="revoke_consent",
            resource=f"users/{user_id}/consents/{consent_id}",
            result="success",
        )
        return consent

    # -- data export / deletion ------------------------------------------

    def export_personal_data(self, current_user: StoredUser, user_id: UUID) -> DataExport:
        self._authorize(current_user, user_id)
        user = self._get_user_or_404(user_id)

        self._repository.record_audit(
            user_id=current_user.id,
            action="export_personal_data",
            resource=f"users/{user_id}/exports",
            result="success",
        )
        return DataExport(
            exported_at=datetime.now(timezone.utc),
            user=self._as_authenticated_user(user),
            profile=self._as_profile(user),
            preferences=user.preferences,
            role=user.role,
            consents=user.consents,
        )

    def delete_account(self, current_user: StoredUser, user_id: UUID) -> None:
        self._authorize(current_user, user_id)
        self._get_user_or_404(user_id)

        self._repository.record_audit(
            user_id=current_user.id,
            action="delete_account",
            resource=f"users/{user_id}",
            result="success",
        )
        self._repository.delete_user(user_id)

    # -- internal helpers -------------------------------------------------

    def _authorize(self, current_user: StoredUser, requested_user_id: UUID) -> None:
        """Owner-or-admin authorization: the only access rule this domain has."""
        if current_user.id == requested_user_id or current_user.role == "admin":
            return
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not authorized to access this resource",
        )

    def _invalid_reset_code(self) -> HTTPException:
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=INVALID_RESET_CODE)

    def _invalid_verification_code(self) -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=INVALID_VERIFICATION_CODE
        )

    def _require_email_sender(self) -> EmailSender:
        if self._email_sender is None:
            raise RuntimeError("an email sender is required to send verification codes")
        return self._email_sender

    def _verification_expiry(self, settings: EmailSettings) -> datetime:
        return datetime.now(timezone.utc) + timedelta(
            minutes=settings.email_verification_code_minutes
        )

    def _send_verification_code(
        self,
        sender: EmailSender,
        user: StoredUser,
        code: str,
        settings: EmailSettings,
        background_tasks: BackgroundTasks,
    ) -> None:
        # Sent after the response, like password recovery, so SMTP latency or
        # failures never reach the client.
        subject, text, html = email_verification_message(
            nombre=user.profile.nombre,
            code=code,
            minutes=settings.email_verification_code_minutes,
        )
        background_tasks.add_task(
            deliver_safely, sender, to=user.email, subject=subject, text=text, html=html
        )

    def _get_user_or_404(self, user_id: UUID) -> StoredUser:
        user = self._repository.get_user(user_id)
        if user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
        return user

    def _as_authenticated_user(self, user: StoredUser) -> AuthenticatedUser:
        return AuthenticatedUser(
            user_id=user.id,
            email=user.email,
            nombre=user.profile.nombre,
            apellido_paterno=user.profile.apellido_paterno,
            role=user.role,
        )

    def _as_profile(self, user: StoredUser) -> ProfileRead:
        return ProfileRead(user_id=user.id, rut=user.rut, **user.profile.model_dump())

    def _as_suggestion(
        self, candidate: SuggestionCandidate, mine: list[UserSport]
    ) -> SuggestedUser:
        return SuggestedUser(
            user_id=candidate.id,
            nombre=candidate.nombre,
            apellido_inicial=f"{candidate.apellido_paterno[:1].upper()}.",
            edad=_age(candidate.fecha_nacimiento),
            foto_perfil=candidate.foto_perfil,
            biografia=candidate.biografia,
            deportes=candidate.deportes,
            compatibilidad=compatibility(mine, candidate.deportes),
            distancia_km=None if candidate.distancia_km is None else round(candidate.distancia_km, 1),
            nivel_coincidente=similar_level(mine, candidate.deportes),
        )

    def _issue_token(self, user: StoredUser) -> TokenResponse:
        token, expires_in_seconds = create_access_token(user.id, user.role, user.email)
        # El rol decide el frontend: las cuentas de club van al sitio web, el resto a la app.
        es_club = user.role == CLUB_ROLE
        return TokenResponse(
            access_token=token,
            expires_in_seconds=expires_in_seconds,
            user=self._as_authenticated_user(user),
            frontend="web" if es_club else "app",
            web_url=get_settings().web_url if es_club else None,
        )


TOUCH_AFTER = timedelta(minutes=15)


def _age(birth_date: date | None) -> int | None:
    if birth_date is None:
        return None
    today = date.today()
    return today.year - birth_date.year - (
        (today.month, today.day) < (birth_date.month, birth_date.day)
    )
