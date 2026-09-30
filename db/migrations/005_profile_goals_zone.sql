-- Objetivos y zona del perfil deportivo (historia "Editar mi perfil").
-- Una fila por usuario que los haya definido; sin fila = sin objetivos ni zona.
-- Es una tabla aparte porque `preferencia_usuario` pertenece a otro rol y la
-- aplicación no puede agregarle columnas.
-- Las coordenadas son opcionales y nunca se muestran a otros usuarios.
-- Idempotente: se puede aplicar varias veces sin perder datos.
BEGIN;
SELECT pg_advisory_xact_lock(hashtextextended('sportmatch-users-schema', 0));

CREATE TABLE IF NOT EXISTS preferencia_perfil (
    usuario_id UUID PRIMARY KEY REFERENCES usuario(id) ON DELETE CASCADE,
    objetivos VARCHAR(50)[] NOT NULL DEFAULT '{}',
    comuna VARCHAR(80),
    latitud NUMERIC(8, 5),
    longitud NUMERIC(8, 5),
    fecha_actualizacion TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT preferencia_perfil_max_objetivos CHECK (cardinality(objetivos) <= 5),
    CONSTRAINT preferencia_perfil_coordenadas_completas
        CHECK ((latitud IS NULL) = (longitud IS NULL)),
    CONSTRAINT preferencia_perfil_coordenadas_con_comuna
        CHECK (comuna IS NOT NULL OR latitud IS NULL)
);

COMMIT;
