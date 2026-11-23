"""Fail-closed personal memory consent application service."""

from collections.abc import Mapping
from enum import StrEnum

from ..core.model import ConsentState, MemoryConsent, Scope
from ..core.policy.consent import ConsentPolicy
from ..core.ports import Clock, ConsentRepository, IdGenerator
from .write_coordinator import WriteCoordinator


class PersonalConsentType(StrEnum):
    PERSONAL_MEMORY = "personal_memory"


class GroupConsentType(StrEnum):
    GROUP_MEMORY = "group_memory"


class ExtractionConsentType(StrEnum):
    CAPTURE = "extraction_capture"
    AUTO_EXTRACT = "extraction_auto_extract"
    EXTERNAL_MODEL = "extraction_external_model"
    STORE_CANDIDATE = "extraction_store_candidate"
    SENSITIVE_CATEGORIES = "extraction_sensitive_categories"


ConsentType = PersonalConsentType | GroupConsentType | ExtractionConsentType


class ConsentService:
    def __init__(self, repository: ConsentRepository, coordinator: WriteCoordinator,
                 clock: Clock, ids: IdGenerator) -> None:
        self._repository = repository
        self._coordinator = coordinator
        self._clock = clock
        self._ids = ids

    def grant(self, scope: Scope, consent_type: ConsentType, *,
              operator_id: str, source: str = "explicit_user_command",
              allow_disabled_scope: bool = False,
              metadata: Mapping[str, object] | None = None) -> MemoryConsent:
        now = self._clock.now()
        event_metadata: dict[str, object] = {"operator_id": operator_id}
        if metadata:
            event_metadata.update(metadata)
        consent = MemoryConsent(
            id=self._ids.new_id("consent"), subject_id=scope.identity.subject_id,
            scope_id=scope.id, consent_type=consent_type.value,
            state=ConsentState.GRANTED, source=source, granted_at=now,
            metadata=event_metadata,
        )
        self._coordinator.write_metadata(
            lambda: self._repository.record(
                consent, allow_disabled_grant=allow_disabled_scope,
            ),
        )
        return consent

    def ensure_auto_enrolled(self, scope: Scope, consent_type: ConsentType, *,
                             operator_id: str) -> bool:
        """Grant consent only when the scope has no consent history.

        An existing GRANTED or REVOKED event always wins: auto-enrollment must
        never override an explicit user decision, including /织忆 撤销授权.
        """
        now = self._clock.now()
        consent = MemoryConsent(
            id=self._ids.new_id("consent"), subject_id=scope.identity.subject_id,
            scope_id=scope.id, consent_type=consent_type.value,
            state=ConsentState.GRANTED, source="auto_enroll", granted_at=now,
            metadata={"operator_id": operator_id, "auto_enrolled": True},
        )

        def operation() -> bool:
            history = self._repository.history(
                subject_id=scope.identity.subject_id, scope_id=scope.id,
                consent_type=consent_type.value,
            )
            if history:
                return False
            self._repository.record(consent)
            return True

        return self._coordinator.write_metadata(operation)

    def revoke(self, scope: Scope, consent_type: ConsentType, *,
               operator_id: str, source: str = "explicit_user_command") -> MemoryConsent:
        now = self._clock.now()
        consent = MemoryConsent(
            id=self._ids.new_id("consent"), subject_id=scope.identity.subject_id,
            scope_id=scope.id, consent_type=consent_type.value,
            state=ConsentState.REVOKED, source=source, revoked_at=now,
            metadata={"operator_id": operator_id},
        )
        self._coordinator.write_metadata(lambda: self._repository.record(consent))
        return consent

    def is_granted(self, scope: Scope, consent_type: ConsentType) -> bool:
        return self._repository.is_granted(
            subject_id=scope.identity.subject_id, scope_id=scope.id,
            consent_type=consent_type.value,
        )

    def current(self, scope: Scope, consent_type: ConsentType) -> MemoryConsent | None:
        return self._repository.current(
            subject_id=scope.identity.subject_id, scope_id=scope.id,
            consent_type=consent_type.value,
        )

    def sensitive_categories(self, scope: Scope) -> tuple[str, ...]:
        event = self.current(scope, ExtractionConsentType.SENSITIVE_CATEGORIES)
        if event is None or event.state is not ConsentState.GRANTED:
            return ()
        raw = event.metadata.get("categories")
        if not isinstance(raw, (list, tuple)):
            return ()
        return tuple(str(item) for item in raw if isinstance(item, str) and item.strip())

    def require(self, scope: Scope, consent_type: ConsentType) -> None:
        events = self._repository.history(
            subject_id=scope.identity.subject_id, scope_id=scope.id,
            consent_type=consent_type.value,
        )
        ConsentPolicy(events).require(
            subject_id=scope.identity.subject_id, scope_id=scope.id,
            consent_type=consent_type.value,
        )
