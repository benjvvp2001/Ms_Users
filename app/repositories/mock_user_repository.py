from __future__ import annotations

from datetime import datetime, timezone
from threading import RLock
from uuid import UUID, uuid4

from app.repositories.base import AuditEvent, StoredUser
from app.schemas.user import ConsentCreate, ConsentRead, PreferencesReplace, ProfileReplace


class MockUserRepository:
    """Temporary mock repository; replace it with the users database adapter."""

    def __init__(self) -> None:
        self._users: dict[UUID, StoredUser] = {}
        self._audit_events: list[AuditEvent] = []
        self._lock = RLock()

    def create_user(
        self,
        *,
        email: str,
        password_hash: bytes,
        rut: str | None,
        profile: ProfileReplace,
        preferences: PreferencesReplace,
    ) -> StoredUser:
        normalized_email = email.casefold()
        with self._lock:
            if any(user.email.casefold() == normalized_email for user in self._users.values()):
                raise ValueError("email already registered")
            if rut and any(user.rut == rut for user in self._users.values()):
                raise ValueError("rut already registered")

            user = StoredUser(
                id=uuid4(),
                email=normalized_email,
                password_hash=password_hash,
                rut=rut,
                profile=profile,
                preferences=preferences,
            )
            self._users[user.id] = user
            return user

    def get_user(self, user_id: UUID) -> StoredUser | None:
        with self._lock:
            return self._users.get(user_id)

    def get_user_by_email(self, email: str) -> StoredUser | None:
        normalized_email = email.casefold()
        with self._lock:
            return next(
                (
                    user
                    for user in self._users.values()
                    if user.email.casefold() == normalized_email
                ),
                None,
            )

    def replace_profile(self, user_id: UUID, profile: ProfileReplace) -> StoredUser:
        with self._lock:
            user = self._users[user_id]
            user.profile = profile
            return user

    def replace_preferences(
        self,
        user_id: UUID,
        preferences: PreferencesReplace,
    ) -> StoredUser:
        with self._lock:
            user = self._users[user_id]
            user.preferences = preferences
            return user

    def add_consent(self, user_id: UUID, consent: ConsentCreate) -> ConsentRead:
        with self._lock:
            user = self._users[user_id]
            granted_at = datetime.now(timezone.utc)
            stored_consent = ConsentRead(
                id=uuid4(),
                user_id=user_id,
                type=consent.type,
                purpose=consent.purpose,
                document_version=consent.document_version,
                method=consent.method,
                granted_at=granted_at,
                revoked_at=None,
                status="active",
            )
            user.consents.append(stored_consent)
            return stored_consent

    def list_consents(self, user_id: UUID) -> list[ConsentRead]:
        with self._lock:
            return list(self._users[user_id].consents)

    def revoke_consent(self, user_id: UUID, consent_id: UUID) -> ConsentRead | None:
        with self._lock:
            user = self._users[user_id]
            for index, consent in enumerate(user.consents):
                if consent.id == consent_id:
                    if consent.status == "revoked":
                        return consent
                    revoked = consent.model_copy(
                        update={
                            "revoked_at": datetime.now(timezone.utc),
                            "status": "revoked",
                        }
                    )
                    user.consents[index] = revoked
                    return revoked
            return None

    def delete_user(self, user_id: UUID) -> bool:
        with self._lock:
            return self._users.pop(user_id, None) is not None

    def record_audit(
        self,
        *,
        user_id: UUID,
        action: str,
        resource: str,
        result: str,
    ) -> None:
        with self._lock:
            self._audit_events.append(
                AuditEvent(
                    user_id=user_id,
                    action=action,
                    resource=resource,
                    timestamp=datetime.now(timezone.utc),
                    result=result,
                )
            )
