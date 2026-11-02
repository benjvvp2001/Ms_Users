-- HU-45: suspensiones temporales o permanentes de cuentas, y la tabla de notificaciones de la cuenta.
-- No toca usuario.is_active (eso es de las cuentas eliminadas): una sancion es un registro aparte.
-- "Activa" se calcula en cada chequeo comparando fin con la hora actual; no depende de un job. Una
-- temporal ya vencida sigue con estado 'activa' en la fila hasta que se aplique otra sancion a la misma
-- cuenta (ahi se cierra sola) o se levante.
-- Idempotente: se puede aplicar varias veces sin perder datos.
BEGIN;
SELECT pg_advisory_xact_lock(hashtextextended('sportmatch-users-schema', 0));

CREATE TABLE IF NOT EXISTS sancion (
    id UUID PRIMARY KEY,
    usuario_id UUID NOT NULL REFERENCES usuario(id) ON DELETE CASCADE,
    tipo VARCHAR(10) NOT NULL,
    motivo VARCHAR(300) NOT NULL,
    inicio TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    fin TIMESTAMPTZ,
    admin_id UUID REFERENCES usuario(id) ON DELETE SET NULL,
    reporte_id UUID REFERENCES reporte(id) ON DELETE SET NULL,
    estado VARCHAR(10) NOT NULL DEFAULT 'activa',
    levantada_por UUID REFERENCES usuario(id) ON DELETE SET NULL,
    levantada_en TIMESTAMPTZ,
    motivo_levantamiento VARCHAR(300),
    fecha_creacion TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT sancion_tipo_valido CHECK (tipo IN ('temporal', 'permanente')),
    CONSTRAINT sancion_estado_valido CHECK (estado IN ('activa', 'levantada')),
    CONSTRAINT sancion_motivo_no_vacio CHECK (btrim(motivo) <> ''),
    CONSTRAINT sancion_fin_segun_tipo CHECK (
        (tipo = 'temporal' AND fin IS NOT NULL AND fin > inicio) OR (tipo = 'permanente' AND fin IS NULL)),
    CONSTRAINT sancion_levantamiento_coherente CHECK (
        (estado = 'activa' AND levantada_en IS NULL AND motivo_levantamiento IS NULL)
        OR (estado = 'levantada' AND levantada_en IS NOT NULL))
);
-- Una sola sancion activa por cuenta (las vencidas se cierran antes de aplicar otra).
CREATE UNIQUE INDEX IF NOT EXISTS sancion_una_activa ON sancion (usuario_id) WHERE estado = 'activa';
CREATE INDEX IF NOT EXISTS sancion_por_usuario ON sancion (usuario_id, fecha_creacion DESC);
CREATE INDEX IF NOT EXISTS sancion_por_estado ON sancion (estado, fecha_creacion DESC);

-- Avisos a la persona (motivo y duracion de una sancion, etc.). El correo y el push usan los mecanismos existentes.
CREATE TABLE IF NOT EXISTS notificacion (
    id UUID PRIMARY KEY,
    usuario_id UUID NOT NULL REFERENCES usuario(id) ON DELETE CASCADE,
    tipo VARCHAR(40) NOT NULL,
    titulo VARCHAR(120) NOT NULL,
    mensaje VARCHAR(600) NOT NULL,
    data JSONB,
    leida_en TIMESTAMPTZ,
    fecha_creacion TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS notificacion_por_usuario ON notificacion (usuario_id, fecha_creacion DESC);

COMMIT;
