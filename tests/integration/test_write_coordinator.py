import asyncio
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.consents import (
    SQLiteConsentStore,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.database import (
    SQLiteDatabase,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.fts import SQLiteFTSIndex
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.store import (
    SQLiteMemoryStore,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.system import UUID4IdGenerator
from data.plugins.astrbot_plugin_weave_memory.app.consent_service import ConsentService
from data.plugins.astrbot_plugin_weave_memory.app.lifecycle_service import (
    LifecycleService,
)
from data.plugins.astrbot_plugin_weave_memory.app.write_coordinator import (
    IndexState,
    WriteCoordinator,
)
from data.plugins.astrbot_plugin_weave_memory.core.errors import (
    Conflict,
    IndexUnavailable,
    InvalidTransition,
    NotFound,
    ServiceClosed,
)
from data.plugins.astrbot_plugin_weave_memory.core.model import (
    ArchiveReason,
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
        coordinator, store, ConsentService(SQLiteConsentStore(_db), coordinator, FixedClock(), UUID4IdGenerator()),
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
        coordinator, store, ConsentService(SQLiteConsentStore(_db), coordinator, FixedClock(), UUID4IdGenerator()),
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

    from data.plugins.astrbot_plugin_weave_memory.core.errors import AuthorizationDenied
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
    from data.plugins.astrbot_plugin_weave_memory.app.startup_recovery import (
        StartupRecoveryService,
    )

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


def assert_no_canary(db_path, *canaries):
    raw = Path(db_path).read_bytes()
    wal = Path(db_path + "-wal")
    wal_raw = wal.read_bytes() if wal.exists() else b""
    for canary in canaries:
        assert canary.encode() not in raw
        assert canary.encode() not in wal_raw


def test_delete_securely_removes_content_from_database_file(stack):
    db, _, _, scope, coordinator = stack
    canary = "SECRET-CANARY-DELETE-8c71"
    memory, revision, source = build(scope, mid="m-secure-delete", content=canary)
    asyncio.run(coordinator.create(memory, revision, source))

    receipt = asyncio.run(coordinator.delete(
        memory.id, scope_id=scope.id, subject_id="u1",
        operation_id="op-secure-delete", operator_type=OperatorType.USER,
        operator_id="u1", occurred_at=NOW,
    ))
    assert receipt.index_state is IndexState.HEALTHY
    assert receipt.purged is True
    assert db.connection.execute("SELECT count(*) FROM memories").fetchone()[0] == 0
    # Check while the connection is still open: close() itself checkpoints WAL.
    assert_no_canary(db.path, canary)

    db.close()
    assert_no_canary(db.path, canary)


def test_clear_securely_removes_content_from_database_file(stack):
    db, store, index, scope, coordinator = stack
    first = "SECRET-CANARY-CLEAR-1a2b"
    second = "SECRET-CANARY-CLEAR-3c4d"
    for mid, content in (("m-secure-clear-1", first), ("m-secure-clear-2", second)):
        memory, revision, source = build(scope, mid=mid, content=content)
        asyncio.run(coordinator.create(memory, revision, source))

    other_scope = Scope(
        "scope-other",
        Identity("astrbot", "default", ScopeType.PERSONAL,
                 "platform-u2", SubjectType.USER, "u2"),
        NOW,
    )
    store.create_scope(other_scope)
    other, other_revision, other_source = build(
        other_scope, mid="m-other-scope", content="OTHER-SCOPE-LIVE",
    )
    asyncio.run(coordinator.create(other, other_revision, other_source))
    db.connection.execute(
        "INSERT INTO memory_tags(memory_id, tag, created_at) VALUES (?, ?, ?)",
        (other.id, "live", NOW.isoformat()),
    )

    receipt = asyncio.run(coordinator.clear_scope(
        scope.id, operation_id="op-secure-clear",
        operator_type=OperatorType.USER, operator_id="u1", occurred_at=NOW,
    ))
    assert receipt.affected_count == 2
    assert receipt.index_state is IndexState.HEALTHY
    assert receipt.purged is True
    assert_no_canary(db.path, first, second)
    assert store.get(other.id, scope_id=other_scope.id, subject_id="u2") == other
    assert index.search("OTHER", scope_id=other_scope.id, subject_id="u2") == (other.id,)
    assert db.connection.execute(
        "SELECT count(*) FROM memory_revisions WHERE memory_id=?", (other.id,)
    ).fetchone()[0] == 1
    assert db.connection.execute(
        "SELECT count(*) FROM memory_sources WHERE memory_id=?", (other.id,)
    ).fetchone()[0] == 1
    assert db.connection.execute(
        "SELECT count(*) FROM memory_tags WHERE memory_id=?", (other.id,)
    ).fetchone()[0] == 1

    db.close()
    assert_no_canary(db.path, first, second)


def test_clear_reports_purge_pending_when_checkpoint_is_blocked(stack):
    import sqlite3

    db, _, _, scope, coordinator = stack
    canary = "SECRET-CANARY-CLEAR-READER-6a7b"
    memory, revision, source = build(scope, mid="m-clear-reader", content=canary)
    asyncio.run(coordinator.create(memory, revision, source))

    reader = sqlite3.connect(db.path, isolation_level=None)
    reader.execute("PRAGMA journal_mode=WAL")
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM memories").fetchone()

    receipt = asyncio.run(coordinator.clear_scope(
        scope.id, operation_id="op-clear-reader",
        operator_type=OperatorType.USER, operator_id="u1", occurred_at=NOW,
    ))
    assert receipt.index_state is IndexState.HEALTHY
    assert receipt.purged is False
    marker = db.connection.execute(
        "SELECT value FROM memory_meta WHERE key='secure_purge_pending'"
    ).fetchone()[0]
    assert marker == "1"

    reader.execute("ROLLBACK")
    reader.close()
    assert db.secure_purge_if_pending() is True
    assert_no_canary(db.path, canary)
    db.close()


def test_secure_purge_works_without_wal(stack):
    db, _, _, scope, coordinator = stack
    db.connection.execute("PRAGMA journal_mode=DELETE")
    canary = "SECRET-CANARY-NOWAL-9d0e"
    memory, revision, source = build(scope, mid="m-no-wal", content=canary)
    asyncio.run(coordinator.create(memory, revision, source))

    receipt = asyncio.run(coordinator.delete(
        memory.id, scope_id=scope.id, subject_id="u1",
        operation_id="op-no-wal", operator_type=OperatorType.USER,
        operator_id="u1", occurred_at=NOW,
    ))
    assert receipt.index_state is IndexState.HEALTHY
    assert receipt.purged is True
    assert_no_canary(db.path, canary)
    db.close()


def test_clear_securely_removes_content_without_wal(stack):
    db, store, _, scope, coordinator = stack
    db.connection.execute("PRAGMA journal_mode=DELETE")
    first = "SECRET-CANARY-CLEAR-NOWAL-1"
    second = "SECRET-CANARY-CLEAR-NOWAL-2"
    for mid, content in (("m-nowal-clear-1", first), ("m-nowal-clear-2", second)):
        memory, revision, source = build(scope, mid=mid, content=content)
        asyncio.run(coordinator.create(memory, revision, source))

    other_scope = Scope(
        "scope-nowal-other",
        Identity("astrbot", "default", ScopeType.PERSONAL,
                 "platform-u3", SubjectType.USER, "u3"),
        NOW,
    )
    store.create_scope(other_scope)
    other, other_revision, other_source = build(
        other_scope, mid="m-nowal-other", content="OTHER-NOWAL-LIVE",
    )
    asyncio.run(coordinator.create(other, other_revision, other_source))

    receipt = asyncio.run(coordinator.clear_scope(
        scope.id, operation_id="op-clear-nowal",
        operator_type=OperatorType.USER, operator_id="u1", occurred_at=NOW,
    ))
    assert receipt.affected_count == 2
    assert receipt.purged is True
    assert_no_canary(db.path, first, second)
    assert store.get(other.id, scope_id=other_scope.id, subject_id="u3") == other
    db.close()


def test_clear_with_dirty_fts_defers_purge_until_startup_recovers(stack, monkeypatch):
    from data.plugins.astrbot_plugin_weave_memory.app.startup_recovery import (
        StartupRecoveryService,
    )

    db, _, index, scope, coordinator = stack
    first = "SECRET-CANARY-CLEAR-FTS-1"
    second = "SECRET-CANARY-CLEAR-FTS-2"
    for mid, content in (("m-clear-fts-1", first), ("m-clear-fts-2", second)):
        memory, revision, source = build(scope, mid=mid, content=content)
        asyncio.run(coordinator.create(memory, revision, source))

    def fail():
        raise IndexUnavailable("simulated FTS rebuild failure")

    monkeypatch.setattr(index, "rebuild", fail)
    receipt = asyncio.run(coordinator.clear_scope(
        scope.id, operation_id="op-clear-fts",
        operator_type=OperatorType.USER, operator_id="u1", occurred_at=NOW,
    ))
    assert receipt.index_state is IndexState.PENDING_REBUILD
    assert receipt.purged is False
    marker = db.connection.execute(
        "SELECT value FROM memory_meta WHERE key='secure_purge_pending'"
    ).fetchone()[0]
    assert marker == "1"

    monkeypatch.undo()
    StartupRecoveryService(db, index).run()
    assert db.secure_purge_if_pending() is True
    assert_no_canary(db.path, first, second)
    db.close()


def test_write_metadata_waits_for_inflight_content_write(stack):
    _, _, _, scope, _ = stack

    class BlockingStore:
        def __init__(self):
            self.order = []
            self.entered = threading.Event()
            self.release = threading.Event()

        def create(self, memory, revision, source):
            self.order.append("content-enter")
            self.entered.set()
            assert self.release.wait(5)
            self.order.append("content-exit")

    class NoopIndex:
        def sync_memory(self, memory_id):
            return None

        def integrity_check(self):
            return None

        def is_healthy(self):
            return True

    store = BlockingStore()
    coordinator = WriteCoordinator(store, NoopIndex())
    memory, revision, source = build(scope)

    def run_content():
        asyncio.run(coordinator.create(memory, revision, source))

    content_thread = threading.Thread(target=run_content)
    content_thread.start()
    assert store.entered.wait(5)

    metadata_done = threading.Event()

    def run_metadata():
        coordinator.write_metadata(lambda: store.order.append("metadata"))
        metadata_done.set()

    metadata_thread = threading.Thread(target=run_metadata)
    metadata_thread.start()
    assert not metadata_done.wait(0.2)

    store.release.set()
    content_thread.join(5)
    metadata_thread.join(5)
    assert store.order == ["content-enter", "content-exit", "metadata"]


def test_delete_reports_purge_pending_when_checkpoint_is_blocked(stack):
    import sqlite3

    db, _, _, scope, coordinator = stack
    canary = "SECRET-CANARY-READER-5e6f"
    memory, revision, source = build(scope, mid="m-reader", content=canary)
    asyncio.run(coordinator.create(memory, revision, source))

    reader = sqlite3.connect(db.path, isolation_level=None)
    reader.execute("PRAGMA journal_mode=WAL")
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM memories").fetchone()

    receipt = asyncio.run(coordinator.delete(
        memory.id, scope_id=scope.id, subject_id="u1",
        operation_id="op-reader-delete", operator_type=OperatorType.USER,
        operator_id="u1", occurred_at=NOW,
    ))
    assert receipt.index_state is IndexState.HEALTHY
    assert receipt.purged is False
    marker = db.connection.execute(
        "SELECT value FROM memory_meta WHERE key='secure_purge_pending'"
    ).fetchone()[0]
    assert marker == "1"

    reader.execute("ROLLBACK")
    reader.close()
    assert db.secure_purge_if_pending() is True
    marker = db.connection.execute(
        "SELECT value FROM memory_meta WHERE key='secure_purge_pending'"
    ).fetchone()[0]
    assert marker == "0"
    assert_no_canary(db.path, canary)
    db.close()


def test_delete_with_dirty_fts_defers_purge_until_startup_recovers(stack, monkeypatch):
    from data.plugins.astrbot_plugin_weave_memory.app.startup_recovery import (
        StartupRecoveryService,
    )

    db, _, index, scope, coordinator = stack
    canary = "SECRET-CANARY-FTS-2b3c"
    memory, revision, source = build(scope, mid="m-fts-pending", content=canary)
    asyncio.run(coordinator.create(memory, revision, source))

    def fail(_memory_id):
        raise IndexUnavailable("simulated FTS failure")

    monkeypatch.setattr(index, "sync_memory", fail)
    receipt = asyncio.run(coordinator.delete(
        memory.id, scope_id=scope.id, subject_id="u1",
        operation_id="op-fts-pending", operator_type=OperatorType.USER,
        operator_id="u1", occurred_at=NOW,
    ))
    assert receipt.index_state is IndexState.PENDING_REBUILD
    assert receipt.purged is False
    marker = db.connection.execute(
        "SELECT value FROM memory_meta WHERE key='secure_purge_pending'"
    ).fetchone()[0]
    assert marker == "1"

    monkeypatch.undo()
    StartupRecoveryService(db, index).run()
    assert db.secure_purge_if_pending() is True
    assert_no_canary(db.path, canary)
    db.close()


def test_status_changes_must_use_lifecycle_service_and_deleted_uses_purge_path(stack):
    from data.plugins.astrbot_plugin_weave_memory.core.policy.lifecycle import (
        transition,
    )

    _db, store, _, scope, coordinator = stack
    memory, revision, source = build(scope)
    lifecycle = LifecycleService(
        coordinator, store, ConsentService(SQLiteConsentStore(_db), coordinator, FixedClock(), UUID4IdGenerator()),
    )

    async def run():
        await coordinator.create(memory, revision, source)
        archived = transition(
            memory, MemoryStatus.ARCHIVED, at=NOW + timedelta(seconds=1),
            archive_reason=ArchiveReason.USER_ARCHIVE,
            purge_after=NOW + timedelta(days=30),
        )
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
    from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.consents import (
        SQLiteConsentStore,
    )
    from data.plugins.astrbot_plugin_weave_memory.adapters.system import (
        UUID4IdGenerator,
    )
    from data.plugins.astrbot_plugin_weave_memory.app.consent_service import (
        ConsentService,
        PersonalConsentType,
    )
    from data.plugins.astrbot_plugin_weave_memory.app.recall_service import (
        MemoryContextComposer,
        RecallService,
    )

    db, store, index, scope, coordinator = stack
    memory, revision, source = build(scope)
    consent = ConsentService(SQLiteConsentStore(db), coordinator, FixedClock(), UUID4IdGenerator())
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
    from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.consents import (
        SQLiteConsentStore,
    )
    from data.plugins.astrbot_plugin_weave_memory.adapters.system import (
        UUID4IdGenerator,
    )
    from data.plugins.astrbot_plugin_weave_memory.app.consent_service import (
        ConsentService,
        PersonalConsentType,
    )
    from data.plugins.astrbot_plugin_weave_memory.app.recall_service import (
        RecallService,
    )

    db, store, index, scope, coordinator = stack
    memory, revision, source = build(scope)
    store.create(memory, revision, source)
    index.sync_memory(memory.id)
    consent = ConsentService(SQLiteConsentStore(db), coordinator, FixedClock(), UUID4IdGenerator())
    consent.grant(scope, PersonalConsentType.PERSONAL_MEMORY, operator_id="u1")

    class DeleteAfterCandidateSearch:
        def search_diagnostics(
            self, query, *, scope_id, subject_id, limit=100, scope_type="PERSONAL",
        ):
            return index.search_diagnostics(
                query, scope_id=scope_id, subject_id=subject_id, limit=limit,
                scope_type=scope_type,
            )

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
    from data.plugins.astrbot_plugin_weave_memory.app.startup_recovery import (
        StartupRecoveryService,
    )

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
