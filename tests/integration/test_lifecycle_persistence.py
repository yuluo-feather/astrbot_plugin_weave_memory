from dataclasses import replace
from datetime import UTC, datetime

import pytest

from weavemem.adapters.sqlite.database import SQLiteDatabase
from weavemem.adapters.sqlite.fts import SQLiteFTSIndex
from weavemem.adapters.sqlite.store import SQLiteMemoryStore
from weavemem.core.errors import AuthorizationDenied, Conflict, NotFound
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


@pytest.fixture
def stack(tmp_path):
    db = SQLiteDatabase(tmp_path / "lifecycle.db")
    db.migrate()
    store = SQLiteMemoryStore(db)
    index = SQLiteFTSIndex(db)
    identity = Identity("astrbot", "default", ScopeType.PERSONAL, "platform-u1", SubjectType.USER, "u1")
    scope = Scope("scope-u1", identity, NOW)
    store.create_scope(scope)
    yield db, store, index, scope
    db.close()


def add(store, scope, *, mid="m1", content="喜欢黑咖啡", status=MemoryStatus.ACTIVE):
    memory = MemoryEntity(
        id=mid, scope_id=scope.id, subject_id=scope.identity.subject_id,
        memory_type=MemoryType.PREFERENCE, content=content,
        normalized_content=normalize_content(content), status=status,
        source_type=SourceType.EXPLICIT, current_revision=1,
        created_at=NOW, updated_at=NOW,
    )
    revision = MemoryRevision("r-" + mid, mid, 1, content, status, NOW, "created")
    source = MemorySource("s-" + mid, mid, SourceType.EXPLICIT,
                          OperatorType.USER, scope.identity.subject_id, NOW)
    store.create(memory, revision, source)
    return memory


def test_delete_physically_purges_content_and_keeps_minimal_tombstone(stack):
    db, store, index, scope = stack
    memory = add(store, scope)
    index.sync_memory(memory.id)
    store.delete(memory.id, scope_id=scope.id, subject_id="u1",
                 operation_id="op-delete-1", operator_type=OperatorType.USER,
                 operator_id="u1", occurred_at=NOW)
    with pytest.raises(NotFound):
        store.get(memory.id, scope_id=scope.id, subject_id="u1")
    assert db.connection.execute("SELECT count(*) FROM memory_revisions WHERE memory_id=?", (memory.id,)).fetchone()[0] == 0
    assert db.connection.execute("SELECT count(*) FROM memory_sources WHERE memory_id=?", (memory.id,)).fetchone()[0] == 0
    audit = db.connection.execute("SELECT * FROM memory_operations WHERE id='op-delete-1'").fetchone()
    assert audit["operation_type"] == "DELETE_MEMORY"
    assert "content" not in audit["metadata_json"]
    assert not index.is_healthy()
    index.sync_memory(memory.id)
    assert index.search("黑咖啡", scope_id=scope.id, subject_id="u1") == ()
    # Exact replay after commit is idempotent.
    store.delete(memory.id, scope_id=scope.id, subject_id="u1",
                 operation_id="op-delete-1", operator_type=OperatorType.USER,
                 operator_id="u1", occurred_at=NOW)
    assert db.connection.execute("SELECT count(*) FROM memory_operations WHERE id='op-delete-1'").fetchone()[0] == 1


def test_delete_audit_conflict_rolls_back_purge(stack):
    db, store, _, scope = stack
    memory = add(store, scope)
    db.connection.execute(
        "INSERT INTO memory_operations(id,scope_id,memory_id,operation_type,operator_type,operator_id,occurred_at,metadata_json) VALUES(?,?,?,?,?,?,?,?)",
        ("op-conflict", scope.id, None, "OTHER", "user", "u1", "2026-11-22T12:00:00.000000Z", "{}"),
    )
    with pytest.raises(Conflict):
        store.delete(memory.id, scope_id=scope.id, subject_id="u1",
                     operation_id="op-conflict", operator_type=OperatorType.USER,
                     operator_id="u1", occurred_at=NOW)
    assert store.get(memory.id, scope_id=scope.id, subject_id="u1") == memory


