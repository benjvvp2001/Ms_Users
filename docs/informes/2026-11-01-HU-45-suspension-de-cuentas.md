# HU-45: suspensión y sanción de cuentas

**Fecha:** 2026-11-01
**Entorno de la verificación:** local aislado (Postgres `127.0.0.1:5433`, gateway `127.0.0.1:8000`, compose
`compose.yaml` + `compose.windows.yaml` con el perfil `clubs`). Nada contra producción.

## 1. Endpoints y reglas

Todos van por el gateway bajo `/api/v1/users/admin/sanctions` y exigen rol `admin` (si no, 403).

| Método y ruta | Qué hace |
|---|---|
| `POST /admin/sanctions` | Aplica una sanción. Cuerpo: `usuario_id`, `tipo` (`temporal` / `permanente`), `dias` (1 a 365, solo temporal), `motivo` (3 a 300), `reporte_id` opcional (debe ser un reporte escalado de esa misma cuenta). 201. |
| `GET /admin/sanctions` | Lista, más reciente primero. Filtros `estado` (`activa`, `levantada`, `vencida`), `usuario_id`, `pagina`, `limit` (máx. 100). |
| `GET /admin/sanctions/{id}` | Una sanción. |
| `POST /admin/sanctions/{id}/lift` | Levanta la sanción con un `motivo`. Idempotente (levantar una ya levantada o vencida no cambia nada). |

Reglas:

- Un admin no puede sancionarse a sí mismo ni a otro admin (422). Una cuenta con sanción vigente no admite otra (409;
  índice único parcial `sancion_una_activa`).
- "Vigente" se calcula en cada chequeo: `estado = 'activa' AND (fin IS NULL OR fin > ahora)`. Una temporal vence sola,
  sin job ni cambio de estado; figura como `vencida` en las consultas.
- La persona sancionada recibe un aviso con motivo y duración (tabla `notificacion`, correo y push por los mecanismos
  existentes). Un aviso que falle se registra pero no deshace la sanción.
- Cómo se aplica la restricción, sin depender de la expiración del JWT:
  1. Login y emisión de token (Ms_Users): 403 `account_suspended`.
  2. Cada petición que llega a Ms_Users con un token existente (`get_current_user`).
  3. Resto de servicios y chat: el gateway pregunta a Ms_Users `GET /internal/v1/accounts/{id}/status` con el secreto
     `INTERNAL_SERVICE_TOKEN` en `X-Internal-Service-Token`. Ese endpoint no está declarado en el gateway.
- Respuesta 403: `{detail, code: "account_suspended", tipo, motivo, fin}` (`fin` nulo si es permanente). Nada sobre
  quién reportó ni quién sancionó. En el chat WebSocket el cierre es el código `4423`.
- Los admins no se consultan (no pueden ser sancionados).
- Caché del gateway: 15 s por cuenta (`ACCOUNT_STATUS_TTL_SECONDS`). Se vacía cuando el gateway reenvía con éxito una
  aplicación o un levantamiento, así que en ese gateway el cambio es inmediato.

### Si Ms_Users no responde (comportamiento del gateway)

| Situación | Respuesta | ¿Deja pasar a un sancionado conocido? |
|---|---|---|
| Caché vigente (< 15 s) | Se usa la caché (403 si estaba sancionada) | No |
| Caché vencida pero dentro de 30 min (`ACCOUNT_STATUS_STALE_SECONDS`) | Se usa la última respuesta conocida y se registra una advertencia | No: si era sancionada sigue 403 |
| Caché de más de 30 min | 503 | No |
| Sin caché | 503 | No |
| Respuesta de error de Ms_Users (401, 403, 404, 500, 503) o cuerpo inválido | Cuenta como "no sé": igual que arriba | No |
| Se aplica o levanta una sanción por el gateway | La caché se vacía; con Ms_Users caído queda "sin caché" | No (503) |

El único hueco es de otra naturaleza: una cuenta cuya última respuesta conocida (de hasta 30 min) era "libre" y que
fue sancionada justo mientras Ms_Users estaba caído pasa hasta que se recupere o venza ese margen. No es un
sancionado conocido. Ver pendientes.

