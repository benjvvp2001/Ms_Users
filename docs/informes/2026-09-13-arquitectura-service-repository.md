# Arquitectura Service-Repository

**Fecha:** 2026-09-13

## Qué se hizo

Se reorganizó `app/`, que antes era un paquete plano (9 archivos sueltos:
`main.py`, `routes.py`, `repository.py`, `postgres_repository.py`,
`schemas.py`, `dependencies.py`, `security.py`, `config.py`, `db.py`), en
capas explícitas:

```
app/
├── main.py                          # arma la app FastAPI (sin cambios de comportamiento)
├── core/
│   ├── config.py                    # antes app/config.py
│   └── security.py                  # antes app/security.py
├── database/
│   └── session.py                   # antes app/db.py (pool de conexiones)
├── schemas/
│   └── user.py                      # antes app/schemas.py
├── repositories/                    # capa Repository (acceso a datos)
│   ├── base.py                      # Protocol UserRepository + StoredUser + AuditEvent
│   ├── mock_user_repository.py      # antes MockUserRepository dentro de app/repository.py
│   └── postgres_user_repository.py  # antes app/postgres_repository.py
├── services/                        # capa Service (NUEVA)
│   └── user_service.py              # lógica de negocio extraída de app/routes.py
└── api/
    ├── dependencies.py              # antes app/dependencies.py
    └── v1/
        └── users.py                 # antes app/routes.py (controlador delgado)
```

También se renombró `db/README.md` para que la referencia al repositorio
Postgres apunte a la nueva ruta (`app.repositories.postgres_user_repository`),
y se actualizó el import en `tests/test_users_api.py`
(`app.repository.MockUserRepository` → `app.repositories.mock_user_repository.MockUserRepository`).

No se renombró ninguna carpeta de español a inglés porque no existía
ninguna: `app`, `db`, `tests`, `work`, `outputs` ya estaban en inglés. Los
nombres en español que sí existen (`nombre`, `apellido_paterno`, `rut`,
`deporte_codigo`, etc.) son campos del dominio definidos en
`app/schemas/user.py` y en el esquema SQL (`db/migrations/001_users_schema.sql`),
no nombres de carpeta — se dejaron intactos porque cambiarlos rompería el
contrato de la API (`{"nombre": ...}` en los JSON de request/response) y el
esquema de base de datos.

## Por qué se hizo

1. **Orden solicitado.** El microservicio venía con todos los archivos al
   mismo nivel dentro de `app/`, sin distinguir HTTP, negocio y persistencia.
2. **Service-Repository real, no solo carpetas.** Antes `app/routes.py`
   llamaba directamente a `UserRepository` (autorización, construcción de
   respuestas y registro de auditoría vivían en el controlador). Se creó
   `app/services/user_service.py` como capa intermedia: los controladores en
   `app/api/v1/users.py` ahora solo traducen HTTP → llamada al servicio →
   `response_model`, y el servicio concentra la regla de autorización
   (dueño-o-admin, antes `ensure_owner_or_admin` en `dependencies.py`), la
   auditoría y el armado de DTOs (`as_profile`, `issue_token`, etc.).
3. **Separar el repositorio en contrato + implementaciones.** El `Protocol
   UserRepository` y los dataclasses `StoredUser`/`AuditEvent` quedaron en
   `repositories/base.py`; `MockUserRepository` (para tests) y
   `PostgresUserRepository` (producción) son archivos independientes que
   implementan ese contrato, así queda claro que son intercambiables.

## Impacto / seguimiento

- **Comportamiento sin cambios**: mismos endpoints, mismos status codes,
  misma lógica de autorización/auditoría. `pytest` (2 tests) pasa sin
  modificaciones funcionales.
- **Se detectó y corrigió un problema de entorno no relacionado**: el
  `.venv` no tenía instalado `psycopg[binary,pool]` pese a estar en
  `pyproject.toml`, por lo que `pytest` no podía ni importar `app.main`. Se
  instaló para poder validar el refactor; si vuelve a pasar en un entorno
  nuevo, correr `pip install -e ".[dev]"` de nuevo.
- **Pendiente a criterio del equipo**: si crece el dominio (por ejemplo, se
  agrega `matching` o `activities` a este mismo repo), cada dominio nuevo
  debería replicar este mismo patrón (`repositories/<dominio>_repository.py`,
  `services/<dominio>_service.py`, `api/v1/<dominio>.py`) en vez de mezclarse
  con `users`.
