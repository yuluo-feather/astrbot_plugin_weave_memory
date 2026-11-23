"""Single-process serialized write coordinator for V0.4."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from threading import RLock
from typing import TypeVar

from ..core.errors import (
    IndexUnavailable,
    InvalidTransition,
    ServiceClosed,
    StorageError,
)
from ..core.model import (
    LedgerEvent,
    LedgerEventType,
    MemoryEntity,
    MemoryOperation,
    MemoryRevision,
    MemorySource,
    OperatorType,
)
from ..core.policy.dedup import DedupKey
from ..core.ports import MemoryStore, SearchIndex
from .ledger_service import LedgerService

T = TypeVar("T")


class IndexState(StrEnum):
    HEALTHY = "HEALTHY"
    PENDING_REBUILD = "PENDING_REBUILD"


class ImportWriteOutcome(StrEnum):
    CREATED = "CREATED"
    DUPLICATE_IMPORT = "DUPLICATE_IMPORT"
    DUPLICATE_ACTIVE = "DUPLICATE_ACTIVE"


@dataclass(frozen=True, slots=True)
class WriteReceipt:
    entity_id: str
    revision: int
    index_state: IndexState


@dataclass(frozen=True, slots=True)
class ImportWriteResult:
    outcome: ImportWriteOutcome
    memory: MemoryEntity | None = None
    receipt: WriteReceipt | None = None


@dataclass(frozen=True, slots=True)
class OperationReceipt:
    operation_id: str
    affected_count: int
    index_state: IndexState
    purged: bool | None = None


class WriteCoordinator:
    """One asyncio event loop, one application instance, one serialized writer.

    SQLite work is synchronous and intentionally runs on the owning event-loop
    thread because the SQLite connection is thread-affine. No await occurs while
    the lock is held except lock acquisition itself.
    """

    def __init__(self, store: MemoryStore, index: SearchIndex,
                 ledger: LedgerService | None = None) -> None:
        self._store = store
        self._index = index
        self._ledger = ledger
        self._lock = asyncio.Lock()
        self._write_lock = RLock()
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise ServiceClosed("write coordinator is closed")

    def write_metadata(self, operation: Callable[[], T]) -> T:
        """Serialize synchronous scope/consent metadata writes with content writes."""
        with self._write_lock:
            self._ensure_open()
            return operation()

    async def run_serialized(self, operation: Callable[[], T]) -> T:
        """Run a short synchronous DB operation under the same write lease."""
        async with self._lock:
            with self._write_lock:
                self._ensure_open()
                return operation()

    def run_metadata_serialized(self, operation: Callable[[], T], *,
                                ledger_event_type: LedgerEventType | None = None,
                                payload: dict[str, object] | None = None,
                                source: str = "user",
                                occurred_at: datetime | None = None,
                                scope_id: str | None = None,
                                subject_id: str | None = None,
                                memory_id: str | None = None) -> T:
        """Metadata write under the same lease, optionally wrapped in ledger events."""
        with self._write_lock:
            self._ensure_open()
            ledger = self._ledger
            intent = None
            if ledger_event_type is not None and ledger is not None and ledger.enabled:
                intent = ledger.record_intent(
                    ledger_event_type, payload=payload or {},
                    source=source, occurred_at=occurred_at,
                    scope_id=scope_id, subject_id=subject_id,
                    memory_id=memory_id,
                )
            result = operation()
            if intent is not None and ledger is not None:
                ledger.record_committed(intent)
            return result

    def run_write_locked(self, operation: Callable[[], T]) -> T:
        """Synchronous maintenance write (backup/restore) under the write lease."""
        with self._write_lock:
            self._ensure_open()
            return operation()

    def ledger_intent(self, event_type: LedgerEventType, *,
                      payload: dict[str, object], source: str,
                      occurred_at: datetime, scope_id: str | None = None,
                      subject_id: str | None = None,
                      memory_id: str | None = None) -> LedgerEvent | None:
        """Append an intent; callers must already hold the write lease."""
        if self._ledger is None or not self._ledger.enabled:
            return None
        return self._ledger.record_intent(
            event_type, payload=payload, source=source,
            occurred_at=occurred_at, scope_id=scope_id,
            subject_id=subject_id, memory_id=memory_id,
        )

    def ledger_committed(self, intent: LedgerEvent | None, *,
                         occurred_at: datetime | None = None) -> None:
        """Append the matching committed event; callers must hold the lease."""
        if self._ledger is not None and intent is not None:
            self._ledger.record_committed(intent, occurred_at=occurred_at)

    def _index_state(self) -> IndexState:
        try:
            healthy = self._index.is_healthy()
        except IndexUnavailable:
            return IndexState.PENDING_REBUILD
        return IndexState.HEALTHY if healthy else IndexState.PENDING_REBUILD

    def _sync_one(self, memory_id: str) -> IndexState:
        try:
            self._index.sync_memory(memory_id)
        except IndexUnavailable:
            return IndexState.PENDING_REBUILD
        return self._index_state()

    async def create(self, memory: MemoryEntity, revision: MemoryRevision,
                     source: MemorySource, *,
                     guard: Callable[[], None] | None = None) -> WriteReceipt:
        async with self._lock:
            with self._write_lock:
                self._ensure_open()
                if guard is not None:
                    guard()
                self._store.create(memory, revision, source)
                return WriteReceipt(memory.id, memory.current_revision,
                                    self._sync_one(memory.id))

    async def create_or_get_duplicate(self, memory: MemoryEntity,
                                      revision: MemoryRevision,
                                      source: MemorySource, *,
                                      guard: Callable[[], None]) -> tuple[MemoryEntity, WriteReceipt, bool]:
        """Serialize explicit saves and return the existing ACTIVE exact duplicate.

        The store lookup and create share this coordinator lock, preventing two
        in-process callers from racing past the idempotency check.
        """
        async with self._lock:
            with self._write_lock:
                self._ensure_open()
                guard()
                existing = self._store.find_active_duplicate(
                    DedupKey.from_memory(memory),
                )
                if existing is not None:
                    receipt = WriteReceipt(
                        existing.id, existing.current_revision, self._index_state(),
                    )
                    return existing, receipt, False
                self._store.create(memory, revision, source)
                receipt = WriteReceipt(
                    memory.id, memory.current_revision, self._sync_one(memory.id),
                )
                return memory, receipt, True

    async def create_import(self, memory: MemoryEntity,
                            revision: MemoryRevision, source: MemorySource, *,
                            guard: Callable[[], None]) -> ImportWriteResult:
        """Create one imported memory, deduplicating on both import key and content.

        The four-tuple idempotency lookup, content dedup and insert share the
        coordinator lock, so concurrent imports of the same file cannot race.
        A CANDIDATE or unique-index collision escapes as ``Conflict`` and is
        reported per entry by the import service.
        """
        async with self._lock:
            with self._write_lock:
                self._ensure_open()
                guard()
                if source.evidence_ref and self._store.find_import_source(
                    source.evidence_ref,
                ):
                    return ImportWriteResult(ImportWriteOutcome.DUPLICATE_IMPORT)
                existing = self._store.find_active_duplicate(
                    DedupKey.from_memory(memory),
                )
                if existing is not None:
                    receipt = WriteReceipt(
                        existing.id, existing.current_revision, self._index_state(),
                    )
                    return ImportWriteResult(
                        ImportWriteOutcome.DUPLICATE_ACTIVE, existing, receipt,
                    )
                self._store.create(memory, revision, source)
                receipt = WriteReceipt(
                    memory.id, memory.current_revision, self._sync_one(memory.id),
                )
                return ImportWriteResult(
                    ImportWriteOutcome.CREATED, memory, receipt,
                )

    async def update(self, memory: MemoryEntity, *, expected_revision: int,
                     revision: MemoryRevision, source: MemorySource | None = None,
                     guard: Callable[[], None] | None = None) -> WriteReceipt:
        """Update content/metadata without changing lifecycle status."""
        async with self._lock:
            with self._write_lock:
                self._ensure_open()
                current = self._store.get(
                    memory.id, scope_id=memory.scope_id, subject_id=memory.subject_id,
                )
                if current.status is not memory.status:
                    raise InvalidTransition("status changes must go through LifecycleService")
                if guard is not None:
                    guard()
                return self._update_locked(memory, expected_revision=expected_revision,
                                           revision=revision, source=source)

    async def update_lifecycle(self, memory: MemoryEntity, *, expected_revision: int,
                               revision: MemoryRevision,
                               operation: MemoryOperation | None = None) -> WriteReceipt:
        """Persist a status transition after the LifecycleService guard."""
        async with self._lock:
            with self._write_lock:
                self._ensure_open()
                return self._update_locked(memory, expected_revision=expected_revision,
                                           revision=revision, source=None,
                                           operation=operation)

    async def reject_candidate(self, memory: MemoryEntity, *, expected_revision: int,
                               revision: MemoryRevision, operation: MemoryOperation,
                               purge_body: bool) -> WriteReceipt:
        """Persist candidate rejection and optional body purge atomically."""
        async with self._lock:
            with self._write_lock:
                self._ensure_open()
                self._store.reject_candidate(
                    memory, expected_revision=expected_revision, revision=revision,
                    operation=operation, purge_body=purge_body,
                )
                return WriteReceipt(memory.id, memory.current_revision,
                                    self._sync_one(memory.id))

    def _update_locked(self, memory: MemoryEntity, *, expected_revision: int,
                       revision: MemoryRevision, source: MemorySource | None,
                       operation: MemoryOperation | None = None) -> WriteReceipt:
        self._store.update(memory, expected_revision=expected_revision,
                           revision=revision, source=source, operation=operation)
        return WriteReceipt(memory.id, memory.current_revision, self._sync_one(memory.id))

    def _secure_purge(self) -> bool:
        try:
            self._store.purge_deleted_payload()
        except StorageError:
            return False
        return True

    async def delete(self, memory_id: str, *, scope_id: str, subject_id: str,
                     operation_id: str, operator_type: OperatorType,
                     operator_id: str, occurred_at: datetime) -> OperationReceipt:
        async with self._lock:
            with self._write_lock:
                self._ensure_open()
                ledger = self._ledger
                lineage: str | None = None
                creation_seq: int | None = None
                intent = None
                if ledger is not None and ledger.enabled:
                    lineage, creation_seq = self._store.get_lineage(
                        memory_id, scope_id=scope_id, subject_id=subject_id,
                    )
                    intent = ledger.record_intent(
                        LedgerEventType.DELETE_MEMORY,
                        payload={
                            "db_lineage_id": lineage,
                            "creation_seq": creation_seq,
                            "memory_id": memory_id,
                            "operation_id": operation_id,
                            "operator_type": operator_type.value,
                            "operator_id": operator_id,
                            "occurred_at": occurred_at.isoformat(),
                        },
                        source=operator_type.value, occurred_at=occurred_at,
                        scope_id=scope_id, subject_id=subject_id,
                        memory_id=memory_id,
                    )
                deleted = self._store.delete(
                    memory_id, scope_id=scope_id, subject_id=subject_id,
                    operation_id=operation_id, operator_type=operator_type,
                    operator_id=operator_id, occurred_at=occurred_at,
                    expected_lineage=lineage,
                    expected_creation_seq=creation_seq,
                    idempotent=intent is not None,
                )
                if intent is not None and ledger is not None:
                    ledger.record_committed(intent)
                state = self._sync_one(memory_id)
                # VACUUM must follow a healthy index sync; otherwise the FTS row
                # still holds the deleted text and a later rebuild would leave it
                # behind. The durable marker lets startup retry either way.
                purged = self._secure_purge() if state is IndexState.HEALTHY else False
                return OperationReceipt(operation_id, 1 if deleted else 0, state, purged)

    async def archive(self, memory: MemoryEntity, *, expected_revision: int,
                      revision: MemoryRevision,
                      operation: MemoryOperation | None = None) -> WriteReceipt:
        """Ledger-first ACTIVE/other -> ARCHIVED transition."""
        async with self._lock:
            with self._write_lock:
                self._ensure_open()
                ledger = self._ledger
                lineage: str | None = None
                creation_seq: int | None = None
                intent = None
                if ledger is not None and ledger.enabled:
                    lineage, creation_seq = self._store.get_lineage(
                        memory.id, scope_id=memory.scope_id, subject_id=memory.subject_id,
                    )
                    intent = ledger.record_intent(
                        LedgerEventType.ARCHIVE_MEMORY,
                        payload={
                            "db_lineage_id": lineage,
                            "creation_seq": creation_seq,
                            "memory_id": memory.id,
                            "archive_reason": memory.archive_reason.value if memory.archive_reason else None,
                            "archived_at": memory.archived_at.isoformat() if memory.archived_at else None,
                            "purge_after": memory.purge_after.isoformat() if memory.purge_after else None,
                            "revision_id": revision.id,
                            "operation_id": operation.id if operation else None,
                            "occurred_at": memory.updated_at.isoformat(),
                        },
                        source=(operation.operator_type.value if operation else "system"),
                        occurred_at=memory.updated_at,
                        scope_id=memory.scope_id, subject_id=memory.subject_id,
                        memory_id=memory.id,
                    )
                self._store.update(memory, expected_revision=expected_revision,
                                   revision=revision, source=None, operation=operation)
                if intent is not None and ledger is not None:
                    ledger.record_committed(intent)
                return WriteReceipt(memory.id, memory.current_revision,
                                    self._sync_one(memory.id))

    async def restore(self, memory: MemoryEntity, *, expected_revision: int,
                      revision: MemoryRevision,
                      operation: MemoryOperation | None = None) -> WriteReceipt:
        """Ledger-first ARCHIVED -> ACTIVE restore (USER_ARCHIVE only)."""
        async with self._lock:
            with self._write_lock:
                self._ensure_open()
                ledger = self._ledger
                lineage: str | None = None
                creation_seq: int | None = None
                intent = None
                if ledger is not None and ledger.enabled:
                    lineage, creation_seq = self._store.get_lineage(
                        memory.id, scope_id=memory.scope_id, subject_id=memory.subject_id,
                    )
                    intent = ledger.record_intent(
                        LedgerEventType.RESTORE_MEMORY,
                        payload={
                            "db_lineage_id": lineage,
                            "creation_seq": creation_seq,
                            "memory_id": memory.id,
                            "previous_reason": memory.archive_reason.value if memory.archive_reason else None,
                            "revision_id": revision.id,
                            "operation_id": operation.id if operation else None,
                            "occurred_at": memory.updated_at.isoformat(),
                        },
                        source=(operation.operator_type.value if operation else "system"),
                        occurred_at=memory.updated_at,
                        scope_id=memory.scope_id, subject_id=memory.subject_id,
                        memory_id=memory.id,
                    )
                self._store.update(memory, expected_revision=expected_revision,
                                   revision=revision, source=None, operation=operation)
                if intent is not None and ledger is not None:
                    ledger.record_committed(intent)
                return WriteReceipt(memory.id, memory.current_revision,
                                    self._sync_one(memory.id))

    async def purge(self, memory_id: str, *, scope_id: str, subject_id: str,
                    operation_id: str, operator_type: OperatorType,
                    operator_id: str, occurred_at: datetime) -> OperationReceipt:
        """Ledger-first irreversible purge of an ARCHIVED memory."""
        async with self._lock:
            with self._write_lock:
                self._ensure_open()
                ledger = self._ledger
                lineage: str | None = None
                creation_seq: int | None = None
                intent = None
                if ledger is not None and ledger.enabled:
                    lineage, creation_seq = self._store.get_lineage(
                        memory_id, scope_id=scope_id, subject_id=subject_id,
                    )
                    intent = ledger.record_intent(
                        LedgerEventType.PURGE_MEMORY,
                        payload={
                            "db_lineage_id": lineage,
                            "creation_seq": creation_seq,
                            "memory_id": memory_id,
                            "operation_id": operation_id,
                            "operator_type": operator_type.value,
                            "operator_id": operator_id,
                            "occurred_at": occurred_at.isoformat(),
                        },
                        source=operator_type.value, occurred_at=occurred_at,
                        scope_id=scope_id, subject_id=subject_id,
                        memory_id=memory_id,
                    )
                purged = self._store.purge_archived(
                    memory_id, scope_id=scope_id, subject_id=subject_id,
                    operation_id=operation_id, operator_type=operator_type,
                    operator_id=operator_id, occurred_at=occurred_at,
                    expected_lineage=lineage,
                    expected_creation_seq=creation_seq,
                    idempotent=intent is not None,
                )
                if intent is not None and ledger is not None:
                    ledger.record_committed(intent)
                state = self._sync_one(memory_id)
                purged_ok = self._secure_purge() if state is IndexState.HEALTHY else False
                return OperationReceipt(operation_id, 1 if purged else 0, state,
                                        purged_ok)

    async def clear_scope(self, scope_id: str, *, operation_id: str,
                          operator_type: OperatorType, operator_id: str,
                          occurred_at: datetime) -> OperationReceipt:
        async with self._lock:
            with self._write_lock:
                self._ensure_open()
                ledger = self._ledger
                boundaries: dict[str, int] | None = None
                intent = None
                if ledger is not None and ledger.enabled:
                    # Read the persisted high watermark only after the lease.
                    boundaries = self._store.scope_lineage_boundaries(scope_id)
                    intent = ledger.record_intent(
                        LedgerEventType.CLEAR_SCOPE,
                        payload={
                            "lineage_boundaries": [
                                {
                                    "db_lineage_id": lineage,
                                    "clear_creation_seq": boundary,
                                    "high_watermark": boundary,
                                }
                                for lineage, boundary in boundaries.items()
                            ],
                            "operation_id": operation_id,
                            "operator_type": operator_type.value,
                            "operator_id": operator_id,
                            "occurred_at": occurred_at.isoformat(),
                        },
                        source=operator_type.value, occurred_at=occurred_at,
                        scope_id=scope_id,
                    )
                deleted_ids = self._store.clear_scope(
                    scope_id, operation_id=operation_id,
                    operator_type=operator_type, operator_id=operator_id,
                    occurred_at=occurred_at, boundaries=boundaries,
                )
                if intent is not None and ledger is not None:
                    ledger.record_committed(intent)
                try:
                    self._index.rebuild()
                except IndexUnavailable:
                    state = IndexState.PENDING_REBUILD
                else:
                    state = self._index_state()
                purged = self._secure_purge() if state is IndexState.HEALTHY else False
                return OperationReceipt(operation_id, len(deleted_ids), state, purged)

    async def remove_attachment(self, *, memory_id: str, scope_id: str,
                                subject_id: str, attachment_type: str,
                                attachment_key: str, operator_type: OperatorType,
                                operator_id: str,
                                occurred_at: datetime) -> bool:
        """Delete one source/tag/relation under the ledger-first protocol."""
        async with self._lock:
            with self._write_lock:
                self._ensure_open()
                lineage, creation_seq = self._store.get_lineage(
                    memory_id, scope_id=scope_id, subject_id=subject_id,
                )
                ledger = self._ledger
                intent = None
                if ledger is not None and ledger.enabled:
                    intent = ledger.record_intent(
                        LedgerEventType.REMOVE_ATTACHMENT,
                        payload={
                            "attachment_type": attachment_type,
                            "attachment_key": attachment_key,
                            "memory_id": memory_id,
                            "db_lineage_id": lineage,
                            "creation_seq": creation_seq,
                        },
                        source=operator_type.value, occurred_at=occurred_at,
                        scope_id=scope_id, subject_id=subject_id,
                        memory_id=memory_id,
                    )
                removed = self._store.remove_attachment(
                    memory_id=memory_id, scope_id=scope_id,
                    subject_id=subject_id, attachment_type=attachment_type,
                    attachment_key=attachment_key,
                )
                if intent is not None and ledger is not None:
                    ledger.record_committed(intent)
                return removed

    async def disable_scope(self, scope_id: str, *, operation_id: str,
                            operator_type: OperatorType, operator_id: str,
                            occurred_at: datetime) -> OperationReceipt:
        async with self._lock:
            with self._write_lock:
                self._ensure_open()
                changed = self._store.disable_scope(
                    scope_id, operation_id=operation_id, operator_type=operator_type,
                    operator_id=operator_id, occurred_at=occurred_at,
                )
                # Scope-enabled filtering prevents recall immediately; V0 has no
                # separate injection cache to invalidate.
                return OperationReceipt(operation_id, 1 if changed else 0,
                                         self._index_state())

    async def record_access(self, memory_ids: tuple[str, ...], *, scope_id: str,
                            subject_id: str, at: datetime,
                            guard: Callable[[], None]) -> None:
        """Persist access metadata without creating a content revision."""
        if not memory_ids:
            return
        async with self._lock:
            with self._write_lock:
                self._ensure_open()
                guard()
                self._store.record_access(
                    memory_ids, scope_id=scope_id, subject_id=subject_id, at=at,
                )

    async def close(self) -> None:
        async with self._lock:
            with self._write_lock:
                self._closed = True