### Falla cerrada sin secreto (corregido en esta verificación)

Antes: con `USERS_SERVICE_URL` y sin `INTERNAL_SERVICE_TOKEN`, el gateway no creaba el guard y dejaba pasar a las
cuentas sancionadas, con solo un `warning` al arrancar.

Ahora: con Ms_Users configurado y sin secreto (o con el valor en blanco), el gateway instala un `ClosedAccountGuard`.
Toda ruta autenticada de un no-admin responde 503 ("No se pudo verificar el estado de la cuenta"), el chat se cierra
con `1013`, y al arrancar se registra un `error` explícito. Las rutas públicas (login, registro, salud, directorio de
clubes) y los admins siguen funcionando.

Por qué esta opción y no abortar el arranque: abortar tumbaría también las rutas públicas, la salud del gateway y a
los admins, que son justo quienes pueden resolver el incidente, y rompería los entornos que arrancan el gateway solo
para otros servicios. El 503 deja el gateway visible y diagnosticable, sin dejar pasar a nadie que necesite el chequeo.
No hay ninguna variable que desactive el chequeo.

## 2. Migración 012 y notificaciones

`Ms_Users/db/migrations/012_sanciones.sql` (idempotente, con `pg_advisory_xact_lock`):

- `sancion`: `id`, `usuario_id` (FK `usuario`, cascade), `tipo`, `motivo`, `inicio`, `fin`, `admin_id`, `reporte_id`,
  `estado` (`activa` / `levantada`), `levantada_por`, `levantada_en`, `motivo_levantamiento`, `fecha_creacion`.
  Restricciones: tipo válido, motivo no vacío, `fin` obligatorio y posterior a `inicio` solo en temporales, coherencia
  de los campos de levantamiento. Índices: una sola activa por cuenta (parcial), por usuario y por estado.
- `notificacion`: avisos a la persona (`tipo`, `titulo`, `mensaje`, `data` JSONB, `leida_en`). El gateway la expone
  en `GET /api/v1/notifications` y `POST /api/v1/notifications/read`.
- No toca `usuario.is_active` (eso es de cuentas eliminadas).
- Una temporal vencida conserva `estado = 'activa'` en la fila hasta que se aplique otra sanción a esa cuenta (se
  cierra sola) o se levante.
- Auditoría: `SANCTION_APPLIED` y `SANCTION_LIFTED` en `audit_events`, en la misma transacción que el cambio (si la
  auditoría no se puede escribir, no se guarda la sanción). Guardan motivo, `resource_type = user`, y en `metadata`
  solo `tipo`, `dias`, `reporte_id` y `sancion_id`.
- Avisos creados: "Tu cuenta fue suspendida" (motivo, tipo y fin) y "Se levantó la suspensión de tu cuenta".

## 3. Archivos

Ms_Users: `db/migrations/012_sanciones.sql`, `app/schemas/sanction.py`, `app/services/sanction_service.py`,
`app/api/internal.py` (estado de la cuenta), `app/api/v1/users.py` (rutas admin), `app/api/dependencies.py`
(`ensure_not_suspended`), `app/repositories/{base,mock_user_repository,postgres_user_repository}.py`,
`app/services/user_service.py` (login y token), `app/schemas/audit.py`, `app/database/migrate.py`.
Tests: `tests/test_sanctions.py` (23), `tests/test_sanctions_postgres.py` (6).

Gateway: `gateway/app/services/account_guard.py`, `gateway/app/api/dependencies/security.py`,
`gateway/app/api/routes/chat_socket.py`, `gateway/app/api/routes/users.py` (rutas y vaciado de caché),
`gateway/app/core/config.py`, `gateway/app/main.py`, `compose.yaml`, `.env.example`.
Tests: `gateway/tests/test_account_guard.py`.

Cambiados en esta verificación (sin commit):

- `Ms_gateway/gateway/app/services/account_guard.py`: nueva clase `ClosedAccountGuard`.
- `Ms_gateway/gateway/app/main.py`: usa `ClosedAccountGuard` y registra un `error` si falta el secreto.
- `Ms_gateway/gateway/app/core/config.py`: comentario actualizado.
- `Ms_gateway/.env.example`: el secreto figura como obligatorio.
- `Ms_gateway/gateway/tests/test_account_guard.py`: el test que afirmaba "sin secreto el chequeo está apagado" pasó a
  afirmar la falla cerrada; 5 tests nuevos (ver sección 5).
