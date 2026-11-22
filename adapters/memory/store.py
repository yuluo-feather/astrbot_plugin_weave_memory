"""Small in-memory store used only by domain/contract tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from threading import RLock

from ...core.errors import Conflict, NotFound
from ...core.model import MemoryEntity, MemoryRevision, MemorySource
from ...core.policy.dedup import DedupKey, participates_in_unique_key


class InMemoryMemoryStore:
    def __init__(self) -> None:
        self._lock = RLock()
        self._memories: dict[str, MemoryEntity] = {}
        self._revisions: dict[str, list[MemoryRevision]] = {}
        self._sources: dict[str, list[MemorySource]] = {}

    def create(self, memory: MemoryEntity, revision: MemoryRevision,
               source: MemorySource) -> None:
        with self._lock:
            if memory.id in self._memories:
                if (self._memories[memory.id] == memory and
                        self._revisions[memory.id] == [revision] and
                        self._sources[memory.id] == [source]):
                    return
                raise Conflict("memory id already exists with different content")
            if memory.status.value not in {"ACTIVE", "CANDIDATE"}:
                raise Conflict("new memories must start as ACTIVE or CANDIDATE")
            if revision.memory_id != memory.id or revision.revision != 1 or memory.current_revision != 1:
                raise Conflict("initial revision and entity must both be revision 1")
            if revision.content != memory.content or revision.status is not memory.status:
                raise Conflict("initial revision snapshot must match memory")
            if source.memory_id != memory.id:
                raise Conflict("source must belong to memory")
            if source.source_type is not memory.source_type:
                raise Conflict("source type must match memory source_type")
            if participates_in_unique_key(memory):
                key = DedupKey.from_memory(memory)
                if any(participates_in_unique_key(item) and DedupKey.from_memory(item) == key
                       for item in self._memories.values()):
                    raise Conflict("active/candidate duplicate exists")
            self._memories[memory.id] = memory
            self._revisions[memory.id] = [revision]
            self._sources[memory.id] = [source]

    def get(self, memory_id: str, *, scope_id: str, subject_id: str) -> MemoryEntity:
        with self._lock:
            memory = self._memories.get(memory_id)
            if memory is None or memory.scope_id != scope_id or memory.subject_id != subject_id:
                raise NotFound("memory not found")
            return memory

    def find_active_duplicate(self, *, scope_id: str, subject_id: str,
                              memory_type: str, normalized_content: str) -> MemoryEntity | None:
        with self._lock:
            for memory in self._memories.values():
                if (memory.scope_id == scope_id and memory.subject_id == subject_id
                        and memory.memory_type.value == memory_type
                        and memory.normalized_content == normalized_content
                        and memory.status.value == "ACTIVE"):
                    return memory
            return None

    def update(self, memory: MemoryEntity, *, expected_revision: int,
               revision: MemoryRevision, source: MemorySource | None = None) -> None:
        with self._lock:
            current = self._memories.get(memory.id)
            if current is None:
                raise NotFound("memory not found")
            if current.current_revision != expected_revision:
                raise Conflict("revision conflict")
            if memory.current_revision != expected_revision + 1:
                raise Conflict("updated entity revision must increment exactly once")
            if revision.memory_id != memory.id or revision.revision != memory.current_revision:
                raise Conflict("revision record does not match updated entity")
            if revision.content != memory.content or revision.status is not memory.status:
                raise Conflict("revision snapshot must match updated entity")
            if memory.source_type is not current.source_type:
                if source is None or source.memory_id != memory.id or source.source_type is not memory.source_type:
                    raise Conflict("source change requires a matching new source record")
            elif source is not None and (source.memory_id != memory.id or source.source_type is not memory.source_type):
                raise Conflict("new source record must match updated memory")
            if participates_in_unique_key(memory):
                key = DedupKey.from_memory(memory)
                if any(item.id != memory.id and participates_in_unique_key(item)
                       and DedupKey.from_memory(item) == key for item in self._memories.values()):
                    raise Conflict("active/candidate duplicate exists")
            self._memories[memory.id] = memory
            self._revisions[memory.id].append(revision)
            if source is not None:
                self._sources[memory.id].append(source)

    def list_active(self, *, scope_id: str, subject_id: str) -> tuple[MemoryEntity, ...]:
        with self._lock:
            return tuple(m for m in self._memories.values()
                         if m.scope_id == scope_id and m.subject_id == subject_id
                         and m.status.value == "ACTIVE")

    def record_access(self, memory_ids: tuple[str, ...], *, scope_id: str,
                      subject_id: str, at: datetime) -> None:
        with self._lock:
            for memory_id in memory_ids:
                memory = self._memories.get(memory_id)
                if (memory is not None and memory.scope_id == scope_id
                        and memory.subject_id == subject_id
                        and memory.status.value == "ACTIVE"):
                    self._memories[memory_id] = replace(memory, last_accessed_at=at)

    def revisions(self, memory_id: str) -> tuple[MemoryRevision, ...]:
        with self._lock:
            if memory_id not in self._memories:
                raise NotFound("memory not found")
            return tuple(self._revisions[memory_id])
