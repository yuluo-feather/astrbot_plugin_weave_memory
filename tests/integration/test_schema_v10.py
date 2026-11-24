"""V2.0 schema v10 migration, rollback guards and archive backfill."""

from datetime import UTC, datetime

import pytest
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.database import (
    SQLiteDatabase,
)
from data.plugins.astrbot_plugin_weave_memory.core.errors import StorageError

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
_STAMP = "2026-11-24T12:00:00.000000Z"


def _columns(db: SQLiteDatabase, table: str) -> set[str]:
    return {
        row["name"]
        for row in db.connection.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _tables(db: SQLiteDatabase) -> set[str]:
    return {
        row["name"]
        for row in db.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'",
        ).fetchall()
    }


def _seed_scope(db: SQLiteDatabase, scope_id: str = "scope-v9") -> None:
    db.connection.execute(
        """INSERT INTO memory_scopes
           (id, platform, tenant_id, scope_type, scope_external_id,
            subject_type, subject_id, created_at, is_enabled, disabled_at,
            admin_ids_json, admin_source)
           VALUES (?, 'astrbot', 'default', 'PERSONAL', 'u9', 'USER', 'u9',
                   ?, 1, NULL, '[]', 'NONE')""",
        (scope_id, _STAMP),
    )


def _seed_v9_archived(db: SQLiteDatabase) -> None:
    db.connection.execute(
        """INSERT INTO memories
           (id, scope_id, subject_id, memory_type, content, normalized_content,
            normalization_version, status, source_type, current_revision,
            valid_from, valid_until, superseded_by, created_at, updated_at,
            last_accessed_at, metadata_json, candidate_expires_at,
            db_lineage_id, creation_seq)
           VALUES ('mem-v9', 'scope-v9', 'u9', 'FACT', '旧归档', '旧归档',
                   1, 'ARCHIVED', 'EXPLICIT', 1, NULL, NULL, NULL, ?, ?, NULL,
                   '{}', NULL,
                   (SELECT value FROM memory_meta WHERE key='db_lineage_id'), 1)""",
        (_STAMP, _STAMP),
    )


def test_fresh_database_is_v10_with_archive_and_index_tables(tmp_path):
    db = SQLiteDatabase(tmp_path / "fresh.db")
    db.migrate()
    assert db.schema_version() == 10
    assert {"archived_at", "purge_after", "archive_reason"} <= _columns(db, "memories")
    assert {
        "memory_embeddings", "index_jobs", "index_state", "provider_call_audit",
    } <= _tables(db)
    assert db.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    db.close()


def test_v9_archived_rows_are_backfilled_not_restorable(tmp_path):
    db = SQLiteDatabase(tmp_path / "backfill.db")
    db.migrate()
    db.rollback_to_v9()
    assert db.schema_version() == 9
    _seed_scope(db)
    _seed_v9_archived(db)
    db.migrate()
    assert db.schema_version() == 10
    row = db.connection.execute(
        "SELECT archived_at, purge_after, archive_reason FROM memories WHERE id='mem-v9'",
    ).fetchone()
    assert row["archived_at"] == _STAMP
    assert row["purge_after"] is not None
    assert row["archive_reason"] == "CANDIDATE_EXPIRED"
    db.close()


def test_rollback_to_v9_refuses_archived_rows_and_pending_jobs(tmp_path):
    db = SQLiteDatabase(tmp_path / "guard.db")
    db.migrate()
    _seed_scope(db)
    _seed_v9_archived(db)
    db.connection.execute(
        """UPDATE memories SET archived_at=?, purge_after=?, archive_reason='USER_ARCHIVE'
           WHERE id='mem-v9'""",
        (_STAMP, "2026-12-24T12:00:00.000000Z"),
    )
    with pytest.raises(StorageError):
        db.rollback_to_v9()
    assert db.schema_version() == 10

    db.connection.execute("DELETE FROM memories WHERE id='mem-v9'")
    db.connection.execute(
        """INSERT INTO index_jobs
           (id, memory_id, target, operation, content_hash, status,
            attempts, last_error, created_at, updated_at)
           VALUES ('job-1', 'mem-x', 'VECTOR', 'UPSERT', 'hash', 'PENDING',
                   0, NULL, ?, ?)""",
        (_STAMP, _STAMP),
    )
    with pytest.raises(StorageError):
        db.rollback_to_v9()
    assert db.schema_version() == 10
    db.close()


def test_rollback_to_v9_preserves_audit_and_can_migrate_back(tmp_path):
    db = SQLiteDatabase(tmp_path / "roundtrip.db")
    db.migrate()
    db.connection.execute(
        """INSERT INTO provider_call_audit
           (id, provider, model, scope_type, scope_id_hash, field_categories,
            result, latency_ms, created_at)
           VALUES ('audit-1', 'p', 'm', 'PERSONAL', 'hash', '[]', 'ok', 1, ?)""",
        (_STAMP,),
    )
    db.rollback_to_v9()
    assert db.schema_version() == 9
    assert "provider_call_audit" in _tables(db)
    assert {"memory_embeddings", "index_jobs", "index_state"}.isdisjoint(_tables(db))
    db.migrate()
    assert db.schema_version() == 10
    assert db.connection.execute(
        "SELECT count(*) FROM provider_call_audit",
    ).fetchone()[0] == 1
    db.close()


def test_rollback_to_v9_rebuilds_v9_indexes(tmp_path):
    db = SQLiteDatabase(tmp_path / "indexes.db")
    db.migrate()
    db.rollback_to_v9()
    indexes = {
        row["name"]
        for row in db.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='memories'",
        ).fetchall()
    }
    assert {
        "uq_memories_active_dedup",
        "ix_memories_scope_status",
        "ix_memories_candidate_expiry",
    } <= indexes
    db.close()