- `Ms_gateway/gateway/tests/test_users_proxy.py` y `test_clubs_proxy.py`: sus fixtures de proxy apagan el guard de forma
  explícita (esas pruebas son de reenvío, no de HU-45); `proxy_client(..., account_check=True)` conserva el guard real.
- `Ms_gateway/.env` (solo pruebas): se agregó `INTERNAL_SERVICE_TOKEN` (64 caracteres aleatorios, no versionar).

## 4. Cambios en app y web

App móvil (`Frontend-SportMatch-APP`):

- `services/api.ts`: el error HTTP expone `code` y los datos de la sanción; `CUENTA_SUSPENDIDA = 'account_suspended'`
  y un aviso global para quien quiera enterarse.
- `app/_layout.tsx`: ante `account_suspended` muestra "Cuenta suspendida" con motivo y duración y cierra la sesión.
- `services/sanciones.ts`, `components/SancionesAdmin.tsx` y `app/(admin)/home.tsx`: vista de sanciones para admin: lista,
  formulario para sancionar (desde un reporte escalado) y levantamiento.
- `scripts/test-sanciones.cjs` (`npm run test:sanciones`; no se ejecutó en esta verificación).

Web (`sportmatch-web`): solo existe el cliente `lib/sanciones.ts`. No encontré una pantalla de administración de
sanciones ni el manejo de `account_suspended` en la web (por confirmar; ver pendientes).

## 5. Decisiones y pendientes

Decisiones:

- Falla cerrada con 503 (y no abortar el arranque) cuando falta el secreto; ver sección 1.
- Los admins no se consultan en el gateway: no pueden ser sancionados, y así un fallo de Ms_Users no bloquea a quien
  debe poder levantar sanciones.
- Un chequeo que falla cuenta como "no sé" y nunca como "libre". Se tolera la última respuesta conocida por 30 min (la
  vida de un token).
- El chat WebSocket abierto se revisa cada ciclo de caché; si Ms_Users no responde mientras tanto el socket sigue
  abierto (ya pasó el chequeo al conectar) y se registra una advertencia.
- `GET /api/v1/clubs` (directorio) es público por diseño: una cuenta suspendida lo ve igual que un visitante.

Pendientes:

1. ~~Defecto previo en la auditoría (no es de HU-45): `GET /api/v1/users/admin/audit-events` respondía 500 con cualquier
   fila porque `audit_events.fecha` era `TIMESTAMP` sin zona.~~ **Corregido el 2026-11-02** con la migración
   `013_audit_fecha_timestamptz.sql` (idempotente; convierte con `AT TIME ZONE 'UTC'` porque las filas se escribieron
   con `CURRENT_TIMESTAMP` en un servidor UTC, y se detiene si la base no está en UTC). Probado con tests contra
   Postgres real y en el stack.
2. Cuenta sancionada mientras Ms_Users está caído y con respuesta "libre" en caché: pasa hasta 30 min o hasta que
   Ms_Users vuelva. Decidir si se acorta `ACCOUNT_STATUS_STALE_SECONDS` o se cierran las rutas sensibles.
3. Varias réplicas del gateway: el vaciado de caché es por réplica; las demás ven el cambio en hasta 15 s.
4. Falla cerrada comprobada con tests; no se levantó un contenedor del gateway sin secreto para verla en vivo.
5. Correo real y push no verificados: el entorno usa `EMAIL_BACKEND=console` y `PUSH_ENABLED=false`.
6. Web: definir si hace falta pantalla de sanciones para admin y el aviso de `account_suspended` (hoy solo hay cliente).
7. `tests/test_sanctions_postgres.py` no se ejecutó en esta verificación (requiere la base `qa_tests`; los corre el
   equipo).
8. Despliegues: `INTERNAL_SERVICE_TOKEN` es ahora obligatorio en el gateway y debe ser el mismo valor en Ms_Users.
   Revisar el `.env` de cada ambiente antes de desplegar este cambio.

