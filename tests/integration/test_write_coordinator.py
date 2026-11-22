import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from weavemem.adapters.sqlite.consents import SQLiteConsentStore
from weavemem.adapters.sqlite.database import SQLiteDatabase
from weavemem.adapters.sqlite.fts import SQLiteFTSIndex
from weavemem.adapters.sqlite.store import SQLiteMemoryStore
from weavemem.adapters.system import UUID4IdGenerator
from weavemem.app.consent_service import ConsentService
from weavemem.app.lifecycle_service import LifecycleService
from weavemem.app.write_coordinator import IndexState, WriteCoordinator
from weavemem.core.errors import (
    Conflict,
    IndexUnavailable,
    InvalidTransition,
    NotFound,
    ServiceClosed,
)
from weavemem.core.model import (
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
from weavemem.core.normalization import normalize_content

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


class FixedClock:
    def now(self):
        return NOW


@pytest.fixture
def stack(tmp_path):
    db = SQLiteDatabase(tmp_path / "coordinator.db")
    db.migrate()
    store = SQLiteMemoryStore(db)
    index = SQLiteFTSIndex(db)
    scope = Scope("scope-1", Identity("astrbot", "default", ScopeType.PERSONAL,
                                      "platform-u1", SubjectType.USER, "u1"), NOW)
    store.create_scope(scope)
    coordinator = WriteCoordinator(store, index)
    yield db, store, index, scope, coordinator
    db.close()


def build(scope, *, mid="m1", content="喜欢黑咖啡", status=MemoryStatus.ACTIVE, valid_until=None):
    memory = MemoryEntity(
        id=mid, scope_id=scope.id, subject_id=scope.identity.subject_id,
        memory_type=MemoryType.PREFERENCE, content=content,
        normalized_content=normalize_content(content), status=status,
        source_type=SourceType.EXPLICIT, current_revision=1,
        created_at=NOW, updated_at=NOW, valid_until=valid_until,
    )
    revision = MemoryRevision("r-" + mid, mid, 1, content, status, NOW, "created")
    source = MemorySource("s-" + mid, mid, SourceType.EXPLICIT,
                          OperatorType.USER, "u1", NOW)
    return memory, revision, source


def test_coordinator_serializes_competing_duplicate_writes(stack):
    _, store, _, scope, coordinator = stack
    m1, r1, s1 = build(scope, mid="m1")
    m2, r2, s2 = build(scope, mid="m2", content=" 喜欢黑咖啡。 ")

    async def run():
        return await asyncio.gather(
            coordinator.create(m1, r1, s1), coordinator.create(m2, r2, s2),
            return_exceptions=True,
        )

    results = asyncio.run(run())
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, Conflict) for result in results) == 1
    assert len(store.list_active(scope_id=scope.id, subject_id="u1")) == 1


def test_lifecycle_service_requires_user_for_candidate_activation(stack):
    _db, store, index, scope, coordinator = stack
    candidate, revision, source = build(scope, status=MemoryStatus.CANDIDATE)
    lifecycle = LifecycleService(
        coordinator, store, ConsentService(SQLiteConsentStore(_db), FixedClock(), UUID4IdGenerator()),
    )

    async def run():
        await coordinator.create(candidate, revision, source)
        with pytest.raises(InvalidTransition):
            await lifecycle.transition_memory(
                candidate, MemoryStatus.ACTIVE, revision_id="r-system",
                at=NOW + timedelta(seconds=1), reason="confirm",
                operator_type=OperatorType.SYSTEM,
            )
        receipt = await lifecycle.transition_memory(
            candidate, MemoryStatus.ACTIVE, revision_id="r-user",
            at=NOW + timedelta(seconds=2), reason="user confirmed",
            operator_type=OperatorType.USER,
        )
        assert receipt.index_state is IndexState.HEALTHY

    asyncio.run(run())
    assert store.get(candidate.id, scope_id=scope.id, subject_id="u1").status is MemoryStatus.ACTIVE
    assert index.search("黑咖啡", scope_id=scope.id, subject_id="u1") == (candidate.id,)


def test_expiry_service_persists_active_expiration(stack):
    _db, store, index, scope, coordinator = stack
    active, revision, source = build(scope, valid_until=NOW - timedelta(seconds=1))
    lifecycle = LifecycleService(
        coordinator, store, ConsentService(SQLiteConsentStore(_db), FixedClock(), UUID4IdGenerator()),
    )

    async def run():
        await coordinator.create(active, revision, source)
        receipt = await lifecycle.expire_active(
            active, revision_id="r-expired", at=NOW,
        )
        assert receipt is not None

    asyncio.run(run())
    assert store.get(active.id, scope_id=scope.id, subject_id="u1").status is MemoryStatus.EXPIRED
    assert index.search("黑咖啡", scope_id=scope.id, subject_id="u1") == ()


