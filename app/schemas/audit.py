"""HU-48: catalogo cerrado de acciones administrativas y formas de entrada/salida de la auditoria."""
from __future__ import annotations

import math
import re
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from app.schemas.user import APIModel

PAGE_SIZE_MAX = 100
PAGE_SIZE_DEFAULT = 25
MOTIVO_MIN_LENGTH = 3
MOTIVO_MAX_LENGTH = 300
RESOURCE_ID_MAX_LENGTH = 120
METADATA_MAX_KEYS = 10
METADATA_VALUE_MAX_LENGTH = 120


class AuditAction(str, Enum):
    """Catalogo cerrado. Para sumar una accion hay que agregarla aqui (y en la lista de la migracion si
    cambia el tipo de recurso). Las H.U. 45, 47 y 56 usan las PLAN_*, MEMBERSHIP_* y PAYMENT_*."""

    REPORT_REVIEWED = "REPORT_REVIEWED"          # aplica hoy: PATCH /admin/reports/{id} con estado revisado
    REPORT_DISCARDED = "REPORT_DISCARDED"        # aplica hoy: PATCH /admin/reports/{id} con estado descartado
    REPORT_ESCALATED = "REPORT_ESCALATED"        # aplica hoy (HU-47): deja el reporte listo para una sancion (HU-45)
    USER_SUSPENDED = "USER_SUSPENDED"
    USER_REACTIVATED = "USER_REACTIVATED"
    SANCTION_APPLIED = "SANCTION_APPLIED"
    SANCTION_LIFTED = "SANCTION_LIFTED"
    PLAN_CREATED = "PLAN_CREATED"
    PLAN_UPDATED = "PLAN_UPDATED"
    PLAN_DEACTIVATED = "PLAN_DEACTIVATED"
    MEMBERSHIP_UPDATED = "MEMBERSHIP_UPDATED"
    PAYMENT_REFUNDED = "PAYMENT_REFUNDED"


class AuditResourceType(str, Enum):
    USER = "user"
    REPORT = "report"
    PLAN = "plan"
    MEMBERSHIP = "membership"
    PAYMENT = "payment"
    CLUB = "club"
    ACTIVITY = "activity"
    MESSAGE = "message"


AuditResult = Literal["success", "failure"]

# Filas escritas antes de HU-48 por la moderacion de reportes; se muestran con el nombre del catalogo.
LEGACY_ACTIONS: dict[str, AuditAction] = {
    "report_revisado": AuditAction.REPORT_REVIEWED,
    "report_descartado": AuditAction.REPORT_DISCARDED,
}

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_RUT = re.compile(r"\b\d{1,2}\.?\d{3}\.?\d{3}-[\dkK]\b")
_JWT = re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}")
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_FORBIDDEN_KEY_PARTS = ("password", "pass", "token", "secret", "rut", "email", "correo", "authorization", "key")
_RESOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]*$")


def check_audit_text(value: str, field: str) -> str:
    """Rechaza caracteres de control (incluido NUL), sustitutos Unicode y datos que no deben quedar en el log."""
    if _CONTROL.search(value):
        raise ValueError(f"{field} contiene caracteres no permitidos.")
    if _EMAIL.search(value) or _RUT.search(value) or _JWT.search(value):
        raise ValueError(f"{field} no puede incluir correos, RUT ni credenciales.")
    return value


class AdminAuditEntry(APIModel):
    """Una accion administrativa a registrar. Es lo unico que reciben `AuditService.record`,
    `insert_admin_audit` y el endpoint interno."""

    actor_user_id: UUID
    action: AuditAction
    resource_type: AuditResourceType
    resource_id: str = Field(min_length=1, max_length=RESOURCE_ID_MAX_LENGTH)
    motivo: str = Field(min_length=MOTIVO_MIN_LENGTH, max_length=MOTIVO_MAX_LENGTH)
    result: AuditResult = "success"
    metadata: dict[str, str | int | float | bool | None] | None = None

    @field_validator("resource_id")
    @classmethod
    def valid_resource_id(cls, value: str) -> str:
        if not _RESOURCE_ID.fullmatch(value):
            raise ValueError("resource_id con formato inválido.")
        return value

    @field_validator("motivo")
    @classmethod
    def valid_motivo(cls, value: str) -> str:
        return check_audit_text(value, "motivo")

    @field_validator("metadata")
    @classmethod
    def valid_metadata(cls, value: dict | None) -> dict | None:
        if value is None:
            return None
        if len(value) > METADATA_MAX_KEYS:
            raise ValueError("metadata tiene demasiadas claves.")
        for key, item in value.items():
            if not _KEY.fullmatch(key) or any(part in key for part in _FORBIDDEN_KEY_PARTS):
                raise ValueError("metadata con una clave no permitida.")
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("metadata no admite NaN ni infinito.")
            if isinstance(item, str):
                if len(item) > METADATA_VALUE_MAX_LENGTH:
                    raise ValueError("metadata con un valor demasiado largo.")
                check_audit_text(item, "metadata")
        return value

    @model_validator(mode="after")
    def user_resource_is_uuid(self) -> "AdminAuditEntry":
        if self.resource_type in (AuditResourceType.USER, AuditResourceType.REPORT):
            try:
                UUID(self.resource_id)
            except ValueError:
                raise ValueError("resource_id debe ser un UUID para este tipo de recurso.") from None
        return self


class AuditQuery(APIModel):
    """Filtros de GET /admin/audit-events. Los nombres de query siguen el estilo de /admin/reports."""

    desde: AwareDatetime | None = None
    hasta: AwareDatetime | None = None
    admin_id: UUID | None = None
    accion: AuditAction | None = None
    recurso_tipo: AuditResourceType | None = None
    recurso_id: Annotated[str | None, Field(max_length=RESOURCE_ID_MAX_LENGTH)] = None
    resultado: AuditResult | None = None
    pagina: Annotated[int, Field(ge=1, le=10_000)] = 1
    por_pagina: Annotated[int, Field(ge=1, le=PAGE_SIZE_MAX)] = PAGE_SIZE_DEFAULT

    @field_validator("recurso_id")
    @classmethod
    def valid_recurso_id(cls, value: str | None) -> str | None:
        if value is not None and not _RESOURCE_ID.fullmatch(value):
            raise ValueError("recurso_id con formato inválido.")
        return value

    @model_validator(mode="after")
    def ordered_range(self) -> "AuditQuery":
        if self.desde and self.hasta and self.desde > self.hasta:
            raise ValueError("desde no puede ser posterior a hasta.")
        return self


class AuditPerson(APIModel):
    """Solo lo necesario para reconocer a la persona: nombre e inicial, sin correo ni RUT."""

    id: UUID | None = None
    nombre: str | None = None


class AuditEventRead(APIModel):
    id: UUID
    fecha: AwareDatetime
    admin: AuditPerson
    accion: str
    recurso_tipo: AuditResourceType | None
    recurso_id: str | None
    afectado: AuditPerson | None
    motivo: str | None
    resultado: str
    detalle: dict[str, Any] | None


class AuditPage(APIModel):
    items: list[AuditEventRead]
    pagina: int
    por_pagina: int
    total: int
    total_paginas: int
