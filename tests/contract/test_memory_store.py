from datetime import timedelta

import pytest
from data.plugins.astrbot_plugin_weave_memory.adapters.memory.store import (
    InMemoryMemoryStore,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.database import (
    SQLiteDatabase,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.store import (
    SQLiteMemoryStore,
)
from data.plugins.astrbot_plugin_weave_memory.core.errors import Conflict, NotFound
from data.plugins.astrbot_plugin_weave_memory.core.model import (
    ArchiveReason,
    Identity,
    MemoryRevision,
    MemorySource,
    MemoryStatus,
    OperatorType,
    Scope,
    ScopeType,
    SourceType,
    SubjectType,
)
from data.plugins.astrbot_plugin_weave_memory.core.policy.dedup import DedupKey
from data.plugins.astrbot_plugin_weave_memory.core.policy.lifecycle import transition

from tests.conftest import NOW


def source(memory_id):
    return MemorySource("src-" + memory_id, memory_id, SourceType.EXPLICIT,
                        OperatorType.USER, "user-a", NOW)


def revision(memory, reason="created"):
    return MemoryRevision(f"rev-{memory.id}-{memory.current_revision}", memory.id,
                          memory.current_revision, memory.content, memory.status,
                          NOW, reason)


@pytest.fixture(params=["in_memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "in_memory":
        yield InMemoryMemoryStore()
        return
    db = SQLiteDatabase(tmp_path / "contract.db")
    db.migrate()
    sqlite_store = SQLiteMemoryStore(db)
    identity = Identity(
        "astrbot", "t1", ScopeType.PERSONAL, "user-a",
        SubjectType.USER, "user-a",
    )
    sqlite_store.create_scope(Scope("scope-a", identity, NOW))
    yield sqlite_store
    db.close()


def revision_count(store, memory_id):
    if isinstance(store, InMemoryMemoryStore):
        return len(store.revisions(memory_id))
    return store.db.connection.execute(
        "SELECT count(*) FROM memory_revisions WHERE memory_id=?", (memory_id,),
    ).fetchone()[0]


def test_create_and_scope_filtered_get(memory_factory, store):
    m = memory_factory()
    store.create(m, revision(m), source(m.id))
    assert store.get(m.id, scope_id=m.scope_id, subject_id=m.subject_id) == m
    with pytest.raises(NotFound):
        store.get(m.id, scope_id="other", subject_id=m.subject_id)


def test_duplicate_active_memory_conflicts(memory_factory, store):
    m1 = memory_factory()
    m2 = memory_factory(id="m-2", content=" 喜欢黑咖啡！ ")
    store.create(m1, revision(m1), source(m1.id))
    with pytest.raises(Conflict):
        store.create(m2, revision(m2), source(m2.id))


def test_optimistic_update_conflict(memory_factory, store):
    m = memory_factory()
    store.create(m, revision(m), source(m.id))
    updated = transition(
        m, MemoryStatus.ARCHIVED, at=NOW + timedelta(seconds=1),
        archive_reason=ArchiveReason.USER_ARCHIVE,
        purge_after=NOW + timedelta(days=30),
    )
    store.update(updated, expected_revision=1, revision=revision(updated, "archived"))
    with pytest.raises(Conflict):
        store.update(updated, expected_revision=1, revision=revision(updated, "stale"))
    assert revision_count(store, m.id) == 2


def test_only_active_memories_are_listed(memory_factory, store):
    m = memory_factory()
    store.create(m, revision(m), source(m.id))
    assert len(store.list_active(scope_id=m.scope_id, subject_id=m.subject_id)) == 1
    archived = transition(
        m, MemoryStatus.ARCHIVED, at=NOW + timedelta(seconds=1),
        archive_reason=ArchiveReason.USER_ARCHIVE,
        purge_after=NOW + timedelta(days=30),
    )
    store.update(archived, expected_revision=1, revision=revision(archived, "archived"))
    assert store.list_active(scope_id=m.scope_id, subject_id=m.subject_id) == ()


def test_exact_create_replay_is_idempotent(memory_factory, store):
    m = memory_factory()
    rev = revision(m)
    src = source(m.id)
    store.create(m, rev, src)
    store.create(m, rev, src)
    assert store.get(m.id, scope_id=m.scope_id, subject_id=m.subject_id) == m
    assert revision_count(store, m.id) == 1


def test_find_active_duplicate_is_scope_subject_and_type_scoped(memory_factory, store):
    memory = memory_factory()
    store.create(memory, revision(memory), source(memory.id))
    key = DedupKey.from_memory(memory)
    assert store.find_active_duplicate(key) == memory
    assert store.find_active_duplicate(
        DedupKey("other-scope", key.subject_id, key.memory_type, key.normalized_content),
    ) is None
    assert store.find_active_duplicate(
        DedupKey(key.scope_id, "other-subject", key.memory_type, key.normalized_content),
    ) is None
    assert store.find_active_duplicate(
        DedupKey(key.scope_id, key.subject_id, "EVENT", key.normalized_content),
    ) is None


def test_find_active_duplicate_is_status_based_not_validity_based(memory_factory, store):
    """Expiry is handled by the save-path sweep, not by the dedup lookup."""
    expired = memory_factory(
        id="m-expired", valid_until=NOW - timedelta(seconds=1),
    )
    future = memory_factory(
        id="m-future", content="明天去爬山", valid_from=NOW + timedelta(seconds=1),
    )
    for memory in (expired, future):
        store.create(memory, revision(memory), source(memory.id))
        assert store.find_active_duplicate(DedupKey.from_memory(memory)) == memory
