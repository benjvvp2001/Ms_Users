"""HU-45: suspensiones y sanciones de cuentas."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.schemas.audit import check_audit_text
from app.schemas.user import APIModel

SANCTION_MIN_DAYS = 1
SANCTION_MAX_DAYS = 365
MOTIVO_MIN_LENGTH = 3
MOTIVO_MAX_LENGTH = 300

SanctionType = Literal["temporal", "permanente"]
SanctionState = Literal["activa", "levantada", "vencida"]  # vencida: temporal cuya fecha de fin ya pasó (calculado)
Days = Annotated[int, Field(strict=True, ge=SANCTION_MIN_DAYS, le=SANCTION_MAX_DAYS)]
Motivo = Annotated[str, Field(min_length=MOTIVO_MIN_LENGTH, max_length=MOTIVO_MAX_LENGTH)]


class SanctionCreate(APIModel):
    usuario_id: UUID
    tipo: SanctionType
    # Obligatoria si es temporal (1 a 365 días); no va si es permanente.
    dias: Days | None = None
    motivo: Motivo
    # Opcional: reporte escalado (HU-47) que dio origen a la sanción.
    reporte_id: UUID | None = None

    @field_validator("motivo")
    @classmethod
    def clean_motivo(cls, value: str) -> str:
        return check_audit_text(value, "motivo")

    @model_validator(mode="after")
    def duration_matches_type(self) -> "SanctionCreate":
        if self.tipo == "temporal" and self.dias is None:
            raise ValueError("Una suspensión temporal necesita su duración en días.")
        if self.tipo == "permanente" and self.dias is not None:
            raise ValueError("Una suspensión permanente no lleva duración.")
        return self


class SanctionLift(APIModel):
    motivo: Motivo

    @field_validator("motivo")
    @classmethod
    def clean_motivo(cls, value: str) -> str:
        return check_audit_text(value, "motivo")


class SanctionListQuery(APIModel):
    estado: SanctionState | None = None
    usuario_id: UUID | None = None
    limit: int = Field(default=50, ge=1, le=100)
    pagina: int = Field(default=1, ge=1, le=10_000)


class SanctionPerson(APIModel):
    """Solo para administradores: id y nombre corto, sin correo, RUT ni foto."""

    user_id: UUID
    nombre: str | None
    apellido_inicial: str | None


class SanctionRead(APIModel):
    id: UUID
    usuario: SanctionPerson
    tipo: SanctionType
    motivo: str
    inicio: datetime
    fin: datetime | None
    estado: SanctionState
    reporte_id: UUID | None
    admin: SanctionPerson | None
    levantada_por: SanctionPerson | None
    levantada_en: datetime | None
    motivo_levantamiento: str | None
    fecha_creacion: datetime


class AccountStatus(APIModel):
    """Lo que el gateway pregunta por cada cuenta. Nunca incluye quién reportó ni qué admin sancionó."""

    suspended: bool
    tipo: SanctionType | None = None
    motivo: str | None = None
    fin: datetime | None = None
