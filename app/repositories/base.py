from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.schemas.user import (
    ConsentCreate,
    ConsentRead,
    PreferencesReplace,
    ProfileReplace,
)


@dataclass
class StoredUser:
    id: UUID
    email: str
    password_hash: bytes
    rut: str | None
    profile: ProfileReplace
    preferences: PreferencesReplace
    role: str = "player"
    consents: list[ConsentRead] = field(default_factory=list)


@dataclass(frozen=True)
class AuditEvent:
    user_id: UUID
    action: str
    resource: str
    timestamp: datetime
    result: str


class UserRepository(Protocol):
    """Shape shared by the mock repository and the PostgreSQL adapter."""

    def create_user(
        self,
        *,
        email: str,
        password_hash: bytes,
        rut: str | None,
        profile: ProfileReplace,
        preferences: PreferencesReplace,
    ) -> StoredUser: ...

    def get_user(self, user_id: UUID) -> StoredUser | None: ...

    def get_user_by_email(self, email: str) -> StoredUser | None: ...

    def replace_profile(self, user_id: UUID, profile: ProfileReplace) -> StoredUser: ...

    def replace_preferences(
        self, user_id: UUID, preferences: PreferencesReplace
    ) -> StoredUser: ...

    def add_consent(self, user_id: UUID, consent: ConsentCreate) -> ConsentRead: ...

    def list_consents(self, user_id: UUID) -> list[ConsentRead]: ...

    def revoke_consent(self, user_id: UUID, consent_id: UUID) -> ConsentRead | None: ...

    def delete_user(self, user_id: UUID) -> bool: ...

    def record_audit(
        self, *, user_id: UUID, action: str, resource: str, result: str
    ) -> None: ...
