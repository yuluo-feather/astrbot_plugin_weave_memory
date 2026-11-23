"""VECTOR outbox, index job state machine and exact vector index tests."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.consents import (
    SQLiteConsentStore,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.database import (
    SQLiteDatabase,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.fts import SQLiteFTSIndex
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.index_jobs import (
    SQLiteIndexJobStore,
    SQLiteIndexStateStore,
    SQLiteVectorOutbox,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.store import (
    SQLiteMemoryStore,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.vector import (
    SQLiteVectorIndex,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.system import UUID4IdGenerator
from data.plugins.astrbot_plugin_weave_memory.app.consent_service import ConsentService
from data.plugins.astrbot_plugin_weave_memory.app.index_job_service import (
    IndexJobService,
)
from data.plugins.astrbot_plugin_weave_memory.app.lifecycle_service import (
    LifecycleService,
)
from data.plugins.astrbot_plugin_weave_memory.app.write_coordinator import (
    WriteCoordinator,
)
from data.plugins.astrbot_plugin_weave_memory.core.errors import NotFound
from data.plugins.astrbot_plugin_weave_memory.core.model import (
    Identity,
    IndexJob,
    IndexJobStatus,
    IndexOperation,
    IndexTarget,
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


def _memory(scope: Scope, *, mid: str = "m1", content: str = "喜欢黑咖啡"):
    return MemoryEntity(
        id=mid, scope_id=scope.id, subject_id="u1",
        memory_type=MemoryType.PREFERENCE, content=content,
        normalized_content=normalize_content(content), status=MemoryStatus.ACTIVE,
        source_type=SourceType.EXPLICIT, current_revision=1,
        created_at=NOW, updated_at=NOW,
    )


def _revision(memory):
    return MemoryRevision(
        id=f"rev-{memory.id}", memory_id=memory.id, revision=1,
        content=memory.content, status=memory.status, changed_at=NOW,
        change_reason="created",
    )


def _source(memory):
    return MemorySource(
        id=f"src-{memory.id}", memory_id=memory.id, source_type=SourceType.EXPLICIT,
        operator_type=OperatorType.USER, operator_id="u1", captured_at=NOW,
    )


@pytest.fixture
def stack(tmp_path):
    db = SQLiteDatabase(tmp_path / "outbox.db")
    db.migrate()
    outbox = SQLiteVectorOutbox()
    store = SQLiteMemoryStore(db, vector_outbox=outbox)
    index = SQLiteFTSIndex(db)
    scope = _scope()
    store.create_scope(scope)
    coordinator = WriteCoordinator(store, index)
    consent = ConsentService(
        SQLiteConsentStore(db), coordinator, FixedClock(), UUID4IdGenerator(),
    )
    lifecycle = LifecycleService(coordinator, store, consent)
    jobs = SQLiteIndexJobStore(db)
    vector = SQLiteVectorIndex(
        db, provider_id="fake", model="m2", dim=2, config_hash="cfg",
        states=SQLiteIndexStateStore(db),
    )
    yield db, store, coordinator, lifecycle, jobs, vector, scope
    db.close()


def _job_rows(db):
    return db.connection.execute(
        "SELECT operation, status FROM index_jobs ORDER BY created_at, id",
    ).fetchall()


def test_writes_enqueue_upsert_and_delete_in_the_same_transaction(stack):
    db, store, coordinator, lifecycle, _, _, scope = stack
    memory = _memory(scope)
    asyncio.run(coordinator.create(memory, _revision(memory), _source(memory)))
    assert [(row["operation"], row["status"]) for row in _job_rows(db)] == [
        ("UPSERT", "PENDING"),
    ]
    asyncio.run(lifecycle.archive_memory(
        memory, revision_id="rev-archive", at=NOW + timedelta(seconds=1),
        reason="archive", operator_type=OperatorType.USER,
    ))
    archived = store.get(memory.id, scope_id=scope.id, subject_id="u1")
    asyncio.run(lifecycle.restore_memory(
        archived, revision_id="rev-restore", at=NOW + timedelta(seconds=2),
        reason="restore", operator_type=OperatorType.USER,
    ))
    operations = [row["operation"] for row in _job_rows(db)]
    assert operations.count("UPSERT") == 1
    assert operations.count("DELETE") == 1


def test_job_claim_fail_and_reset(stack):
    _, _, _, _, jobs, _, _ = stack
    job, created = jobs.create_or_get(
        IndexJob(
            id="job-1", memory_id="m1", target=IndexTarget.VECTOR,
            operation=IndexOperation.UPSERT,
            content_hash="h", status=IndexJobStatus.PENDING, attempts=0,
            created_at=NOW, updated_at=NOW,
        ),
    )
    assert created is True
    claimed = jobs.claim_next(at=NOW)
    assert claimed is not None and claimed.status is IndexJobStatus.RUNNING
    assert jobs.reset_running_to_pending(at=NOW) == 1
    claimed = jobs.claim_next(at=NOW)
    assert claimed is not None
    status = jobs.mark_failed("job-1", error_code="provider_timeout",
                              max_attempts=1, at=NOW)
    assert status is IndexJobStatus.FAILED


def test_vector_search_is_scoped_active_and_body_free(stack):
    _, store, coordinator, _, _, vector, scope = stack
    first = _memory(scope, mid="m1", content="黑咖啡")
    second = _memory(scope, mid="m2", content="绿茶")
    asyncio.run(coordinator.create(first, _revision(first), _source(first)))
    asyncio.run(coordinator.create(second, _revision(second), _source(second)))
    vector.upsert(
        first.id, (1.0, 0.0), dim=2, provider="fake", model="m2",
        content_hash="h1", db_lineage_id=store.get_lineage(
            first.id, scope_id=scope.id, subject_id="u1",
        )[0], creation_seq=1, revision=1, created_at=NOW,
    )
    vector.upsert(
        second.id, (0.0, 1.0), dim=2, provider="fake", model="m2",
        content_hash="h2", db_lineage_id=store.get_lineage(
            second.id, scope_id=scope.id, subject_id="u1",
        )[0], creation_seq=2, revision=1, created_at=NOW,
    )
    found = vector.search((1.0, 0.0), scope_id=scope.id, subject_id="u1",
                          limit=5, at=NOW)
    assert [item.memory_id for item in found][0] == "m1"


def test_index_job_service_drains_upsert(stack):
    _, store, coordinator, _, jobs, vector, scope = stack
    memory = _memory(scope)
    asyncio.run(coordinator.create(memory, _revision(memory), _source(memory)))
    service = IndexJobService(
        store, jobs, vector, FakeProvider(), coordinator, FixedClock(),
        max_attempts=2, enabled=True,
    )
    assert asyncio.run(service.drain_once()) == 1
    row = stack[0].connection.execute(
        "SELECT count(*) FROM memory_embeddings",
    ).fetchone()
    assert row[0] == 1
    assert all(
        item.status is IndexJobStatus.DONE for item in jobs.list_by_status(
            (IndexJobStatus.DONE,),
        )
    )
    with pytest.raises(NotFound):
        store.get_any("missing")
