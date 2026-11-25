import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.database import (
    SQLiteDatabase,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.fts import SQLiteFTSIndex
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.store import (
    SQLiteMemoryStore,
)
from data.plugins.astrbot_plugin_weave_memory.app.startup_recovery import (
    StartupRecoveryService,
)
from data.plugins.astrbot_plugin_weave_memory.core.errors import StorageError
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


def add(store, scope, *, content="喜欢黑咖啡"):
    memory = MemoryEntity(
        id="m1", scope_id=scope.id, subject_id="u1", memory_type=MemoryType.FACT,
        content=content, normalized_content=normalize_content(content),
        status=MemoryStatus.ACTIVE, source_type=SourceType.EXPLICIT,
        current_revision=1, created_at=NOW, updated_at=NOW,
    )
    store.create(memory,
                 MemoryRevision("r1", "m1", 1, memory.content, memory.status, NOW, "created"),
                 MemorySource("s1", "m1", SourceType.EXPLICIT, OperatorType.USER, "u1", NOW))
    return memory


def test_secure_purge_if_pending_runs_and_clears_marker(tmp_path):
    db, _, _, _ = make_stack(tmp_path / "purge.db")
    db.connection.execute(
        "INSERT INTO memory_meta(key,value) VALUES('secure_purge_pending','1') "
        "ON CONFLICT(key) DO UPDATE SET value='1'"
    )
    assert db.secure_purge_if_pending() is True
    row = db.connection.execute(
        "SELECT value FROM memory_meta WHERE key='secure_purge_pending'"
    ).fetchone()
    assert row["value"] == "0"
    baseline = db.connection.execute(
        "SELECT value FROM memory_meta WHERE key='secure_purge_baseline_done'"
    ).fetchone()
    assert baseline["value"] == "1"
    assert db.secure_purge_if_pending() is False
    db.close()


def make_legacy_residue(path, *, journal_mode="WAL",
                        canary="SECRET-CANARY-LEGACY-7a8b"):
    db, store, index, scope = make_stack(path)
    db.connection.execute(f"PRAGMA journal_mode={journal_mode}")
    db.connection.execute("PRAGMA secure_delete=OFF")
    add(store, scope, content=canary)
    store.delete("m1", scope_id=scope.id, subject_id="u1",
                 operation_id="op-legacy", operator_type=OperatorType.USER,
                 operator_id="u1", occurred_at=NOW)
    index.sync_memory("m1")
    db.connection.execute(
        "DELETE FROM memory_meta WHERE key IN "
        "('secure_purge_pending','secure_purge_baseline_done')"
    )
    db.close()
    return canary


def test_secure_purge_baseline_cleans_pre_upgrade_residue(tmp_path):
    path = tmp_path / "legacy.db"
    canary = make_legacy_residue(path)

    raw = Path(path).read_bytes()
    wal = Path(str(path) + "-wal")
    assert canary.encode() in raw or (
        wal.exists() and canary.encode() in wal.read_bytes()
    )

    db2 = SQLiteDatabase(path)
    db2.migrate()
    assert db2.secure_purge_if_pending() is True
    assert db2.secure_purge_if_pending() is False
    db2.close()

    raw = Path(path).read_bytes()
    wal = Path(str(path) + "-wal")
    assert canary.encode() not in raw
    assert not wal.exists() or canary.encode() not in wal.read_bytes()


def test_secure_purge_baseline_retries_after_reader_busy(tmp_path):
    path = tmp_path / "baseline-reader.db"
    canary = make_legacy_residue(path)
    db = SQLiteDatabase(path)
    db.migrate()
    reader = sqlite3.connect(path, isolation_level=None)
    reader.execute("PRAGMA journal_mode=WAL")
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM memories").fetchone()

    with pytest.raises(StorageError):
        db.secure_purge_if_pending()
    baseline = db.connection.execute(
        "SELECT value FROM memory_meta WHERE key='secure_purge_baseline_done'"
    ).fetchone()
    assert baseline is None

    reader.execute("ROLLBACK")
    reader.close()
    assert db.secure_purge_if_pending() is True
    assert db.secure_purge_if_pending() is False
    raw = Path(path).read_bytes()
    wal = Path(str(path) + "-wal")
    assert canary.encode() not in raw
    assert not wal.exists() or canary.encode() not in wal.read_bytes()
    db.close()


def test_secure_purge_baseline_failure_does_not_mark_done(tmp_path, monkeypatch):
    path = tmp_path / "baseline-fail.db"
    make_legacy_residue(path)
    db = SQLiteDatabase(path)
    db.migrate()

    def fail():
        raise StorageError("simulated checkpoint failure")

    monkeypatch.setattr(db, "_checkpoint_truncate", fail)
    with pytest.raises(StorageError):
        db.secure_purge_if_pending()
    baseline = db.connection.execute(
        "SELECT value FROM memory_meta WHERE key='secure_purge_baseline_done'"
    ).fetchone()
    assert baseline is None

    monkeypatch.undo()
    assert db.secure_purge_if_pending() is True
    baseline = db.connection.execute(
        "SELECT value FROM memory_meta WHERE key='secure_purge_baseline_done'"
    ).fetchone()
    assert baseline["value"] == "1"
    db.close()


def test_secure_purge_second_checkpoint_busy_keeps_pending(tmp_path, monkeypatch):
    path = tmp_path / "second-checkpoint.db"
    db, _, _, _ = make_stack(path)
    db.connection.execute(
        "INSERT INTO memory_meta(key,value) VALUES('secure_purge_pending','1') "
        "ON CONFLICT(key) DO UPDATE SET value='1'"
    )
    original = db._checkpoint_truncate
    calls = {"count": 0}

    def second_busy():
        calls["count"] += 1
        if calls["count"] == 1:
            return original()
        raise StorageError("simulated second checkpoint busy")

    monkeypatch.setattr(db, "_checkpoint_truncate", second_busy)
    with pytest.raises(StorageError):
        db.secure_purge_if_pending()
    pending = db.connection.execute(
        "SELECT value FROM memory_meta WHERE key='secure_purge_pending'"
    ).fetchone()
    baseline = db.connection.execute(
        "SELECT value FROM memory_meta WHERE key='secure_purge_baseline_done'"
    ).fetchone()
    assert pending["value"] == "1"
    assert baseline is None

    monkeypatch.undo()
    assert db.secure_purge_if_pending() is True
    db.close()


def test_secure_purge_baseline_works_without_wal(tmp_path):
    path = tmp_path / "baseline-nowal.db"
    canary = make_legacy_residue(path, journal_mode="DELETE")
    db = SQLiteDatabase(path)
    db.migrate()
    assert db.connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"

    assert db.secure_purge_if_pending() is True
    raw = Path(path).read_bytes()
    assert canary.encode() not in raw
    db.close()


def test_startup_recovery_rebuilds_dirty_index(tmp_path):
    db, store, index, scope = make_stack(tmp_path / "recovery.db")
    add(store, scope)
    report = StartupRecoveryService(db, index).run()
    assert report.schema_version == 13
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
