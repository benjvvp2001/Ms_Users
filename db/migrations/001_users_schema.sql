-- SportMatch users service: PostgreSQL schema
-- Run while connected to the users_db database as sportmatch_users.
--
-- Ownership boundaries (agreed with the team):
--   * matching owns ubicacion_usuario, match, solicitud_match.
--   * activities owns actividad, comunidad, inscripcion_actividad.
--   * clubs owns club, membresia, pago.
--   * users (this file) owns everything below: usuario, rol,
--     preferencia_usuario, disponibilidad, usuario_deporte, consent,
--     audit_events.
--
-- No table here has a foreign key into another service's tables.
-- usuario_deporte stores the sport as a free-text code (deporte_codigo)
-- instead of referencing a "deporte" catalog, because that catalog belongs
-- to another service's database.

BEGIN;

CREATE TABLE IF NOT EXISTS rol (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    nombre VARCHAR(30) UNIQUE NOT NULL,
    descripcion VARCHAR(150)
);

INSERT INTO rol (nombre, descripcion) VALUES
    ('player', 'Usuario que busca partidos (match) por defecto'),
    ('club_admin', 'Administra un club especifico; permisos acotados a ese club'),
    ('admin', 'Administrador del servicio de usuarios')
ON CONFLICT (nombre) DO NOTHING;

CREATE TABLE IF NOT EXISTS usuario (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rut VARCHAR(20) UNIQUE,
    rol_id UUID NOT NULL REFERENCES rol(id),
    nombre VARCHAR(50) NOT NULL,
    apellido_paterno VARCHAR(50) NOT NULL,
    apellido_materno VARCHAR(50),
    email VARCHAR(100) UNIQUE NOT NULL,
    password VARCHAR(255) NOT NULL,
    fecha_nacimiento DATE,
    telefono VARCHAR(20),
    foto_perfil VARCHAR(255),
    biografia VARCHAR(500),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    fecha_creacion TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    fecha_actualizacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS preferencia_usuario (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    usuario_id UUID UNIQUE NOT NULL REFERENCES usuario(id) ON DELETE CASCADE,
    rango_distancia_km VARCHAR(10),
    rango_edad_min VARCHAR(5),
    rango_edad_max VARCHAR(5),
    mismo_nivel BOOLEAN,
    disponibilidad_match BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS disponibilidad (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    usuario_id UUID NOT NULL REFERENCES usuario(id) ON DELETE CASCADE,
    dia_semana VARCHAR(15) NOT NULL,
    hora_inicio VARCHAR(10) NOT NULL,
    hora_fin VARCHAR(10) NOT NULL
);

-- Deportes declarados por el usuario. Sin FK a un catalogo "deporte": ese
-- catalogo pertenece al microservicio matching, no a users.
CREATE TABLE IF NOT EXISTS usuario_deporte (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    usuario_id UUID NOT NULL REFERENCES usuario(id) ON DELETE CASCADE,
    deporte_codigo VARCHAR(50) NOT NULL,
    nivel SMALLINT NOT NULL,
    CONSTRAINT usuario_deporte_codigo_no_vacio CHECK (btrim(deporte_codigo) <> ''),
    CONSTRAINT usuario_deporte_nivel_valido CHECK (nivel BETWEEN 1 AND 5)
);

CREATE UNIQUE INDEX IF NOT EXISTS usuario_deporte_usuario_codigo_unico
    ON usuario_deporte (usuario_id, lower(deporte_codigo));

-- Tabla de Consentimientos (obligatoria por la Ley N.º 21.719).
CREATE TABLE IF NOT EXISTS consent (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES usuario(id) ON DELETE CASCADE,
    type VARCHAR(50) NOT NULL,
    purpose VARCHAR(255) NOT NULL,
    document_version VARCHAR(20) NOT NULL,
    granted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at TIMESTAMP,
    method VARCHAR(50) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'active'
);

-- Auditoria de accesos y cambios sobre datos personales (misma ley).
-- Sin FK: el evento debe sobrevivir aunque la cuenta se elimine despues.
CREATE TABLE IF NOT EXISTS audit_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_user_id UUID NOT NULL,
    action VARCHAR(80) NOT NULL,
    resource VARCHAR(255) NOT NULL,
    fecha TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    result VARCHAR(30) NOT NULL,
    CONSTRAINT audit_events_action_no_vacio CHECK (btrim(action) <> ''),
    CONSTRAINT audit_events_resource_no_vacio CHECK (btrim(resource) <> ''),
    CONSTRAINT audit_events_result_no_vacio CHECK (btrim(result) <> '')
);

CREATE INDEX IF NOT EXISTS audit_events_actor_fecha_idx
    ON audit_events (actor_user_id, fecha DESC);

COMMIT;
