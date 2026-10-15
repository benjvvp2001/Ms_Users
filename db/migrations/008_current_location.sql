-- Ubicación actual del deportista, para recomendar personas cercanas.
-- La zona fija del perfil (preferencia_perfil, 005) no cambia: sirve de respaldo cuando no hay una posición
-- reciente. Esta tabla guarda solo la ÚLTIMA posición de cada persona (una fila), nunca un historial.
-- Las coordenadas no salen de este servicio: las tarjetas solo muestran una distancia redondeada.
-- Idempotente: se puede aplicar varias veces sin perder datos.
BEGIN;
SELECT pg_advisory_xact_lock(hashtextextended('sportmatch-users-schema', 0));

CREATE TABLE IF NOT EXISTS ubicacion_actual (
    usuario_id UUID PRIMARY KEY REFERENCES usuario(id) ON DELETE CASCADE,
    latitud NUMERIC(8, 5) NOT NULL CHECK (latitud BETWEEN -90 AND 90),
    longitud NUMERIC(8, 5) NOT NULL CHECK (longitud BETWEEN -180 AND 180),
    precision_m INTEGER CHECK (precision_m IS NULL OR precision_m >= 0),
    actualizada_en TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ubicacion_actual_actualizada ON ubicacion_actual(actualizada_en);

COMMIT;
