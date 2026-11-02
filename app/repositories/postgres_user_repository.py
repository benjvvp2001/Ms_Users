from __future__ import annotations

import logging
from pathlib import Path
from psycopg.types.json import Jsonb
from datetime import datetime
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.repositories.base import (
    ATHLETE_ROLES,
    REPORT_TRANSITIONS,
    InvalidReportTransitionError,
    SanctionConflictError,
    SanctionRecord,
    SanctionReportError,
    SanctionTargetNotFoundError,
    AuditRecord,
    DependencyUnavailableError,
    display_name,
    EmailVerificationRecord,
    LocationRecord,
    NewEmailVerification,
    PasswordResetRecord,
    ReportRecord,
    StoredUser,
    SuggestionCandidate,
)
from app.schemas.audit import (
    LEGACY_ACTIONS,
    AdminAuditEntry,
    AuditAction,
    AuditQuery,
    AuditResourceType,
)
from app.schemas.user import (
    AvailabilityWindow,
    ConsentCreate,
    ConsentRead,
    PreferencesReplace,
    ProfileReplace,
    ReportCreate,
    UserSport,
    SuggestionFilters,
    Zona,
)


def insert_admin_audit(conn: psycopg.Connection, entry: AdminAuditEntry) -> None:
    """Insert one administrative action using the caller's connection, so it commits or rolls back with
    whatever the caller is doing. Other repositories (plans, memberships, payments) call it inside their own
    `with conn.transaction():` right after the change they audit."""
    conn.execute(
        """
        INSERT INTO audit_events (id, actor_user_id, action, resource, result,
                                  motivo, resource_type, resource_id, metadata)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (uuid4(), entry.actor_user_id, entry.action.value, f"{entry.resource_type.value}/{entry.resource_id}",
         entry.result, entry.motivo, entry.resource_type.value, entry.resource_id,
         None if entry.metadata is None else Jsonb(entry.metadata)),
    )


_REPORT_ACTIONS = {"revisado": AuditAction.REPORT_REVIEWED, "descartado": AuditAction.REPORT_DISCARDED,
                   "escalado": AuditAction.REPORT_ESCALATED}
_LEGACY_BY_ACTION: dict[str, list[str]] = {}
for _raw, _action in LEGACY_ACTIONS.items():
    _LEGACY_BY_ACTION.setdefault(_action.value, []).append(_raw)
_CATALOG_RAW = [a.value for a in AuditAction] + list(LEGACY_ACTIONS)


logger = logging.getLogger(__name__)


class PostgresUserRepository:
    """PostgreSQL-backed repository for the sportmach_users schema (rol, usuario,
    preferencia_usuario, disponibilidad, usuario_deporte, consent, audit_events,
    password_reset_token, email_verificacion, preferencia_perfil).
    """

    def __init__(self, pool: ConnectionPool, push_inbox: str = "notifications.inbox") -> None:
        self._pool = pool
        # Inbox of the gateway's push / in-app notifications (HU-45 queues sanction notices there when it exists).
        self._push_inbox = push_inbox

    def close(self) -> None:
        self._pool.close()

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
        user_id = uuid4()
        normalized_email = email.casefold()
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT id FROM rol WHERE nombre = %s", (role,))
                    role_row = cur.fetchone()
                    if role_row is None:
                        raise RuntimeError(f"role {role!r} is missing from rol")
                    role_id = role_row[0]

                    cur.execute(
                        """
                        INSERT INTO usuario
                            (id, rut, rol_id, nombre, apellido_paterno, apellido_materno,
                             email, password, fecha_nacimiento, telefono, foto_perfil,
                             biografia)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            user_id,
                            rut,
                            role_id,
                            profile.nombre,
                            profile.apellido_paterno,
                            profile.apellido_materno,
                            normalized_email,
                            password_hash.decode("utf-8"),
                            profile.fecha_nacimiento,
                            profile.telefono,
                            profile.foto_perfil,
                            profile.biografia,
                        ),
                    )
                    cur.execute(
                        """
                        INSERT INTO preferencia_usuario
                            (usuario_id, rango_distancia_km, rango_edad_min,
                             rango_edad_max, mismo_nivel, disponibilidad_match)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            user_id,
                            preferences.rango_distancia_km,
                            preferences.rango_edad_min,
                            preferences.rango_edad_max,
                            preferences.mismo_nivel,
                            preferences.disponibilidad_match,
                        ),
                    )
                    self._replace_deportes(cur, user_id, preferences.deportes)
                    self._replace_disponibilidad(cur, user_id, preferences.disponibilidad)
                    self._replace_objetivos_zona(cur, user_id, preferences)
                    cur.execute(
                        """
                        INSERT INTO email_verificacion (usuario_id, code_hash, expires_at)
                        VALUES (%s, %s, %s)
                        """,
                        (user_id, email_verification.code_hash, email_verification.expires_at),
                    )
        except psycopg.errors.UniqueViolation as error:
            constraint = getattr(error.diag, "constraint_name", "") or ""
            if "rut" in constraint:
                raise ValueError("rut already registered") from error
            raise ValueError("email already registered") from error

        return StoredUser(
            id=user_id,
            email=normalized_email,
            password_hash=password_hash,
            rut=rut,
            profile=profile,
            preferences=preferences,
            role=role,
            consents=[],
            email_verified=False,
        )

    def get_user(self, user_id: UUID) -> StoredUser | None:
        with self._pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT u.id, u.email, u.password, u.rut, u.nombre,
                           u.apellido_paterno, u.apellido_materno, u.fecha_nacimiento,
                           u.telefono, u.foto_perfil, u.biografia, r.nombre AS role,
                           -- Accounts created before email verification have no row.
                           (ev.usuario_id IS NULL OR ev.verified_at IS NOT NULL)
                               AS email_verified
                    FROM usuario u
                    JOIN rol r ON r.id = u.rol_id
                    LEFT JOIN email_verificacion ev ON ev.usuario_id = u.id
                    WHERE u.id = %s AND u.is_active = TRUE
                    """,
                    (user_id,),
                )
                user_row = cur.fetchone()
                if user_row is None:
                    return None

                cur.execute(
                    """
                    SELECT rango_distancia_km, rango_edad_min, rango_edad_max,
                           mismo_nivel, disponibilidad_match
                    FROM preferencia_usuario WHERE usuario_id = %s
                    """,
                    (user_id,),
                )
                preferences_row = cur.fetchone() or {
                    "rango_distancia_km": None,
                    "rango_edad_min": None,
                    "rango_edad_max": None,
                    "mismo_nivel": None,
                    "disponibilidad_match": True,
                }

                cur.execute(
                    "SELECT deporte_codigo, nivel FROM usuario_deporte WHERE usuario_id = %s",
                    (user_id,),
                )
                deportes = [
                    UserSport(deporte_codigo=row["deporte_codigo"], nivel=row["nivel"])
                    for row in cur.fetchall()
                ]

                cur.execute(
                    "SELECT dia_semana, hora_inicio, hora_fin FROM disponibilidad "
                    "WHERE usuario_id = %s",
                    (user_id,),
                )
                disponibilidad = [
                    AvailabilityWindow(
                        dia_semana=row["dia_semana"],
                        hora_inicio=row["hora_inicio"],
                        hora_fin=row["hora_fin"],
                    )
                    for row in cur.fetchall()
                ]

                cur.execute(
                    "SELECT objetivos, comuna, latitud, longitud FROM preferencia_perfil "
                    "WHERE usuario_id = %s",
                    (user_id,),
                )
                perfil_row = cur.fetchone()

                cur.execute(
                    """
                    SELECT id, type, purpose, document_version, method,
                           granted_at, revoked_at, status
                    FROM consent WHERE user_id = %s
                    ORDER BY granted_at DESC
                    """,
                    (user_id,),
                )
                consents = [ConsentRead(user_id=user_id, **row) for row in cur.fetchall()]

        return StoredUser(
            id=user_row["id"],
            email=user_row["email"],
            password_hash=user_row["password"].encode("utf-8"),
            rut=user_row["rut"],
            profile=ProfileReplace(
                nombre=user_row["nombre"],
                apellido_paterno=user_row["apellido_paterno"],
                apellido_materno=user_row["apellido_materno"],
                fecha_nacimiento=user_row["fecha_nacimiento"],
                telefono=user_row["telefono"],
                foto_perfil=user_row["foto_perfil"],
                biografia=user_row["biografia"],
            ),
            preferences=PreferencesReplace(
                deportes=deportes,
                disponibilidad=disponibilidad,
                objetivos=list(perfil_row["objetivos"]) if perfil_row else [],
                zona=self._as_zona(perfil_row),
                rango_distancia_km=preferences_row["rango_distancia_km"],
                rango_edad_min=preferences_row["rango_edad_min"],
                rango_edad_max=preferences_row["rango_edad_max"],
                mismo_nivel=preferences_row["mismo_nivel"],
                disponibilidad_match=preferences_row["disponibilidad_match"],
            ),
            role=user_row["role"],
            consents=consents,
            email_verified=user_row["email_verified"],
        )

    def get_user_by_email(self, email: str) -> StoredUser | None:
        normalized_email = email.casefold()
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM usuario WHERE lower(email) = lower(%s)",
                    (normalized_email,),
                )
                row = cur.fetchone()
        if row is None:
            return None
        return self.get_user(row[0])

    def replace_profile(self, user_id: UUID, profile: ProfileReplace) -> StoredUser:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE usuario
                    SET nombre = %s, apellido_paterno = %s, apellido_materno = %s,
                        fecha_nacimiento = %s, telefono = %s, foto_perfil = %s,
                        biografia = %s, fecha_actualizacion = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (
                        profile.nombre,
                        profile.apellido_paterno,
                        profile.apellido_materno,
                        profile.fecha_nacimiento,
                        profile.telefono,
                        profile.foto_perfil,
                        profile.biografia,
                        user_id,
                    ),
                )
        return self._get_user_or_raise(user_id)

    def replace_preferences(
        self, user_id: UUID, preferences: PreferencesReplace
    ) -> StoredUser:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO preferencia_usuario
                        (usuario_id, rango_distancia_km, rango_edad_min,
                         rango_edad_max, mismo_nivel, disponibilidad_match)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (usuario_id) DO UPDATE SET
                        rango_distancia_km = EXCLUDED.rango_distancia_km,
                        rango_edad_min = EXCLUDED.rango_edad_min,
                        rango_edad_max = EXCLUDED.rango_edad_max,
                        mismo_nivel = EXCLUDED.mismo_nivel,
                        disponibilidad_match = EXCLUDED.disponibilidad_match
                    """,
                    (
                        user_id,
                        preferences.rango_distancia_km,
                        preferences.rango_edad_min,
                        preferences.rango_edad_max,
                        preferences.mismo_nivel,
                        preferences.disponibilidad_match,
                    ),
                )
                self._replace_deportes(cur, user_id, preferences.deportes)
                self._replace_disponibilidad(cur, user_id, preferences.disponibilidad)
                self._replace_objetivos_zona(cur, user_id, preferences)
        return self._get_user_or_raise(user_id)

    def add_consent(self, user_id: UUID, consent: ConsentCreate) -> ConsentRead:
        consent_id = uuid4()
        with self._pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    INSERT INTO consent
                        (id, user_id, type, purpose, document_version, method, status)
                    VALUES (%s, %s, %s, %s, %s, %s, 'active')
                    RETURNING granted_at
                    """,
                    (
                        consent_id,
                        user_id,
                        consent.type,
                        consent.purpose,
                        consent.document_version,
                        consent.method,
                    ),
                )
                granted_at = cur.fetchone()["granted_at"]

        return ConsentRead(
            id=consent_id,
            user_id=user_id,
            type=consent.type,
            purpose=consent.purpose,
            document_version=consent.document_version,
            method=consent.method,
            granted_at=granted_at,
            revoked_at=None,
            status="active",
        )

    def list_consents(self, user_id: UUID) -> list[ConsentRead]:
        with self._pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT id, type, purpose, document_version, method,
                           granted_at, revoked_at, status
                    FROM consent WHERE user_id = %s
                    ORDER BY granted_at DESC
                    """,
                    (user_id,),
                )
                rows = cur.fetchall()
        return [ConsentRead(user_id=user_id, **row) for row in rows]

    def revoke_consent(self, user_id: UUID, consent_id: UUID) -> ConsentRead | None:
        with self._pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    UPDATE consent
                    SET status = 'revoked', revoked_at = CURRENT_TIMESTAMP
                    WHERE id = %s AND user_id = %s AND status = 'active'
                    RETURNING id, type, purpose, document_version, method,
                              granted_at, revoked_at, status
                    """,
                    (consent_id, user_id),
                )
                row = cur.fetchone()
                if row is None:
                    cur.execute(
                        """
                        SELECT id, type, purpose, document_version, method,
                               granted_at, revoked_at, status
                        FROM consent WHERE id = %s AND user_id = %s
                        """,
                        (consent_id, user_id),
                    )
                    row = cur.fetchone()
        if row is None:
            return None
        return ConsentRead(user_id=user_id, **row)

    def delete_user(self, user_id: UUID) -> bool:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                # Keep notifications, memberships and payments consistent. An
                # inactive account cannot log in or use previously issued JWTs.
                cur.execute("DELETE FROM ubicacion_actual WHERE usuario_id = %s", (user_id,))
                cur.execute(
                    "UPDATE usuario SET is_active = FALSE, "
                    "fecha_actualizacion = CURRENT_TIMESTAMP "
                    "WHERE id = %s AND is_active = TRUE", (user_id,)
                )
                return cur.rowcount > 0

    def list_suggestion_candidates(
        self, *, exclude_user_id: UUID, limit: int, filters: SuggestionFilters,
        origin: Zona | LocationRecord | None, sports: list[UserSport], fresh_since: datetime
    ) -> list[SuggestionCandidate]:
        params = {
            "fresh_since": fresh_since,
            **filters.model_dump(), "user_id": exclude_user_id, "limit": limit,
            "roles": list(ATHLETE_ROLES), "sports": Jsonb([s.model_dump() for s in sports]),
            "latitude": origin.latitud if origin else None,
            "longitude": origin.longitud if origin else None,
            "filter_sport": filters.sport is not None or filters.shared_sports or filters.level_tolerance is not None
                            or filters.min_level != 1 or filters.max_level != 5,
        }
        with self._pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute((Path(__file__).parent / "sql/suggestions.sql").read_text(), params)
                rows = cur.fetchall()
                deportes: dict[UUID, list[UserSport]] = {row["id"]: [] for row in rows}
                if deportes:
                    cur.execute(
                        "SELECT usuario_id, deporte_codigo, nivel FROM usuario_deporte "
                        "WHERE usuario_id = ANY(%s)",
                        (list(deportes),),
                    )
                    for sport in cur.fetchall():
                        deportes[sport["usuario_id"]].append(
                            UserSport(deporte_codigo=sport["deporte_codigo"], nivel=sport["nivel"])
                        )
        return [
            SuggestionCandidate(
                id=row["id"],
                nombre=row["nombre"],
                apellido_paterno=row["apellido_paterno"],
                fecha_nacimiento=row["fecha_nacimiento"],
                foto_perfil=row["foto_perfil"],
                biografia=row["biografia"],
                deportes=deportes[row["id"]],
                distancia_km=row["distancia_km"],
                nivel_coincidente=row["nivel_coincidente"],
            )
            for row in rows
        ]

    def get_location(self, user_id: UUID) -> LocationRecord | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT latitud::float8, longitud::float8, precision_m, actualizada_en "
                "FROM ubicacion_actual WHERE usuario_id = %s", (user_id,),
            ).fetchone()
        return LocationRecord(*row) if row else None

    def save_location(self, user_id: UUID, latitud: float, longitud: float, precision_m: int | None) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO ubicacion_actual (usuario_id, latitud, longitud, precision_m) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (usuario_id) DO UPDATE SET latitud = EXCLUDED.latitud, longitud = EXCLUDED.longitud, "
                "precision_m = EXCLUDED.precision_m, actualizada_en = CURRENT_TIMESTAMP",
                (user_id, latitud, longitud, precision_m),
            )

    def touch_location(self, user_id: UUID) -> None:
        with self._pool.connection() as conn:
            conn.execute("UPDATE ubicacion_actual SET actualizada_en = CURRENT_TIMESTAMP WHERE usuario_id = %s", (user_id,))

    def delete_location(self, user_id: UUID) -> bool:
        with self._pool.connection() as conn:
            return conn.execute("DELETE FROM ubicacion_actual WHERE usuario_id = %s", (user_id,)).rowcount > 0

    def record_audit(
        self, *, user_id: UUID, action: str, resource: str, result: str
    ) -> None:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO audit_events (id, actor_user_id, action, resource, result)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (uuid4(), user_id, action, resource, result),
                )

    def block_user(self, blocker_id: UUID, blocked_id: UUID) -> bool:
        with self._pool.connection() as conn:
            cur = conn.execute(
                "INSERT INTO bloqueo_usuario (bloqueador_id, bloqueado_id) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING", (blocker_id, blocked_id),
            )
            return cur.rowcount == 1

    def blocked_between(self, user_a: UUID, user_b: UUID) -> bool:
        with self._pool.connection() as conn:
            return conn.execute(
                "SELECT 1 FROM bloqueo_usuario WHERE (bloqueador_id = %s AND bloqueado_id = %s) "
                "OR (bloqueador_id = %s AND bloqueado_id = %s)", (user_a, user_b, user_b, user_a),
            ).fetchone() is not None

    def blocked_with(self, user_id: UUID, others: list[UUID]) -> set[UUID]:
        if not others:
            return set()
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT bloqueado_id FROM bloqueo_usuario WHERE bloqueador_id = %(me)s AND bloqueado_id = ANY(%(ids)s) "
                "UNION SELECT bloqueador_id FROM bloqueo_usuario WHERE bloqueado_id = %(me)s AND bloqueador_id = ANY(%(ids)s)",
                {"me": user_id, "ids": others},
            ).fetchall()
        return {row[0] for row in rows}

    def change_mutes(self, muter_id: UUID, mute: list[UUID], unmute: list[UUID]) -> None:
        with self._pool.connection() as conn, conn.transaction():
            if unmute:
                conn.execute(
                    "DELETE FROM silencio_usuario WHERE silenciador_id = %s AND silenciado_id = ANY(%s)",
                    (muter_id, unmute),
                )
            if mute:
                conn.execute(
                    "INSERT INTO silencio_usuario (silenciador_id, silenciado_id) "
                    "SELECT %s, unnest(%s::uuid[]) ON CONFLICT DO NOTHING", (muter_id, mute),
                )

    def list_muted(self, muter_id: UUID) -> list[tuple[UUID, datetime]]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT silenciado_id, fecha_creacion FROM silencio_usuario "
                "WHERE silenciador_id = %s ORDER BY fecha_creacion DESC, silenciado_id", (muter_id,),
            ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def unblock_user(self, blocker_id: UUID, blocked_id: UUID) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "DELETE FROM bloqueo_usuario WHERE bloqueador_id = %s AND bloqueado_id = %s",
                (blocker_id, blocked_id),
            )

    def list_blocked(self, blocker_id: UUID) -> list[tuple[UUID, datetime]]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT bloqueado_id, fecha_creacion FROM bloqueo_usuario "
                "WHERE bloqueador_id = %s ORDER BY fecha_creacion DESC", (blocker_id,),
            ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def create_report(self, reporter_id: UUID, report: ReportCreate, reporter_role: str) -> ReportRecord | None:
        with self._pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    INSERT INTO reporte (id, reportante_id, reportante_rol, tipo, objetivo_id, objetivo_nombre,
                                         motivo, detalle)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (reportante_id, tipo, objetivo_id, motivo) DO NOTHING
                    RETURNING *
                    """,
                    (uuid4(), reporter_id, reporter_role, report.tipo, report.objetivo_id, report.objetivo_nombre,
                     report.motivo, report.detalle),
                )
                row = cur.fetchone()
        return None if row is None else self._as_report(row)

    def list_reports(self, estado: str | None, limit: int, offset: int = 0) -> list[ReportRecord]:
        with self._pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT * FROM reporte WHERE %(estado)s::text IS NULL OR estado = %(estado)s "
                    "ORDER BY fecha_creacion DESC, id LIMIT %(limit)s OFFSET %(offset)s",
                    {"estado": estado, "limit": limit, "offset": offset},
                )
                return [self._as_report(row) for row in cur.fetchall()]

    def list_reports_without_author(self, estado: str | None, limit: int, offset: int = 0) -> list[ReportRecord]:
        with self._pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT * FROM reporte WHERE tipo IN ('actividad', 'mensaje') AND autor_id IS NULL "
                    "AND (%(estado)s::text IS NULL OR estado = %(estado)s) "
                    "ORDER BY fecha_creacion DESC, id LIMIT %(limit)s OFFSET %(offset)s",
                    {"estado": estado, "limit": limit, "offset": offset},
                )
                return [self._as_report(row) for row in cur.fetchall()]

    def set_report_author(
        self, report_id: UUID, autor_id: UUID | None, consulted_at: datetime
    ) -> ReportRecord | None:
        with self._pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                # The account may have been deleted meanwhile: then the author stays unknown, never a dangling id.
                cur.execute(
                    "UPDATE reporte SET autor_id = (SELECT id FROM usuario WHERE id = %s), autor_consultado_en = %s "
                    "WHERE id = %s AND tipo IN ('actividad', 'mensaje') RETURNING *",
                    (autor_id, consulted_at, report_id),
                )
                row = cur.fetchone()
        return None if row is None else self._as_report(row)

    def review_report(
        self, report_id: UUID, estado: str, reviewer_id: UUID, motivo: str | None = None
    ) -> ReportRecord | None:
        try:
            with self._pool.connection() as conn, conn.transaction():
                with conn.cursor(row_factory=dict_row) as cur:
                    # The row lock makes two simultaneous decisions on one report happen one after the other.
                    cur.execute("SELECT * FROM reporte WHERE id = %s FOR UPDATE", (report_id,))
                    current = cur.fetchone()
                    if current is None:
                        return None
                    if current["estado"] == estado:
                        return self._as_report(current)  # Same decision again: no change, no new audit row.
                    if estado not in REPORT_TRANSITIONS.get(current["estado"], ()):
                        raise InvalidReportTransitionError(current["estado"])
                    cur.execute(
                        "UPDATE reporte SET estado = %s, revisado_por = %s, fecha_revision = CURRENT_TIMESTAMP, "
                        "motivo_revision = %s WHERE id = %s RETURNING *",
                        (estado, reviewer_id, motivo, report_id),
                    )
                    row = cur.fetchone()
                # Same transaction: if the audit row fails, the change is rolled back.
                insert_admin_audit(conn, AdminAuditEntry(
                    actor_user_id=reviewer_id, action=_REPORT_ACTIONS[estado],
                    resource_type=AuditResourceType.REPORT, resource_id=str(report_id),
                    motivo=motivo or row["motivo"], metadata={"tipo": row["tipo"], "desde": current["estado"]},
                ))
        except psycopg.Error as error:
            raise DependencyUnavailableError("audit or report storage unavailable") from error
        return self._as_report(row)

    # -- HU-45: sanctions ------------------------------------------------------------------------

    @staticmethod
    def _as_sanction(row: dict) -> SanctionRecord:
        return SanctionRecord(
            id=row["id"], usuario_id=row["usuario_id"], tipo=row["tipo"], motivo=row["motivo"], inicio=row["inicio"],
            fin=row["fin"], admin_id=row["admin_id"], reporte_id=row["reporte_id"], estado=row["estado"],
            levantada_por=row["levantada_por"], levantada_en=row["levantada_en"],
            motivo_levantamiento=row["motivo_levantamiento"], fecha_creacion=row["fecha_creacion"],
        )

    def get_active_sanction(self, user_id: UUID, now: datetime) -> SanctionRecord | None:
        try:
            with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT * FROM sancion WHERE usuario_id = %s AND estado = 'activa' "
                    "AND (fin IS NULL OR fin > %s) LIMIT 1", (user_id, now))
                row = cur.fetchone()
        except psycopg.Error as error:
            raise DependencyUnavailableError("sanction storage unavailable") from error
        return None if row is None else self._as_sanction(row)

    def apply_sanction(
        self, *, sanction_id: UUID, user_id: UUID, tipo: str, dias: int | None, motivo: str, admin_id: UUID,
        report_id: UUID | None, now: datetime, fin: datetime | None,
    ) -> SanctionRecord:
        try:
            with self._pool.connection() as conn, conn.transaction():
                with conn.cursor(row_factory=dict_row) as cur:
                    # Locking the account serializes two admins sanctioning it at the same time.
                    cur.execute("SELECT id FROM usuario WHERE id = %s AND is_active = TRUE FOR UPDATE", (user_id,))
                    if cur.fetchone() is None:
                        raise SanctionTargetNotFoundError(user_id)
                    # A temporal sanction that already ended is closed here, not by a job.
                    cur.execute(
                        "UPDATE sancion SET estado = 'levantada', levantada_en = fin, "
                        "motivo_levantamiento = 'Vencimiento automático' "
                        "WHERE usuario_id = %s AND estado = 'activa' AND fin IS NOT NULL AND fin <= %s", (user_id, now))
                    cur.execute("SELECT 1 FROM sancion WHERE usuario_id = %s AND estado = 'activa'", (user_id,))
                    if cur.fetchone() is not None:
                        raise SanctionConflictError(user_id)
                    if report_id is not None:
                        cur.execute("SELECT tipo, objetivo_id, estado, autor_id FROM reporte WHERE id = %s FOR SHARE", (report_id,))
                        report = cur.fetchone()
                        if report is None:
                            raise SanctionReportError("not_found")
                        if report["estado"] != "escalado":
                            raise SanctionReportError("not_escalated")
                        # A report of a person names that person; one of an activity or a message, the account that made it.
                        if (report["objetivo_id"] != str(user_id)) if report["tipo"] == "usuario" else (report["autor_id"] != user_id):
                            raise SanctionReportError("other_target")
                    cur.execute(
                        "INSERT INTO sancion (id, usuario_id, tipo, motivo, inicio, fin, admin_id, reporte_id) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                        (sanction_id, user_id, tipo, motivo, now, fin, admin_id, report_id))
                    row = cur.fetchone()
                # Same transaction: if the audit row fails, the sanction is not saved.
                insert_admin_audit(conn, AdminAuditEntry(
                    actor_user_id=admin_id, action=AuditAction.SANCTION_APPLIED,
                    resource_type=AuditResourceType.USER, resource_id=str(user_id), motivo=motivo,
                    metadata={"sancion_id": str(sanction_id), "tipo": tipo, "dias": dias,
                              "reporte_id": None if report_id is None else str(report_id)},
                ))
        except psycopg.errors.UniqueViolation as error:
            raise SanctionConflictError(user_id) from error
        except psycopg.Error as error:
            raise DependencyUnavailableError("sanction or audit storage unavailable") from error
        return self._as_sanction(row)

    def lift_sanction(
        self, sanction_id: UUID, admin_id: UUID, motivo: str, now: datetime
    ) -> tuple[SanctionRecord, bool] | None:
        try:
            with self._pool.connection() as conn, conn.transaction():
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute("SELECT * FROM sancion WHERE id = %s FOR UPDATE", (sanction_id,))
                    current = cur.fetchone()
                    if current is None:
                        return None
                    record = self._as_sanction(current)
                    if not record.in_force_at(now):  # already lifted, or it ended by itself
                        return record, False
                    cur.execute(
                        "UPDATE sancion SET estado = 'levantada', levantada_por = %s, levantada_en = %s, "
                        "motivo_levantamiento = %s WHERE id = %s RETURNING *", (admin_id, now, motivo, sanction_id))
                    row = cur.fetchone()
                insert_admin_audit(conn, AdminAuditEntry(
                    actor_user_id=admin_id, action=AuditAction.SANCTION_LIFTED,
                    resource_type=AuditResourceType.USER, resource_id=str(record.usuario_id), motivo=motivo,
                    metadata={"sancion_id": str(sanction_id), "tipo": record.tipo},
                ))
        except psycopg.Error as error:
            raise DependencyUnavailableError("sanction or audit storage unavailable") from error
        return self._as_sanction(row), True

    def get_sanction(self, sanction_id: UUID) -> SanctionRecord | None:
        try:
            with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT * FROM sancion WHERE id = %s", (sanction_id,))
                row = cur.fetchone()
        except psycopg.Error as error:
            raise DependencyUnavailableError("sanction storage unavailable") from error
        return None if row is None else self._as_sanction(row)

    def list_sanctions(
        self, estado: str | None, user_id: UUID | None, limit: int, offset: int, now: datetime
    ) -> list[SanctionRecord]:
        where, params = ["TRUE"], {"now": now, "limit": limit, "offset": offset}
        if estado == "activa":
            where.append("estado = 'activa' AND (fin IS NULL OR fin > %(now)s)")
        elif estado == "vencida":
            where.append("estado = 'activa' AND fin IS NOT NULL AND fin <= %(now)s")
        elif estado == "levantada":
            where.append("estado = 'levantada'")
        if user_id is not None:
            where.append("usuario_id = %(user)s")
            params["user"] = user_id
        try:
            with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                cur.execute(f"SELECT * FROM sancion WHERE {' AND '.join(where)} "
                            "ORDER BY fecha_creacion DESC, id DESC LIMIT %(limit)s OFFSET %(offset)s", params)
                return [self._as_sanction(row) for row in cur.fetchall()]
        except psycopg.Error as error:
            raise DependencyUnavailableError("sanction storage unavailable") from error

    def add_notification(self, user_id: UUID, tipo: str, titulo: str, mensaje: str, data: dict) -> None:
        try:
            with self._pool.connection() as conn, conn.transaction():
                notice_id = uuid4()
                conn.execute(
                    "INSERT INTO notificacion (id, usuario_id, tipo, titulo, mensaje, data) VALUES (%s, %s, %s, %s, %s, %s)",
                    (notice_id, user_id, tipo, titulo, mensaje, Jsonb(data)))
                # Push / in-app inbox of the gateway (schema created by the gateway). Best effort: if it is
                # missing or not writable the saved notice above still stands.
                if conn.execute("SELECT to_regclass(%s)", (self._push_inbox,)).fetchone()[0] is not None:
                    try:
                        with conn.transaction():
                            conn.execute(
                                sql.SQL("INSERT INTO {} (id, source, source_id, user_id, kind, title, body, data, created_at) "
                                        "VALUES (%s, 'users', hashtextextended(%s, 0), %s, %s, %s, %s, %s, CURRENT_TIMESTAMP) "
                                        "ON CONFLICT DO NOTHING").format(sql.Identifier(*self._push_inbox.split("."))),
                                (notice_id, str(notice_id), user_id, tipo, titulo, mensaje, Jsonb(data)))
                    except psycopg.Error:
                        logger.warning("push inbox not available for notice %s", notice_id)
        except psycopg.Error as error:
            raise DependencyUnavailableError("notification storage unavailable") from error

    def record_admin_audit(self, entry: AdminAuditEntry) -> None:
        try:
            with self._pool.connection() as conn, conn.transaction():
                insert_admin_audit(conn, entry)
        except psycopg.Error as error:
            raise DependencyUnavailableError("audit storage unavailable") from error

    def list_admin_audit(self, query: AuditQuery) -> tuple[list[AuditRecord], int]:
        # Only administrative actions: the closed catalog plus the report rows written before HU-48.
        where = ["a.action = ANY(%(catalog)s)"]
        params: dict = {"catalog": _CATALOG_RAW, "limit": query.por_pagina,
                        "offset": (query.pagina - 1) * query.por_pagina}
        if query.desde:
            where.append("a.fecha >= %(desde)s")
            params["desde"] = query.desde
        if query.hasta:
            where.append("a.fecha <= %(hasta)s")
            params["hasta"] = query.hasta
        if query.admin_id:
            where.append("a.actor_user_id = %(admin)s")
            params["admin"] = query.admin_id
        if query.accion:
            where.append("a.action = ANY(%(accion)s)")
            params["accion"] = [query.accion.value, *_LEGACY_BY_ACTION.get(query.accion.value, [])]
        if query.resultado:
            where.append("a.result = %(resultado)s")
            params["resultado"] = query.resultado
        if query.recurso_tipo:
            legacy = " OR (a.resource_type IS NULL AND a.resource LIKE 'reports/%%')"                 if query.recurso_tipo is AuditResourceType.REPORT else ""
            where.append(f"(a.resource_type = %(rtype)s{legacy})")
            params["rtype"] = query.recurso_tipo.value
        if query.recurso_id:
            where.append("COALESCE(a.resource_id, CASE WHEN a.resource LIKE 'reports/%%' "
                         "THEN substr(a.resource, 9) END) = %(rid)s")
            params["rid"] = query.recurso_id
        clause = " AND ".join(where)
        try:
            with self._pool.connection() as conn:
                total = conn.execute(f"SELECT count(*) FROM audit_events a WHERE {clause}", params).fetchone()[0]
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(
                        f"SELECT a.id, a.actor_user_id, a.action, a.resource, a.fecha, a.result, a.motivo, "
                        f"a.resource_type, a.resource_id, a.metadata FROM audit_events a WHERE {clause} "
                        f"ORDER BY a.fecha DESC, a.id DESC LIMIT %(limit)s OFFSET %(offset)s", params)
                    rows = cur.fetchall()
                affected = {r["resource_id"] for r in rows if r["resource_type"] == "user"}
                ids = list({r["actor_user_id"] for r in rows} | {UUID(i) for i in affected})
                names = {} if not ids else {
                    row[0]: display_name(row[1], row[2]) for row in conn.execute(
                        "SELECT id, nombre, apellido_paterno FROM usuario WHERE id = ANY(%s)", (ids,))
                }
        except psycopg.Error as error:
            raise DependencyUnavailableError("audit storage unavailable") from error
        records = []
        for r in rows:
            rtype, rid = r["resource_type"], r["resource_id"]
            if rtype is None and r["resource"].startswith("reports/"):
                rtype, rid = "report", r["resource"][len("reports/"):]
            records.append(AuditRecord(
                id=r["id"], fecha=r["fecha"], admin_id=r["actor_user_id"], admin_nombre=names.get(r["actor_user_id"]),
                action=LEGACY_ACTIONS[r["action"]].value if r["action"] in LEGACY_ACTIONS else r["action"],
                resource_type=rtype, resource_id=rid,
                afectado_nombre=names.get(UUID(rid)) if rtype == "user" else None,
                motivo=r["motivo"], result=r["result"], metadata=r["metadata"],
            ))
        return records, total

    def _as_report(self, row: dict) -> ReportRecord:
        return ReportRecord(
            id=row["id"], reportante_id=row["reportante_id"], tipo=row["tipo"],
            objetivo_id=row["objetivo_id"], objetivo_nombre=row["objetivo_nombre"],
            motivo=row["motivo"], detalle=row["detalle"], estado=row["estado"],
            fecha_creacion=row["fecha_creacion"], fecha_revision=row["fecha_revision"],
            reportante_rol=row["reportante_rol"], revisado_por=row.get("revisado_por"),
            motivo_revision=row.get("motivo_revision"), autor_id=row.get("autor_id"),
            autor_consultado_en=row.get("autor_consultado_en"),
        )

    def create_password_reset(
        self, *, user_id: UUID, code_hash: str, expires_at: datetime
    ) -> PasswordResetRecord:
        with self._pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "UPDATE password_reset_token SET used_at = CURRENT_TIMESTAMP "
                    "WHERE usuario_id = %s AND used_at IS NULL",
                    (user_id,),
                )
                cur.execute(
                    """
                    INSERT INTO password_reset_token (id, usuario_id, code_hash, expires_at)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id, usuario_id, code_hash, expires_at, created_at, attempts
                    """,
                    (uuid4(), user_id, code_hash, expires_at),
                )
                row = cur.fetchone()
        return self._as_password_reset(row)

    def get_active_password_reset(self, user_id: UUID) -> PasswordResetRecord | None:
        with self._pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT id, usuario_id, code_hash, expires_at, created_at, attempts
                    FROM password_reset_token
                    WHERE usuario_id = %s AND used_at IS NULL
                      AND expires_at > CURRENT_TIMESTAMP
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (user_id,),
                )
                row = cur.fetchone()
        return None if row is None else self._as_password_reset(row)

    def register_password_reset_attempt(self, reset_id: UUID, max_attempts: int) -> bool:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE password_reset_token SET attempts = attempts + 1
                    WHERE id = %s AND used_at IS NULL AND attempts < %s
                      AND expires_at > CURRENT_TIMESTAMP
                    """,
                    (reset_id, max_attempts),
                )
                return cur.rowcount > 0

    def complete_password_reset(
        self, *, reset_id: UUID, user_id: UUID, password_hash: bytes
    ) -> bool:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE password_reset_token SET used_at = CURRENT_TIMESTAMP
                    WHERE id = %s AND usuario_id = %s AND used_at IS NULL
                      AND expires_at > CURRENT_TIMESTAMP
                    """,
                    (reset_id, user_id),
                )
                if cur.rowcount == 0:
                    return False
                cur.execute(
                    "UPDATE usuario SET password = %s, "
                    "fecha_actualizacion = CURRENT_TIMESTAMP "
                    "WHERE id = %s AND is_active = TRUE",
                    (password_hash.decode("utf-8"), user_id),
                )
                return cur.rowcount > 0

    def get_pending_email_verification(self, user_id: UUID) -> EmailVerificationRecord | None:
        with self._pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT usuario_id, code_hash, expires_at, sent_at, attempts
                    FROM email_verificacion
                    WHERE usuario_id = %s AND verified_at IS NULL
                    """,
                    (user_id,),
                )
                row = cur.fetchone()
        if row is None:
            return None
        return EmailVerificationRecord(
            user_id=row["usuario_id"],
            code_hash=row["code_hash"],
            expires_at=row["expires_at"],
            sent_at=row["sent_at"],
            attempts=row["attempts"],
        )

    def restart_email_verification(
        self, *, user_id: UUID, code_hash: str, expires_at: datetime
    ) -> bool:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE email_verificacion
                    SET code_hash = %s, expires_at = %s, attempts = 0,
                        sent_at = CURRENT_TIMESTAMP
                    WHERE usuario_id = %s AND verified_at IS NULL
                    """,
                    (code_hash, expires_at, user_id),
                )
                return cur.rowcount > 0

    def register_email_verification_attempt(self, user_id: UUID, max_attempts: int) -> bool:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE email_verificacion SET attempts = attempts + 1
                    WHERE usuario_id = %s AND verified_at IS NULL AND attempts < %s
                      AND expires_at > CURRENT_TIMESTAMP
                    """,
                    (user_id, max_attempts),
                )
                return cur.rowcount > 0

    def complete_email_verification(self, *, user_id: UUID, code_hash: str) -> bool:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE email_verificacion SET verified_at = CURRENT_TIMESTAMP
                    WHERE usuario_id = %s AND code_hash = %s AND verified_at IS NULL
                      AND expires_at > CURRENT_TIMESTAMP
                    """,
                    (user_id, code_hash),
                )
                return cur.rowcount > 0

    def _get_user_or_raise(self, user_id: UUID) -> StoredUser:
        user = self.get_user(user_id)
        if user is None:
            raise KeyError(user_id)
        return user

    def _replace_deportes(
        self, cur: psycopg.Cursor, user_id: UUID, deportes: list[UserSport]
    ) -> None:
        cur.execute("DELETE FROM usuario_deporte WHERE usuario_id = %s", (user_id,))
        for deporte in deportes:
            cur.execute(
                """
                INSERT INTO usuario_deporte (id, usuario_id, deporte_codigo, nivel)
                VALUES (%s, %s, %s, %s)
                """,
                (uuid4(), user_id, deporte.deporte_codigo, deporte.nivel),
            )

    def _replace_disponibilidad(
        self, cur: psycopg.Cursor, user_id: UUID, disponibilidad: list[AvailabilityWindow]
    ) -> None:
        cur.execute("DELETE FROM disponibilidad WHERE usuario_id = %s", (user_id,))
        for window in disponibilidad:
            cur.execute(
                """
                INSERT INTO disponibilidad (id, usuario_id, dia_semana, hora_inicio, hora_fin)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (uuid4(), user_id, window.dia_semana, window.hora_inicio, window.hora_fin),
            )

    def _replace_objetivos_zona(
        self, cur: psycopg.Cursor, user_id: UUID, preferences: PreferencesReplace
    ) -> None:
        zona = preferences.zona
        if not preferences.objetivos and zona is None:
            cur.execute("DELETE FROM preferencia_perfil WHERE usuario_id = %s", (user_id,))
            return
        cur.execute(
            """
            INSERT INTO preferencia_perfil (usuario_id, objetivos, comuna, latitud, longitud)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (usuario_id) DO UPDATE SET
                objetivos = EXCLUDED.objetivos,
                comuna = EXCLUDED.comuna,
                latitud = EXCLUDED.latitud,
                longitud = EXCLUDED.longitud,
                fecha_actualizacion = CURRENT_TIMESTAMP
            """,
            (
                user_id,
                preferences.objetivos,
                zona.comuna if zona else None,
                zona.latitud if zona else None,
                zona.longitud if zona else None,
            ),
        )

    def _as_zona(self, row: dict | None) -> Zona | None:
        if row is None or row["comuna"] is None:
            return None
        return Zona(
            comuna=row["comuna"],
            latitud=None if row["latitud"] is None else float(row["latitud"]),
            longitud=None if row["longitud"] is None else float(row["longitud"]),
        )

    def _as_password_reset(self, row: dict) -> PasswordResetRecord:
        return PasswordResetRecord(
            id=row["id"],
            user_id=row["usuario_id"],
            code_hash=row["code_hash"],
            expires_at=row["expires_at"],
            created_at=row["created_at"],
            attempts=row["attempts"],
        )
