"""V0 exact-duplicate key and conflict classification."""

from dataclasses import dataclass

from ..model import MemoryEntity, MemoryStatus


@dataclass(frozen=True, slots=True)
class DedupKey:
    scope_id: str
    subject_id: str
    memory_type: str
    normalized_content: str

    @classmethod
    def from_memory(cls, memory: MemoryEntity) -> "DedupKey":
        return cls(memory.scope_id, memory.subject_id, memory.memory_type.value,
                   memory.normalized_content)


def participates_in_unique_key(memory: MemoryEntity) -> bool:
    return memory.status in {MemoryStatus.ACTIVE, MemoryStatus.CANDIDATE}


def classify_duplicate(existing: MemoryEntity, incoming: MemoryEntity) -> str:
    """Return EXACT_DUPLICATE or DISTINCT; updates remain explicit in V0."""
    return "EXACT_DUPLICATE" if DedupKey.from_memory(existing) == DedupKey.from_memory(incoming) else "DISTINCT"
