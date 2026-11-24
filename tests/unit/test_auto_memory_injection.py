from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from data.plugins.astrbot_plugin_weave_memory.app.recall_service import (
    MemoryContextComposer,
)
from data.plugins.astrbot_plugin_weave_memory.core.model import (
    Identity,
    MemoryEntity,
    MemoryStatus,
    MemoryType,
    Scope,
    ScopeType,
    SourceType,
    SubjectType,
)
from data.plugins.astrbot_plugin_weave_memory.core.normalization import (
    normalize_content,
)

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
SCOPE = Scope(
    "scope-personal",
    Identity("astrbot", "default", ScopeType.PERSONAL, "u1", SubjectType.USER, "u1"),
    NOW,
)


def _memory(memory_id: str, content: str, source_type: SourceType,
            metadata: dict[str, object]) -> MemoryEntity:
    return MemoryEntity(
        id=memory_id, scope_id=SCOPE.id, subject_id="u1",
        memory_type=MemoryType.FACT, content=content,
        normalized_content=normalize_content(content), status=MemoryStatus.ACTIVE,
        source_type=source_type, current_revision=1,
        created_at=NOW, updated_at=NOW, metadata=metadata,
    )


class FakeRecall:
    def __init__(self, memories) -> None:
        self._memories = tuple(memories)
        self.accessed: tuple[str, ...] = ()

    def recall_with_trace(self, query, scope, *, limit=12):
        return self._memories, object()

    def record_diagnostics(self, trace, **kwargs) -> None:
        pass

    async def record_access(self, memory_ids, scope) -> None:
        self.accessed = tuple(memory_ids)


def test_auto_memory_injection_can_be_disabled_without_hiding_explicit_memory():
    explicit = _memory("m-explicit", "用户住在杭州", SourceType.EXPLICIT, {})
    auto = _memory(
        "m-auto", "用户喜欢黑咖啡", SourceType.INFERRED,
        {"auto": True, "importance": 0.7},
    )
    recall = FakeRecall((explicit, auto))
    composer = MemoryContextComposer(
        recall, max_tokens=200, max_memories=4, auto_memory_enabled=False,
    )
    context = asyncio.run(composer.compose("我住哪", SCOPE))
    assert context is not None
    assert context.selected_memory_ids == ("m-explicit",)
    assert "黑咖啡" not in context.content


def test_user_confirmed_inferred_memory_still_injects_when_auto_is_disabled():
    confirmed = _memory(
        "m-confirmed", "用户喜欢黑咖啡", SourceType.INFERRED,
        {"auto": False, "user_confirmed": True, "importance": 0.8},
    )
    recall = FakeRecall((confirmed,))
    composer = MemoryContextComposer(
        recall, max_tokens=200, max_memories=4, auto_memory_enabled=False,
    )
    context = asyncio.run(composer.compose("我喜欢什么", SCOPE))
    assert context is not None
    assert context.selected_memory_ids == ("m-confirmed",)


def test_auto_memory_injection_is_kept_when_enabled():
    auto = _memory(
        "m-auto", "用户喜欢黑咖啡", SourceType.INFERRED,
        {"auto": True, "importance": 0.7},
    )
    recall = FakeRecall((auto,))
    composer = MemoryContextComposer(
        recall, max_tokens=200, max_memories=4, auto_memory_enabled=True,
    )
    context = asyncio.run(composer.compose("我喜欢什么", SCOPE))
    assert context is not None
    assert context.selected_memory_ids == ("m-auto",)
