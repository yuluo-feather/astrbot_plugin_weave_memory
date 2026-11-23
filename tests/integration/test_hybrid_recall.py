"""Hybrid recall: RRF ordering, degradation and body-free traces."""

import asyncio
import json
from datetime import UTC, datetime

import pytest
from data.plugins.astrbot_plugin_weave_memory.adapters.astrbot.retrieval_trace import (
    RetrievalTraceRecorder,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.consents import (
    SQLiteConsentStore,
)
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
from data.plugins.astrbot_plugin_weave_memory.adapters.system import UUID4IdGenerator
from data.plugins.astrbot_plugin_weave_memory.app.consent_service import (
    ConsentService,
    PersonalConsentType,
)
from data.plugins.astrbot_plugin_weave_memory.app.embedding_cache import (
    EmbeddingLRUCache,
)
from data.plugins.astrbot_plugin_weave_memory.app.hybrid_recall_service import (
    HybridRecallService,
)
from data.plugins.astrbot_plugin_weave_memory.app.provider_config import (
    ProviderConfig,
)
from data.plugins.astrbot_plugin_weave_memory.app.provider_policy import (
    ProviderPolicy,
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


class FakeProvider:
    provider_id = "fake"
    model = "m2"
    dimension = 2

    async def embed_query(self, text):
        return (1.0, 0.0)

    async def embed_documents(self, texts):
        return tuple((1.0, 0.0) for _ in texts)


def _scope() -> Scope:
    identity = Identity(
        "astrbot", "default", ScopeType.PERSONAL, "u1", SubjectType.USER, "u1",
    )
    return Scope("scope-1", identity, NOW)


def _memory(scope: Scope, *, mid: str, content: str):
    return MemoryEntity(
        id=mid, scope_id=scope.id, subject_id="u1",
        memory_type=MemoryType.PREFERENCE, content=content,
        normalized_content=normalize_content(content), status=MemoryStatus.ACTIVE,
        source_type=SourceType.EXPLICIT, current_revision=1,
        created_at=NOW, updated_at=NOW,
    )


@pytest.fixture
def stack(tmp_path):
    db = SQLiteDatabase(tmp_path / "hybrid.db")
    db.migrate()
    store = SQLiteMemoryStore(db)
    index = SQLiteFTSIndex(db)
    scope = _scope()
    store.create_scope(scope)
    coordinator = WriteCoordinator(store, index)
    consent = ConsentService(
        SQLiteConsentStore(db), coordinator, FixedClock(), UUID4IdGenerator(),
    )
    consent.grant(scope, PersonalConsentType.PERSONAL_MEMORY, operator_id="u1")
    for mid, content in (("m1", "喜欢黑咖啡"), ("m2", "喜欢绿茶")):
        memory = _memory(scope, mid=mid, content=content)
        asyncio.run(coordinator.create(
            memory,
            MemoryRevision(
                f"rev-{mid}", mid, 1, content, MemoryStatus.ACTIVE, NOW, "created",
            ),
            MemorySource(
                f"src-{mid}", mid, SourceType.EXPLICIT, OperatorType.USER,
                "u1", NOW,
            ),
        ))
    states = SQLiteIndexStateStore(db)
    vector = SQLiteVectorIndex(
        db, provider_id="fake", model="m2", dim=2, config_hash="cfg",
        states=states,
    )
    for order, mid in enumerate(("m1", "m2"), start=1):
        lineage, creation_seq = store.get_lineage(
            mid, scope_id=scope.id, subject_id="u1",
        )
        vector.upsert(
            mid, (1.0, 0.0) if mid == "m1" else (0.0, 1.0), dim=2,
            provider="fake", model="m2", content_hash=f"h{order}",
            db_lineage_id=lineage, creation_seq=creation_seq, revision=1,
            created_at=NOW,
        )
    vector.mark_healthy(at=NOW)
    yield db, store, index, vector, consent, coordinator, scope
    db.close()


def _policy():
    return ProviderPolicy(
        FakeProvider(),
        ProviderConfig.from_mapping({
            "external_provider_opt_in": True,
            "external_provider_allowlist": ["fake"],
        }),
        None, EmbeddingLRUCache(max_mb=1, ttl_seconds=60), FixedClock(),
    )


def test_hybrid_rrf_ranks_fts_and_vector_and_trace_has_no_body(stack):
    _, store, index, vector, consent, coordinator, scope = stack
    recorder = RetrievalTraceRecorder()
    hybrid = HybridRecallService(
        store, index, consent, FixedClock(), store, coordinator,
        vector=vector, provider=_policy(), recorder=recorder,
        vector_candidate_limit=5, hybrid_candidate_limit=10,
    )
    memories, trace = asyncio.run(
        hybrid.recall_with_trace("黑咖啡", scope, limit=5),
    )
    assert memories[0].id == "m1"
    assert [channel.name for channel in trace.channels] == ["FTS", "VECTOR"]
    assert trace.fusion["method"] == "RRF"
    payload = trace.to_dict()
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "黑咖啡" not in encoded
    assert "绿茶" not in encoded
    assert payload["channels"][1]["ok"] is True

    hybrid.record_diagnostics(
        trace, injected_ids=(memories[0].id,), estimated_tokens=10,
    )
    recent = recorder.recent(1)[0]
    assert recent["final"]["injected_ids"] == ["m1"]
    assert "黑咖啡" not in json.dumps(recent, ensure_ascii=False)


def test_hybrid_degrades_to_fts_when_provider_missing(stack):
    _, store, index, vector, consent, coordinator, scope = stack
    hybrid = HybridRecallService(
        store, index, consent, FixedClock(), store, coordinator,
        vector=vector, provider=None,
    )
    memories, trace = asyncio.run(
        hybrid.recall_with_trace("黑咖啡", scope, limit=5),
    )
    assert memories and memories[0].id == "m1"
    assert trace.channels[1].degraded is True
    assert trace.channels[1].error_code == "provider_unavailable"
    assert trace.fusion["method"] == "NONE"
