"""V1.9 importance decay used only for ordering (ADR-0030)."""

from __future__ import annotations

from datetime import datetime
from math import pow

from ..model import MemoryEntity, SourceType

_DEFAULT_IMPORTANCE = {
    SourceType.EXPLICIT: 0.9,
    SourceType.AGENT: 0.9,
    SourceType.MANUAL: 0.8,
    SourceType.IMPORTED: 0.6,
    SourceType.OBSERVED: 0.6,
    SourceType.INFERRED: 0.5,
}


def memory_importance(memory: MemoryEntity) -> float:
    raw = memory.metadata.get("importance")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return min(1.0, max(0.0, float(raw)))
    return _DEFAULT_IMPORTANCE.get(memory.source_type, 0.5)


def importance_weight(
    memory: MemoryEntity, *, now: datetime, half_life_days: int,
) -> float:
    if half_life_days <= 0:
        return 1.0
    anchor = memory.created_at
    if memory.last_accessed_at is not None and memory.last_accessed_at > anchor:
        anchor = memory.last_accessed_at
    age_days = max(0.0, (now - anchor).total_seconds() / 86400.0)
    decay = pow(0.5, age_days / half_life_days)
    return 0.5 + 0.5 * memory_importance(memory) * decay


def rank_by_importance(
    memories: list[MemoryEntity], *, rank_by_id: dict[str, int],
    now: datetime, half_life_days: int,
) -> list[MemoryEntity]:
    """Re-rank recalled memories without adding or dropping any."""
    if half_life_days <= 0 or len(memories) < 2:
        return memories
    return sorted(
        memories,
        key=lambda memory: (
            -(importance_weight(
                memory, now=now, half_life_days=half_life_days,
            ) / (1.0 + 0.1 * rank_by_id.get(memory.id, len(memories)))),
            rank_by_id.get(memory.id, len(memories)),
            memory.id,
        ),
    )
