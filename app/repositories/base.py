from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol
from uuid import UUID

from app.schemas.user import (
    ConsentCreate,
    ConsentRead,
    PreferencesReplace,
    ProfileReplace,
    UserSport,
    SuggestionFilters,
    Zona,
)

# `usuario` is the athlete role in the original SportMatch database.
ATHLETE_ROLES = ("player", "usuario")


@dataclass
class StoredUser:
    id: UUID
    email: str
    password_hash: bytes
    rut: str | None
    profile: ProfileReplace
    preferences: PreferencesReplace
    role: str = "player"
    consents: list[ConsentRead] = field(default_factory=list)
    # False until the owner types the code emailed at registration.
    email_verified: bool = True


@dataclass(frozen=True)
class AuditEvent:
    user_id: UUID
    action: str
    resource: str
    timestamp: datetime
    result: str


@dataclass(frozen=True)
class PasswordResetRecord:
    id: UUID
    user_id: UUID
    code_hash: str
    expires_at: datetime
    created_at: datetime
    attempts: int


@dataclass(frozen=True)
class NewEmailVerification:
    code_hash: str
    expires_at: datetime


@dataclass(frozen=True)
class EmailVerificationRecord:
    user_id: UUID
    code_hash: str
    expires_at: datetime
    sent_at: datetime
    attempts: int


@dataclass(frozen=True)
class SuggestionCandidate:
    id: UUID
    nombre: str
    apellido_paterno: str
    fecha_nacimiento: date | None
    foto_perfil: str | None
    biografia: str | None
    deportes: list[UserSport]
    distancia_km: float | None = None
    nivel_coincidente: bool = False


class UserRepository(Protocol):
    """Shape shared by the mock repository and the PostgreSQL adapter."""

    def create_user(
        self,
        *,
        email: str,
        password_hash: bytes,
        rut: str | None,
        profile: ProfileReplace,
        preferences: PreferencesReplace,
        email_verification: NewEmailVerification,
    ) -> StoredUser:
        """Create the account unverified, together with its first code."""
        ...

    def get_user(self, user_id: UUID) -> StoredUser | None: ...

    def get_user_by_email(self, email: str) -> StoredUser | None: ...

    def replace_profile(self, user_id: UUID, profile: ProfileReplace) -> StoredUser: ...

    def replace_preferences(
        self, user_id: UUID, preferences: PreferencesReplace
    ) -> StoredUser: ...

    def add_consent(self, user_id: UUID, consent: ConsentCreate) -> ConsentRead: ...

    def list_consents(self, user_id: UUID) -> list[ConsentRead]: ...

    def revoke_consent(self, user_id: UUID, consent_id: UUID) -> ConsentRead | None: ...

    def delete_user(self, user_id: UUID) -> bool: ...

    def list_suggestion_candidates(
        self, *, exclude_user_id: UUID, limit: int, filters: SuggestionFilters,
        origin: Zona | None, sports: list[UserSport]
    ) -> list[SuggestionCandidate]:
        """Filter and rank eligible athletes before applying the result limit."""
        ...

    def record_audit(
        self, *, user_id: UUID, action: str, resource: str, result: str
    ) -> None: ...

    def create_password_reset(
        self, *, user_id: UUID, code_hash: str, expires_at: datetime
    ) -> PasswordResetRecord:
        """Store a new code and invalidate any previous unused one of the user."""
        ...

    def get_active_password_reset(self, user_id: UUID) -> PasswordResetRecord | None:
        """Latest unused and unexpired code of the user, if any."""
        ...

    def register_password_reset_attempt(self, reset_id: UUID, max_attempts: int) -> bool:
        """Atomically count one attempt; False when the code is no longer usable."""
        ...

    def complete_password_reset(
        self, *, reset_id: UUID, user_id: UUID, password_hash: bytes
    ) -> bool:
        """Mark the code as used and replace the password in one step."""
        ...

    def get_pending_email_verification(self, user_id: UUID) -> EmailVerificationRecord | None:
        """The user's code while the email is unverified (even if expired)."""
        ...

    def restart_email_verification(
        self, *, user_id: UUID, code_hash: str, expires_at: datetime
    ) -> bool:
        """Replace the pending code with a new one and reset its attempts."""
        ...

    def register_email_verification_attempt(self, user_id: UUID, max_attempts: int) -> bool:
        """Atomically count one attempt; False when the code is no longer usable."""
        ...

    def complete_email_verification(self, *, user_id: UUID, code_hash: str) -> bool:
        """Mark the email as verified if that code is still the pending one."""
        ...
