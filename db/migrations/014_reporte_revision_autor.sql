-- HU-47 (panel web): lo que el panel de reportes necesita y la tabla no guardaba.
--
-- reporte
--   * motivo_revision: la nota que escribió el administrador al resolver el reporte (opcional). Quién lo resolvió
--     (revisado_por) y cuándo (fecha_revision) ya existían; ahora también se devuelven. Las filas ya resueltas
--     quedan con la nota vacía: no se inventa.
--   * autor_id: la cuenta que hizo el contenido reportado, para los reportes de una actividad o de un mensaje (en los
--     reportes de una persona, la persona reportada ya es objetivo_id). Se averigua preguntando al servicio dueño del
--     dato (Ms_Activities o Ms_Matching) y queda guardado: así se puede sancionar a esa cuenta aunque el contenido
--     se borre después. NULL = no se pudo averiguar (todavía).
--   * autor_consultado_en: cuándo se preguntó por última vez, para no repetir la pregunta por un contenido que ya no
--     existe (por ejemplo, reportes de pruebas). NULL = aún no se preguntó.
--
-- Idempotente: se puede aplicar varias veces sin perder datos.
BEGIN;
SELECT pg_advisory_xact_lock(hashtextextended('sportmatch-users-schema', 0));

ALTER TABLE reporte ADD COLUMN IF NOT EXISTS motivo_revision VARCHAR(300);
ALTER TABLE reporte ADD COLUMN IF NOT EXISTS autor_id UUID REFERENCES usuario(id) ON DELETE SET NULL;
ALTER TABLE reporte ADD COLUMN IF NOT EXISTS autor_consultado_en TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'reporte_motivo_revision_largo'
                   AND conrelid = 'reporte'::regclass) THEN
        ALTER TABLE reporte ADD CONSTRAINT reporte_motivo_revision_largo
            CHECK (motivo_revision IS NULL OR length(btrim(motivo_revision)) >= 3);
    END IF;
    -- Solo un reporte de una actividad o de un mensaje tiene "autor" aparte: el de una persona ya la nombra.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'reporte_autor_solo_contenido'
                   AND conrelid = 'reporte'::regclass) THEN
        ALTER TABLE reporte ADD CONSTRAINT reporte_autor_solo_contenido
            CHECK (autor_id IS NULL OR tipo IN ('actividad', 'mensaje'));
    END IF;
END $$;

-- Los reportes que todavía faltan por averiguar (se buscan al listar).
CREATE INDEX IF NOT EXISTS reporte_autor_pendiente ON reporte (fecha_creacion DESC)
    WHERE autor_id IS NULL AND tipo IN ('actividad', 'mensaje');

COMMIT;
