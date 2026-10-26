-- Rol de la cuenta que reporta, guardado al momento del reporte (un club queda como club_admin).
-- La columna es nullable: los reportes anteriores no tienen rol registrado y no se modifican.
-- Idempotente: se puede aplicar varias veces sin perder datos.
BEGIN;
SELECT pg_advisory_xact_lock(hashtextextended('sportmatch-users-schema', 0));

ALTER TABLE reporte ADD COLUMN IF NOT EXISTS reportante_rol VARCHAR(20);

COMMIT;
