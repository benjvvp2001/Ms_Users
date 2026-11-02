-- HU-48 (defecto): audit_events.fecha debe ser TIMESTAMPTZ.
-- La migracion 001 creo la tabla con "fecha TIMESTAMP" (sin zona) y la 002 la declara TIMESTAMPTZ, pero con
-- CREATE TABLE IF NOT EXISTS: en una base creada por la 001 la 002 no cambia nada. El codigo (AuditEventRead.fecha
-- es AwareDatetime) espera timestamptz, y GET /api/v1/users/admin/audit-events respondia 500 con cualquier fila.
--
-- Las filas existentes se escribieron siempre con el DEFAULT CURRENT_TIMESTAMP (ningun codigo pasa fecha), con la
-- zona de la sesion; el servidor y la aplicacion corren en UTC. Por eso se convierte con AT TIME ZONE 'UTC': el
-- instante de cada fila no cambia. Si la base NO esta en UTC la migracion se detiene en vez de adivinar la zona:
-- hay que revisar en que zona se escribieron las filas y ajustar USING antes de aplicarla.
-- Idempotente: solo altera si la columna sigue siendo "timestamp without time zone"; se aplica en cada arranque.
-- Reconstruye por si solo los indices que usan la columna (audit_events_actor_fecha_idx, audit_events_fecha_idx,
-- audit_events_action_fecha_idx) y conserva el DEFAULT CURRENT_TIMESTAMP.
BEGIN;
SELECT pg_advisory_xact_lock(hashtextextended('sportmatch-users-schema', 0));

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'audit_events' AND column_name = 'fecha'
          AND data_type = 'timestamp without time zone'
    ) THEN
        IF current_setting('TimeZone') NOT IN ('UTC', 'Etc/UTC', 'Etc/UCT', 'UCT', 'Zulu', 'Etc/Zulu') THEN
            RAISE EXCEPTION
                'audit_events.fecha es TIMESTAMP sin zona y TimeZone=% no es UTC: verifique en que zona se escribieron las filas antes de convertirla',
                current_setting('TimeZone');
        END IF;
        ALTER TABLE audit_events ALTER COLUMN fecha TYPE TIMESTAMPTZ USING fecha AT TIME ZONE 'UTC';
    END IF;
END $$;

COMMIT;