def test_index_failure_returns_committed_pending_receipt(stack):
    _, store, index, scope, coordinator = stack
    memory, revision, source = build(scope)
    index.db.connection.execute("DROP TABLE memory_fts")

    async def run():
        receipt = await coordinator.create(memory, revision, source)
        assert receipt.index_state is IndexState.PENDING_REBUILD

    asyncio.run(run())
    assert store.get(memory.id, scope_id=scope.id, subject_id="u1") == memory
    with pytest.raises(IndexUnavailable):
        index.search("黑咖啡", scope_id=scope.id, subject_id="u1")


def test_coordinator_close_rejects_future_writes(stack):
    _, _, _, scope, coordinator = stack
    memory, revision, source = build(scope)

    async def run():
        await coordinator.close()
        with pytest.raises(ServiceClosed):
            await coordinator.create(memory, revision, source)

    asyncio.run(run())


def test_coordinator_clear_scope_rebuilds_index(stack):
    db, store, index, scope, coordinator = stack
    first = build(scope, mid="m1", content="第一条记忆")
    second = build(scope, mid="m2", content="第二条记忆")

    async def run():
        await coordinator.create(*first)
        await coordinator.create(*second)
        receipt = await coordinator.clear_scope(
            scope.id, operation_id="op-clear", operator_type=OperatorType.USER,
            operator_id="u1", occurred_at=NOW,
        )
        assert receipt.affected_count == 2
        assert receipt.index_state is IndexState.HEALTHY

    asyncio.run(run())
    assert store.list_active(scope_id=scope.id, subject_id="u1") == ()
    assert index.is_healthy()
    assert db.connection.execute("SELECT count(*) FROM memory_operations WHERE operation_type='CLEAR_SCOPE'").fetchone()[0] == 1


def test_coordinator_disable_scope_blocks_recall_and_write(stack):
    _, store, index, scope, coordinator = stack
    memory, revision, source = build(scope)

    async def run():
        await coordinator.create(memory, revision, source)
        receipt = await coordinator.disable_scope(
            scope.id, operation_id="op-disable", operator_type=OperatorType.USER,
            operator_id="u1", occurred_at=NOW,
        )
        assert receipt.affected_count == 1
        assert receipt.index_state is IndexState.HEALTHY
        blocked, blocked_revision, blocked_source = build(scope, mid="m2", content="新记忆")
        with pytest.raises(AuthorizationDenied):
            await coordinator.create(blocked, blocked_revision, blocked_source)

    from weavemem.core.errors import AuthorizationDenied
    asyncio.run(run())
    assert store.list_active(scope_id=scope.id, subject_id="u1") == ()
    assert index.search("黑咖啡", scope_id=scope.id, subject_id="u1") == ()


def test_delete_commits_even_when_index_sync_is_pending(stack):
    _, store, index, scope, coordinator = stack
    memory, revision, source = build(scope)

    async def run():
        await coordinator.create(memory, revision, source)
        index.db.connection.execute("DROP TABLE memory_fts")
        receipt = await coordinator.delete(
            memory.id, scope_id=scope.id, subject_id="u1",
            operation_id="op-delete-pending", operator_type=OperatorType.USER,
            operator_id="u1", occurred_at=NOW,
        )
        assert receipt.index_state is IndexState.PENDING_REBUILD

    asyncio.run(run())
    with pytest.raises(NotFound):
        store.get(memory.id, scope_id=scope.id, subject_id="u1")


def test_clear_pending_index_is_recovered_on_startup(stack):
    from weavemem.app.startup_recovery import StartupRecoveryService

    db, _store, index, scope, coordinator = stack
    memory, revision, source = build(scope)

    async def run():
        await coordinator.create(memory, revision, source)
        index.db.connection.execute("DROP TABLE memory_fts")
        receipt = await coordinator.clear_scope(
            scope.id, operation_id="op-clear-pending", operator_type=OperatorType.USER,
            operator_id="u1", occurred_at=NOW,
        )
        assert receipt.index_state is IndexState.PENDING_REBUILD

    asyncio.run(run())
    report = StartupRecoveryService(db, index).run()
    assert report.fts_rebuilt is True
    assert report.indexed_active_count == 0
    assert index.is_healthy()


def test_status_changes_must_use_lifecycle_service_and_deleted_uses_purge_path(stack):
    from weavemem.core.policy.lifecycle import transition

    _db, store, _, scope, coordinator = stack
    memory, revision, source = build(scope)
    lifecycle = LifecycleService(
        coordinator, store, ConsentService(SQLiteConsentStore(_db), FixedClock(), UUID4IdGenerator()),
    )

    async def run():
        await coordinator.create(memory, revision, source)
        archived = transition(memory, MemoryStatus.ARCHIVED, at=NOW + timedelta(seconds=1))
        archived_revision = MemoryRevision("r-archive", memory.id, 2, archived.content,
                                           archived.status, archived.updated_at, "archive")
        with pytest.raises(InvalidTransition):
            await coordinator.update(archived, expected_revision=1, revision=archived_revision)
        with pytest.raises(InvalidTransition):
            await lifecycle.transition_memory(
                memory, MemoryStatus.DELETED, revision_id="r-delete",
                at=NOW + timedelta(seconds=2), reason="delete",
                operator_type=OperatorType.USER,
            )

    asyncio.run(run())
    assert store.get(memory.id, scope_id=scope.id, subject_id="u1") == memory


