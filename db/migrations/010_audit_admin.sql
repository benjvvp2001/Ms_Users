-- HU-48: auditoria de acciones administrativas sobre la tabla audit_events existente.
-- Solo agrega columnas opcionales: las filas anteriores (login, perfil, etc.) no se tocan y
-- record_audit sigue funcionando igual. Una accion administrativa nueva llena ademas el motivo,
-- el tipo y el id del recurso afectado y datos extra acotados.
-- Idempotente: se puede aplicar varias veces sin perder datos.
BEGIN;
SELECT pg_advisory_xact_lock(hashtextextended('sportmatch-users-schema', 0));

ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS motivo VARCHAR(300);
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS resource_type VARCHAR(30);
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS resource_id VARCHAR(120);
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS metadata JSONB;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'audit_events_motivo_valido'
                   AND conrelid = 'audit_events'::regclass) THEN
        ALTER TABLE audit_events ADD CONSTRAINT audit_events_motivo_valido
            CHECK (motivo IS NULL OR btrim(motivo) <> '');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'audit_events_resource_type_valido'
                   AND conrelid = 'audit_events'::regclass) THEN
        ALTER TABLE audit_events ADD CONSTRAINT audit_events_resource_type_valido
            CHECK (resource_type IS NULL OR resource_type IN
                   ('user', 'report', 'plan', 'membership', 'payment', 'club', 'activity', 'message'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'audit_events_resource_id_valido'
                   AND conrelid = 'audit_events'::regclass) THEN
        ALTER TABLE audit_events ADD CONSTRAINT audit_events_resource_id_valido
            CHECK (resource_id IS NULL OR (resource_type IS NOT NULL AND btrim(resource_id) <> ''));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'audit_events_metadata_acotada'
                   AND conrelid = 'audit_events'::regclass) THEN
        ALTER TABLE audit_events ADD CONSTRAINT audit_events_metadata_acotada
            CHECK (metadata IS NULL OR (jsonb_typeof(metadata) = 'object' AND octet_length(metadata::text) <= 2048));
    END IF;
END $$;

-- Consultas del historial: por fecha, por admin (ya existe audit_events_actor_fecha_idx), por accion y por recurso.
CREATE INDEX IF NOT EXISTS audit_events_fecha_idx ON audit_events (fecha DESC, id DESC);
CREATE INDEX IF NOT EXISTS audit_events_action_fecha_idx ON audit_events (action, fecha DESC);
CREATE INDEX IF NOT EXISTS audit_events_resource_idx ON audit_events (resource_type, resource_id)
    WHERE resource_type IS NOT NULL;

COMMIT;
