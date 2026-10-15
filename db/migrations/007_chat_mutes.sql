-- HU-51: silenciar personas. Quien silencia deja de recibir sus notificaciones de chat,
-- pero la persona sigue pudiendo escribirle (es lo que lo distingue de un bloqueo).
-- Los bloqueos ya existen en bloqueo_usuario (006); no se duplican.
-- Idempotente: se puede aplicar varias veces sin perder datos.
BEGIN;
SELECT pg_advisory_xact_lock(hashtextextended('sportmatch-users-schema', 0));

CREATE TABLE IF NOT EXISTS silencio_usuario (
    silenciador_id UUID NOT NULL REFERENCES usuario(id) ON DELETE CASCADE,
    silenciado_id UUID NOT NULL REFERENCES usuario(id) ON DELETE CASCADE,
    fecha_creacion TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (silenciador_id, silenciado_id),
    CONSTRAINT silencio_usuario_no_a_si_mismo CHECK (silenciador_id <> silenciado_id)
);
CREATE INDEX IF NOT EXISTS silencio_usuario_silenciado ON silencio_usuario(silenciado_id);

COMMIT;
