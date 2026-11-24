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
    GroupConsentType,
    PersonalConsentType,
)
from data.plugins.astrbot_plugin_weave_memory.app.embedding_cache import (
    EmbeddingLRUCache,
)
from data.plugins.astrbot_plugin_weave_memory.app.hybrid_recall_service import (
    HybridGroupRecallService,
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
    assert [channel.name for channel in trace.channels][:2] == ["FTS", "VECTOR"]
    assert {channel.name for channel in trace.channels} >= {"FTS", "VECTOR", "TAGS", "TIME"}
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


def test_tag_and_time_candidates_join_the_fusion(stack):
    db, store, index, vector, consent, coordinator, scope = stack
    db.connection.execute(
        "INSERT INTO memory_tags(memory_id, tag, created_at) VALUES ('m1', '咖啡', ?)",
        (NOW.isoformat(),),
    )
    hybrid = HybridRecallService(
        store, index, consent, FixedClock(), store, coordinator,
        vector=vector, provider=None,
    )
    _, trace = asyncio.run(
        hybrid.recall_with_trace("今天 咖啡", scope, limit=5),
    )
    channels = {channel.name: channel for channel in trace.channels}
    assert channels["TAGS"].enabled is True
    assert "m1" in {item.memory_id for item in channels["TAGS"].candidates}
    assert channels["TIME"].enabled is True
    assert channels["TIME"].candidates
    assert trace.fusion["method"] == "RRF"


def test_group_hybrid_recall_uses_group_scope_and_consent(stack):
    db, store, index, vector, consent, coordinator, _ = stack
    group_scope = Scope(
        "scope-group",
        Identity("astrbot", "default", ScopeType.GROUP, "g1",
                 SubjectType.GROUP, "g1"),
        NOW,
    )
    store.create_scope(group_scope)
    db.connection.execute(
        "UPDATE memory_scopes SET is_enabled=1 WHERE id=?", (group_scope.id,),
    )
    consent.grant(
        group_scope, GroupConsentType.GROUP_MEMORY, operator_id="admin",
    )
    memory = MemoryEntity(
        id="g1m", scope_id=group_scope.id, subject_id="g1",
        memory_type=MemoryType.FACT, content="群约定周五复盘",
        normalized_content=normalize_content("群约定周五复盘"),
        status=MemoryStatus.ACTIVE, source_type=SourceType.EXPLICIT,
        current_revision=1, created_at=NOW, updated_at=NOW,
    )
    asyncio.run(coordinator.create(
        memory,
        MemoryRevision("rev-g1m", "g1m", 1, memory.content, memory.status,
                       NOW, "created"),
        MemorySource("src-g1m", "g1m", SourceType.EXPLICIT,
                     OperatorType.USER, "admin", NOW),
    ))
    lineage, creation_seq = store.get_lineage(
        "g1m", scope_id=group_scope.id, subject_id="g1",
    )
    vector.upsert(
        "g1m", (1.0, 0.0), dim=2, provider="fake", model="m2",
        content_hash="h-g1m", db_lineage_id=lineage,
        creation_seq=creation_seq, revision=1, created_at=NOW,
    )
    hybrid = HybridGroupRecallService(
        store, index, consent, FixedClock(), store, coordinator,
        vector=vector, provider=_policy(),
    )
    memories, trace = asyncio.run(
        hybrid.recall_group_with_trace("群复盘", group_scope, limit=5),
    )
    assert [memory.id for memory in memories] == ["g1m"]
    assert trace.scope["type"] == "GROUP"


class FakeRerankProvider:
    provider_id = "local-rerank"
    model = "bge-reranker-v2-m3"

    async def rerank(self, query, documents, top_n):
        return tuple(
            (index, 1.0 if "绿茶" in document else 0.0)
            for index, document in enumerate(documents)
        )


def test_hybrid_rerank_reorders_and_trace_records_provider(stack):
    _, store, index, vector, consent, coordinator, scope = stack
    hybrid = HybridRecallService(
        store, index, consent, FixedClock(), store, coordinator,
        vector=vector, provider=_policy(),
        rerank=FakeRerankProvider(), rerank_enabled=True,
        rerank_timeout_seconds=5,
        vector_candidate_limit=5, hybrid_candidate_limit=10,
    )
    memories, trace = asyncio.run(
        hybrid.recall_with_trace("喜欢", scope, limit=5),
    )
    assert memories and memories[0].id == "m2"
    assert trace.rerank["provider"] == "local-rerank"
    assert trace.rerank["model"] == "bge-reranker-v2-m3"
    assert trace.rerank["candidates"]
    assert "degraded" not in trace.rerank


class RecordingRerank:
    provider_id = "recording"
    model = "test-model"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def rerank(self, query, documents, top_n):
        self.calls.append(list(documents))
        return tuple((index, 1.0 - index / 10) for index in range(len(documents)))


class FailingRerank:
    provider_id = "failing"
    model = "test-model"

    def __init__(self) -> None:
        self.calls = 0

    async def rerank(self, query, documents, top_n):
        self.calls += 1
        raise RuntimeError("rerank down")


def test_rerank_truncates_candidates_and_document_text(stack):
    _, store, index, vector, consent, coordinator, scope = stack
    reranker = RecordingRerank()
    hybrid = HybridRecallService(
        store, index, consent, FixedClock(), store, coordinator,
        vector=vector, provider=_policy(), rerank=reranker, rerank_enabled=True,
        rerank_max_docs=1, rerank_max_text_chars=2,
        vector_candidate_limit=5, hybrid_candidate_limit=10,
    )
    asyncio.run(hybrid.recall_with_trace("喜欢", scope, limit=5))
    assert len(reranker.calls) == 1
    assert len(reranker.calls[0]) == 1
    assert all(len(document) <= 2 for document in reranker.calls[0])


def test_rerank_failure_opens_cooldown_and_skips_following_calls(stack):
    _, store, index, vector, consent, coordinator, scope = stack
    reranker = FailingRerank()
    hybrid = HybridRecallService(
        store, index, consent, FixedClock(), store, coordinator,
        vector=vector, provider=_policy(), rerank=reranker, rerank_enabled=True,
        rerank_cooldown_seconds=60,
        vector_candidate_limit=5, hybrid_candidate_limit=10,
    )
    _, first = asyncio.run(hybrid.recall_with_trace("喜欢", scope, limit=5))
    _, second = asyncio.run(hybrid.recall_with_trace("喜欢", scope, limit=5))
    assert first.rerank["degraded"] is True
    assert second.rerank.get("skipped") == "cooldown"
    assert reranker.calls == 1


def test_hybrid_recall_records_read_access_events(stack):
    db, store, index, vector, consent, coordinator, scope = stack
    hybrid = HybridRecallService(
        store, index, consent, FixedClock(), store, coordinator,
        vector=vector, provider=_policy(),
        vector_candidate_limit=5, hybrid_candidate_limit=10,
    )
    memories, _ = asyncio.run(
        hybrid.recall_with_trace("喜欢", scope, limit=5),
    )
    asyncio.run(hybrid.record_access((memories[0].id,), scope))
    rows = db.connection.execute(
        "SELECT memory_id, actor_id, surface FROM memory_access_events",
    ).fetchall()
    assert [row["memory_id"] for row in rows] == [memories[0].id]
    assert rows and all(
        row["actor_id"] == "u1" and row["surface"] == "personal" for row in rows
    )
