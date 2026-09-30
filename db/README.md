# PostgreSQL database for the users service

## Integración actual con sportmach

La base activa es `sportmach_users` en PostgreSQL local:5432. La variable canónica es `USERS_DATABASE_URL` (`DATABASE_URL` se acepta como alias para instalaciones anteriores). La migración `002_sportmach_users_integration.sql` conserva el esquema existente y añade las tablas de esta API. La migración `003_password_reset.sql` agrega `password_reset_token` (códigos de recuperación de contraseña, solo su hash). La migración `004_email_verification.sql` agrega `email_verificacion` (una fila por cuenta registrada desde entonces, con el hash del código y `verified_at`; las cuentas sin fila se consideran verificadas). Es una tabla aparte porque `usuario` pertenece a otro rol y la aplicación no puede agregarle columnas. `python -m app.database.migrate` aplica todas y es idempotente. La 002 solo altera lo que falta (tipo de `rut`, índices), porque `ALTER TABLE` y `CREATE INDEX` exigen ser dueño de la tabla y el rol de la aplicación normalmente no lo es; si algo falta y no hay permisos, emite un `WARNING` y continúa. No se crea una segunda base `users_db`.

El rol existente de `rol.id` es integer y se conserva; el repositorio también funciona con los UUID del esquema original del compañero. Las tablas de preferencias de esta API son independientes de las de matching. Los planes, membresías y pagos existentes siguen en USERS y no se trasladan.

Las instrucciones siguientes documentan el diseño original para una instalación nueva; para esta integración sigue [la guía del gateway](../../Ms_gateway/README.md).

Este esquema es el que efectivamente pertenece al microservicio de usuarios,
acordado con el equipo a partir del modelo monolítico de referencia
(`Sportmach.sql`):

- `ubicacion_usuario`, `match`, `solicitud_match` pasan a **matching**.
- `actividad`, `comunidad`, `inscripcion_actividad` pasan a **activities**.
- `club`, `membresia`, `pago` pasan a **clubs**.
- Todo lo demás relacionado a `usuario` (rol, preferencias, disponibilidad,
  deportes declarados, consentimientos, auditoría) se queda en **users**.

Reglas de diseño:

- UUID v4 como identificador principal (`usuario.id`). El `rut` se conserva
  como columna única, pero ya no es llave primaria ni se usa para relacionar
  tablas de otros servicios.
- Ninguna tabla de `users` tiene una foreign key hacia una tabla de otro
  microservicio. Cuando se necesita referenciar un concepto de otro dominio
  (por ejemplo, qué deporte juega un usuario), se guarda como texto libre
  (`usuario_deporte.deporte_codigo`), no como FK a un catálogo ajeno.
- Un usuario tiene un único rol (`usuario.rol_id`), no una lista. Los roles
  base son `player` (usuario que busca match), `club_admin` (administra un
  club específico, con permisos acotados a ese club) y `admin`
  (administrador del servicio de usuarios).
- Las conexiones/matches entre usuarios NO se almacenan en `users`: las
  resuelve el microservicio `matching`, típicamente a través de un gateway
  que pide los UUID relevantes a `users` para comparar contra su propia
  base de datos.
- Se guardan hashes de contraseña, nunca contraseñas en texto plano.
- Las tablas `consent` y `audit_events` existen porque la API de usuarios
  las requiere para cumplir la Ley N.º 21.719 de protección de datos
  personales.

## 1. Crear la base de datos

Abre el Query Tool de PostgreSQL conectado a la base `postgres` como
administrador. En `db/00_create_database.sql`, reemplaza el password de
ejemplo por uno largo y aleatorio, y ejecuta el archivo una vez. Esto crea
el rol `sportmatch_users` y la base `users_db`.

## 2. Crear el esquema de usuarios

Reconéctate usando el rol `sportmatch_users` y la base `users_db`. Ejecuta:

    db/migrations/001_users_schema.sql

Esto crea las tablas `rol`, `usuario`, `preferencia_usuario`,
`disponibilidad`, `usuario_deporte`, `consent` y `audit_events`, todas en el
esquema `public`.

## 3. Configurar la aplicación

Copia la forma de DATABASE_URL de `.env.example` a un archivo `.env` local
(o expórtala como variable de entorno) y reemplaza el password por el que
elegiste para el rol `sportmatch_users`. El servicio abre un pool de
conexiones a esta base al arrancar y persiste cada solicitud a través de
`app.repositories.postgres_user_repository.PostgresUserRepository`.
