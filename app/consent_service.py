"""Fail-closed personal memory consent application service."""

from collections.abc import Mapping
from enum import StrEnum

from ..core.model import (
    ConsentState,
    LedgerEventType,
    MemoryConsent,
    Scope,
)
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


ConsentType = PersonalConsentType | GroupConsentType | ExtractionConsentType | str


def _consent_type_value(consent_type: ConsentType) -> str:
    return consent_type.value if isinstance(consent_type, StrEnum) else str(consent_type)


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
              metadata: Mapping[str, object] | None = None,
              ledger_source: str = "user",
              subject_id: str | None = None) -> MemoryConsent:
        now = self._clock.now()
        consent_subject_id = subject_id or scope.identity.subject_id
        event_metadata: dict[str, object] = {"operator_id": operator_id}
        if metadata:
            event_metadata.update(metadata)
        consent = MemoryConsent(
            id=self._ids.new_id("consent"), subject_id=consent_subject_id,
            scope_id=scope.id, consent_type=_consent_type_value(consent_type),
            state=ConsentState.GRANTED, source=source, granted_at=now,
            metadata=event_metadata,
        )
        previous = self.current(scope, consent_type, subject_id=consent_subject_id)
        self._coordinator.run_metadata_serialized(
            lambda: self._repository.record(
                consent, allow_disabled_grant=allow_disabled_scope,
            ),
            ledger_event_type=LedgerEventType.CONSENT_CHANGE,
            payload=self._consent_payload(
                scope, consent, previous_state=(
                    previous.state.value if previous is not None else None
                ),
            ),
            source=ledger_source, occurred_at=now,
            scope_id=scope.id, subject_id=consent_subject_id,
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
            scope_id=scope.id, consent_type=_consent_type_value(consent_type),
            state=ConsentState.GRANTED, source="auto_enroll", granted_at=now,
            metadata={"operator_id": operator_id, "auto_enrolled": True},
        )

        def operation() -> bool:
            history = self._repository.history(
                subject_id=scope.identity.subject_id, scope_id=scope.id,
                consent_type=_consent_type_value(consent_type),
            )
            if history:
                return False
            intent = self._coordinator.ledger_intent(
                LedgerEventType.CONSENT_CHANGE,
                payload=self._consent_payload(
                    scope, consent, previous_state=None,
                ),
                source="system", occurred_at=now, scope_id=scope.id,
                subject_id=scope.identity.subject_id,
            )
            self._repository.record(consent)
            self._coordinator.ledger_committed(intent, occurred_at=now)
            return True

        return self._coordinator.write_metadata(operation)

    def revoke(self, scope: Scope, consent_type: ConsentType, *,
               operator_id: str, source: str = "explicit_user_command",
               ledger_source: str = "user",
               subject_id: str | None = None) -> MemoryConsent:
        now = self._clock.now()
        consent_subject_id = subject_id or scope.identity.subject_id
        consent = MemoryConsent(
            id=self._ids.new_id("consent"), subject_id=consent_subject_id,
            scope_id=scope.id, consent_type=_consent_type_value(consent_type),
            state=ConsentState.REVOKED, source=source, revoked_at=now,
            metadata={"operator_id": operator_id},
        )
        previous = self.current(scope, consent_type, subject_id=consent_subject_id)
        self._coordinator.run_metadata_serialized(
            lambda: self._repository.record(consent),
            ledger_event_type=LedgerEventType.CONSENT_CHANGE,
            payload=self._consent_payload(
                scope, consent, previous_state=(
                    previous.state.value if previous is not None else None
                ),
            ),
            source=ledger_source, occurred_at=now,
            scope_id=scope.id, subject_id=consent_subject_id,
        )
        return consent

    @staticmethod
    def _consent_payload(scope: Scope, consent: MemoryConsent, *,
                         previous_state: str | None) -> dict[str, object]:
        return {
            "scope_id": scope.id,
            "subject_id": consent.subject_id,
            "consent_type": consent.consent_type,
            "old_state": previous_state,
            "new_state": consent.state.value,
            "source": consent.source,
            "consent_id": consent.id,
            "granted_at": consent.granted_at.isoformat() if consent.granted_at else None,
            "revoked_at": consent.revoked_at.isoformat() if consent.revoked_at else None,
            "metadata": dict(consent.metadata),
        }

    def is_granted(self, scope: Scope, consent_type: ConsentType, *,
                   subject_id: str | None = None) -> bool:
        return self._repository.is_granted(
            subject_id=subject_id or scope.identity.subject_id, scope_id=scope.id,
            consent_type=_consent_type_value(consent_type),
        )

    def current(self, scope: Scope, consent_type: ConsentType, *,
                subject_id: str | None = None) -> MemoryConsent | None:
        return self._repository.current(
            subject_id=subject_id or scope.identity.subject_id, scope_id=scope.id,
            consent_type=_consent_type_value(consent_type),
        )

    def sensitive_categories(self, scope: Scope) -> tuple[str, ...]:
        event = self.current(scope, ExtractionConsentType.SENSITIVE_CATEGORIES)
        if event is None or event.state is not ConsentState.GRANTED:
            return ()
        raw = event.metadata.get("categories")
        if not isinstance(raw, (list, tuple)):
            return ()
        return tuple(str(item) for item in raw if isinstance(item, str) and item.strip())

    def require(self, scope: Scope, consent_type: ConsentType, *,
                subject_id: str | None = None) -> None:
        consent_subject_id = subject_id or scope.identity.subject_id
        events = self._repository.history(
            subject_id=consent_subject_id, scope_id=scope.id,
            consent_type=_consent_type_value(consent_type),
        )
        ConsentPolicy(events).require(
            subject_id=consent_subject_id, scope_id=scope.id,
            consent_type=_consent_type_value(consent_type),
        )
