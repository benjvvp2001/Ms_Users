from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import RLock
from uuid import UUID, uuid4

from app.core.recommendations import compatibility, distance_km, matches_filters, similar_level
from app.repositories.base import (
    LocationRecord,
    ATHLETE_ROLES,
    AuditEvent,
    EmailVerificationRecord,
    NewEmailVerification,
    PasswordResetRecord,
    ReportRecord,
    StoredUser,
    SuggestionCandidate,
)
from app.schemas.user import ConsentCreate, ConsentRead, PreferencesReplace, ProfileReplace, ReportCreate


class MockUserRepository:
    """Temporary mock repository; replace it with the users database adapter."""

    def __init__(self) -> None:
        self._users: dict[UUID, StoredUser] = {}
        self._audit_events: list[AuditEvent] = []
        self._password_resets: dict[UUID, PasswordResetRecord] = {}
        self._email_verifications: dict[UUID, EmailVerificationRecord] = {}
        self._blocks: dict[tuple[UUID, UUID], datetime] = {}
        self._mutes: dict[tuple[UUID, UUID], datetime] = {}
        self._last_tick: datetime | None = None
        self._reports: dict[UUID, ReportRecord] = {}
        self._locations: dict[UUID, LocationRecord] = {}
        self._lock = RLock()

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
        normalized_email = email.casefold()
        with self._lock:
            if any(user.email.casefold() == normalized_email for user in self._users.values()):
                raise ValueError("email already registered")
            if rut and any(user.rut == rut for user in self._users.values()):
                raise ValueError("rut already registered")

            user = StoredUser(
                id=uuid4(),
                email=normalized_email,
                password_hash=password_hash,
                rut=rut,
                profile=profile,
                preferences=preferences,
                role=role,
                email_verified=False,
            )
            self._users[user.id] = user
            self._email_verifications[user.id] = EmailVerificationRecord(
                user_id=user.id,
                code_hash=email_verification.code_hash,
                expires_at=email_verification.expires_at,
                sent_at=datetime.now(timezone.utc),
                attempts=0,
            )
            return user

    def get_user(self, user_id: UUID) -> StoredUser | None:
        with self._lock:
            return self._users.get(user_id)

    def get_user_by_email(self, email: str) -> StoredUser | None:
        normalized_email = email.casefold()
        with self._lock:
            return next(
                (
                    user
                    for user in self._users.values()
                    if user.email.casefold() == normalized_email
                ),
                None,
            )

    def replace_profile(self, user_id: UUID, profile: ProfileReplace) -> StoredUser:
        with self._lock:
            user = self._users[user_id]
            user.profile = profile
            return user

    def replace_preferences(
        self,
        user_id: UUID,
        preferences: PreferencesReplace,
    ) -> StoredUser:
        with self._lock:
            user = self._users[user_id]
            user.preferences = preferences
            return user

    def add_consent(self, user_id: UUID, consent: ConsentCreate) -> ConsentRead:
        with self._lock:
            user = self._users[user_id]
            granted_at = datetime.now(timezone.utc)
            stored_consent = ConsentRead(
                id=uuid4(),
                user_id=user_id,
                type=consent.type,
                purpose=consent.purpose,
                document_version=consent.document_version,
                method=consent.method,
                granted_at=granted_at,
                revoked_at=None,
                status="active",
            )
            user.consents.append(stored_consent)
            return stored_consent

    def list_consents(self, user_id: UUID) -> list[ConsentRead]:
        with self._lock:
            return list(self._users[user_id].consents)

    def revoke_consent(self, user_id: UUID, consent_id: UUID) -> ConsentRead | None:
        with self._lock:
            user = self._users[user_id]
            for index, consent in enumerate(user.consents):
                if consent.id == consent_id:
                    if consent.status == "revoked":
                        return consent
                    revoked = consent.model_copy(
                        update={
                            "revoked_at": datetime.now(timezone.utc),
                            "status": "revoked",
                        }
                    )
                    user.consents[index] = revoked
                    return revoked
            return None

    def delete_user(self, user_id: UUID) -> bool:
        with self._lock:
            self._locations.pop(user_id, None)
            return self._users.pop(user_id, None) is not None

    def list_suggestion_candidates(
        self, *, exclude_user_id: UUID, limit: int, filters, origin, sports, fresh_since
    ) -> list[SuggestionCandidate]:
        with self._lock:
            # Dicts keep insertion order, so reversed() is newest first.
            users = [
                user
                for user in reversed(self._users.values())
                if user.id != exclude_user_id and user.email_verified and user.role in ATHLETE_ROLES
                and user.preferences.disponibilidad_match
                and (exclude_user_id, user.id) not in self._blocks
                and (user.id, exclude_user_id) not in self._blocks
            ]
            candidates = [
                SuggestionCandidate(
                    id=user.id,
                    nombre=user.profile.nombre,
                    apellido_paterno=user.profile.apellido_paterno,
                    fecha_nacimiento=user.profile.fecha_nacimiento,
                    foto_perfil=user.profile.foto_perfil,
                    biografia=user.profile.biografia,
                    deportes=list(user.preferences.deportes),
                    distancia_km=distance_km(origin, self._position(user, fresh_since)),
                    nivel_coincidente=similar_level(sports, user.preferences.deportes),
                )
                for user in users
            ]

            candidates = [c for c in candidates if matches_filters(c.deportes, sports, c.distancia_km, filters)]
            candidates.sort(key=lambda c: (c.distancia_km if c.distancia_km is not None else float('inf'),
                                          not c.nivel_coincidente, -compatibility(sports, c.deportes)))
            return candidates[:limit]

    def record_audit(
        self,
        *,
        user_id: UUID,
        action: str,
        resource: str,
        result: str,
    ) -> None:
        with self._lock:
            self._audit_events.append(
                AuditEvent(
                    user_id=user_id,
                    action=action,
                    resource=resource,
                    timestamp=datetime.now(timezone.utc),
                    result=result,
                )
            )

    def create_password_reset(
        self, *, user_id: UUID, code_hash: str, expires_at: datetime
    ) -> PasswordResetRecord:
        with self._lock:
            self._password_resets = {
                key: reset
                for key, reset in self._password_resets.items()
                if reset.user_id != user_id
            }
            reset = PasswordResetRecord(
                id=uuid4(),
                user_id=user_id,
                code_hash=code_hash,
                expires_at=expires_at,
                created_at=datetime.now(timezone.utc),
                attempts=0,
            )
            self._password_resets[reset.id] = reset
            return reset

    def get_active_password_reset(self, user_id: UUID) -> PasswordResetRecord | None:
        now = datetime.now(timezone.utc)
        with self._lock:
            return next(
                (
                    reset
                    for reset in self._password_resets.values()
                    if reset.user_id == user_id and reset.expires_at > now
                ),
                None,
            )

    def register_password_reset_attempt(self, reset_id: UUID, max_attempts: int) -> bool:
        with self._lock:
            reset = self._password_resets.get(reset_id)
            if (
                reset is None
                or reset.attempts >= max_attempts
                or reset.expires_at <= datetime.now(timezone.utc)
            ):
                return False
            self._password_resets[reset_id] = replace(reset, attempts=reset.attempts + 1)
            return True

    def complete_password_reset(
        self, *, reset_id: UUID, user_id: UUID, password_hash: bytes
    ) -> bool:
        with self._lock:
            reset = self._password_resets.pop(reset_id, None)
            user = self._users.get(user_id)
            if reset is None or user is None:
                return False
            user.password_hash = password_hash
            return True

    def get_pending_email_verification(self, user_id: UUID) -> EmailVerificationRecord | None:
        with self._lock:
            return self._email_verifications.get(user_id)

    def restart_email_verification(
        self, *, user_id: UUID, code_hash: str, expires_at: datetime
    ) -> bool:
        with self._lock:
            if user_id not in self._email_verifications:
                return False
            self._email_verifications[user_id] = EmailVerificationRecord(
                user_id=user_id,
                code_hash=code_hash,
                expires_at=expires_at,
                sent_at=datetime.now(timezone.utc),
                attempts=0,
            )
            return True

    def register_email_verification_attempt(self, user_id: UUID, max_attempts: int) -> bool:
        with self._lock:
            pending = self._email_verifications.get(user_id)
            if (
                pending is None
                or pending.attempts >= max_attempts
                or pending.expires_at <= datetime.now(timezone.utc)
            ):
                return False
            self._email_verifications[user_id] = replace(pending, attempts=pending.attempts + 1)
            return True

    def complete_email_verification(self, *, user_id: UUID, code_hash: str) -> bool:
        with self._lock:
            pending = self._email_verifications.get(user_id)
            user = self._users.get(user_id)
            if (
                pending is None
                or user is None
                or pending.code_hash != code_hash
                or pending.expires_at <= datetime.now(timezone.utc)
            ):
                return False
            del self._email_verifications[user_id]
            user.email_verified = True
            return True

    def _position(self, user, fresh_since: datetime):
        live = self._locations.get(user.id)
        return live if live is not None and live.actualizada_en >= fresh_since else user.preferences.zona

    def get_location(self, user_id: UUID) -> LocationRecord | None:
        with self._lock:
            return self._locations.get(user_id)

    def save_location(self, user_id: UUID, latitud: float, longitud: float, precision_m: int | None) -> None:
        with self._lock:
            self._locations[user_id] = LocationRecord(latitud, longitud, precision_m, self._tick())

    def touch_location(self, user_id: UUID) -> None:
        with self._lock:
            if user_id in self._locations:
                self._locations[user_id] = replace(self._locations[user_id], actualizada_en=self._tick())

    def delete_location(self, user_id: UUID) -> bool:
        with self._lock:
            return self._locations.pop(user_id, None) is not None

    def _tick(self) -> datetime:
        """Now, strictly increasing: the system clock can repeat a value (Windows) and the lists are
        ordered by these dates. PostgreSQL's CURRENT_TIMESTAMP is per transaction and needs no help."""
        now = datetime.now(timezone.utc)
        if self._last_tick is not None and now <= self._last_tick:
            now = self._last_tick + timedelta(microseconds=1)
        self._last_tick = now
        return now

    def block_user(self, blocker_id: UUID, blocked_id: UUID) -> bool:
        with self._lock:
            created = (blocker_id, blocked_id) not in self._blocks
            self._blocks.setdefault((blocker_id, blocked_id), self._tick())
            return created

    def blocked_between(self, user_a: UUID, user_b: UUID) -> bool:
        with self._lock:
            return (user_a, user_b) in self._blocks or (user_b, user_a) in self._blocks

    def blocked_with(self, user_id: UUID, others: list[UUID]) -> set[UUID]:
        with self._lock:
            return {other for other in others
                    if (user_id, other) in self._blocks or (other, user_id) in self._blocks}

    def change_mutes(self, muter_id: UUID, mute: list[UUID], unmute: list[UUID]) -> None:
        with self._lock:
            for user_id in unmute:
                self._mutes.pop((muter_id, user_id), None)
            for user_id in mute:
                self._mutes.setdefault((muter_id, user_id), self._tick())

    def list_muted(self, muter_id: UUID) -> list[tuple[UUID, datetime]]:
        with self._lock:
            rows = [(muted, at) for (muter, muted), at in self._mutes.items() if muter == muter_id]
        return sorted(rows, key=lambda row: (row[1], str(row[0])), reverse=True)

    def unblock_user(self, blocker_id: UUID, blocked_id: UUID) -> None:
        with self._lock:
            self._blocks.pop((blocker_id, blocked_id), None)

    def list_blocked(self, blocker_id: UUID) -> list[tuple[UUID, datetime]]:
        with self._lock:
            rows = [(blocked, at) for (blocker, blocked), at in self._blocks.items() if blocker == blocker_id]
        return sorted(rows, key=lambda row: row[1], reverse=True)

    def create_report(self, reporter_id: UUID, report: ReportCreate, reporter_role: str) -> ReportRecord | None:
        with self._lock:
            if any((r.reportante_id, r.tipo, r.objetivo_id, r.motivo)
                   == (reporter_id, report.tipo, report.objetivo_id, report.motivo)
                   for r in self._reports.values()):
                return None
            record = ReportRecord(
                id=uuid4(), reportante_id=reporter_id, tipo=report.tipo, objetivo_id=report.objetivo_id,
                objetivo_nombre=report.objetivo_nombre, motivo=report.motivo, detalle=report.detalle,
                estado="pendiente", fecha_creacion=datetime.now(timezone.utc), fecha_revision=None,
                reportante_rol=reporter_role,
            )
            self._reports[record.id] = record
            return record

    def list_reports(self, estado: str | None, limit: int) -> list[ReportRecord]:
        with self._lock:
            rows = [r for r in reversed(self._reports.values()) if estado is None or r.estado == estado]
        return rows[:limit]

    def review_report(self, report_id: UUID, estado: str, reviewer_id: UUID) -> ReportRecord | None:
        with self._lock:
            if report_id not in self._reports:
                return None
            record = replace(self._reports[report_id], estado=estado, fecha_revision=datetime.now(timezone.utc))
            self._reports[report_id] = record
            return record
