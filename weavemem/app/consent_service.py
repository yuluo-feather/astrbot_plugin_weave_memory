"""Fail-closed personal memory consent application service."""

from enum import StrEnum

from ..core.errors import AuthorizationDenied
from ..core.model import ConsentState, MemoryConsent, Scope
from ..core.ports import Clock, ConsentRepository, IdGenerator


class PersonalConsentType(StrEnum):
    PERSONAL_MEMORY = "personal_memory"


class ConsentService:
    def __init__(self, repository: ConsentRepository, clock: Clock,
                 ids: IdGenerator) -> None:
        self._repository = repository
        self._clock = clock
        self._ids = ids

    def grant(self, scope: Scope, consent_type: PersonalConsentType, *,
              operator_id: str, source: str = "explicit_user_command") -> MemoryConsent:
        now = self._clock.now()
        consent = MemoryConsent(
            id=self._ids.new_id("consent"), subject_id=scope.identity.subject_id,
            scope_id=scope.id, consent_type=consent_type.value,
            state=ConsentState.GRANTED, source=source, granted_at=now,
            metadata={"operator_id": operator_id},
        )
        self._repository.record(consent)
        return consent

    def revoke(self, scope: Scope, consent_type: PersonalConsentType, *,
               operator_id: str, source: str = "explicit_user_command") -> MemoryConsent:
        now = self._clock.now()
        consent = MemoryConsent(
            id=self._ids.new_id("consent"), subject_id=scope.identity.subject_id,
            scope_id=scope.id, consent_type=consent_type.value,
            state=ConsentState.REVOKED, source=source, revoked_at=now,
            metadata={"operator_id": operator_id},
        )
        self._repository.record(consent)
        return consent

    def is_granted(self, scope: Scope, consent_type: PersonalConsentType) -> bool:
        return self._repository.is_granted(
            subject_id=scope.identity.subject_id, scope_id=scope.id,
            consent_type=consent_type.value,
        )

    def require(self, scope: Scope, consent_type: PersonalConsentType) -> None:
        if not self.is_granted(scope, consent_type):
            raise AuthorizationDenied(f"{consent_type.value} consent is not granted")
