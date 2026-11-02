"""HU-48: registro y consulta de acciones administrativas.

Para auditar una accion desde otra H.U. (planes, membresias, pagos, sanciones), una sola llamada:

    AuditService(repository).record(
        actor_id=admin.id, action=AuditAction.PLAN_CREATED,
        resource_type=AuditResourceType.PLAN, resource_id=str(plan.id), motivo="Alta del plan mensual",
    )

Si el cambio y el registro deben ser atomicos (lo recomendado), el repositorio que hace el cambio llama
`insert_admin_audit(conn, entry)` (app/repositories/postgres_user_repository.py) dentro de su misma
transaccion: si el registro falla, el cambio se revierte. `record` abre su propia transaccion, asi que
solo es atomico con la accion si esta ya se guardo antes: ahi un fallo sube como 503 y quien llama debe
abortar o compensar.

Politica de fallos: nunca se traga el error. Un registro que no se pudo escribir es DependencyUnavailableError
(HTTP 503), de modo que una accion administrativa no queda sin auditar sin que se note.
"""
from __future__ import annotations

from math import ceil
from uuid import UUID

from fastapi import HTTPException, status

from app.repositories.base import StoredUser, UserRepository
from app.schemas.audit import (
    AdminAuditEntry,
    AuditAction,
    AuditEventRead,
    AuditPage,
    AuditPerson,
    AuditQuery,
    AuditResourceType,
    AuditResult,
)


class AuditService:
    def __init__(self, repository: UserRepository) -> None:
        self._repository = repository

    def record(
        self,
        *,
        actor_id: UUID,
        action: AuditAction,
        resource_type: AuditResourceType,
        resource_id: str,
        motivo: str,
        result: AuditResult = "success",
        metadata: dict | None = None,
    ) -> None:
        """Validates (same rules as the internal endpoint) and writes one administrative action."""
        self.record_entry(AdminAuditEntry(
            actor_user_id=actor_id, action=action, resource_type=resource_type, resource_id=resource_id,
            motivo=motivo, result=result, metadata=metadata,
        ))

    def record_entry(self, entry: AdminAuditEntry) -> None:
        self._repository.record_admin_audit(entry)

    def list_events(self, current_user: StoredUser, query: AuditQuery) -> AuditPage:
        if current_user.role != "admin":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Solo un administrador puede consultar la auditoría.")
        records, total = self._repository.list_admin_audit(query)
        items = [
            AuditEventRead(
                id=r.id, fecha=r.fecha, admin=AuditPerson(id=r.admin_id, nombre=r.admin_nombre),
                accion=r.action, recurso_tipo=r.resource_type, recurso_id=r.resource_id,
                afectado=AuditPerson(nombre=r.afectado_nombre) if r.resource_type == "user" else None,
                motivo=r.motivo, resultado=r.result, detalle=r.metadata,
            )
            for r in records
        ]
        return AuditPage(items=items, pagina=query.pagina, por_pagina=query.por_pagina, total=total,
                         total_paginas=ceil(total / query.por_pagina))
