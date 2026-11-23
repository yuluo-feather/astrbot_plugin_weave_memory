"""Vector index rebuild uses the authoritative ACTIVE set."""

import asyncio
from datetime import UTC, datetime

from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.database import (
    SQLiteDatabase,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.fts import SQLiteFTSIndex
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.index_jobs import (
    SQLiteIndexStateStore,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.store import (
    SQLiteMemoryStore,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.vector import (
    SQLiteVectorIndex,
)
from data.plugins.astrbot_plugin_weave_memory.app.index_rebuild_service import (
    IndexRebuildService,
)
from data.plugins.astrbot_plugin_weave_memory.app.write_coordinator import (
    WriteCoordinator,
)
from data.plugins.astrbot_plugin_weave_memory.core.model import (
    Identity,
    IndexStateStatus,
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


class FakeProvider:
    provider_id = "fake"
    model = "m"
    dimension = 2

    async def embed_query(self, text):
        return (1.0, 0.0)

    async def embed_documents(self, texts, *, scope_type="", scope_id=""):
        return tuple((1.0, float(index)) for index, _ in enumerate(texts))


def test_rebuild_all_recreates_embeddings_and_healthy_state(tmp_path):
    db = SQLiteDatabase(tmp_path / "rebuild.db")
    db.migrate()
    store = SQLiteMemoryStore(db)
    index = SQLiteFTSIndex(db)
    identity = Identity(
        "astrbot", "default", ScopeType.PERSONAL, "u1", SubjectType.USER, "u1",
    )
    scope = Scope("scope-1", identity, NOW)
    store.create_scope(scope)
    coordinator = WriteCoordinator(store, index)
    for mid, content in (("m1", "黑咖啡"), ("m2", "绿茶")):
        memory = MemoryEntity(
            id=mid, scope_id=scope.id, subject_id="u1",
            memory_type=MemoryType.PREFERENCE, content=content,
            normalized_content=normalize_content(content),
            status=MemoryStatus.ACTIVE, source_type=SourceType.EXPLICIT,
            current_revision=1, created_at=NOW, updated_at=NOW,
        )
        asyncio.run(coordinator.create(
            memory,
            MemoryRevision(f"rev-{mid}", mid, 1, content, MemoryStatus.ACTIVE,
                           NOW, "created"),
            MemorySource(f"src-{mid}", mid, SourceType.EXPLICIT,
                         OperatorType.USER, "u1", NOW),
        ))
    vector = SQLiteVectorIndex(
        db, provider_id="fake", model="m", dim=2, config_hash="cfg",
        states=SQLiteIndexStateStore(db),
    )
    rebuild = IndexRebuildService(store, vector, FakeProvider(), coordinator, FixedClock())
    assert asyncio.run(rebuild.rebuild_all()) == 2
    assert vector.count() == 2
    assert vector.status().status is IndexStateStatus.HEALTHY
    found = vector.search((1.0, 0.0), scope_id=scope.id, subject_id="u1",
                          limit=2, at=NOW)
    assert len(found) == 2
    db.close()
