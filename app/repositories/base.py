from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol
from uuid import UUID, uuid4

from app.schemas.audit import AdminAuditEntry, AuditQuery
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


# HU-47. Un reporte pendiente se resuelve de tres formas; uno "revisado" aun puede escalarse a sancion.
# "descartado" y "escalado" son finales aqui (la sancion la sigue HU-45). Repetir el estado actual no es un cambio.
REPORT_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "pendiente": ("revisado", "descartado", "escalado"),
    "revisado": ("escalado",),
    "descartado": (),
    "escalado": (),
}


class InvalidReportTransitionError(Exception):
    """The report is already closed in a way that does not allow the requested state."""


@dataclass(frozen=True)
class SanctionRecord:
    """HU-45. `estado` is what is stored (activa | levantada); whether a temporal one is still in force depends
    on `fin` and the current time (see state_at / in_force_at), so it expires on its own."""

    id: UUID
    usuario_id: UUID
    tipo: str
    motivo: str
    inicio: datetime
    fin: datetime | None
    admin_id: UUID | None
    reporte_id: UUID | None
    estado: str
    levantada_por: UUID | None
    levantada_en: datetime | None
    motivo_levantamiento: str | None
    fecha_creacion: datetime

    def in_force_at(self, now: datetime) -> bool:
        return self.estado == "activa" and (self.fin is None or self.fin > now)

    def state_at(self, now: datetime) -> str:
        if self.estado == "levantada":
            return "levantada"
        return "activa" if self.in_force_at(now) else "vencida"


class SanctionConflictError(Exception):
    """The account already has a sanction in force."""


class SanctionTargetNotFoundError(Exception):
    """The account to sanction does not exist (or was deleted)."""


class SanctionReportError(Exception):
    """The report given as origin does not exist, is not escalated or is about someone else."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason  # not_found | not_escalated | other_target


class DependencyUnavailableError(Exception):
    """The database (or another dependency) failed; the API answers 503, never 500."""


def display_name(nombre: str | None, apellido_paterno: str | None) -> str | None:
    """'Ana T.': enough to recognise a person in the audit log without exposing their full data."""
    if not nombre:
        return None
    return f"{nombre} {(apellido_paterno or '')[:1].upper()}.".replace(" .", "")


@dataclass(frozen=True)
class AuditEvent:
    user_id: UUID
    action: str
    resource: str
    timestamp: datetime
    result: str
    # HU-48: only administrative actions fill these; older events keep them empty.
    id: UUID = field(default_factory=uuid4)
    motivo: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    metadata: dict | None = None


@dataclass(frozen=True)
class AuditRecord:
    """One row of the admin audit history, already with the (short) names of the people involved."""

    id: UUID
    fecha: datetime
    admin_id: UUID
    admin_nombre: str | None
    action: str
    resource_type: str | None
    resource_id: str | None
    afectado_nombre: str | None
    motivo: str | None
    result: str
    metadata: dict | None


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
    reportante_rol: str | None = None
    # Quien lo resolvio y la nota que escribio (HU-47). Quien lo hizo y cuando se resolvio van en las mismas columnas
    # que usa la fila de auditoria del evento.
    revisado_por: UUID | None = None
    motivo_revision: str | None = None
    # Reportes de una actividad o un mensaje: la cuenta que hizo ese contenido, si ya se averiguo, y cuando se pregunto.
    autor_id: UUID | None = None
    autor_consultado_en: datetime | None = None


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

    def create_report(self, reporter_id: UUID, report: ReportCreate, reporter_role: str) -> ReportRecord | None:
        """None when this person already sent the same reason about the same target.
        reporter_role is saved as it was when the report was sent."""
        ...

    def list_reports(self, estado: str | None, limit: int, offset: int = 0) -> list[ReportRecord]:
        """Newest first; all states when estado is None."""
        ...

    def set_report_author(
        self, report_id: UUID, autor_id: UUID | None, consulted_at: datetime
    ) -> ReportRecord | None:
        """Remembers who made the reported content (an activity or a message) and when it was asked. autor_id None
        = the origin service said there is no such content: the question is not repeated for a while. Only for
        reports of tipo actividad or mensaje. None when the report does not exist."""
        ...

    def list_reports_without_author(self, estado: str | None, limit: int, offset: int = 0) -> list[ReportRecord]:
        """Reports of tipo actividad or mensaje whose author is not known yet (same order as list_reports)."""
        ...

    def review_report(
        self, report_id: UUID, estado: str, reviewer_id: UUID, motivo: str | None = None
    ) -> ReportRecord | None:
        """Moves the report to `estado`, keeping who decided it, when and the note, and writes its audit row in the
        same transaction with those same data: if the audit cannot
        be written the change is not saved either (DependencyUnavailableError). motivo defaults to the
        report's own reason in the audit row (the note itself is kept only if the administrator wrote one). Same state as now: returns it untouched and audits nothing. A state REPORT_TRANSITIONS
        does not allow raises InvalidReportTransitionError. None when the report does not exist."""
        ...

    # -- HU-45: sanctions ------------------------------------------------------------------------

    def get_active_sanction(self, user_id: UUID, now: datetime) -> SanctionRecord | None:
        """The sanction in force for this account at `now`, if any."""
        ...

    def apply_sanction(
        self, *, sanction_id: UUID, user_id: UUID, tipo: str, dias: int | None, motivo: str, admin_id: UUID,
        report_id: UUID | None, now: datetime, fin: datetime | None,
    ) -> SanctionRecord:
        """One transaction: closes expired ones of the account, refuses a second one in force
        (SanctionConflictError), checks the origin report (SanctionReportError), saves the sanction and its
        SANCTION_APPLIED audit row. If the audit cannot be written nothing is saved (DependencyUnavailableError)."""
        ...

    def lift_sanction(
        self, sanction_id: UUID, admin_id: UUID, motivo: str, now: datetime
    ) -> tuple[SanctionRecord, bool] | None:
        """(sanction, changed). Lifting one already lifted or already expired changes nothing and audits nothing.
        None if it does not exist. The SANCTION_LIFTED audit row is written in the same transaction."""
        ...

    def get_sanction(self, sanction_id: UUID) -> SanctionRecord | None: ...

    def list_sanctions(
        self, estado: str | None, user_id: UUID | None, limit: int, offset: int, now: datetime
    ) -> list[SanctionRecord]:
        """Newest first. estado: activa | levantada | vencida (computed from `fin`), None for all."""
        ...

    def add_notification(self, user_id: UUID, tipo: str, titulo: str, mensaje: str, data: dict) -> None:
        """Save a notice for the account (users.notificacion) and, when the push inbox exists, queue it there."""
        ...

    def record_admin_audit(self, entry: AdminAuditEntry) -> None:
        """Write one administrative action in its own transaction (DependencyUnavailableError on failure)."""
        ...

    def list_admin_audit(self, query: AuditQuery) -> tuple[list[AuditRecord], int]:
        """(page of the history newest first, total rows matching the filters). Read only."""
        ...
