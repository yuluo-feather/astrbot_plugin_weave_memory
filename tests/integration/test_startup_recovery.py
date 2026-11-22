from datetime import UTC, datetime

from weavemem.adapters.sqlite.database import SQLiteDatabase
from weavemem.adapters.sqlite.fts import SQLiteFTSIndex
from weavemem.adapters.sqlite.store import SQLiteMemoryStore
from weavemem.app.startup_recovery import StartupRecoveryService
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


def make_stack(path):
    db = SQLiteDatabase(path)
    db.migrate()
    store = SQLiteMemoryStore(db)
    index = SQLiteFTSIndex(db)
    scope = Scope("scope-1", Identity("astrbot", "default", ScopeType.PERSONAL,
                                      "platform-u1", SubjectType.USER, "u1"), NOW)
    store.create_scope(scope)
    return db, store, index, scope


def add(store, scope):
    memory = MemoryEntity(
        id="m1", scope_id=scope.id, subject_id="u1", memory_type=MemoryType.FACT,
        content="喜欢黑咖啡", normalized_content=normalize_content("喜欢黑咖啡"),
        status=MemoryStatus.ACTIVE, source_type=SourceType.EXPLICIT,
        current_revision=1, created_at=NOW, updated_at=NOW,
    )
    store.create(memory,
                 MemoryRevision("r1", "m1", 1, memory.content, memory.status, NOW, "created"),
                 MemorySource("s1", "m1", SourceType.EXPLICIT, OperatorType.USER, "u1", NOW))
    return memory


def test_startup_recovery_rebuilds_dirty_index(tmp_path):
    db, store, index, scope = make_stack(tmp_path / "recovery.db")
    add(store, scope)
    report = StartupRecoveryService(db, index).run()
    assert report.schema_version == 4
    assert report.fts_rebuilt is True
    assert report.indexed_active_count == 1
    assert index.is_healthy()
    db.close()


def test_startup_recovery_recreates_missing_fts_table(tmp_path):
    db, store, index, scope = make_stack(tmp_path / "missing-fts.db")
    add(store, scope)
    db.connection.execute("DROP TABLE memory_fts")
    report = StartupRecoveryService(db, index).run()
    assert report.fts_rebuilt is True
    assert report.indexed_active_count == 1
    assert index.search("黑咖啡", scope_id=scope.id, subject_id="u1") == ("m1",)
    db.close()


def test_startup_recovery_leaves_clean_index_untouched(tmp_path):
    db, store, index, scope = make_stack(tmp_path / "clean.db")
    memory = add(store, scope)
    index.sync_memory(memory.id)
    report = StartupRecoveryService(db, index).run()
    assert report.fts_rebuilt is False
    assert report.indexed_active_count == 1
    db.close()
