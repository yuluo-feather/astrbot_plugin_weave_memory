"""Personal-memory use cases: explicit save/update and the constrained agent tool write."""

from dataclasses import replace
from datetime import datetime

from ..core.errors import Conflict, NotFound, ValidationError
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
from .export import build_export
from .lifecycle_service import LifecycleService
from .write_coordinator import WriteCoordinator, WriteReceipt


class MemoryApplicationService:
    def __init__(self, store: MemoryStore, coordinator: WriteCoordinator,
                 consent: ConsentService, lifecycle: LifecycleService,
                 clock: Clock, ids: IdGenerator) -> None:
        self._store = store
        self._coordinator = coordinator
        self._consent = consent
        self._lifecycle = lifecycle
        self._clock = clock
        self._ids = ids

    async def _expire_overdue_duplicate(self, scope: Scope, memory_type: MemoryType,
                                        normalized: str, now: datetime) -> None:
        """Expire an overdue ACTIVE duplicate so it stops blocking the unique key."""
        for memory in self._store.list_active(
            scope_id=scope.id, subject_id=scope.identity.subject_id,
        ):
            if (memory.memory_type is memory_type
                    and memory.normalized_content == normalized
                    and memory.valid_until is not None
                    and memory.valid_until <= now):
                try:
                    await self._lifecycle.expire_active(
                        memory, revision_id=self._ids.new_id("revision"), at=now,
                    )
                except (Conflict, NotFound):
                    # Another writer expired, deleted, or cleared it first.
                    pass

    async def save_explicit(self, scope: Scope, content: str, *,
                            operator_id: str,
                            memory_type: MemoryType = MemoryType.FACT) -> tuple[MemoryEntity, WriteReceipt]:
        self._consent.require(scope, PersonalConsentType.PERSONAL_MEMORY)
        normalized = normalize_content(content)
        now = self._clock.now()
        await self._expire_overdue_duplicate(scope, memory_type, normalized, now)
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

    # AGENT writes stay behind agent_write_enabled until a deterministic trigger
    # phrase check, candidate mode, and a daily cap are implemented.
    async def save_agent(self, scope: Scope, content: str, *,
                         operator_id: str,
                         tool_call_id: str,
                         memory_type: MemoryType = MemoryType.FACT) -> tuple[MemoryEntity, WriteReceipt]:
        """Save a memory requested by the model through an explicit user signal.

        The model tool is constrained by its description; the source remains
        AGENT so it is distinguishable from /织忆 保存 and from inferred
        background extraction (which only produces candidates in V1.5).
        """
        if not tool_call_id.strip():
            raise ValidationError("tool_call_id must be non-empty")
        self._consent.require(scope, PersonalConsentType.PERSONAL_MEMORY)
        normalized = normalize_content(content)
        now = self._clock.now()
        await self._expire_overdue_duplicate(scope, memory_type, normalized, now)
        memory_id = self._ids.new_id("memory")
        memory = MemoryEntity(
            id=memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
            memory_type=memory_type, content=content, normalized_content=normalized,
            status=MemoryStatus.ACTIVE, source_type=SourceType.AGENT,
            current_revision=1, created_at=now, updated_at=now,
        )
        revision = MemoryRevision(
            id=self._ids.new_id("revision"), memory_id=memory_id, revision=1,
            content=content, status=MemoryStatus.ACTIVE, changed_at=now,
            change_reason="agent memory tool",
            snapshot={"scope_id": scope.id, "subject_id": scope.identity.subject_id,
                      "memory_type": memory_type.value, "source_type": SourceType.AGENT.value},
        )
        source = MemorySource(
            id=self._ids.new_id("source"), memory_id=memory_id,
            source_type=SourceType.AGENT, operator_type=OperatorType.AGENT,
            operator_id=operator_id, captured_at=now, evidence_ref=tool_call_id,
        )
        saved, receipt, _created = await self._coordinator.create_or_get_duplicate(
            memory, revision, source,
            guard=lambda: self._consent.require(scope, PersonalConsentType.PERSONAL_MEMORY),
        )
        return saved, receipt

    async def update_agent(
        self, scope: Scope, memory_id: str, content: str, *,
        expected_revision: int, operator_id: str, tool_call_id: str,
    ) -> tuple[MemoryEntity, WriteReceipt]:
        """Update only the body of a selected Personal memory as AGENT."""
        if not tool_call_id.strip():
            raise ValidationError("tool_call_id must be non-empty")
        if expected_revision < 1:
            raise ValidationError("expected_revision must be >= 1")
        self._consent.require(scope, PersonalConsentType.PERSONAL_MEMORY)
        current = self._store.get(
            memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
        )
        if current.status is not MemoryStatus.ACTIVE:
            raise Conflict("only ACTIVE memories can be updated")
        if current.current_revision != expected_revision:
            raise Conflict("revision conflict")
        normalized = normalize_content(content)
        now = self._clock.now()
        updated = replace(
            current,
            content=content,
            normalized_content=normalized,
            source_type=SourceType.AGENT,
            current_revision=current.current_revision + 1,
            updated_at=now,
        )
        revision = MemoryRevision(
            id=self._ids.new_id("revision"), memory_id=memory_id,
            revision=updated.current_revision, content=updated.content,
            status=updated.status, changed_at=now,
            change_reason="agent memory tool update",
            snapshot={
                "scope_id": scope.id,
                "subject_id": scope.identity.subject_id,
                "memory_type": updated.memory_type.value,
                "source_type": SourceType.AGENT.value,
                "normalized_content": updated.normalized_content,
            },
        )
        source = MemorySource(
            id=self._ids.new_id("source"), memory_id=memory_id,
            source_type=SourceType.AGENT, operator_type=OperatorType.AGENT,
            operator_id=operator_id, captured_at=now, evidence_ref=tool_call_id,
        )
        receipt = await self._coordinator.update(
            updated, expected_revision=expected_revision,
            revision=revision, source=source,
            guard=lambda: self._consent.require(scope, PersonalConsentType.PERSONAL_MEMORY),
        )
        return updated, receipt

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
            source_type=SourceType.EXPLICIT,
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

    def get_memory(self, scope: Scope, memory_id: str) -> MemoryEntity:
        self._consent.require(scope, PersonalConsentType.PERSONAL_MEMORY)
        memory = self._store.get(
            memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
        )
        if memory.status is not MemoryStatus.ACTIVE:
            # Match the Agent get contract: non-active records are not readable
            # through this tool, and must not reveal their lifecycle state.
            raise NotFound("memory not found")
        return memory

    def list_active_page(
        self, scope: Scope, *, limit: int = 20, offset: int = 0,
        memory_type: MemoryType | None = None,
    ) -> tuple[tuple[MemoryEntity, ...], int]:
        if type(limit) is not int or limit < 1 or limit > 100:
            raise ValidationError("limit must be between 1 and 100")
        if type(offset) is not int or offset < 0:
            raise ValidationError("offset must be non-negative")
        self._consent.require(scope, PersonalConsentType.PERSONAL_MEMORY)
        return self._store.list_active_page(
            scope_id=scope.id, subject_id=scope.identity.subject_id,
            limit=limit, offset=offset, memory_type=memory_type,
        )

    def history(
        self, scope: Scope, memory_id: str,
    ) -> tuple[tuple[MemoryRevision, ...], tuple[MemorySource, ...]]:
        self._consent.require(scope, PersonalConsentType.PERSONAL_MEMORY)
        # Verify visibility before reading either append-only history table.
        self._store.get(
            memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
        )
        revisions = self._store.list_revisions(
            memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
        )
        sources = self._store.list_sources(
            memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
        )
        return revisions, sources

    def statistics(self, scope: Scope) -> dict[str, object]:
        self._consent.require(scope, PersonalConsentType.PERSONAL_MEMORY)
        return self._store.statistics(
            scope_id=scope.id, subject_id=scope.identity.subject_id,
        )

    def operations(
        self, scope: Scope, *, limit: int = 20, offset: int = 0,
    ) -> tuple[tuple[dict[str, object], ...], int]:
        if type(limit) is not int or limit < 1 or limit > 100:
            raise ValidationError("limit must be between 1 and 100")
        if type(offset) is not int or offset < 0:
            raise ValidationError("offset must be non-negative")
        self._consent.require(scope, PersonalConsentType.PERSONAL_MEMORY)
        return self._store.list_operations(
            scope_id=scope.id, subject_id=scope.identity.subject_id,
            limit=limit, offset=offset,
        )

    def export_personal(self, scope: Scope) -> dict:
        """Export only this Personal scope; consent is re-checked by list_active."""
        return build_export(scope, self.list_active(scope), exported_at=self._clock.now())
