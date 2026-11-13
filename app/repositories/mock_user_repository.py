from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import RLock
from uuid import UUID, uuid4

from app.core.recommendations import compatibility, distance_km, matches_filters, similar_level
from app.repositories.base import (
    LocationRecord,
    ATHLETE_ROLES,
    REPORT_TRANSITIONS,
    AuditEvent,
    BlockedPerson,
    InvalidReportTransitionError,
    SanctionConflictError,
    SanctionRecord,
    SanctionReportError,
    SanctionTargetNotFoundError,
    AuditRecord,
    DependencyUnavailableError,
    EmailVerificationRecord,
    NewEmailVerification,
    PasswordResetRecord,
    ReportRecord,
    StoredUser,
    SuggestionCandidate,
    display_name,
)
from app.schemas.audit import LEGACY_ACTIONS, AdminAuditEntry, AuditAction, AuditQuery, AuditResourceType
from app.schemas.user import ConsentCreate, ConsentRead, PreferencesReplace, ProfileReplace, ReportCreate


def _now() -> datetime:
    from app.services import sanction_service  # late import: the service module imports repositories
    return sanction_service.utcnow()


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
        # Tests set it to simulate the audit storage failing.
        self.audit_unavailable = False
        self._sanctions: dict[UUID, SanctionRecord] = {}
        self.notifications: list[dict] = []
        self.notifications_unavailable = False

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

    def set_photo(self, user_id: UUID, key: str | None) -> tuple[bool, str | None]:
        with self._lock:
            user = self._users.get(user_id)
            if user is None:
                return False, None
            previous = user.profile.foto_perfil
            user.profile = user.profile.model_copy(update={"foto_perfil": key})
            return True, previous

    def photo_keys(self) -> set[str]:
        with self._lock:
            return {u.profile.foto_perfil for u in self._users.values() if u.profile.foto_perfil}

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
                and not any(s.usuario_id == user.id and s.in_force_at(_now())
                            for s in self._sanctions.values())
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

    def list_blocked_people(self, blocker_id: UUID, limit: int, offset: int = 0) -> list[BlockedPerson]:
        with self._lock:
            rows = [(blocked, at) for (blocker, blocked), at in self._blocks.items()
                    if blocker == blocker_id and blocked in self._users]
            rows.sort(key=lambda row: (-row[1].timestamp(), str(row[0])))
            return [BlockedPerson(user_id=blocked, nombre=self._users[blocked].profile.nombre,
                                  apellido_paterno=self._users[blocked].profile.apellido_paterno,
                                  foto_perfil=self._users[blocked].profile.foto_perfil, fecha_bloqueo=at)
                    for blocked, at in rows[offset:offset + limit]]

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

    def list_reports(self, estado: str | None, limit: int, offset: int = 0) -> list[ReportRecord]:
        with self._lock:
            rows = [r for r in reversed(self._reports.values()) if estado is None or r.estado == estado]
        return rows[offset:offset + limit]

    def list_reports_without_author(self, estado: str | None, limit: int, offset: int = 0) -> list[ReportRecord]:
        with self._lock:
            rows = [r for r in reversed(self._reports.values())
                    if r.tipo in ("actividad", "mensaje") and r.autor_id is None and (estado is None or r.estado == estado)]
        return rows[offset:offset + limit]

    def set_report_author(
        self, report_id: UUID, autor_id: UUID | None, consulted_at: datetime
    ) -> ReportRecord | None:
        with self._lock:
            current = self._reports.get(report_id)
            if current is None or current.tipo not in ("actividad", "mensaje"):
                return None
            known = autor_id if autor_id in self._users else None
            record = replace(current, autor_id=known, autor_consultado_en=consulted_at)
            self._reports[report_id] = record
            return record

    def review_report(
        self, report_id: UUID, estado: str, reviewer_id: UUID, motivo: str | None = None
    ) -> ReportRecord | None:
        with self._lock:
            if report_id not in self._reports:
                return None
            current = self._reports[report_id]
            if current.estado == estado:
                return current  # Same decision again: no change, no new audit row.
            if estado not in REPORT_TRANSITIONS.get(current.estado, ()):
                raise InvalidReportTransitionError(current.estado)
            if self.audit_unavailable:  # nothing is saved when the audit cannot be written
                raise DependencyUnavailableError("audit storage unavailable")
            record = replace(current, estado=estado, fecha_revision=datetime.now(timezone.utc),
                             revisado_por=reviewer_id, motivo_revision=motivo)
            self._reports[report_id] = record
            self._append_admin_audit(AdminAuditEntry(
                actor_user_id=reviewer_id,
                action={"revisado": AuditAction.REPORT_REVIEWED, "descartado": AuditAction.REPORT_DISCARDED,
                        "escalado": AuditAction.REPORT_ESCALATED}[estado],
                resource_type=AuditResourceType.REPORT, resource_id=str(report_id),
                motivo=motivo or current.motivo, metadata={"tipo": current.tipo, "desde": current.estado},
            ))
            return record

    def _append_admin_audit(self, entry: AdminAuditEntry) -> None:
        self._audit_events.append(AuditEvent(
            user_id=entry.actor_user_id, action=entry.action.value,
            resource=f"{entry.resource_type.value}/{entry.resource_id}", timestamp=datetime.now(timezone.utc),
            result=entry.result, motivo=entry.motivo, resource_type=entry.resource_type.value,
            resource_id=entry.resource_id, metadata=entry.metadata,
        ))

    # -- HU-45: sanctions ------------------------------------------------------------------------

    def get_active_sanction(self, user_id: UUID, now: datetime) -> SanctionRecord | None:
        with self._lock:
            return next((s for s in self._sanctions.values() if s.usuario_id == user_id and s.in_force_at(now)), None)

    def apply_sanction(
        self, *, sanction_id: UUID, user_id: UUID, tipo: str, dias: int | None, motivo: str, admin_id: UUID,
        report_id: UUID | None, now: datetime, fin: datetime | None,
    ) -> SanctionRecord:
        with self._lock:
            if user_id not in self._users:
                raise SanctionTargetNotFoundError(user_id)
            for key, s in list(self._sanctions.items()):   # an ended temporal one is closed here
                if s.usuario_id == user_id and s.estado == "activa" and s.fin is not None and s.fin <= now:
                    self._sanctions[key] = replace(s, estado="levantada", levantada_en=s.fin,
                                                   motivo_levantamiento="Vencimiento automático")
            if any(s.usuario_id == user_id and s.estado == "activa" for s in self._sanctions.values()):
                raise SanctionConflictError(user_id)
            if report_id is not None:
                report = self._reports.get(report_id)
                if report is None:
                    raise SanctionReportError("not_found")
                if report.estado != "escalado":
                    raise SanctionReportError("not_escalated")
                if (report.objetivo_id != str(user_id)) if report.tipo == "usuario" else (report.autor_id != user_id):
                    raise SanctionReportError("other_target")
            if self.audit_unavailable:  # nothing is saved when the audit cannot be written
                raise DependencyUnavailableError("audit storage unavailable")
            record = SanctionRecord(
                id=sanction_id, usuario_id=user_id, tipo=tipo, motivo=motivo, inicio=now, fin=fin, admin_id=admin_id,
                reporte_id=report_id, estado="activa", levantada_por=None, levantada_en=None,
                motivo_levantamiento=None, fecha_creacion=now)
            self._sanctions[sanction_id] = record
            self._append_admin_audit(AdminAuditEntry(
                actor_user_id=admin_id, action=AuditAction.SANCTION_APPLIED, resource_type=AuditResourceType.USER,
                resource_id=str(user_id), motivo=motivo,
                metadata={"sancion_id": str(sanction_id), "tipo": tipo, "dias": dias,
                          "reporte_id": None if report_id is None else str(report_id)}))
            return record

    def lift_sanction(
        self, sanction_id: UUID, admin_id: UUID, motivo: str, now: datetime
    ) -> tuple[SanctionRecord, bool] | None:
        with self._lock:
            current = self._sanctions.get(sanction_id)
            if current is None:
                return None
            if not current.in_force_at(now):
                return current, False
            if self.audit_unavailable:
                raise DependencyUnavailableError("audit storage unavailable")
            record = replace(current, estado="levantada", levantada_por=admin_id, levantada_en=now,
                             motivo_levantamiento=motivo)
            self._sanctions[sanction_id] = record
            self._append_admin_audit(AdminAuditEntry(
                actor_user_id=admin_id, action=AuditAction.SANCTION_LIFTED, resource_type=AuditResourceType.USER,
                resource_id=str(current.usuario_id), motivo=motivo,
                metadata={"sancion_id": str(sanction_id), "tipo": current.tipo}))
            return record, True

    def get_sanction(self, sanction_id: UUID) -> SanctionRecord | None:
        with self._lock:
            return self._sanctions.get(sanction_id)

    def list_sanctions(
        self, estado: str | None, user_id: UUID | None, limit: int, offset: int, now: datetime
    ) -> list[SanctionRecord]:
        with self._lock:
            rows = [s for s in reversed(self._sanctions.values())
                    if (estado is None or s.state_at(now) == estado) and (user_id is None or s.usuario_id == user_id)]
        return rows[offset:offset + limit]

    def add_notification(self, user_id: UUID, tipo: str, titulo: str, mensaje: str, data: dict) -> None:
        with self._lock:
            if self.notifications_unavailable:
                raise DependencyUnavailableError("notification storage unavailable")
            self.notifications.append({"usuario_id": user_id, "tipo": tipo, "titulo": titulo, "mensaje": mensaje,
                                       "data": data})

    def record_admin_audit(self, entry: AdminAuditEntry) -> None:
        with self._lock:
            if self.audit_unavailable:
                raise DependencyUnavailableError("audit storage unavailable")
            self._append_admin_audit(entry)

    def list_admin_audit(self, query: AuditQuery) -> tuple[list[AuditRecord], int]:
        catalog = {a.value for a in AuditAction}
        with self._lock:
            events = [e for e in reversed(self._audit_events) if e.action in catalog or e.action in LEGACY_ACTIONS]
            users = dict(self._users)

        def name(user_id: UUID) -> str | None:
            user = users.get(user_id)
            return None if user is None else display_name(user.profile.nombre, user.profile.apellido_paterno)

        def normalized(event: AuditEvent) -> AuditRecord:
            rtype, rid = event.resource_type, event.resource_id
            if rtype is None and event.resource.startswith("reports/"):
                rtype, rid = "report", event.resource[len("reports/"):]
            action = LEGACY_ACTIONS[event.action].value if event.action in LEGACY_ACTIONS else event.action
            return AuditRecord(
                id=event.id, fecha=event.timestamp, admin_id=event.user_id, admin_nombre=name(event.user_id),
                action=action, resource_type=rtype, resource_id=rid,
                afectado_nombre=name(UUID(rid)) if rtype == "user" and rid else None,
                motivo=event.motivo, result=event.result, metadata=event.metadata,
            )

        rows = [normalized(e) for e in events]
        rows = [r for r in rows
                if (not query.desde or r.fecha >= query.desde) and (not query.hasta or r.fecha <= query.hasta)
                and (not query.admin_id or r.admin_id == query.admin_id)
                and (not query.accion or r.action == query.accion.value)
                and (not query.recurso_tipo or r.resource_type == query.recurso_tipo.value)
                and (not query.recurso_id or r.resource_id == query.recurso_id)
                and (not query.resultado or r.result == query.resultado)]
        rows.sort(key=lambda r: r.fecha, reverse=True)  # stable: same instant keeps newest-inserted first
        start = (query.pagina - 1) * query.por_pagina
        return rows[start:start + query.por_pagina], len(rows)