## Resultados de la verificación

### Tests del gateway

`pytest` en `Ms_gateway/gateway`: **219 pasan, 11 omitidos, 0 fallos** (línea base antes del cambio: 214 y 11).
Tests nuevos en `test_account_guard.py`: falla cerrada sin secreto (HTTP, con log de error), secreto en blanco, chat
rechazado sin secreto (1013), Ms_Users caído con sancionada conocida (403 / 403 / 503), con "libre" antiguo o sin
caché (200 / 503 / 503), y sanción aplicada con Ms_Users caído (503, nunca 200).

### Stack real

Imágenes de `ms_users` y `gateway` reconstruidas y stack levantado con los dos compose y el perfil `clubs`. Los dos
servicios tienen el secreto (64 caracteres, mismo valor); migración 012 aplicada; el gateway no registró el error de
falta de secreto.

### Prueba de humo (45 PASS, 0 FAIL)

Cuentas nuevas: admin, admin2, deportista A, deportista B y C (esta última para el caso "sin caché"). Rutas probadas
con el token de A: perfil y bloqueos (users), `matching/state`, `chats`, `activities`, `activities/chats`,
`notifications`, `clubs/canales/me`, `clubs/notices/me` (9 rutas) y el WebSocket del chat.

| Paso | Resultado |
|---|---|
| Línea base | A y B: 9 rutas con 200; WebSocket entra |
| Temporal (1 día) a A | 201. Con el MISMO token: 9 rutas con 403 `account_suspended` (cuerpo con `detail`, `code`, `tipo`, `motivo`, `fin`); WebSocket cerrado con 4423; B sigue con 200 y su WebSocket entra; A no puede iniciar sesión (403) |
| Aviso | Creado con tipo `sancion`, título, mensaje con fecha de fin y motivo, y `data` con motivo y fin |
| Duplicada / sin permiso | Segunda sanción activa: 409. Un deportista intentando sancionar: 403 |
| Levantar | 200; A recupera las 9 rutas y el WebSocket en 0,2 s con el mismo token (la caché se vació); levantar otra vez es idempotente; A inicia sesión; aviso de levantamiento creado |
| Vencimiento simulado | Nueva temporal, `fin` movido al pasado por SQL: A vuelve solo tras el TTL; la sanción figura `vencida` |
| Permanente | 201 con `fin` nulo; 9 rutas con 403, mensaje "permanente"; WebSocket 4423; duplicada: 409; admin se sanciona a sí mismo: 422; admin sanciona a otro admin: 422; una sola vigente en la BD; listado sin correo, RUT ni foto |
| Ms_Users detenido | Sancionada con caché vigente: 403. Cuenta sin caché: 503. Caché vencida dentro del margen: 403. Ninguno dejó pasar a una sancionada |
| Levantar permanente | 200 y acceso inmediato |
| Auditoría (tabla) | 3 `SANCTION_APPLIED` y 2 `SANCTION_LIFTED` (la temporal vencida no se levanta), todas con motivo y `resource_type = user`; `metadata` solo con `tipo`, `dias`, `reporte_id`, `sancion_id`; sin correos, contraseñas ni tokens |

Hallazgos de la prueba: `GET /clubs` da 200 a una cuenta suspendida porque es público por diseño (se cambió la ruta de
la prueba a `/clubs/canales/me`); el historial de auditoría por la API responde 500 (pendiente 1).

### Limpieza

Cuentas desechables borradas (con sus sanciones y avisos por cascada), 24 filas de auditoría de esas cuentas y 10
avisos huérfanos de `notifications.inbox` creados durante las pruebas. El conteo de filas de las 5 bases (`sportmach_users`,
`sportmach_matching`, `sportmach_activities`, `sportmatch_clubs`, `qa_tests`) quedó idéntico al de antes; `qa_tests`
sigue sin tablas. No se tocaron las cuentas de las pruebas QA anteriores. `INTERNAL_SERVICE_TOKEN` se dejó en
`Ms_gateway/.env` porque el stack lo necesita (sin él, el gateway ahora responde 503).
