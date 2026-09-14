from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

WEEKDAYS = {"lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"}


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RegisterRequest(APIModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)
    nombre: str = Field(min_length=1, max_length=50)
    apellido_paterno: str = Field(min_length=1, max_length=50)
    apellido_materno: str | None = Field(default=None, max_length=50)
    rut: str | None = Field(default=None, max_length=20)


class LoginRequest(APIModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class ProfileReplace(APIModel):
    """The mutable part of the profile; rut is fixed at registration."""

    nombre: str = Field(min_length=1, max_length=50)
    apellido_paterno: str = Field(min_length=1, max_length=50)
    apellido_materno: str | None = Field(default=None, max_length=50)
    fecha_nacimiento: date | None = None
    telefono: str | None = Field(default=None, max_length=20)
    foto_perfil: str | None = Field(default=None, max_length=255)
    biografia: str | None = Field(default=None, max_length=500)


class ProfileRead(ProfileReplace):
    user_id: UUID
    rut: str | None


class UserSport(APIModel):
    """A sport the user declares, identified by a free-text code.

    No foreign key to a sports catalog: that catalog belongs to the matching
    microservice, not to users.
    """

    deporte_codigo: str = Field(min_length=1, max_length=50)
    nivel: int = Field(ge=1, le=5)


class AvailabilityWindow(APIModel):
    dia_semana: str
    hora_inicio: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$", max_length=10)
    hora_fin: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$", max_length=10)

    @field_validator("dia_semana")
    @classmethod
    def dia_semana_valido(cls, value: str) -> str:
        normalized = value.casefold()
        if normalized not in WEEKDAYS:
            raise ValueError(f"dia_semana must be one of {sorted(WEEKDAYS)}")
        return normalized

    @model_validator(mode="after")
    def hora_fin_after_hora_inicio(self) -> AvailabilityWindow:
        if self.hora_inicio >= self.hora_fin:
            raise ValueError("hora_fin must be later than hora_inicio")
        return self


class PreferencesReplace(APIModel):
    deportes: list[UserSport] = Field(default_factory=list, max_length=10)
    disponibilidad: list[AvailabilityWindow] = Field(default_factory=list, max_length=21)
    rango_distancia_km: str | None = Field(default=None, max_length=10)
    rango_edad_min: str | None = Field(default=None, max_length=5)
    rango_edad_max: str | None = Field(default=None, max_length=5)
    mismo_nivel: bool | None = None
    disponibilidad_match: bool = True

    @field_validator("deportes")
    @classmethod
    def deportes_sin_duplicados(cls, deportes: list[UserSport]) -> list[UserSport]:
        normalized = [item.deporte_codigo.casefold() for item in deportes]
        if len(normalized) != len(set(normalized)):
            raise ValueError("deportes must not contain duplicates")
        return deportes


class RoleRead(APIModel):
    role: str


class ConsentCreate(APIModel):
    type: str = Field(min_length=1, max_length=50)
    purpose: str = Field(min_length=1, max_length=255)
    document_version: str = Field(min_length=1, max_length=20)
    method: str = Field(min_length=1, max_length=50)


class ConsentRead(ConsentCreate):
    id: UUID
    user_id: UUID
    granted_at: datetime
    revoked_at: datetime | None
    status: Literal["active", "revoked"]


class AuthenticatedUser(APIModel):
    user_id: UUID
    email: EmailStr
    nombre: str
    apellido_paterno: str
    role: str


class TokenResponse(APIModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in_seconds: int
    user: AuthenticatedUser


class DataExport(APIModel):
    exported_at: datetime
    user: AuthenticatedUser
    profile: ProfileRead
    preferences: PreferencesReplace
    role: str
    consents: list[ConsentRead]
