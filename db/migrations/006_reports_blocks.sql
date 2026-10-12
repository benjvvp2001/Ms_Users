-- Bloqueos y reportes (HU-42 y moderación HU-43).
-- Un bloqueo oculta a ambas personas entre sí en Descubrir; no toca matches,
-- chats ni actividades. Un reporte queda pendiente hasta que un administrador
-- lo marca como revisado o descartado. Para usuarios y actividades, objetivo_id
-- es su UUID; para mensajes es "<match>:<mensaje>".
-- Idempotente: se puede aplicar varias veces sin perder datos.
BEGIN;
SELECT pg_advisory_xact_lock(hashtextextended('sportmatch-users-schema', 0));

CREATE TABLE IF NOT EXISTS bloqueo_usuario (
    bloqueador_id UUID NOT NULL REFERENCES usuario(id) ON DELETE CASCADE,
    bloqueado_id UUID NOT NULL REFERENCES usuario(id) ON DELETE CASCADE,
    fecha_creacion TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (bloqueador_id, bloqueado_id),
    CONSTRAINT bloqueo_usuario_no_a_si_mismo CHECK (bloqueador_id <> bloqueado_id)
);
CREATE INDEX IF NOT EXISTS bloqueo_usuario_bloqueado ON bloqueo_usuario(bloqueado_id);

CREATE TABLE IF NOT EXISTS reporte (
    id UUID PRIMARY KEY,
    reportante_id UUID REFERENCES usuario(id) ON DELETE SET NULL,
    tipo VARCHAR(20) NOT NULL,
    objetivo_id VARCHAR(120) NOT NULL,
    objetivo_nombre VARCHAR(200) NOT NULL,
    motivo VARCHAR(60) NOT NULL,
    detalle VARCHAR(300) NOT NULL DEFAULT '',
    estado VARCHAR(20) NOT NULL DEFAULT 'pendiente',
    fecha_creacion TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    fecha_revision TIMESTAMPTZ,
    revisado_por UUID REFERENCES usuario(id) ON DELETE SET NULL,
    CONSTRAINT reporte_tipo_valido CHECK (tipo IN ('usuario', 'actividad', 'mensaje')),
    CONSTRAINT reporte_estado_valido CHECK (estado IN ('pendiente', 'revisado', 'descartado'))
);
-- El mismo motivo sobre lo mismo cuenta una sola vez por persona.
CREATE UNIQUE INDEX IF NOT EXISTS reporte_unico ON reporte(reportante_id, tipo, objetivo_id, motivo);
CREATE INDEX IF NOT EXISTS reporte_por_estado ON reporte(estado, fecha_creacion DESC);

COMMIT;
