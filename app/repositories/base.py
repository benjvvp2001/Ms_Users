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
    ReportCreate,
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
class LocationRecord:
    """Last known position; has latitud/longitud so it can be used wherever a Zona is."""

    latitud: float
    longitud: float
    precision_m: int | None
    actualizada_en: datetime


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


@dataclass(frozen=True)
class ReportRecord:
    id: UUID
    reportante_id: UUID | None
    tipo: str
    objetivo_id: str
    objetivo_nombre: str
    motivo: str
    detalle: str
    estado: str
    fecha_creacion: datetime
    fecha_revision: datetime | None


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
        role: str = "player",
    ) -> StoredUser:
        """Create the account unverified, together with its first code.
        `role` is the rol.nombre to assign (club sign-up uses club_admin)."""
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
        origin: Zona | LocationRecord | None, sports: list[UserSport], fresh_since: datetime
    ) -> list[SuggestionCandidate]:
        """Filter and rank eligible athletes before applying the result limit. A candidate's distance uses
        their live position when it is newer than `fresh_since`, otherwise their profile zone."""
        ...

    def get_location(self, user_id: UUID) -> LocationRecord | None: ...

    def save_location(self, user_id: UUID, latitud: float, longitud: float, precision_m: int | None) -> None:
        """Insert or replace the one row of this person."""
        ...

    def touch_location(self, user_id: UUID) -> None:
        """Mark the saved position as current without changing it (the person has not moved)."""
        ...

    def delete_location(self, user_id: UUID) -> bool: ...

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

    def block_user(self, blocker_id: UUID, blocked_id: UUID) -> bool:
        """Idempotent: blocking twice keeps the original date. True only when the block is new."""
        ...

    def blocked_between(self, user_a: UUID, user_b: UUID) -> bool:
        """Whether either of them has blocked the other."""
        ...

    def blocked_with(self, user_id: UUID, others: list[UUID]) -> set[UUID]:
        """Those of `others` that have a block with `user_id`, whoever blocked whom."""
        ...

    def change_mutes(self, muter_id: UUID, mute: list[UUID], unmute: list[UUID]) -> None:
        """Mute and unmute people together; muting twice keeps the original date."""
        ...

    def list_muted(self, muter_id: UUID) -> list[tuple[UUID, datetime]]:
        """(muted user, muted at), newest first."""
        ...

    def unblock_user(self, blocker_id: UUID, blocked_id: UUID) -> None: ...

    def list_blocked(self, blocker_id: UUID) -> list[tuple[UUID, datetime]]:
        """(blocked user, blocked at), newest first."""
        ...

    def create_report(self, reporter_id: UUID, report: ReportCreate) -> ReportRecord | None:
        """None when this person already sent the same reason about the same target."""
        ...

    def list_reports(self, estado: str | None, limit: int) -> list[ReportRecord]:
        """Newest first; all states when estado is None."""
        ...

    def review_report(self, report_id: UUID, estado: str, reviewer_id: UUID) -> ReportRecord | None: ...
