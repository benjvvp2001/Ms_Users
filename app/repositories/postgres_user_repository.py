from __future__ import annotations

from pathlib import Path
from psycopg.types.json import Jsonb
from datetime import datetime
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.repositories.base import (
    ATHLETE_ROLES,
    EmailVerificationRecord,
    NewEmailVerification,
    PasswordResetRecord,
    StoredUser,
    SuggestionCandidate,
)
from app.schemas.user import (
    AvailabilityWindow,
    ConsentCreate,
    ConsentRead,
    PreferencesReplace,
    ProfileReplace,
    UserSport,
    SuggestionFilters,
    Zona,
)


class PostgresUserRepository:
    """PostgreSQL-backed repository for the sportmach_users schema (rol, usuario,
    preferencia_usuario, disponibilidad, usuario_deporte, consent, audit_events,
    password_reset_token, email_verificacion, preferencia_perfil).
    """

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

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
    ) -> StoredUser:
        user_id = uuid4()
        normalized_email = email.casefold()
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT id FROM rol WHERE nombre = 'player'")
                    role_row = cur.fetchone()
                    if role_row is None:
                        raise RuntimeError("default 'player' role is missing from rol")
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
            role="player",
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
                cur.execute(
                    "UPDATE usuario SET is_active = FALSE, "
                    "fecha_actualizacion = CURRENT_TIMESTAMP "
                    "WHERE id = %s AND is_active = TRUE", (user_id,)
                )
                return cur.rowcount > 0

    def list_suggestion_candidates(
        self, *, exclude_user_id: UUID, limit: int, filters: SuggestionFilters,
        origin: Zona | None, sports: list[UserSport]
    ) -> list[SuggestionCandidate]:
        params = {
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
