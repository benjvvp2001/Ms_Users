-- Verificación de correo al registrarse: código de un solo uso enviado por correo.
-- Una fila por cuenta creada desde esta migración. Las cuentas sin fila (creadas
-- antes) se consideran verificadas. Solo se guarda un HMAC del código.
-- Es una tabla aparte porque `usuario` pertenece a otro rol y la aplicación no
-- puede agregarle columnas.
-- Idempotente: se puede aplicar varias veces sin perder datos.
BEGIN;
SELECT pg_advisory_xact_lock(hashtextextended('sportmatch-users-schema', 0));

CREATE TABLE IF NOT EXISTS email_verificacion (
    usuario_id UUID PRIMARY KEY REFERENCES usuario(id) ON DELETE CASCADE,
    code_hash VARCHAR(64) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    attempts SMALLINT NOT NULL DEFAULT 0,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    verified_at TIMESTAMPTZ,
    CONSTRAINT email_verificacion_attempts_validos CHECK (attempts >= 0)
);

COMMIT;
