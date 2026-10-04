from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from threading import RLock
from uuid import UUID, uuid4

from app.repositories.base import (
    ATHLETE_ROLES,
    AuditEvent,
    EmailVerificationRecord,
    NewEmailVerification,
    PasswordResetRecord,
    StoredUser,
    SuggestionCandidate,
)
from app.schemas.user import ConsentCreate, ConsentRead, PreferencesReplace, ProfileReplace


class MockUserRepository:
    """Temporary mock repository; replace it with the users database adapter."""

    def __init__(self) -> None:
        self._users: dict[UUID, StoredUser] = {}
        self._audit_events: list[AuditEvent] = []
        self._password_resets: dict[UUID, PasswordResetRecord] = {}
        self._email_verifications: dict[UUID, EmailVerificationRecord] = {}
        self._lock = RLock()

    def create_user(
        self,
        *,
        email: str,
        password_hash: bytes,
        rut: str | None,
        profile: ProfileReplace,
        preferences: PreferencesReplace,
        email_verification: NewEmailVerification,
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
                email_verified=False,
            )
            self._users[user.id] = user
            self._email_verifications[user.id] = EmailVerificationRecord(
                user_id=user.id,
                code_hash=email_verification.code_hash,
                expires_at=email_verification.expires_at,
                sent_at=datetime.now(timezone.utc),
                attempts=0,
            )
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

    def list_suggestion_candidates(
        self, *, exclude_user_id: UUID, limit: int
    ) -> list[SuggestionCandidate]:
        with self._lock:
            # Dicts keep insertion order, so reversed() is newest first.
            users = [
                user
                for user in reversed(self._users.values())
                if user.id != exclude_user_id and user.email_verified and user.role in ATHLETE_ROLES
            ]
            return [
                SuggestionCandidate(
                    id=user.id,
                    nombre=user.profile.nombre,
                    apellido_paterno=user.profile.apellido_paterno,
                    fecha_nacimiento=user.profile.fecha_nacimiento,
                    foto_perfil=user.profile.foto_perfil,
                    biografia=user.profile.biografia,
                    deportes=list(user.preferences.deportes),
                )
                for user in users[:limit]
            ]

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

    def create_password_reset(
        self, *, user_id: UUID, code_hash: str, expires_at: datetime
    ) -> PasswordResetRecord:
        with self._lock:
            self._password_resets = {
                key: reset
                for key, reset in self._password_resets.items()
                if reset.user_id != user_id
            }
            reset = PasswordResetRecord(
                id=uuid4(),
                user_id=user_id,
                code_hash=code_hash,
                expires_at=expires_at,
                created_at=datetime.now(timezone.utc),
                attempts=0,
            )
            self._password_resets[reset.id] = reset
            return reset

    def get_active_password_reset(self, user_id: UUID) -> PasswordResetRecord | None:
        now = datetime.now(timezone.utc)
        with self._lock:
            return next(
                (
                    reset
                    for reset in self._password_resets.values()
                    if reset.user_id == user_id and reset.expires_at > now
                ),
                None,
            )

    def register_password_reset_attempt(self, reset_id: UUID, max_attempts: int) -> bool:
        with self._lock:
            reset = self._password_resets.get(reset_id)
            if (
                reset is None
                or reset.attempts >= max_attempts
                or reset.expires_at <= datetime.now(timezone.utc)
            ):
                return False
            self._password_resets[reset_id] = replace(reset, attempts=reset.attempts + 1)
            return True

    def complete_password_reset(
        self, *, reset_id: UUID, user_id: UUID, password_hash: bytes
    ) -> bool:
        with self._lock:
            reset = self._password_resets.pop(reset_id, None)
            user = self._users.get(user_id)
            if reset is None or user is None:
                return False
            user.password_hash = password_hash
            return True

    def get_pending_email_verification(self, user_id: UUID) -> EmailVerificationRecord | None:
        with self._lock:
            return self._email_verifications.get(user_id)

    def restart_email_verification(
        self, *, user_id: UUID, code_hash: str, expires_at: datetime
    ) -> bool:
        with self._lock:
            if user_id not in self._email_verifications:
                return False
            self._email_verifications[user_id] = EmailVerificationRecord(
                user_id=user_id,
                code_hash=code_hash,
                expires_at=expires_at,
                sent_at=datetime.now(timezone.utc),
                attempts=0,
            )
            return True

    def register_email_verification_attempt(self, user_id: UUID, max_attempts: int) -> bool:
        with self._lock:
            pending = self._email_verifications.get(user_id)
            if (
                pending is None
                or pending.attempts >= max_attempts
                or pending.expires_at <= datetime.now(timezone.utc)
            ):
                return False
            self._email_verifications[user_id] = replace(pending, attempts=pending.attempts + 1)
            return True

    def complete_email_verification(self, *, user_id: UUID, code_hash: str) -> bool:
        with self._lock:
            pending = self._email_verifications.get(user_id)
            user = self._users.get(user_id)
            if (
                pending is None
                or user is None
                or pending.code_hash != code_hash
                or pending.expires_at <= datetime.now(timezone.utc)
            ):
                return False
            del self._email_verifications[user_id]
            user.email_verified = True
            return True
