from __future__ import annotations

from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.repositories.base import StoredUser
from app.schemas.user import (
    AvailabilityWindow,
    ConsentCreate,
    ConsentRead,
    PreferencesReplace,
    ProfileReplace,
    UserSport,
)


class PostgresUserRepository:
    """PostgreSQL-backed repository for the users_db schema (rol, usuario,
    preferencia_usuario, disponibilidad, usuario_deporte, consent, audit_events).
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
        )

    def get_user(self, user_id: UUID) -> StoredUser | None:
        with self._pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT u.id, u.email, u.password, u.rut, u.nombre,
                           u.apellido_paterno, u.apellido_materno, u.fecha_nacimiento,
                           u.telefono, u.foto_perfil, u.biografia, r.nombre AS role
                    FROM usuario u
                    JOIN rol r ON r.id = u.rol_id
                    WHERE u.id = %s
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
                rango_distancia_km=preferences_row["rango_distancia_km"],
                rango_edad_min=preferences_row["rango_edad_min"],
                rango_edad_max=preferences_row["rango_edad_max"],
                mismo_nivel=preferences_row["mismo_nivel"],
                disponibilidad_match=preferences_row["disponibilidad_match"],
            ),
            role=user_row["role"],
            consents=consents,
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
                cur.execute("DELETE FROM usuario WHERE id = %s", (user_id,))
                return cur.rowcount > 0

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
