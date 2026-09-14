-- Compatibilidad con sportmach_users ya existente. Conserva los IDs,
-- perfiles, notificaciones, planes, membresías y pagos actuales.
-- Las preferencias de USERS son independientes de las de MATCHING.
BEGIN;
SELECT pg_advisory_xact_lock(hashtextextended('sportmatch-users-schema', 0));

-- El contrato del microservicio permite un RUT opcional de hasta 20 caracteres.
ALTER TABLE usuario ALTER COLUMN rut TYPE VARCHAR(20);
ALTER TABLE usuario ALTER COLUMN rut DROP NOT NULL;

-- No se cambia el tipo ni los IDs de rol: funciona con integer o UUID.
INSERT INTO rol (nombre, descripcion)
VALUES ('player', 'Deportista: rol por defecto del microservicio de usuarios')
ON CONFLICT (nombre) DO NOTHING;

-- El repositorio normaliza email a minúsculas; la base debe garantizar lo mismo.
CREATE UNIQUE INDEX IF NOT EXISTS usuario_email_lower_unique ON usuario (lower(email));

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

CREATE TABLE IF NOT EXISTS consent (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES usuario(id) ON DELETE CASCADE,
    type VARCHAR(50) NOT NULL,
    purpose VARCHAR(255) NOT NULL,
    document_version VARCHAR(20) NOT NULL,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at TIMESTAMPTZ,
    method VARCHAR(50) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS audit_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_user_id UUID NOT NULL,
    action VARCHAR(80) NOT NULL,
    resource VARCHAR(255) NOT NULL,
    fecha TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    result VARCHAR(30) NOT NULL,
    CONSTRAINT audit_events_action_no_vacio CHECK (btrim(action) <> ''),
    CONSTRAINT audit_events_resource_no_vacio CHECK (btrim(resource) <> ''),
    CONSTRAINT audit_events_result_no_vacio CHECK (btrim(result) <> '')
);

CREATE INDEX IF NOT EXISTS audit_events_actor_fecha_idx
    ON audit_events (actor_user_id, fecha DESC);

COMMIT;
