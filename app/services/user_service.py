from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status

from app.core.security import create_access_token, hash_password, verify_password
from app.repositories.base import StoredUser, UserRepository
from app.schemas.user import (
    AuthenticatedUser,
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


class UserService:
    """Business logic for the users domain: registration, profile, preferences,
    roles, consents, exports and account deletion.

    Sits between the HTTP controllers (app.api) and the data-access layer
    (app.repositories): it enforces owner-or-admin authorization and records
    audit events, so routes stay thin HTTP adapters.
    """

    def __init__(self, repository: UserRepository) -> None:
        self._repository = repository

    # -- auth -----------------------------------------------------------

    def register(self, payload: RegisterRequest) -> TokenResponse:
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
            )
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(error),
            ) from error

        self._repository.record_audit(
            user_id=user.id,
            action="register",
            resource="users",
            result="success",
        )
        return self._issue_token(user)

    def login(self, payload: LoginRequest) -> TokenResponse:
        user = self._repository.get_user_by_email(str(payload.email))
        if user is None or not verify_password(payload.password, user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid email or password",
            )

        self._repository.record_audit(
            user_id=user.id,
            action="login",
            resource="auth",
            result="success",
        )
        return self._issue_token(user)

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

    def _issue_token(self, user: StoredUser) -> TokenResponse:
        token, expires_in_seconds = create_access_token(user.id, user.role)
        return TokenResponse(
            access_token=token,
            expires_in_seconds=expires_in_seconds,
            user=self._as_authenticated_user(user),
        )
