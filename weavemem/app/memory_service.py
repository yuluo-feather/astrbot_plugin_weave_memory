"""Explicit personal-memory use cases; no automatic extraction in V1.0."""

from dataclasses import replace

from ..core.errors import Conflict
from ..core.model import (
    MemoryEntity,
    MemoryRevision,
    MemorySource,
    MemoryStatus,
    MemoryType,
    OperatorType,
    Scope,
    SourceType,
)
from ..core.normalization import normalize_content
from ..core.ports import Clock, IdGenerator, MemoryStore
from .consent_service import ConsentService, PersonalConsentType
from .write_coordinator import WriteCoordinator, WriteReceipt


class MemoryApplicationService:
    def __init__(self, store: MemoryStore, coordinator: WriteCoordinator,
                 consent: ConsentService, clock: Clock, ids: IdGenerator) -> None:
        self._store = store
        self._coordinator = coordinator
        self._consent = consent
        self._clock = clock
        self._ids = ids

    async def save_explicit(self, scope: Scope, content: str, *,
                            operator_id: str,
                            memory_type: MemoryType = MemoryType.FACT) -> tuple[MemoryEntity, WriteReceipt]:
        self._consent.require(scope, PersonalConsentType.PERSONAL_MEMORY)
        normalized = normalize_content(content)
        now = self._clock.now()
        memory_id = self._ids.new_id("memory")
        memory = MemoryEntity(
            id=memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
            memory_type=memory_type, content=content, normalized_content=normalized,
            status=MemoryStatus.ACTIVE, source_type=SourceType.EXPLICIT,
            current_revision=1, created_at=now, updated_at=now,
        )
        revision = MemoryRevision(
            id=self._ids.new_id("revision"), memory_id=memory_id, revision=1,
            content=content, status=MemoryStatus.ACTIVE, changed_at=now,
            change_reason="explicit user save",
            snapshot={"scope_id": scope.id, "subject_id": scope.identity.subject_id,
                      "memory_type": memory_type.value},
        )
        source = MemorySource(
            id=self._ids.new_id("source"), memory_id=memory_id,
            source_type=SourceType.EXPLICIT, operator_type=OperatorType.USER,
            operator_id=operator_id, captured_at=now,
        )
        saved, receipt, _created = await self._coordinator.create_or_get_duplicate(
            memory, revision, source,
            guard=lambda: self._consent.require(scope, PersonalConsentType.PERSONAL_MEMORY),
        )
        return saved, receipt

    async def update_explicit(self, scope: Scope, memory_id: str, content: str, *,
                              expected_revision: int, operator_id: str,
                              reason: str = "explicit user update") -> tuple[MemoryEntity, WriteReceipt]:
        """Update one explicitly selected memory using optimistic concurrency.

        The caller must identify the memory; this method never guesses which
        other fact a new sentence should supersede.
        """
        self._consent.require(scope, PersonalConsentType.PERSONAL_MEMORY)
        current = self._store.get(
            memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
        )
        if current.status is not MemoryStatus.ACTIVE:
            raise Conflict("only ACTIVE memories can be explicitly edited")
        if current.current_revision != expected_revision:
            raise Conflict("revision conflict")
        normalized = normalize_content(content)
        now = self._clock.now()
        updated = replace(
            current, content=content, normalized_content=normalized,
            current_revision=current.current_revision + 1, updated_at=now,
        )
        revision = MemoryRevision(
            id=self._ids.new_id("revision"), memory_id=memory_id,
            revision=updated.current_revision, content=updated.content,
            status=updated.status, changed_at=now, change_reason=reason,
            snapshot={"scope_id": scope.id, "subject_id": scope.identity.subject_id,
                      "memory_type": updated.memory_type.value,
                      "source_type": updated.source_type.value,
                      "normalized_content": updated.normalized_content},
        )
        source = MemorySource(
            id=self._ids.new_id("source"), memory_id=memory_id,
            source_type=SourceType.EXPLICIT, operator_type=OperatorType.USER,
            operator_id=operator_id, captured_at=now,
        )
        receipt = await self._coordinator.update(
            updated, expected_revision=expected_revision,
            revision=revision, source=source,
            guard=lambda: self._consent.require(scope, PersonalConsentType.PERSONAL_MEMORY),
        )
        return updated, receipt

    def list_active(self, scope: Scope) -> tuple[MemoryEntity, ...]:
        self._consent.require(scope, PersonalConsentType.PERSONAL_MEMORY)
        return self._store.list_active(scope_id=scope.id,
                                       subject_id=scope.identity.subject_id)
