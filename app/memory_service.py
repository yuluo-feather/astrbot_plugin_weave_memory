"""Personal-memory use cases: explicit save/update and the constrained agent tool write."""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime

from ..core.errors import Conflict, NotFound, ValidationError
from ..core.model import (
    MemoryEntity,
    MemoryOperation,
    MemoryRevision,
    MemorySource,
    MemoryStatus,
    MemoryType,
    OperatorType,
    Scope,
    SourceType,
)
from ..core.normalization import normalize_content
from ..core.policy.auto_commit import similarity
from ..core.policy.dedup import DedupKey
from ..core.ports import Clock, IdGenerator, MemoryStore
from .consent_service import ConsentService, PersonalConsentType
from .export import EXPORTED_STATUSES, build_export
from .lifecycle_service import LifecycleService
from .write_coordinator import OperationReceipt, WriteCoordinator, WriteReceipt

_SOURCE_PRIORITY = {
    SourceType.INFERRED: 1,
    SourceType.OBSERVED: 2,
    SourceType.IMPORTED: 2,
    SourceType.MANUAL: 3,
    SourceType.EXPLICIT: 4,
    SourceType.AGENT: 4,
}


class MemoryApplicationService:
    def __init__(self, store: MemoryStore, coordinator: WriteCoordinator,
                 consent: ConsentService, lifecycle: LifecycleService,
                 clock: Clock, ids: IdGenerator, *,
                 consolidation_similarity: float = 0.9) -> None:
        self._store = store
        self._coordinator = coordinator
        self._consent = consent
        self._lifecycle = lifecycle
        self._clock = clock
        self._ids = ids
        self._consolidation_similarity = min(
            1.0, max(0.0, consolidation_similarity),
        )

    async def _expire_overdue_duplicate(self, scope: Scope, memory_type: MemoryType,
                                        normalized: str, now: datetime) -> None:
        """Expire an overdue ACTIVE duplicate so it stops blocking the unique key."""
        for memory in self._store.list_active(
            scope_id=scope.id, subject_id=scope.identity.subject_id,
            at=now, include_overdue=True,
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

    def _reject_candidate_collision(
        self, scope: Scope, memory_type: MemoryType, normalized: str,
    ) -> None:
        candidate = self._store.find_candidate_duplicate(
            DedupKey(
                scope.id, scope.identity.subject_id, memory_type.value, normalized,
            ),
        )
        if candidate is not None:
            raise Conflict(
                f"已存在同内容候选记忆 {candidate.id}；"
                f"请先 /织忆 候选 确认 {candidate.id} 或 /织忆 候选 拒绝 {candidate.id}"
            )

    def _near_duplicate_active(
        self, scope: Scope, memory_type: MemoryType, content: str,
        now: datetime,
    ) -> MemoryEntity | None:
        if self._consolidation_similarity <= 0:
            return None
        rows = self._store.list_active(
            scope_id=scope.id, subject_id=scope.identity.subject_id, at=now,
        )
        for memory in rows:
            if memory.memory_type is not memory_type:
                continue
            if similarity(content, memory.content) >= self._consolidation_similarity:
                return memory
        return None

    async def _merge_into_active(
        self, scope: Scope, current: MemoryEntity, content: str, *,
        source_type: SourceType, operator_type: OperatorType,
        operator_id: str, reason: str, tags: Sequence[str],
        source_metadata: Mapping[str, object] | None, now: datetime,
    ) -> tuple[MemoryEntity, WriteReceipt]:
        """V1.9 M4b: upgrade one near-duplicate ACTIVE row instead of duplicating it."""
        normalized = normalize_content(content)
        existing_tags = list(current.metadata.get("tags", []))
        merged_tags = list(dict.fromkeys((*existing_tags, *tags)))
        metadata: dict[str, object] = {
            **dict(current.metadata),
            "merged_sources": [
                *list(current.metadata.get("merged_sources", [])),
                f"{source_type.value.lower()}_save",
            ],
            "last_merged_at": now.isoformat(),
        }
        if merged_tags:
            metadata["tags"] = merged_tags
        new_source_type = (
            source_type
            if _SOURCE_PRIORITY[source_type] >= _SOURCE_PRIORITY[current.source_type]
            else current.source_type
        )
        if new_source_type in {SourceType.EXPLICIT, SourceType.AGENT}:
            metadata["auto"] = False
            metadata["user_confirmed"] = True
        updated = replace(
            current, content=content, normalized_content=normalized,
            source_type=new_source_type,
            current_revision=current.current_revision + 1, updated_at=now,
            metadata=metadata,
        )
        revision = MemoryRevision(
            id=self._ids.new_id("revision"), memory_id=current.id,
            revision=updated.current_revision, content=content,
            status=updated.status, changed_at=now, change_reason=reason,
            snapshot={
                "scope_id": scope.id, "subject_id": scope.identity.subject_id,
                "memory_type": updated.memory_type.value,
                "source_type": updated.source_type.value,
                "merged_similarity": round(
                    similarity(content, current.content), 4,
                ),
            },
        )
        source = MemorySource(
            id=self._ids.new_id("source"), memory_id=current.id,
            source_type=new_source_type, operator_type=operator_type,
            operator_id=operator_id, captured_at=now,
            metadata={
                **dict(source_metadata or {}),
                "merged_similarity": round(similarity(content, current.content), 4),
            },
        )
        receipt = await self._coordinator.update(
            updated, expected_revision=current.current_revision,
            revision=revision, source=source,
            guard=lambda: self._consent.require(
                scope, PersonalConsentType.PERSONAL_MEMORY,
            ),
        )
        self._store.record_operation(
            operation_id=self._ids.new_id("operation"), scope_id=scope.id,
            memory_id=current.id, operation_type="MEMORY_MERGE",
            operator_type=operator_type, operator_id=operator_id,
            occurred_at=now,
            metadata={
                "similarity": round(similarity(content, current.content), 4),
                "result": "SUCCESS",
            },
        )
        return updated, receipt

    async def save_explicit(self, scope: Scope, content: str, *,
                            operator_id: str,
                            memory_type: MemoryType = MemoryType.FACT,
                            tags: Sequence[str] = (),
                            ) -> tuple[MemoryEntity, WriteReceipt]:
        self._consent.require(scope, PersonalConsentType.PERSONAL_MEMORY)
        normalized = normalize_content(content)
        now = self._clock.now()
        await self._expire_overdue_duplicate(scope, memory_type, normalized, now)
        self._reject_candidate_collision(scope, memory_type, normalized)
        near = self._near_duplicate_active(scope, memory_type, content, now)
        if near is not None and near.normalized_content != normalized:
            return await self._merge_into_active(
                scope, near, content, source_type=SourceType.EXPLICIT,
                operator_type=OperatorType.USER, operator_id=operator_id,
                reason="explicit save merged near duplicate", tags=tags,
                source_metadata=None, now=now,
            )
        memory_id = self._ids.new_id("memory")
        memory = MemoryEntity(
            id=memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
            memory_type=memory_type, content=content, normalized_content=normalized,
            status=MemoryStatus.ACTIVE, source_type=SourceType.EXPLICIT,
            current_revision=1, created_at=now, updated_at=now,
            metadata={"tags": list(tags)} if tags else {},
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

    async def save_agent(self, scope: Scope, content: str, *,
                         operator_id: str,
                         evidence_ref: str,
                         memory_type: MemoryType = MemoryType.FACT,
                         source_metadata: Mapping[str, object] | None = None,
                         tags: Sequence[str] = (),
                         ) -> tuple[MemoryEntity, WriteReceipt]:
        """Save a memory requested by the model through an explicit user signal.

        The model tool is constrained by its description; the source remains
        AGENT so it is distinguishable from /织忆 保存 and from inferred
        background extraction (which only produces candidates in V1.5).
        """
        if not evidence_ref.strip():
            raise ValidationError("evidence_ref must be non-empty")
        self._consent.require(scope, PersonalConsentType.PERSONAL_MEMORY)
        normalized = normalize_content(content)
        now = self._clock.now()
        await self._expire_overdue_duplicate(scope, memory_type, normalized, now)
        self._reject_candidate_collision(scope, memory_type, normalized)
        near = self._near_duplicate_active(scope, memory_type, content, now)
        if near is not None and near.normalized_content != normalized:
            return await self._merge_into_active(
                scope, near, content, source_type=SourceType.AGENT,
                operator_type=OperatorType.AGENT, operator_id=operator_id,
                reason="agent memory tool merged near duplicate", tags=tags,
                source_metadata=source_metadata, now=now,
            )
        memory_id = self._ids.new_id("memory")
        memory = MemoryEntity(
            id=memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
            memory_type=memory_type, content=content, normalized_content=normalized,
            status=MemoryStatus.ACTIVE, source_type=SourceType.AGENT,
            current_revision=1, created_at=now, updated_at=now,
            metadata={"tags": list(tags)} if tags else {},
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
            operator_id=operator_id, captured_at=now, evidence_ref=evidence_ref,
            metadata=source_metadata or {},
        )
        saved, receipt, _created = await self._coordinator.create_or_get_duplicate(
            memory, revision, source,
            guard=lambda: self._consent.require(scope, PersonalConsentType.PERSONAL_MEMORY),
        )
        return saved, receipt

    async def update_agent(
        self, scope: Scope, memory_id: str, content: str, *,
        expected_revision: int, operator_id: str, evidence_ref: str,
        source_metadata: Mapping[str, object] | None = None,
    ) -> tuple[MemoryEntity, WriteReceipt]:
        """Update only the body of a selected Personal memory as AGENT."""
        if not evidence_ref.strip():
            raise ValidationError("evidence_ref must be non-empty")
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
            operator_id=operator_id, captured_at=now, evidence_ref=evidence_ref,
            metadata=source_metadata or {},
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
        return self._store.list_active(
            scope_id=scope.id, subject_id=scope.identity.subject_id,
            at=self._clock.now(),
        )

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

    async def archive_memory(self, scope: Scope, memory_id: str, *,
                             operator_id: str) -> WriteReceipt:
        self._consent.require(scope, PersonalConsentType.PERSONAL_MEMORY)
        memory = self._store.get(
            memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
        )
        if memory.status is not MemoryStatus.ACTIVE:
            raise Conflict("only ACTIVE memory can be archived")
        now = self._clock.now()
        operation = MemoryOperation(
            id=self._ids.new_id("operation"), scope_id=scope.id, memory_id=memory_id,
            operation_type="ARCHIVE_MEMORY", operator_type=OperatorType.USER,
            operator_id=operator_id, occurred_at=now,
            metadata={"archive_reason": "USER_ARCHIVE"},
        )
        return await self._lifecycle.archive_memory(
            memory, revision_id=self._ids.new_id("revision"), at=now,
            reason="user archived memory", operator_type=OperatorType.USER,
            operation=operation,
        )

    async def restore_memory(self, scope: Scope, memory_id: str, *,
                             operator_id: str) -> WriteReceipt:
        self._consent.require(scope, PersonalConsentType.PERSONAL_MEMORY)
        memory = self._store.get(
            memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
        )
        now = self._clock.now()
        operation = MemoryOperation(
            id=self._ids.new_id("operation"), scope_id=scope.id, memory_id=memory_id,
            operation_type="RESTORE_MEMORY", operator_type=OperatorType.USER,
            operator_id=operator_id, occurred_at=now,
            metadata={"archive_reason": memory.archive_reason.value if memory.archive_reason else None},
        )
        return await self._lifecycle.restore_memory(
            memory, revision_id=self._ids.new_id("revision"), at=now,
            reason="user restored memory", operator_type=OperatorType.USER,
            operation=operation,
        )

    async def purge_memory(self, scope: Scope, memory_id: str, *,
                           operator_id: str) -> OperationReceipt:
        self._consent.require(scope, PersonalConsentType.PERSONAL_MEMORY)
        memory = self._store.get(
            memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
        )
        if memory.status is MemoryStatus.ACTIVE:
            now = self._clock.now()
            archive_operation = MemoryOperation(
                id=self._ids.new_id("operation"), scope_id=scope.id, memory_id=memory_id,
                operation_type="ARCHIVE_MEMORY", operator_type=OperatorType.USER,
                operator_id=operator_id, occurred_at=now,
                metadata={"archive_reason": "USER_ARCHIVE", "reason": "purge_requested"},
            )
            await self._lifecycle.archive_memory(
                memory, revision_id=self._ids.new_id("revision"), at=now,
                reason="archive before explicit purge", operator_type=OperatorType.USER,
                operation=archive_operation,
            )
            memory = self._store.get(
                memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
            )
        if memory.status is not MemoryStatus.ARCHIVED:
            raise Conflict("only ACTIVE or ARCHIVED memory can be purged")
        now = self._clock.now()
        return await self._lifecycle.purge_memory(
            memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
            operation_id=self._ids.new_id("operation"),
            operator_type=OperatorType.USER, operator_id=operator_id, at=now,
        )

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
            at=self._clock.now(),
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

    def statistics(self, scope: Scope, *,
                   require_consent: bool = True) -> dict[str, object]:
        """Counts only; management/audit reads may skip the consent re-check."""
        if require_consent:
            self._consent.require(scope, PersonalConsentType.PERSONAL_MEMORY)
        return self._store.statistics(
            scope_id=scope.id, subject_id=scope.identity.subject_id,
            at=self._clock.now(),
        )

    def operations(
        self, scope: Scope, *, limit: int = 20, offset: int = 0,
        require_consent: bool = True,
    ) -> tuple[tuple[dict[str, object], ...], int]:
        if type(limit) is not int or limit < 1 or limit > 100:
            raise ValidationError("limit must be between 1 and 100")
        if type(offset) is not int or offset < 0:
            raise ValidationError("offset must be non-negative")
        if require_consent:
            self._consent.require(scope, PersonalConsentType.PERSONAL_MEMORY)
        return self._store.list_operations(
            scope_id=scope.id, subject_id=scope.identity.subject_id,
            limit=limit, offset=offset,
        )

    def export_personal(self, scope: Scope, *, include_all: bool = False) -> dict:
        """Export only this Personal scope; consent is re-checked before reading."""
        self._consent.require(scope, PersonalConsentType.PERSONAL_MEMORY)
        if include_all:
            memories = self._store.list_for_export(
                scope_id=scope.id, subject_id=scope.identity.subject_id,
                statuses=EXPORTED_STATUSES, at=self._clock.now(),
            )
        else:
            memories = self.list_active(scope)
        return build_export(
            scope, memories, exported_at=self._clock.now(),
            export_id=self._ids.new_id("export"),
        )
