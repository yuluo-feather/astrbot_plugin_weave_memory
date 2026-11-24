from __future__ import annotations

from datetime import UTC, datetime, timedelta

from data.plugins.astrbot_plugin_weave_memory.core.model import (
    MemoryEntity,
    MemoryStatus,
    MemoryType,
    SourceType,
)
from data.plugins.astrbot_plugin_weave_memory.core.normalization import (
    normalize_content,
)
from data.plugins.astrbot_plugin_weave_memory.core.policy.importance import (
    importance_weight,
    rank_by_importance,
)

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


def _memory(memory_id: str, *, importance: float, age_days: int = 0,
            source_type=SourceType.EXPLICIT) -> MemoryEntity:
    created = NOW - timedelta(days=age_days)
    return MemoryEntity(
        id=memory_id, scope_id="s", subject_id="u",
        memory_type=MemoryType.FACT, content=f"记忆 {memory_id}",
        normalized_content=normalize_content(f"记忆 {memory_id}"),
        status=MemoryStatus.ACTIVE, source_type=source_type,
        current_revision=1, created_at=created, updated_at=created,
        metadata={"importance": importance},
    )


def test_half_life_zero_disables_decay():
    memory = _memory("m", importance=1.0, age_days=3650)
    assert importance_weight(memory, now=NOW, half_life_days=0) == 1.0


def test_weight_decays_with_age_and_importance():
    fresh = _memory("fresh", importance=1.0)
    old = _memory("old", importance=1.0, age_days=90)
    assert importance_weight(fresh, now=NOW, half_life_days=90) == 1.0
    assert importance_weight(old, now=NOW, half_life_days=90) == 0.75
    low = _memory("low", importance=0.1)
    assert importance_weight(low, now=NOW, half_life_days=90) == 0.55


def test_rank_by_importance_only_reorders_existing_memories():
    low = _memory("low", importance=0.0)
    high = _memory("high", importance=1.0)
    rank_by_id = {"low": 0, "high": 5}
    ranked = rank_by_importance(
        [low, high], rank_by_id=rank_by_id, now=NOW, half_life_days=90,
    )
    assert [memory.id for memory in ranked] == ["high", "low"]
    assert set(memory.id for memory in ranked) == {"low", "high"}


def test_rank_by_importance_is_noop_when_disabled():
    low = _memory("low", importance=0.0)
    high = _memory("high", importance=1.0)
    ranked = rank_by_importance(
        [low, high], rank_by_id={"low": 0, "high": 5}, now=NOW, half_life_days=0,
    )
    assert [memory.id for memory in ranked] == ["low", "high"]
