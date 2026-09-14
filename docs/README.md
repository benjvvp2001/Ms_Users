# Informes de cambios

Esta carpeta guarda un informe por cada cambio relevante que se hace en el
microservicio: qué se hizo y por qué se hizo. Vive al mismo nivel que `app/`
para que sea lo primero que se vea junto al código.

## Convención

- Cada cambio relevante (refactor de arquitectura, nueva funcionalidad,
  decisión de diseño, fix no trivial) se documenta en `docs/informes/` con
  un archivo nuevo, no editando los anteriores.
- Nombre de archivo: `AAAA-MM-DD-slug-corto.md` (fecha del cambio + resumen
  en kebab-case), por ejemplo `2026-09-13-arquitectura-service-repository.md`.
- Cada informe responde tres preguntas, en este orden:
  1. **Qué se hizo** — el cambio concreto (archivos/carpetas afectados).
  2. **Por qué se hizo** — el motivo o problema que resuelve.
  3. **Impacto / seguimiento** — qué queda pendiente o a qué hay que prestar
     atención después de este cambio (si aplica).
- No se documentan cambios triviales (typos, formateo) salvo que tengan
  contexto relevante.

## Índice

- [2026-09-13 — Arquitectura Service-Repository](informes/2026-09-13-arquitectura-service-repository.md)
- [2026-09-14 — Pruebas funcionales en Postman contra PostgreSQL](informes/2026-09-14-pruebas-postman-postgresql.md)