def test_delete_and_recall_interleaving_never_returns_deleted_memory_after_delete(stack):
    from weavemem.adapters.sqlite.consents import SQLiteConsentStore
    from weavemem.adapters.system import UUID4IdGenerator
    from weavemem.app.consent_service import ConsentService, PersonalConsentType
    from weavemem.app.recall_service import MemoryContextComposer, RecallService

    db, store, index, scope, coordinator = stack
    memory, revision, source = build(scope)
    consent = ConsentService(SQLiteConsentStore(db), FixedClock(), UUID4IdGenerator())
    consent.grant(scope, PersonalConsentType.PERSONAL_MEMORY, operator_id="u1")
    recall = RecallService(store, index, consent, FixedClock(), coordinator, store)
    composer = MemoryContextComposer(recall)

    async def run():
        await coordinator.create(memory, revision, source)
        before_delete = await composer.compose("黑咖啡", scope)
        assert before_delete is not None
        await coordinator.delete(
            memory.id, scope_id=scope.id, subject_id="u1",
            operation_id="op-concurrent-delete", operator_type=OperatorType.USER,
            operator_id="u1", occurred_at=NOW,
        )
        after_delete = await composer.compose("黑咖啡", scope)
        assert after_delete is None

    asyncio.run(run())
    assert index.search("黑咖啡", scope_id=scope.id, subject_id="u1") == ()
    assert store.list_active(scope_id=scope.id, subject_id="u1") == ()


def test_delete_linearizing_between_fts_candidates_and_authoritative_fetch(stack):
    from weavemem.adapters.sqlite.consents import SQLiteConsentStore
    from weavemem.adapters.system import UUID4IdGenerator
    from weavemem.app.consent_service import ConsentService, PersonalConsentType
    from weavemem.app.recall_service import RecallService

    db, store, index, scope, _ = stack
    memory, revision, source = build(scope)
    store.create(memory, revision, source)
    index.sync_memory(memory.id)
    consent = ConsentService(SQLiteConsentStore(db), FixedClock(), UUID4IdGenerator())
    consent.grant(scope, PersonalConsentType.PERSONAL_MEMORY, operator_id="u1")

    class DeleteAfterCandidateSearch:
        def search(self, query, *, scope_id, subject_id, limit=20, at=None):
            candidates = index.search(
                query, scope_id=scope_id, subject_id=subject_id, limit=limit, at=at,
            )
            store.delete(
                memory.id, scope_id=scope.id, subject_id="u1",
                operation_id="op-delete-between-search-fetch",
                operator_type=OperatorType.USER, operator_id="u1", occurred_at=NOW,
            )
            index.sync_memory(memory.id)
            return candidates

    recall = RecallService(
        store, DeleteAfterCandidateSearch(), consent, FixedClock(),
        WriteCoordinator(store, index), store,
    )
    assert recall.recall("黑咖啡", scope) == ()


def test_update_commits_authority_when_fts_is_missing_and_startup_rebuilds(stack):
    from weavemem.app.startup_recovery import StartupRecoveryService

    db, store, index, scope, coordinator = stack
    memory, revision, source = build(scope)

    async def run():
        await coordinator.create(memory, revision, source)
        db.connection.execute("DROP TABLE memory_fts")
        updated = replace(
            memory, content="喜欢燕麦拿铁",
            normalized_content=normalize_content("喜欢燕麦拿铁"),
            current_revision=2, updated_at=NOW + timedelta(seconds=1),
        )
        updated_revision = MemoryRevision(
            "r-m1-2", memory.id, 2, updated.content, updated.status,
            updated.updated_at, "explicit update",
        )
        receipt = await coordinator.update(
            updated, expected_revision=1, revision=updated_revision,
        )
        assert receipt.index_state is IndexState.PENDING_REBUILD
        assert store.get(memory.id, scope_id=scope.id, subject_id="u1") == updated
        return updated

    updated = asyncio.run(run())
    recovery = StartupRecoveryService(db, index).run()
    assert recovery.fts_rebuilt is True
    assert index.search("燕麦拿铁", scope_id=scope.id, subject_id="u1") == (updated.id,)
    assert index.search("黑咖啡", scope_id=scope.id, subject_id="u1") == ()


def test_coordinator_close_waits_for_writer_then_rejects_queued_work(stack):
    _, _, _, scope, coordinator = stack
    first, first_revision, first_source = build(scope, mid="close-first")
    queued, queued_revision, queued_source = build(
        scope, mid="close-queued", content="另一条记忆",
    )

    async def run():
        await coordinator._lock.acquire()
        close_task = asyncio.create_task(coordinator.close())
        await asyncio.sleep(0)
        queued_task = asyncio.create_task(
            coordinator.create(queued, queued_revision, queued_source),
        )
        await asyncio.sleep(0)
        assert not close_task.done()
        coordinator._lock.release()
        await close_task
        with pytest.raises(ServiceClosed):
            await queued_task
        with pytest.raises(ServiceClosed):
            await coordinator.create(first, first_revision, first_source)

    asyncio.run(run())
