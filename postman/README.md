# Pruebas Postman — microservicio de usuarios (Postgres real)

Esta carpeta contiene la suite de pruebas end-to-end del microservicio,
ejecutada contra la base de datos PostgreSQL real (`users_db`), no el
repositorio mock que usa `pytest`.

## Archivos

- `SportMatch-Users.postman_collection.json` — la colección (27 requests en
  9 carpetas, cubre todo el contrato `/api/v1/users`).
- `SportMatch.postman_environment.json` — el entorno (`base_url`, credenciales
  de las cuentas de prueba, variables que se llenan solas al correr).
- `reports/newman-report.html` — reporte visual de la última corrida (abrir
  con cualquier navegador).
- `reports/newman-report.json` — el mismo resultado en JSON, por si se quiere
  procesar o adjuntar a un ticket.
- `reports/SportMatch.postman_environment.result.json` — el entorno con los
  valores (tokens/ids) que quedaron seteados al final de la última corrida.

## Cómo verlo en la app de Postman

1. Abre Postman → File → Import → arrastra los dos `.json` de esta carpeta
   (la colección y el entorno).
2. Selecciona el entorno "SportMatch users service - local" arriba a la
   derecha.
3. Asegúrate de que el servicio esté corriendo en `http://127.0.0.1:8000`
   contra Postgres (ver "Cómo levantar el servicio" abajo).
4. Abre la colección → botón "Run" (o ejecuta requests uno por uno) para
   ver en vivo cada request/response.

## Cómo levantar el servicio contra Postgres

```powershell
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --env-file .env
```

Esto usa `app.repositories.postgres_user_repository.PostgresUserRepository`
(no el mock), leyendo `DATABASE_URL`/`JWT_SECRET` desde `.env`.

## Cómo volver a correr todo por consola (sin abrir Postman)

```bash
npx -p newman -p newman-reporter-htmlextra newman run \
  postman/SportMatch-Users.postman_collection.json \
  -e postman/SportMatch.postman_environment.json \
  --reporters cli,json,htmlextra \
  --reporter-json-export postman/reports/newman-report.json \
  --reporter-htmlextra-export postman/reports/newman-report.html
```

## Cuenta admin de prueba (fixture)

El endpoint público de registro siempre crea usuarios con rol `player` (es
la regla de negocio: nadie se auto-asigna `admin`). Para poder probar la
rama de autorización "admin ve el recurso de cualquier usuario" se sembró
**una cuenta de prueba directamente en la base de datos** (no vía la API):

- Email: `qa.admin.seed@example.com`
- Password: `SportMatch-QA-Admin-2026!`

Es una cuenta local, solo para pruebas — no la uses fuera de este entorno de
desarrollo. Si compartes este repo, considera rotar/borrar esa fila de
`usuario` primero (ver `docs/informes/2026-09-14-pruebas-postman-postgresql.md`
para el detalle de cómo se creó).

## Qué queda en la base de datos después de correr esto

Los usuarios "Ana" (uno por corrida) y la cuenta admin quedan persistidos a
propósito, para que puedas revisarlos en pgAdmin4. El usuario "Bruno" de
cada corrida se elimina como parte de la prueba del endpoint
`DELETE /{user_id}` (su rastro de auditoría en `audit_events` sí sobrevive,
a propósito, esa tabla no tiene FK hacia `usuario`).
