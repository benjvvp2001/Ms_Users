# SportMatch users service

Microservicio de usuarios de SportMatch. Expone exclusivamente rutas bajo
/api/v1/users y persiste en la base de datos PostgreSQL propia del servicio
(ver [db/README.md](db/README.md)). Las pruebas automatizadas siguen usando
un repositorio mock en memoria para no depender de una base real.

## Alcance

- Registro e inicio de sesión con JWT.
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

## Ejecutar

Se necesita Python 3.11 o posterior.

1. Crear y activar un entorno virtual.
2. Instalar el servicio y sus dependencias de desarrollo con:

   pip install -e ".[dev]"

3. Definir un secreto de JWT largo y aleatorio y la cadena de conexión a
   PostgreSQL en el entorno (ver [db/README.md](db/README.md) para crear la
   base de datos primero). En PowerShell:

   $env:JWT_SECRET = "reemplaza-esto-por-un-secreto-largo-y-aleatorio"
   $env:DATABASE_URL = "postgresql+psycopg://sportmatch_users:tu-password@localhost:5432/users_db"

4. Arrancar el servicio:

   uvicorn app.main:app --reload

5. Ejecutar pruebas (usan un repositorio mock, no requieren base de datos):

   pytest

No se proporciona un secreto por defecto y no se registran contraseñas,
tokens, ubicaciones ni otros datos sensibles.

## Base de datos PostgreSQL

El esquema del servicio está en [db/README.md](db/README.md). Solo contiene
las tablas que pertenecen a users y usa UUID v4; no es una copia de la base
monolítica de referencia. Ejecuta primero db/00_create_database.sql con una
cuenta administradora y luego db/migrations/001_users_schema.sql conectado a
la nueva base.

## Contrato v1

Todas las rutas usan el prefijo /api/v1/users:

- POST /auth/register
- POST /auth/login
- GET y PUT /{id}/profile
- GET /{id}/roles
- GET y PUT /{id}/preferences
- GET y POST /{id}/consents
- DELETE /{id}/consents/{consent_id}
- GET /{id}/exports
- DELETE /{id}

La documentación interactiva no se expone hasta que el equipo acuerde una ruta
que no contradiga las convenciones de API.