def test_clear_scope_only_removes_target_scope_and_audits_each_tombstone(stack):
    db, store, index, scope = stack
    first = add(store, scope, mid="m1", content="第一条记忆")
    second_identity = Identity("astrbot", "default", ScopeType.PERSONAL,
                               "platform-u2", SubjectType.USER, "u2")
    second_scope = Scope("scope-u2", second_identity, NOW)
    store.create_scope(second_scope)
    other = add(store, second_scope, mid="m2", content="第二条记忆")
    index.rebuild()
    removed = store.clear_scope(scope.id, operation_id="op-clear-1",
                                operator_type=OperatorType.USER, operator_id="u1",
                                occurred_at=NOW)
    assert removed == (first.id,)
    with pytest.raises(NotFound):
        store.get(first.id, scope_id=scope.id, subject_id="u1")
    assert store.get(other.id, scope_id=second_scope.id, subject_id="u2") == other
    assert db.connection.execute("SELECT count(*) FROM memory_operations WHERE operation_type='CLEAR_MEMORY'").fetchone()[0] == 1
    assert db.connection.execute("SELECT count(*) FROM memory_operations WHERE id='op-clear-1'").fetchone()[0] == 1
    assert not index.is_healthy()
    index.rebuild()
    assert index.search("第二条", scope_id=second_scope.id, subject_id="u2") == (other.id,)
    assert index.search("第一条", scope_id=scope.id, subject_id="u1") == ()
    assert store.clear_scope(scope.id, operation_id="op-clear-1",
                             operator_type=OperatorType.USER, operator_id="u1",
                             occurred_at=NOW) == ()


def test_disable_scope_stops_reads_and_writes_without_deleting_data(stack):
    db, store, index, scope = stack
    memory = add(store, scope)
    index.sync_memory(memory.id)
    assert store.disable_scope(scope.id, operation_id="op-disable-1",
                               operator_type=OperatorType.USER, operator_id="u1",
                               occurred_at=NOW)
    with pytest.raises(NotFound):
        store.get(memory.id, scope_id=scope.id, subject_id="u1")
    assert store.list_active(scope_id=scope.id, subject_id="u1") == ()
    assert index.search("黑咖啡", scope_id=scope.id, subject_id="u1") == ()
    assert db.connection.execute("SELECT count(*) FROM memories WHERE id=?", (memory.id,)).fetchone()[0] == 1
    # A direct create against a disabled scope is rejected.
    candidate = replace(memory, id="m2", content="另一条记忆",
                        normalized_content=normalize_content("另一条记忆"))
    with pytest.raises(AuthorizationDenied):
        store.create(candidate,
                     MemoryRevision("r-m2", "m2", 1, candidate.content, candidate.status, NOW, "created"),
                     MemorySource("s-m2", "m2", SourceType.EXPLICIT, OperatorType.USER, "u1", NOW))
    assert not store.disable_scope(scope.id, operation_id="op-disable-1",
                                   operator_type=OperatorType.USER, operator_id="u1",
                                   occurred_at=NOW)


def test_delete_purges_incoming_and_outgoing_reserved_relations(stack):
    db, store, _, scope = stack
    first = add(store, scope, mid="relation-a", content="第一条")
    second = add(store, scope, mid="relation-b", content="第二条")
    db.connection.execute(
        "INSERT INTO memory_relations(id,source_memory_id,target_memory_id,relation_type,created_at) VALUES(?,?,?,?,?)",
        ("rel-out", first.id, second.id, "RELATED_TO", "2026-11-22T12:00:00.000000Z"),
    )
    db.connection.execute(
        "INSERT INTO memory_relations(id,source_memory_id,target_memory_id,relation_type,created_at) VALUES(?,?,?,?,?)",
        ("rel-in", second.id, first.id, "RELATED_TO", "2026-11-22T12:00:00.000000Z"),
    )
    store.delete(first.id, scope_id=scope.id, subject_id="u1",
                 operation_id="op-delete-rel", operator_type=OperatorType.USER,
                 operator_id="u1", occurred_at=NOW)
    assert db.connection.execute(
        "SELECT count(*) FROM memory_relations WHERE source_memory_id=? OR target_memory_id=?",
        (first.id, first.id),
    ).fetchone()[0] == 0
    assert store.get(second.id, scope_id=scope.id, subject_id="u1") == second


def test_clear_scope_removes_relations_touching_its_memories(stack):
    db, store, _, scope = stack
    first = add(store, scope, mid="clear-rel-a", content="作用域内记忆")
    other_identity = Identity(
        "astrbot", "default", ScopeType.PERSONAL, "platform-u2",
        SubjectType.USER, "u2",
    )
    other_scope = Scope("scope-u2-rel", other_identity, NOW)
    store.create_scope(other_scope)
    other = add(store, other_scope, mid="clear-rel-b", content="其他用户记忆")
    db.connection.execute(
        "INSERT INTO memory_relations(id,source_memory_id,target_memory_id,relation_type,created_at) VALUES(?,?,?,?,?)",
        ("rel-cross-scope", first.id, other.id, "RELATED_TO", "2026-11-22T12:00:00.000000Z"),
    )
    store.clear_scope(scope.id, operation_id="op-clear-rel", operator_type=OperatorType.USER,
                      operator_id="u1", occurred_at=NOW)
    assert db.connection.execute(
        "SELECT count(*) FROM memory_relations WHERE id='rel-cross-scope'",
    ).fetchone()[0] == 0
    assert store.get(other.id, scope_id=other_scope.id, subject_id="u2") == other
