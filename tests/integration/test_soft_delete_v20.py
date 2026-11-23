"""V2.0 soft-delete state machine, retention and ledger replay."""

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
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.ledger import SQLiteLedger
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.store import (
    SQLiteMemoryStore,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.system import UUID4IdGenerator
from data.plugins.astrbot_plugin_weave_memory.app.consent_service import (
    ConsentService,
    PersonalConsentType,
)
from data.plugins.astrbot_plugin_weave_memory.app.ledger_service import LedgerService
from data.plugins.astrbot_plugin_weave_memory.app.lifecycle_service import (
    LifecycleService,
)
from data.plugins.astrbot_plugin_weave_memory.app.memory_service import (
    MemoryApplicationService,
)
from data.plugins.astrbot_plugin_weave_memory.app.retention_service import (
    RetentionService,
)
from data.plugins.astrbot_plugin_weave_memory.app.write_coordinator import (
    WriteCoordinator,
)
from data.plugins.astrbot_plugin_weave_memory.core.errors import (
    InvalidTransition,
    NotFound,
    StorageError,
)
from data.plugins.astrbot_plugin_weave_memory.core.model import (
    ArchiveReason,
    Identity,
    LedgerEventType,
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


def _identity() -> Identity:
    return Identity(
        "astrbot", "default", ScopeType.PERSONAL, "u1", SubjectType.USER, "u1",
    )


def _scope() -> Scope:
    return Scope("scope-1", _identity(), NOW)


def _memory(scope: Scope, *, mid: str = "m1", content: str = "喜欢黑咖啡",
            status: MemoryStatus = MemoryStatus.ACTIVE,
            candidate_expires_at: datetime | None = None) -> MemoryEntity:
    return MemoryEntity(
        id=mid, scope_id=scope.id, subject_id="u1",
        memory_type=MemoryType.PREFERENCE, content=content,
        normalized_content=normalize_content(content), status=status,
        source_type=SourceType.EXPLICIT, current_revision=1,
        created_at=NOW, updated_at=NOW, candidate_expires_at=candidate_expires_at,
    )


def _revision(memory: MemoryEntity) -> MemoryRevision:
    return MemoryRevision(
        id=f"rev-{memory.id}", memory_id=memory.id, revision=1,
        content=memory.content, status=memory.status, changed_at=NOW,
        change_reason="created",
    )


def _source(memory: MemoryEntity) -> MemorySource:
    return MemorySource(
        id=f"src-{memory.id}", memory_id=memory.id, source_type=SourceType.EXPLICIT,
        operator_type=OperatorType.USER, operator_id="u1", captured_at=NOW,
    )


@pytest.fixture
def stack(tmp_path):
    db = SQLiteDatabase(tmp_path / "soft-delete.db")
    db.migrate()
    store = SQLiteMemoryStore(db)
    index = SQLiteFTSIndex(db)
    scope = _scope()
    store.create_scope(scope)
    consent_repo = SQLiteConsentStore(db)
    coordinator = WriteCoordinator(store, index)
    consent = ConsentService(
        consent_repo, coordinator, FixedClock(), UUID4IdGenerator(),
    )
    lifecycle = LifecycleService(
        coordinator, store, consent, archive_retention_days=30,
    )
    yield db, store, index, consent_repo, consent, coordinator, lifecycle, scope
    db.close()


@pytest.fixture
def ledger_stack(tmp_path):
    db = SQLiteDatabase(tmp_path / "soft-delete-ledger.db")
    db.migrate()
    store = SQLiteMemoryStore(db)
    index = SQLiteFTSIndex(db)
    scope = _scope()
    store.create_scope(scope)
    ledger_store = SQLiteLedger(tmp_path / "memory_ledger.sqlite3")
    ledger = LedgerService(ledger_store, FixedClock(), UUID4IdGenerator())
    consent_repo = SQLiteConsentStore(db)
    coordinator = WriteCoordinator(store, index, ledger)
    consent = ConsentService(
        consent_repo, coordinator, FixedClock(), UUID4IdGenerator(),
    )
    lifecycle = LifecycleService(coordinator, store, consent, archive_retention_days=30)
    yield db, store, index, ledger_store, ledger, consent_repo, coordinator, lifecycle, scope
    ledger_store.close()
    db.close()


def _create(coordinator: WriteCoordinator, scope: Scope,
            memory: MemoryEntity) -> None:
    asyncio.run(coordinator.create(memory, _revision(memory), _source(memory)))


def test_archive_hides_from_active_and_restore_returns_it(stack):
    _, store, index, _, _, coordinator, lifecycle, scope = stack
    memory = _memory(scope)
    _create(coordinator, scope, memory)
    assert index.search("黑咖啡", scope_id=scope.id, subject_id="u1") == (memory.id,)

    receipt = asyncio.run(lifecycle.archive_memory(
        memory, revision_id="rev-archive", at=NOW + timedelta(seconds=1),
        reason="user archive", operator_type=OperatorType.USER,
    ))
    assert receipt.revision == 2
    archived = store.get(memory.id, scope_id=scope.id, subject_id="u1")
    assert archived.status is MemoryStatus.ARCHIVED
    assert archived.archive_reason is ArchiveReason.USER_ARCHIVE
    assert archived.archived_at == NOW + timedelta(seconds=1)
    assert archived.purge_after == NOW + timedelta(days=30, seconds=1)
    assert store.list_active(scope_id=scope.id, subject_id="u1") == ()
    assert index.search("黑咖啡", scope_id=scope.id, subject_id="u1") == ()

    asyncio.run(lifecycle.restore_memory(
        archived, revision_id="rev-restore", at=NOW + timedelta(seconds=2),
        reason="user restore", operator_type=OperatorType.USER,
    ))
    restored = store.get(memory.id, scope_id=scope.id, subject_id="u1")
    assert restored.status is MemoryStatus.ACTIVE
    assert restored.archived_at is None
    assert restored.purge_after is None
    assert restored.archive_reason is None
    assert index.search("黑咖啡", scope_id=scope.id, subject_id="u1") == (memory.id,)


def test_candidate_expiry_is_not_restorable(stack):
    _, store, _, _, _, coordinator, lifecycle, scope = stack
    candidate = _memory(
        scope, mid="c1", status=MemoryStatus.CANDIDATE,
        candidate_expires_at=NOW + timedelta(seconds=1),
    )
    _create(coordinator, scope, candidate)
    asyncio.run(lifecycle.archive_candidate(
        candidate, candidate_expires_at=candidate.candidate_expires_at,
        revision_id="rev-expire", at=NOW + timedelta(seconds=2),
    ))
    archived = store.get(candidate.id, scope_id=scope.id, subject_id="u1")
    assert archived.archive_reason is ArchiveReason.CANDIDATE_EXPIRED
    with pytest.raises(InvalidTransition):
        asyncio.run(lifecycle.restore_memory(
            archived, revision_id="rev-restore", at=NOW + timedelta(seconds=3),
            reason="should fail", operator_type=OperatorType.USER,
        ))


def test_purge_removes_body_and_derived_rows(stack):
    db, store, index, _, _, coordinator, lifecycle, scope = stack
    memory = _memory(scope)
    _create(coordinator, scope, memory)
    asyncio.run(lifecycle.archive_memory(
        memory, revision_id="rev-archive", at=NOW + timedelta(seconds=1),
        reason="archive", operator_type=OperatorType.USER,
    ))
    db.connection.execute(
        """INSERT INTO memory_embeddings
           (memory_id, db_lineage_id, creation_seq, revision, provider, model,
            dim, content_hash, vector, created_at)
           VALUES (?, 'lineage', 1, 1, 'p', 'm', 1, 'hash', x'00', ?)""",
        (memory.id, NOW.isoformat()),
    )
    db.connection.execute(
        """INSERT INTO index_jobs
           (id, memory_id, target, operation, content_hash, status,
            attempts, last_error, created_at, updated_at)
           VALUES ('job-1', ?, 'VECTOR', 'DELETE', 'hash', 'PENDING',
                   0, NULL, ?, ?)""",
        (memory.id, NOW.isoformat(), NOW.isoformat()),
    )
    receipt = asyncio.run(lifecycle.purge_memory(
        memory.id, scope_id=scope.id, subject_id="u1",
        operation_id="purge-1", operator_type=OperatorType.USER,
        operator_id="u1", at=NOW + timedelta(seconds=2),
    ))
    assert receipt.affected_count == 1
    with pytest.raises(NotFound):
        store.get(memory.id, scope_id=scope.id, subject_id="u1")
    assert db.connection.execute(
        "SELECT count(*) FROM memory_embeddings WHERE memory_id=?", (memory.id,),
    ).fetchone()[0] == 0
    assert db.connection.execute(
        "SELECT count(*) FROM index_jobs WHERE memory_id=?", (memory.id,),
    ).fetchone()[0] == 0
    assert index.search("黑咖啡", scope_id=scope.id, subject_id="u1") == ()


def test_purge_active_memory_archives_then_purges(stack):
    db, store, index, consent_repo, consent, coordinator, lifecycle, scope = stack
    consent.grant(scope, PersonalConsentType.PERSONAL_MEMORY, operator_id="u1")
    service = MemoryApplicationService(
        store, coordinator, consent, lifecycle, FixedClock(), UUID4IdGenerator(),
    )
    memory = _memory(scope)
    _create(coordinator, scope, memory)
    receipt = asyncio.run(service.purge_memory(scope, memory.id, operator_id="u1"))
    assert receipt.affected_count == 1
    with pytest.raises(NotFound):
        store.get(memory.id, scope_id=scope.id, subject_id="u1")
    operation_types = {
        row["operation_type"]
        for row in db.connection.execute(
            "SELECT operation_type FROM memory_operations",
        ).fetchall()
    }
    assert {"ARCHIVE_MEMORY", "PURGE_MEMORY"} <= operation_types


def test_retention_purges_only_expired_archives(stack):
    _, store, _, _, consent, coordinator, _, scope = stack
    memory = _memory(scope)
    _create(coordinator, scope, memory)
    lifecycle = LifecycleService(
        coordinator, store, consent, archive_retention_days=1,
    )
    asyncio.run(lifecycle.archive_memory(
        memory, revision_id="rev-archive", at=NOW, reason="archive",
        operator_type=OperatorType.USER,
    ))
    retention = RetentionService(
        store, lifecycle, FixedClock(), UUID4IdGenerator(), batch_size=10,
    )
    assert asyncio.run(retention.purge_expired(now=NOW + timedelta(hours=12))) == 0
    assert asyncio.run(retention.purge_expired(now=NOW + timedelta(days=2))) == 1
    with pytest.raises(NotFound):
        store.get(memory.id, scope_id=scope.id, subject_id="u1")


def test_ledger_records_archive_restore_purge(ledger_stack):
    _, store, _, ledger_store, ledger, _, coordinator, lifecycle, scope = ledger_stack
    memory = _memory(scope)
    _create(coordinator, scope, memory)
    asyncio.run(lifecycle.archive_memory(
        memory, revision_id="rev-archive", at=NOW + timedelta(seconds=1),
        reason="archive", operator_type=OperatorType.USER,
    ))
    archived = store.get(memory.id, scope_id=scope.id, subject_id="u1")
    asyncio.run(lifecycle.restore_memory(
        archived, revision_id="rev-restore", at=NOW + timedelta(seconds=2),
        reason="restore", operator_type=OperatorType.USER,
    ))
    active = store.get(memory.id, scope_id=scope.id, subject_id="u1")
    asyncio.run(lifecycle.archive_memory(
        active, revision_id="rev-archive-2", at=NOW + timedelta(seconds=3),
        reason="archive again", operator_type=OperatorType.USER,
    ))
    asyncio.run(lifecycle.purge_memory(
        memory.id, scope_id=scope.id, subject_id="u1",
        operation_id="purge-1", operator_type=OperatorType.USER,
        operator_id="u1", at=NOW + timedelta(seconds=4),
    ))
    events = ledger_store.all_events()
    types = {event.event_type for event in events}
    assert {
        LedgerEventType.ARCHIVE_MEMORY,
        LedgerEventType.RESTORE_MEMORY,
        LedgerEventType.PURGE_MEMORY,
    } <= types
    assert ledger_store.pending_intents() == ()
    for event in events:
        assert "content" not in event.payload


def test_pending_archive_intent_is_replayed(ledger_stack):
    _, store, _, ledger_store, ledger, consent_repo, coordinator, _, scope = ledger_stack
    memory = _memory(scope)
    _create(coordinator, scope, memory)
    lineage, creation_seq = store.get_lineage(
        memory.id, scope_id=scope.id, subject_id="u1",
    )
    ledger.record_intent(
        LedgerEventType.ARCHIVE_MEMORY,
        payload={
            "db_lineage_id": lineage,
            "creation_seq": creation_seq,
            "memory_id": memory.id,
            "archive_reason": ArchiveReason.USER_ARCHIVE.value,
            "archived_at": (NOW + timedelta(seconds=1)).isoformat(),
            "purge_after": (NOW + timedelta(days=30)).isoformat(),
            "occurred_at": (NOW + timedelta(seconds=1)).isoformat(),
        },
        source="user", occurred_at=NOW + timedelta(seconds=1),
        scope_id=scope.id, subject_id="u1", memory_id=memory.id,
    )
    applied, failed = ledger.drain_pending(store, consent_repo)
    assert applied == 1
    assert failed == ()
    archived = store.get(memory.id, scope_id=scope.id, subject_id="u1")
    assert archived.status is MemoryStatus.ARCHIVED
    assert archived.archive_reason is ArchiveReason.USER_ARCHIVE
    assert ledger_store.pending_intents() == ()


def test_unknown_ledger_event_fails_closed_for_maintenance(ledger_stack):
    import sqlite3

    _, store, _, ledger_store, ledger, consent_repo, _, _, _ = ledger_stack
    path = ledger_store.path
    connection = sqlite3.connect(str(path))
    try:
        seq = int(connection.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM ledger_events",
        ).fetchone()[0])
        connection.execute(
            """INSERT INTO ledger_events
               (event_id, state, seq, event_type, scope_id, subject_id, memory_id,
                source, occurred_at, payload_json)
               VALUES ('future-1', 'intent', ?, 'future_event', NULL, NULL, NULL,
                       'system', ?, '{}')""",
            (seq, NOW.isoformat()),
        )
        connection.commit()
    finally:
        connection.close()
    applied, failed = ledger.drain_pending(store, consent_repo)
    assert applied == 0
    assert "future-1" in failed
    with pytest.raises(StorageError):
        ledger.strict_drain(store, consent_repo)
