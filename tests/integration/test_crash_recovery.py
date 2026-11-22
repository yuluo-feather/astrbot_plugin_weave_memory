"""Process-termination recovery checks for SQLite WAL transactions.

These tests exercise abrupt process death, not power-loss or storage-device failure.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tests.integration.test_startup_recovery import add, make_stack
from weavemem.adapters.sqlite.database import SQLiteDatabase
from weavemem.adapters.sqlite.fts import SQLiteFTSIndex
from weavemem.adapters.sqlite.store import SQLiteMemoryStore
from weavemem.app.startup_recovery import StartupRecoveryService


def _kill_child_after_ready(script: str, database_path: Path) -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(database_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    try:
        ready = child.stdout.readline().strip()
        assert ready == "READY", f"child did not reach crash point: {ready!r}"
        child.kill()
        child.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        if child.stdout:
            child.stdout.close()
        if child.stderr:
            child.stderr.close()


def test_process_kill_during_uncommitted_wal_transaction_rolls_back(tmp_path):
    db_path = tmp_path / "killed-in-transaction.db"
    db = SQLiteDatabase(db_path)
    db.migrate()
    db.connection.execute("INSERT INTO memory_meta(key, value) VALUES ('before_crash', 'kept')")
    db.close()

    script = r'''
import sqlite3, sys, time
conn = sqlite3.connect(sys.argv[1], isolation_level=None, timeout=5)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA synchronous=FULL")
conn.execute("BEGIN IMMEDIATE")
conn.execute("INSERT INTO memory_meta(key, value) VALUES ('uncommitted_crash_write', 'must_rollback')")
print("READY", flush=True)
time.sleep(60)
'''
    _kill_child_after_ready(script, db_path)

    reopened = SQLiteDatabase(db_path)
    assert reopened.schema_version() == 4
    assert reopened.connection.execute(
        "SELECT value FROM memory_meta WHERE key='before_crash'",
    ).fetchone()[0] == "kept"
    assert reopened.connection.execute(
        "SELECT 1 FROM memory_meta WHERE key='uncommitted_crash_write'",
    ).fetchone() is None
    reopened.close()


def test_process_exit_after_commit_preserves_committed_wal_write(tmp_path):
    db_path = tmp_path / "committed-before-exit.db"
    db = SQLiteDatabase(db_path)
    db.migrate()
    db.close()

    script = r'''
import os, sqlite3, sys
conn = sqlite3.connect(sys.argv[1], isolation_level=None, timeout=5)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA synchronous=FULL")
conn.execute("BEGIN IMMEDIATE")
conn.execute("INSERT INTO memory_meta(key, value) VALUES ('committed_before_exit', 'durable')")
conn.execute("COMMIT")
print("READY", flush=True)
os._exit(0)
'''
    child = subprocess.run(
        [sys.executable, "-c", script, str(db_path)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert child.returncode == 0
    assert child.stdout.strip() == "READY"

    reopened = SQLiteDatabase(db_path)
    assert reopened.connection.execute(
        "SELECT value FROM memory_meta WHERE key='committed_before_exit'",
    ).fetchone()[0] == "durable"
    reopened.close()


def test_startup_recovery_repairs_committed_dirty_fts_state_after_process_exit(tmp_path):
    db_path = tmp_path / "dirty-index-after-exit.db"
    db, store, index, scope = make_stack(db_path)
    memory = add(store, scope)
    index.sync_memory(memory.id)
    db.close()

    script = r'''
import os, sqlite3, sys
conn = sqlite3.connect(sys.argv[1], isolation_level=None, timeout=5)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA synchronous=FULL")
conn.execute("BEGIN IMMEDIATE")
conn.execute("UPDATE memory_meta SET value='[\"m1\"]' WHERE key='fts_dirty_ids'")
conn.execute("DELETE FROM memory_fts WHERE memory_id='m1'")
conn.execute("COMMIT")
print("READY", flush=True)
os._exit(0)
'''
    child = subprocess.run(
        [sys.executable, "-c", script, str(db_path)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert child.returncode == 0
    assert child.stdout.strip() == "READY"

    recovered = SQLiteDatabase(db_path)
    recovered_store = SQLiteMemoryStore(recovered)
    recovered_index = SQLiteFTSIndex(recovered)
    report = StartupRecoveryService(recovered, recovered_index).run()
    assert report.fts_rebuilt is True
    assert report.indexed_active_count == 1
    assert recovered_index.search("黑咖啡", scope_id=scope.id, subject_id="u1") == ("m1",)
    assert recovered_store.get("m1", scope_id=scope.id, subject_id="u1").content == "喜欢黑咖啡"
    assert recovered_index.is_healthy()
    recovered.close()


def test_process_kill_during_v4_migration_rolls_back_ddl_and_allows_retry(tmp_path):
    db_path = tmp_path / "killed-v4-migration.db"
    db = SQLiteDatabase(db_path)
    db.migrate()
    db.connection.execute("DROP TABLE memory_relations")
    db.connection.execute("DELETE FROM schema_migrations WHERE version=4")
    assert db.schema_version() == 3
    db.close()

    script = r'''
import sqlite3, sys, time
conn = sqlite3.connect(sys.argv[1], isolation_level=None, timeout=5)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA synchronous=FULL")
conn.execute("BEGIN IMMEDIATE")
conn.execute("CREATE TABLE memory_relations (id TEXT PRIMARY KEY)")
conn.execute("CREATE INDEX ix_memory_relations_source ON memory_relations(id)")
conn.execute("INSERT INTO schema_migrations(version, name, applied_at) VALUES (4, 'interrupted', 'now')")
print("READY", flush=True)
time.sleep(60)
'''
    _kill_child_after_ready(script, db_path)

    recovered = SQLiteDatabase(db_path)
    assert recovered.schema_version() == 3
    assert recovered.connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_relations'",
    ).fetchone() is None
    recovered.migrate()
    assert recovered.schema_version() == 4
    assert recovered.connection.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='memory_relations'",
    ).fetchone()[0] == 1
    recovered.close()
