-- Recuperación de contraseña: códigos de un solo uso enviados por correo.
-- Solo se guarda un HMAC del código, nunca el código en texto plano.
-- Idempotente: se puede aplicar varias veces sin perder datos.
BEGIN;
SELECT pg_advisory_xact_lock(hashtextextended('sportmatch-users-schema', 0));

CREATE TABLE IF NOT EXISTS password_reset_token (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    usuario_id UUID NOT NULL REFERENCES usuario(id) ON DELETE CASCADE,
    code_hash VARCHAR(64) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    attempts SMALLINT NOT NULL DEFAULT 0,
    used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT password_reset_token_attempts_validos CHECK (attempts >= 0)
);

CREATE INDEX IF NOT EXISTS password_reset_token_usuario_activo_idx
    ON password_reset_token (usuario_id, created_at DESC)
    WHERE used_at IS NULL;

COMMIT;
