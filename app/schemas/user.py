from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

WEEKDAYS = {"lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"}


# Shared account rules. They mirror Frontend-SportMatch-APP/services/validators.ts;
# change both places together (see the general README).
EMAIL_MAX_LENGTH = 100
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 64
NAME_MIN_LENGTH = 2
NAME_MAX_LENGTH = 50
_LETTERS = "A-Za-zÁÉÍÓÚÜÑáéíóúüñ"
NAME_PATTERN = rf"^[{_LETTERS}]+(?:[ '-][{_LETTERS}]+)*$"
RUT_PATTERN = re.compile(r"^\d{7,8}-[\dK]$")
# Comunas like "Ñuñoa", "O'Higgins", "Pedro Aguirre Cerda" or "Til-Til".
COMUNA_PATTERN = rf"^[{_LETTERS}.]+(?:[ '-][{_LETTERS}.]+)*$"
OBJETIVOS_MAX = 5
OBJETIVO_MIN_LENGTH = 2
OBJETIVO_MAX_LENGTH = 50


def check_email_length(email: str) -> str:
    if len(email) > EMAIL_MAX_LENGTH:
        raise ValueError(f"El correo no puede superar {EMAIL_MAX_LENGTH} caracteres.")
    return email


def check_password_policy(password: str) -> str:
    """Same rules the app shows under the password field."""
    problems = [
        (re.search(r"[A-ZÁÉÍÓÚÑ]", password) is None, "una letra mayúscula"),
        (re.search(r"[a-záéíóúñ]", password) is None, "una letra minúscula"),
        (re.search(r"\d", password) is None, "un número"),
        (re.search(r"[^A-Za-z0-9ÁÉÍÓÚÑáéíóúñ\s]", password) is None, "un carácter especial"),
    ]
    missing = [rule for failed, rule in problems if failed]
    if missing:
        raise ValueError("La contraseña debe incluir " + ", ".join(missing) + ".")
    if re.search(r"\s", password):
        raise ValueError("La contraseña no puede contener espacios.")
    return password


def normalize_rut(rut: str | None) -> str | None:
    """Accept 12.345.678-5 or 12345678-5 and store 12345678-5 (DV checked)."""
    if rut is None or not rut.strip():
        return None
    value = rut.replace(".", "").replace(" ", "").upper()
    if not RUT_PATTERN.fullmatch(value):
        raise ValueError("RUT con formato inválido. Usa 12345678-9.")
    body, dv = value.split("-")
    total, factor = 0, 2
    for digit in reversed(body):
        total += int(digit) * factor
        factor = 2 if factor == 7 else factor + 1
    expected = 11 - total % 11
    expected_dv = "0" if expected == 11 else "K" if expected == 10 else str(expected)
    if dv != expected_dv:
        raise ValueError("El RUT no es válido (dígito verificador incorrecto).")
    return value


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RegisterRequest(APIModel):
    email: EmailStr
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)
    nombre: str = Field(
        min_length=NAME_MIN_LENGTH, max_length=NAME_MAX_LENGTH, pattern=NAME_PATTERN
    )
    apellido_paterno: str = Field(
        min_length=NAME_MIN_LENGTH, max_length=NAME_MAX_LENGTH, pattern=NAME_PATTERN
    )
    apellido_materno: str | None = Field(
        default=None, min_length=NAME_MIN_LENGTH, max_length=NAME_MAX_LENGTH, pattern=NAME_PATTERN
    )
    rut: str | None = Field(default=None, max_length=20)

    @field_validator("email")
    @classmethod
    def email_length(cls, value: str) -> str:
        return check_email_length(value)

    @field_validator("password")
    @classmethod
    def password_policy(cls, value: str) -> str:
        return check_password_policy(value)

    @field_validator("apellido_materno", mode="before")
    @classmethod
    def empty_apellido_materno_is_none(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("rut")
    @classmethod
    def rut_valido(cls, value: str | None) -> str | None:
        return normalize_rut(value)


class LoginRequest(APIModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class PasswordResetRequest(APIModel):
    email: EmailStr


class PasswordResetConfirm(APIModel):
    email: EmailStr
    code: str = Field(pattern=r"^\d{6}$")
    new_password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)

    @field_validator("new_password")
    @classmethod
    def password_policy(cls, value: str) -> str:
        return check_password_policy(value)


class EmailVerificationRequest(APIModel):
    email: EmailStr


class EmailVerificationConfirm(APIModel):
    email: EmailStr
    code: str = Field(pattern=r"^\d{6}$")


class MessageResponse(APIModel):
    detail: str


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


class Zona(APIModel):
    """Where the user plays. The comuna is shown; coordinates stay private and
    are only for computing distances later."""

    comuna: str = Field(min_length=2, max_length=80, pattern=COMUNA_PATTERN)
    latitud: float | None = Field(default=None, ge=-90, le=90)
    longitud: float | None = Field(default=None, ge=-180, le=180)

    @field_validator("comuna", mode="before")
    @classmethod
    def trim_comuna(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def coordenadas_completas(self) -> Zona:
        if (self.latitud is None) != (self.longitud is None):
            raise ValueError("latitud and longitud must be sent together")
        return self


class PreferencesReplace(APIModel):
    deportes: list[UserSport] = Field(default_factory=list, max_length=10)
    disponibilidad: list[AvailabilityWindow] = Field(default_factory=list, max_length=21)
    # Free-text codes chosen in the app (e.g. "competir", "mejorar_condicion").
    objetivos: list[str] = Field(default_factory=list, max_length=OBJETIVOS_MAX)
    zona: Zona | None = None
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

    @field_validator("objetivos")
    @classmethod
    def objetivos_validos(cls, objetivos: list[str]) -> list[str]:
        limpios = [objetivo.strip() for objetivo in objetivos]
        if any(
            not OBJETIVO_MIN_LENGTH <= len(objetivo) <= OBJETIVO_MAX_LENGTH for objetivo in limpios
        ):
            raise ValueError(
                f"each objetivo must have {OBJETIVO_MIN_LENGTH} to {OBJETIVO_MAX_LENGTH} characters"
            )
        if len({objetivo.casefold() for objetivo in limpios}) != len(limpios):
            raise ValueError("objetivos must not contain duplicates")
        return limpios


class SuggestionFilters(APIModel):
    limit: int = Field(default=20, ge=1, le=50)
    radius_km: float | None = Field(default=None, ge=1, le=100)
    sport: str | None = Field(default=None, min_length=1, max_length=50)
    min_level: int = Field(default=1, ge=1, le=5)
    max_level: int = Field(default=5, ge=1, le=5)
    shared_sports: bool = False
    level_tolerance: int | None = Field(default=None, ge=0, le=4)

    @model_validator(mode="after")
    def valid_level_range(self) -> SuggestionFilters:
        if self.min_level > self.max_level:
            raise ValueError("El nivel mínimo no puede superar al máximo.")
        return self


class SuggestedUser(APIModel):
    """Public card of another player. Only what the card shows: no email, rut,
    phone, birth date or full last name."""

    user_id: UUID
    nombre: str
    apellido_inicial: str
    edad: int | None
    foto_perfil: str | None
    biografia: str | None
    deportes: list[UserSport]
    compatibilidad: int = Field(ge=0, le=100)
    distancia_km: float | None = None
    nivel_coincidente: bool = False


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


class RegisterResponse(APIModel):
    """No token yet: the account stays inactive until the emailed code is confirmed."""

    detail: str
    email_verification_required: Literal[True] = True
    user: AuthenticatedUser


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
