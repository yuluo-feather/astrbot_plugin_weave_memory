"""FTS relevance floor: weak OR hits are filtered relative to the best hit."""

import asyncio
from datetime import UTC, datetime

from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.database import (
    SQLiteDatabase,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.fts import (
    SQLiteFTSIndex,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.store import (
    SQLiteMemoryStore,
)
from data.plugins.astrbot_plugin_weave_memory.app.write_coordinator import (
    WriteCoordinator,
)
from data.plugins.astrbot_plugin_weave_memory.core.model import (
    Identity,
    MemoryEntity,
    MemoryRevision,
    MemorySource,
    MemoryStatus,
    MemoryType,
    OperatorType,
    Scope,
    ScopeType,
    SourceType,
    SubjectType,
)
from data.plugins.astrbot_plugin_weave_memory.core.normalization import (
    normalize_content,
)

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


class FixedClock:
    def now(self):
        return NOW


def _scope() -> Scope:
    identity = Identity(
        "astrbot", "default", ScopeType.PERSONAL, "u1", SubjectType.USER, "u1",
    )
    return Scope("scope-1", identity, NOW)


def _build(tmp_path):
    db = SQLiteDatabase(tmp_path / "fts.db")
    db.migrate()
    store = SQLiteMemoryStore(db)
    index = SQLiteFTSIndex(db)
    scope = _scope()
    store.create_scope(scope)
    coordinator = WriteCoordinator(store, index)
    contents = {
        "strong": "喜欢咖啡，每天都喝手冲咖啡",
        "weak": "喜欢散步和看书",
    }
    for memory_id, content in contents.items():
        asyncio.run(coordinator.create(
            MemoryEntity(
                id=memory_id, scope_id=scope.id, subject_id="u1",
                memory_type=MemoryType.PREFERENCE, content=content,
                normalized_content=normalize_content(content),
                status=MemoryStatus.ACTIVE, source_type=SourceType.EXPLICIT,
                current_revision=1, created_at=NOW, updated_at=NOW,
            ),
            MemoryRevision(
                f"rev-{memory_id}", memory_id, 1, content,
                MemoryStatus.ACTIVE, NOW, "created",
            ),
            MemorySource(
                f"src-{memory_id}", memory_id, SourceType.EXPLICIT,
                OperatorType.USER, "u1", NOW,
            ),
        ))
    return db, scope


def test_ratio_filters_weak_hits_and_zero_keeps_all(tmp_path):
    db, scope = _build(tmp_path)
    kwargs = {"scope_id": scope.id, "subject_id": "u1"}
    filtered = SQLiteFTSIndex(db, relevance_ratio=0.5).search("喜欢咖啡", **kwargs)
    assert filtered == ("strong",)

    unfiltered = SQLiteFTSIndex(db, relevance_ratio=0.0).search("喜欢咖啡", **kwargs)
    assert set(unfiltered) == {"strong", "weak"}


def test_single_hit_and_empty_query_are_unchanged(tmp_path):
    db, scope = _build(tmp_path)
    index = SQLiteFTSIndex(db, relevance_ratio=0.9)
    assert index.search("咖啡", scope_id=scope.id, subject_id="u1") == ("strong",)
    assert index.search("   ", scope_id=scope.id, subject_id="u1") == ()
    assert index.search("完全不相干的词", scope_id=scope.id, subject_id="u1") == ()
