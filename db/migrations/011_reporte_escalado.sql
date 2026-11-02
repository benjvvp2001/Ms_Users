-- HU-47: un reporte puede quedar "escalado" (listo para una sancion, HU-45).
-- Solo amplia los estados permitidos; no toca ninguna fila. Idempotente: se puede aplicar varias veces.
BEGIN;
SELECT pg_advisory_xact_lock(hashtextextended('sportmatch-users-schema', 0));

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'reporte_estado_valido'
               AND conrelid = 'reporte'::regclass
               AND pg_get_constraintdef(oid) NOT LIKE '%escalado%') THEN
        ALTER TABLE reporte DROP CONSTRAINT reporte_estado_valido;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'reporte_estado_valido'
                   AND conrelid = 'reporte'::regclass) THEN
        ALTER TABLE reporte ADD CONSTRAINT reporte_estado_valido
            CHECK (estado IN ('pendiente', 'revisado', 'descartado', 'escalado'));
    END IF;
END $$;

COMMIT;
