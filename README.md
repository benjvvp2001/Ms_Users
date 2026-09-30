# SportMatch users service

Integrado con el gateway hermano `../Ms_gateway`. La ejecución conjunta y sus pruebas están en [la guía de integración](../Ms_gateway/README.md). El despliegue local utiliza `USERS_DATABASE_URL` (base `sportmach_users` o `users_db`).

La eliminación de cuentas en PostgreSQL es lógica (`is_active=false`): conserva las referencias existentes y bloquea el acceso posterior.

Microservicio de usuarios de SportMatch. Expone exclusivamente rutas bajo
/api/v1/users y persiste en la base de datos PostgreSQL propia del servicio
(ver [db/README.md](db/README.md)). Las pruebas automatizadas siguen usando
un repositorio mock en memoria para no depender de una base real.

## Alcance

- Registro e inicio de sesión con JWT.
- Recuperación de contraseña con un código de 6 dígitos enviado por correo
  (SMTP, cualquier proveedor).
- Perfil, rol único y preferencias (incluye deportes declarados y
  disponibilidad horaria).
- Consentimientos versionados, exportación de datos y eliminación de cuenta.
- Autorización en el servidor: el propietario del recurso o un administrador.
- Auditoría persistida (tabla audit_events) para los accesos y cambios de
  datos personales, requerida por la Ley N.º 21.719.

Las conexiones/matches entre usuarios NO se manejan aquí: las resuelve el
microservicio matching, típicamente vía un gateway que solicita los UUID
relevantes a users para comparar contra su propia base de datos.

Los nombres JSON son snake_case, los IDs son UUID v4 y los errores usan
{"detail": "mensaje"}.

## Reglas de cuenta (compartidas con la app)

Las constantes están en `app/schemas/user.py`. La app
(`Frontend-SportMatch-APP/services/validators.ts`) debería aplicar las mismas;
hoy todavía acepta contraseñas de 8 caracteres y nombres de hasta 40 (pendiente
del equipo de frontend).

| Campo | Regla |
|---|---|
| `email` | Formato válido, máx. 100 caracteres; se guarda en minúsculas. |
| `password` / `new_password` | 12 a 64 caracteres, con mayúscula, minúscula, número y carácter especial, sin espacios. El login acepta cualquier contraseña de hasta 128 caracteres (cuentas antiguas). |
| `nombre`, `apellido_paterno` | Obligatorios, 2 a 50 letras (tildes, ñ, espacio, apóstrofo o guion entre palabras). |
| `apellido_materno` | Opcional, misma regla; `""` se guarda como `null`. |
| `rut` | Opcional en la API (la app lo exige). Se acepta `12.345.678-5` o `12345678-5`, se valida el dígito verificador y se guarda como `12345678-5`. |

`PUT /{id}/profile` no aplica la regla de letras para no bloquear la edición de
cuentas antiguas; solo los límites de largo.

## Ejecutar

Se necesita Python 3.11 o posterior.

1. Crear y activar un entorno virtual.
2. Instalar el servicio y sus dependencias de desarrollo con:

   pip install -e ".[dev]"

3. Definir un secreto de JWT largo y aleatorio y la cadena de conexión a
   PostgreSQL en el entorno (ver [db/README.md](db/README.md) para crear la
   base de datos primero). En PowerShell:

   $env:JWT_SECRET = "reemplaza-esto-por-un-secreto-largo-y-aleatorio"
   $env:USERS_DATABASE_URL = "postgresql://sportmach_users:tu-password@localhost:5432/sportmach_users"

4. Arrancar el servicio:

   uvicorn app.main:app --env-file .env --port 8001 --reload

5. Ejecutar pruebas (usan un repositorio mock, no requieren base de datos):

   pytest

No se proporciona un secreto por defecto y no se registran contraseñas,
tokens, ubicaciones ni otros datos sensibles.

## Base de datos PostgreSQL

El esquema del servicio está en [db/README.md](db/README.md). Solo contiene
las tablas que pertenecen a users y usa UUID v4; no es una copia de la base
monolítica de referencia. Para la base existente `sportmach_users`, la migración
`db/migrations/002_sportmach_users_integration.sql` agrega las tablas que faltan y
conserva los datos. `users_migrate` la ejecuta desde Compose. No ejecutes
`db/00_create_database.sql` para esta integración.

## Contrato v1

Todas las rutas usan el prefijo /api/v1/users:

- POST /auth/register
- POST /auth/email-verification/request
- POST /auth/email-verification/confirm
- POST /auth/login
- POST /auth/password-reset/request
- POST /auth/password-reset/confirm
- GET /suggestions
- GET y PUT /{id}/profile
- GET /{id}/roles
- GET y PUT /{id}/preferences
- GET y POST /{id}/consents
- DELETE /{id}/consents/{consent_id}
- GET /{id}/exports
- DELETE /{id}

## Deportistas sugeridos

`GET /suggestions?limit=20` (requiere JWT; `limit` entre 1 y 50) devuelve las
cards de la app: otros jugadores activos con el correo verificado, **nunca
quien consulta** ni cuentas `admin`/`club_admin`. Cada card expone solo datos
públicos:

```json
{"user_id": "...", "nombre": "Diego", "apellido_inicial": "A.", "edad": 27,
 "foto_perfil": null, "biografia": "...",
 "deportes": [{"deporte_codigo": "tennis", "nivel": 3}], "compatibilidad": 50}
```

No incluye correo, RUT, teléfono, fecha de nacimiento ni apellidos completos.
`compatibilidad` es provisoria: el porcentaje de deportes en común (compartidos
÷ distintos entre los dos; 0 si alguno no declaró deportes). Se ordena por
compatibilidad y, en empate, por registro más reciente. Se consideran los 200
registros más recientes. Cuando exista el servicio de matching, la
compatibilidad y la distancia deberían venir de ahí.

## Verificación de correo

Una cuenta nueva queda **inactiva hasta verificar su correo**:

1. `POST /auth/register` responde `201` **sin token**:
   `{"detail": "...", "email_verification_required": true, "user": {...}}`, y
   envía al correo un código de 6 dígitos (en segundo plano, después de responder).
2. `POST /auth/login` con una cuenta sin verificar responde `403`
   (`email not verified`). Se comprueba después de la contraseña, así que una
   contraseña incorrecta sigue dando el `401` genérico.
3. `POST /auth/email-verification/confirm` con `{"email": "...", "code": "123456"}`
   activa la cuenta y responde `200` con el mismo cuerpo que el login
   (`access_token`, `user`), así la app entra directo. Responde `400`
   (`invalid or expired verification code`) si el código es incorrecto, venció,
   ya se usó o se superó `EMAIL_VERIFICATION_MAX_ATTEMPTS`.
4. `POST /auth/email-verification/request` con `{"email": "..."}` envía un
   código nuevo, que reemplaza al anterior y reinicia los intentos. Siempre
   responde `202` con el mismo mensaje (cuenta inexistente, ya verificada o
   pedido antes de `EMAIL_VERIFICATION_RESEND_SECONDS`), para no revelar qué
   correos existen.

El código vence a los `EMAIL_VERIFICATION_CODE_MINUTES` minutos (30 por
defecto). En la base (`email_verificacion`, migración
`004_email_verification.sql`) solo se guarda su HMAC-SHA256, con un prefijo
distinto al de la recuperación de contraseña para que un código no sirva para
el otro flujo. Las cuentas creadas antes de esta migración no tienen fila y se
consideran verificadas. Registro, reenvíos y confirmaciones quedan en
`audit_events`.

## Recuperación de contraseña

1. `POST /auth/password-reset/request` con `{"email": "..."}`. Siempre
   responde `202` con el mismo mensaje, exista o no la cuenta, para no revelar
   qué correos están registrados. Si la cuenta existe y está activa, se envía
   al correo ingresado un código de 6 dígitos (en segundo plano, después de
   responder). Si se pide otro antes de `PASSWORD_RESET_RESEND_SECONDS`, no se
   reenvía.
2. `POST /auth/password-reset/confirm` con
   `{"email": "...", "code": "123456", "new_password": "..."}` (mínimo 12
   caracteres). Responde `200` si el código es válido; `400`
   (`invalid or expired reset code`) si es incorrecto, venció, ya se usó o se
   superó `PASSWORD_RESET_MAX_ATTEMPTS`.

El código vence a los `PASSWORD_RESET_CODE_MINUTES` minutos, es de un solo uso
y en la base (`password_reset_token`, migración `003_password_reset.sql`) solo
se guarda su HMAC-SHA256 con `JWT_SECRET`. Pedir un código nuevo invalida el
anterior. Solicitudes y confirmaciones quedan en `audit_events`.

Configuración del correo (ver `.env.example`):

| Variable | Uso |
|---|---|
| `EMAIL_BACKEND` | `smtp` (por defecto) o `console` (imprime el correo en el log; solo desarrollo). |
| `SMTP_HOST` / `SMTP_PORT` | Servidor SMTP. Gmail: `smtp.gmail.com:587`. Outlook: `smtp.office365.com:587`. |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | Credenciales. En Gmail se usa una "contraseña de aplicación". |
| `SMTP_USE_STARTTLS` / `SMTP_USE_SSL` | `STARTTLS` en 587 (por defecto) o SSL directo en 465. |
| `EMAIL_FROM` | Remitente; por defecto `SMTP_USERNAME`. |
| `PASSWORD_RESET_CODE_MINUTES` / `_MAX_ATTEMPTS` / `_RESEND_SECONDS` | Recuperación de contraseña: 15 min, 5 intentos, 60 s. |
| `EMAIL_VERIFICATION_CODE_MINUTES` / `_MAX_ATTEMPTS` / `_RESEND_SECONDS` | Verificación de correo: 30 min, 5 intentos, 60 s. |

Si un correo no sale, el log del servicio muestra
`email '<asunto>' could not be delivered` con el detalle del error; el cliente
no lo ve.

Limitación conocida: cambiar la contraseña no revoca los JWT ya emitidos;
vencen solos según `JWT_ACCESS_TOKEN_MINUTES`.

La documentación interactiva de la integración está en el gateway: `http://localhost:8000/docs`. El microservicio también expone `/api/v1/users/health/ready` para comprobar su conexión a PostgreSQL.
