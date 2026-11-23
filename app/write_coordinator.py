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
    MemoryEntity,
    MemoryOperation,
    MemoryRevision,
    MemorySource,
    OperatorType,
)
from ..core.policy.dedup import DedupKey
from ..core.ports import MemoryStore, SearchIndex

T = TypeVar("T")


class IndexState(StrEnum):
    HEALTHY = "HEALTHY"
    PENDING_REBUILD = "PENDING_REBUILD"


@dataclass(frozen=True, slots=True)
class WriteReceipt:
    entity_id: str
    revision: int
    index_state: IndexState


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

    def __init__(self, store: MemoryStore, index: SearchIndex) -> None:
        self._store = store
        self._index = index
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
        """Run a synchronous multi-step write under the coordinator locks."""
        async with self._lock:
            with self._write_lock:
                self._ensure_open()
                return operation()

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
                self._store.delete(
                    memory_id, scope_id=scope_id, subject_id=subject_id,
                    operation_id=operation_id, operator_type=operator_type,
                    operator_id=operator_id, occurred_at=occurred_at,
                )
                state = self._sync_one(memory_id)
                # VACUUM must follow a healthy index sync; otherwise the FTS row
                # still holds the deleted text and a later rebuild would leave it
                # behind. The durable marker lets startup retry either way.
                purged = self._secure_purge() if state is IndexState.HEALTHY else False
                return OperationReceipt(operation_id, 1, state, purged)

    async def clear_scope(self, scope_id: str, *, operation_id: str,
                          operator_type: OperatorType, operator_id: str,
                          occurred_at: datetime) -> OperationReceipt:
        async with self._lock:
            with self._write_lock:
                self._ensure_open()
                deleted_ids = self._store.clear_scope(
                    scope_id, operation_id=operation_id, operator_type=operator_type,
                    operator_id=operator_id, occurred_at=occurred_at,
                )
                try:
                    self._index.rebuild()
                except IndexUnavailable:
                    state = IndexState.PENDING_REBUILD
                else:
                    state = self._index_state()
                purged = self._secure_purge() if state is IndexState.HEALTHY else False
                return OperationReceipt(operation_id, len(deleted_ids), state, purged)

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
