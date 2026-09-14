# Pruebas funcionales en Postman contra PostgreSQL real

**Fecha:** 2026-09-14

## Qué se hizo

Se probó el microservicio completo (los 12 endpoints de `/api/v1/users`)
levantándolo con `PostgresUserRepository` contra la base de datos real
`users_db` (no el `MockUserRepository` que usa `pytest`), y se ejecutaron las
pruebas como requests reales de Postman, no scripts sueltos:

1. **Se verificó el estado de la base** sin exponer credenciales (un script
   que usa `app.core.config`/`app.database.session` internamente y solo
   imprime metadatos no sensibles): `users_db` ya existía, con el esquema de
   `db/migrations/001_users_schema.sql` aplicado y 0 usuarios — base limpia
   para probar.
2. **Se sembró una cuenta admin de prueba directamente en Postgres**
   (`qa.admin.seed@example.com`, ver `postman/README.md`), porque el
   endpoint público de registro nunca asigna el rol `admin` (correcto por
   diseño) y sin eso no había forma de probar la rama "admin ve el recurso
   de cualquier usuario" de `UserService._authorize`.
3. **Se levantó el servicio real**: `uvicorn app.main:app --env-file .env`
   en `http://127.0.0.1:8000`, usando el repositorio de Postgres.
4. **Se construyó `postman/SportMatch-Users.postman_collection.json`**: 27
   requests en 9 carpetas (Auth, Profile, Roles, Preferences, Consents, Data
   Export, Autorización admin, Ciclo de vida de cuenta, Contrato de rutas),
   con scripts de test (`pm.test`) que encadenan tokens/ids entre requests
   vía variables de entorno y verifican tanto casos exitosos como errores
   esperados (401 sin token, 403 entre usuarios distintos, 404 recurso
   inexistente, 409 email duplicado).
5. **Se corrió la colección con `newman`** (el motor de línea de comandos de
   Postman — mismo formato de colección que abre la app de escritorio) contra
   el servicio real. Resultado final: **27/27 requests, 49/49 asserts,
   0 fallos**.
6. **Se verificó directamente en Postgres** (consultas a `usuario`,
   `usuario_deporte`, `disponibilidad`, `consent`, `audit_events`) que cada
   acción probada en Postman efectivamente escribió una fila.

## Por qué se hizo

Se pidió probar las funciones del microservicio contra la base de datos real
(no el mock) y dejar todo registrado tanto en Postman como en la base, para
poder verificar visualmente si algo falla. `pytest` ya prueba el contrato
HTTP contra el mock, pero nunca había corrido contra Postgres real — cosas
como el mapeo `usuario`/`preferencia_usuario`/`usuario_deporte`/
`disponibilidad` en `PostgresUserRepository`, las constraints SQL (unique
email/rut, checks de `usuario_deporte`), y el comportamiento real de
`audit_events` (sin FK, sobrevive al `DELETE`) nunca se habían ejercitado de
punta a punta.

## Resultado de la corrida (evidencia)

Reporte completo en `postman/reports/newman-report.html` (abrir en
navegador) y `postman/reports/newman-report.json`. Resumen:

```
requests:          27 ejecutados, 0 fallidos
test-scripts:       27 ejecutados, 0 fallidos
assertions:         49 ejecutadas, 0 fallidas
tiempo de respuesta: 67ms promedio (min 3ms, max 284ms)
```

Casos negativos verificados explícitamente (todos con el status esperado):
registro con email duplicado (409), login con password incorrecta (401),
acceso sin token (401), acceso al recurso de otro usuario (403), revocar un
consentimiento inexistente (404), admin consultando un usuario inexistente
(404), acceder al perfil de una cuenta ya eliminada (401), ruta fuera del
prefijo `/api/v1` (404).

Verificación cruzada en Postgres (consulta directa, sin pasar por la API):
cada `usuario` creado en Postman aparece con sus filas correspondientes en
`usuario_deporte`, `disponibilidad` y `consent`; `audit_events` registró una
fila por cada acción sensible (`register`, `login`, `read_profile`,
`replace_profile`, `replace_preferences`, `grant_consent`,
`revoke_consent`, `export_personal_data`, `delete_account`) — incluida la
del usuario "Bruno" ya eliminado, confirmando que la tabla de auditoría
sobrevive al borrado de la cuenta tal como exige la Ley N.º 21.719.

## Impacto / seguimiento

- **Nada del código de `app/` cambió.** Esto fue solo testing; no se tocó
  ninguna ruta, servicio ni repositorio.
- **Quedan datos de prueba en `users_db`**: 1 cuenta admin sembrada + 2
  cuentas "Ana" (una por cada corrida completa de la colección), visibles
  en pgAdmin4. Las cuentas "Bruno" de cada corrida fueron eliminadas por la
  propia prueba del endpoint `DELETE /{user_id}`. Si se quiere una base
  limpia antes de una demo, hay que borrar esas filas a mano (no se automatizó
  ninguna limpieza para no correr el riesgo de borrar algo que no fuera de
  esta prueba).
- **La colección es re-ejecutable**: cada corrida genera emails únicos
  (`qa.ana.<timestamp>@example.com`) así que se puede correr muchas veces
  sin chocar con el 409 de email duplicado; solo la cuenta admin es fija
  (es un fixture, no se recrea).
- **Pendiente si el equipo lo quiere**: automatizar esto en CI correría
  contra una base Postgres efímera (contenedor), no contra `users_db` local;
  hoy es una suite pensada para correr a mano o con `newman` cuando se
  necesite validar manualmente contra una base real.
- Ver también `postman/README.md` para instrucciones de cómo abrir la
  colección en la app de Postman y cómo volver a correrla.
