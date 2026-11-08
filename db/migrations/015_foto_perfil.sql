-- HU-61: foto de perfil guardada en el backend.
--
-- usuario.foto_perfil ya existe (VARCHAR(255), desde la migración 001). Desde ahora guarda SOLO la clave de un archivo
-- que Ms_Users generó al recibir la foto (32 caracteres hexadecimales aleatorios + .jpg o .png); la API la devuelve
-- como la ruta /api/v1/users/photos/<clave>. Ningún cliente puede escribir otro valor: PUT /{id}/profile ya no cambia
-- este campo.
--
--   * Los valores anteriores (texto libre que alguien pudo guardar con el PUT del perfil) no apuntan a ningún archivo y
--     la API ya los entrega como "sin foto"; aquí se dejan en NULL para que la base diga lo mismo que la API.
--   * Un CHECK impide que vuelvan a entrar. Si el rol de la aplicación no es dueño de la tabla (ver la migración 002),
--     no se puede agregar: emite un WARNING y sigue; la validación de la aplicación basta.
--
-- Idempotente: se puede aplicar varias veces sin perder datos.
BEGIN;
SELECT pg_advisory_xact_lock(hashtextextended('sportmatch-users-schema', 0));

UPDATE usuario
SET foto_perfil = NULL
WHERE foto_perfil IS NOT NULL
  AND foto_perfil !~ '^[0-9a-f]{32}\.(jpg|png)$';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'usuario_foto_perfil_clave' AND conrelid = 'usuario'::regclass
    ) THEN
        ALTER TABLE usuario ADD CONSTRAINT usuario_foto_perfil_clave
            CHECK (foto_perfil IS NULL OR foto_perfil ~ '^[0-9a-f]{32}\.(jpg|png)$');
    END IF;
EXCEPTION WHEN insufficient_privilege THEN
    RAISE WARNING 'No se pudo agregar usuario_foto_perfil_clave: el rol no es dueño de usuario';
END
$$;

COMMIT;
