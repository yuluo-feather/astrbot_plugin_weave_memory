"""Single-process serialized write coordinator for V0.4."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from ..core.errors import IndexUnavailable, InvalidTransition, ServiceClosed
from ..core.model import MemoryEntity, MemoryRevision, MemorySource, OperatorType
from ..core.ports import MemoryStore, SearchIndex


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
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise ServiceClosed("write coordinator is closed")

    def _index_state(self) -> IndexState:
        try:
            self._index.integrity_check()
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
                     source: MemorySource) -> WriteReceipt:
        async with self._lock:
            self._ensure_open()
            self._store.create(memory, revision, source)
            return WriteReceipt(memory.id, memory.current_revision, self._sync_one(memory.id))

    async def create_or_get_duplicate(self, memory: MemoryEntity,
                                      revision: MemoryRevision,
                                      source: MemorySource, *,
                                      guard: Callable[[], None]) -> tuple[MemoryEntity, WriteReceipt, bool]:
        """Serialize explicit saves and return the existing ACTIVE exact duplicate.

        The store lookup and create share this coordinator lock, preventing two
        in-process callers from racing past the idempotency check.
        """
        async with self._lock:
            self._ensure_open()
            guard()
            existing = self._store.find_active_duplicate(
                scope_id=memory.scope_id, subject_id=memory.subject_id,
                memory_type=memory.memory_type.value,
                normalized_content=memory.normalized_content,
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
                               revision: MemoryRevision) -> WriteReceipt:
        """Persist a status transition after the LifecycleService guard."""
        async with self._lock:
            self._ensure_open()
            return self._update_locked(memory, expected_revision=expected_revision,
                                       revision=revision, source=None)

    def _update_locked(self, memory: MemoryEntity, *, expected_revision: int,
                       revision: MemoryRevision, source: MemorySource | None) -> WriteReceipt:
        self._store.update(memory, expected_revision=expected_revision,
                           revision=revision, source=source)
        return WriteReceipt(memory.id, memory.current_revision, self._sync_one(memory.id))

    async def delete(self, memory_id: str, *, scope_id: str, subject_id: str,
                     operation_id: str, operator_type: OperatorType,
                     operator_id: str, occurred_at: datetime) -> OperationReceipt:
        async with self._lock:
            self._ensure_open()
            self._store.delete(
                memory_id, scope_id=scope_id, subject_id=subject_id,
                operation_id=operation_id, operator_type=operator_type,
                operator_id=operator_id, occurred_at=occurred_at,
            )
            state = self._sync_one(memory_id)
            return OperationReceipt(operation_id, 1, state)

    async def clear_scope(self, scope_id: str, *, operation_id: str,
                          operator_type: OperatorType, operator_id: str,
                          occurred_at: datetime) -> OperationReceipt:
        async with self._lock:
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
            return OperationReceipt(operation_id, len(deleted_ids), state)

    async def disable_scope(self, scope_id: str, *, operation_id: str,
                            operator_type: OperatorType, operator_id: str,
                            occurred_at: datetime) -> OperationReceipt:
        async with self._lock:
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
            self._ensure_open()
            guard()
            self._store.record_access(
                memory_ids, scope_id=scope_id, subject_id=subject_id, at=at,
            )

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
