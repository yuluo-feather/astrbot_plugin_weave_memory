"""SQLite connection, schema migrations, and transaction boundary (V0.3)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Self

from ...core.errors import ServiceClosed, StorageError

_SCHEMA_V1 = (Path(__file__).parent / "migrations" / "001_initial.sql").read_text(encoding="utf-8")
SECURE_PURGE_KEY = "secure_purge_pending"
SECURE_PURGE_BASELINE_KEY = "secure_purge_baseline_done"
MIGRATION_BACKUP_DIR = "migration_backups"


def _split_sql_statements(script: str) -> list[str]:
    """Split a migration script on top-level semicolons.

    Semicolons inside quoted strings, ``--`` line comments and ``/* */`` block
    comments are preserved. Trigger bodies (``BEGIN ... END``) are not
    special-cased; the migration files do not use them.
    """
    statements: list[str] = []
    buffer: list[str] = []
    index = 0
    length = len(script)
    while index < length:
        char = script[index]
        if char in ("'", '"'):
            quote = char
            buffer.append(char)
            index += 1
            while index < length:
                current = script[index]
                buffer.append(current)
                if current == quote:
                    if index + 1 < length and script[index + 1] == quote:
                        buffer.append(script[index + 1])
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            continue
        if char == "-" and index + 1 < length and script[index + 1] == "-":
            end = script.find("\n", index)
            if end == -1:
                buffer.append(script[index:])
                index = length
            else:
                buffer.append(script[index:end + 1])
                index = end + 1
            continue
        if char == "/" and index + 1 < length and script[index + 1] == "*":
            end = script.find("*/", index + 2)
            if end == -1:
                buffer.append(script[index:])
                index = length
            else:
                buffer.append(script[index:end + 2])
                index = end + 2
            continue
        if char == ";":
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer.clear()
            index += 1
            continue
        buffer.append(char)
        index += 1
    tail = "".join(buffer).strip()
    if tail:
        statements.append(tail)
    return statements


def _apply_script(connection: sqlite3.Connection, script: str) -> None:
    for statement in _split_sql_statements(script):
        connection.execute(statement)


class SQLiteDatabase:
    """Single-owner, single-process SQLite connection.

    The connection is intentionally thread-affine. V0.4's coordinator will be
    the single write entry point; this adapter does not promise multi-thread or
    multi-process write coordination.
    """

    CURRENT_SCHEMA_VERSION = 10

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = RLock()
        self._closed = False
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self.path, isolation_level=None, timeout=5.0,
                detect_types=sqlite3.PARSE_DECLTYPES,
            )
            self.connection = connection
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("PRAGMA busy_timeout = 5000")
            self.connection.execute("PRAGMA secure_delete = ON")
            if self.path != ":memory:":
                self.connection.execute("PRAGMA journal_mode = WAL")
                self.connection.execute("PRAGMA synchronous = FULL")
        except sqlite3.Error as exc:
            if connection is not None:
                connection.close()
            raise StorageError("could not open SQLite database") from exc

    def _ensure_open(self) -> None:
        if self._closed:
            raise ServiceClosed("SQLite database is closed")

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._ensure_open()
            try:
                self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                yield self.connection
                self.connection.execute("COMMIT")
            except BaseException:
                if self.connection.in_transaction:
                    self.connection.execute("ROLLBACK")
                raise

    def _read_schema_version(self) -> int:
        self._ensure_open()
        try:
            exists = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'",
            ).fetchone()
            if exists is None:
                return 0
            row = self.connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations",
            ).fetchone()
            return int(row["version"])
        except sqlite3.Error as exc:
            raise StorageError("could not read schema version") from exc

    def _migration_backup_path(self, version: int) -> Path:
        database = Path(self.path)
        backup_dir = database.parent / MIGRATION_BACKUP_DIR
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError("could not create migration backup directory") from exc
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        base = f"{database.stem}_v{version}_to_v{self.CURRENT_SCHEMA_VERSION}_{stamp}"
        candidate = backup_dir / f"{base}.sqlite3"
        counter = 1
        while candidate.exists():
            candidate = backup_dir / f"{base}_{counter}.sqlite3"
            counter += 1
        return candidate

    @staticmethod
    def _discard_backup(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def _backup_before_migration(self, version: int) -> Path | None:
        if self.path == ":memory:" or version < 1 or version >= self.CURRENT_SCHEMA_VERSION:
            return None
        destination = self._migration_backup_path(version)
        try:
            self.backup_to(destination)
        except StorageError:
            self._discard_backup(destination)
            raise
        try:
            backup = sqlite3.connect(str(destination))
            try:
                integrity = backup.execute("PRAGMA integrity_check").fetchone()
                if integrity is None or integrity[0] != "ok":
                    raise StorageError("migration backup integrity check failed")
                row = backup.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_migrations",
                ).fetchone()
                if row is None or int(row[0]) != version:
                    raise StorageError("migration backup schema version mismatch")
            finally:
                backup.close()
        except StorageError:
            self._discard_backup(destination)
            raise
        except sqlite3.Error as exc:
            self._discard_backup(destination)
            raise StorageError("could not verify migration backup") from exc
        return destination

    def migrate(self) -> Path | None:
        self._ensure_open()
        version = self._read_schema_version()
        if version > self.CURRENT_SCHEMA_VERSION:
            raise StorageError("database schema is newer than this application")
        backup_path = self._backup_before_migration(version)
        self.connection.execute("PRAGMA foreign_keys = OFF")
        try:
            with self.transaction() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        applied_at TEXT NOT NULL
                    )
                """)
                row = conn.execute("SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations").fetchone()
                version = int(row["version"])
                if version > self.CURRENT_SCHEMA_VERSION:
                    raise StorageError("database schema is newer than this application")
                if version < 1:
                    # Avoid executescript(): sqlite3 executescript implicitly commits.
                    # Execute each statement to keep DDL and migration marker atomic.
                    _apply_script(conn, _SCHEMA_V1)
                    conn.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (1, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                        ("initial_v0_schema",),
                    )
                    version = 1
                if version < 2:
                    migration_v2 = (Path(__file__).parent / "migrations" / "002_scope_disable.sql").read_text(encoding="utf-8")
                    _apply_script(conn, migration_v2)
                    conn.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (2, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                        ("scope_disable_and_close",),
                    )
                    version = 2
                if version < 3:
                    migration_v3 = (Path(__file__).parent / "migrations" / "003_memory_consents.sql").read_text(encoding="utf-8")
                    _apply_script(conn, migration_v3)
                    conn.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (3, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                        ("personal_memory_consents",),
                    )
                    version = 3
                if version < 4:
                    migration_v4 = (Path(__file__).parent / "migrations" / "004_memory_relations.sql").read_text(encoding="utf-8")
                    _apply_script(conn, migration_v4)
                    conn.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (4, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                        ("reserved_memory_relations",),
                    )
                    version = 4
                if version < 5:
                    migration_v5 = (Path(__file__).parent / "migrations" / "005_group_scopes.sql").read_text(encoding="utf-8")
                    _apply_script(conn, migration_v5)
                    conn.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (5, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                        ("group_scopes_admin",),
                    )
                if version < 6:
                    migration_v6 = (Path(__file__).parent / "migrations" / "006_extraction_jobs.sql").read_text(encoding="utf-8")
                    _apply_script(conn, migration_v6)
                    conn.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (6, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                        ("extraction_jobs",),
                    )
                    version = 6
                if version < 7:
                    migration_v7 = (Path(__file__).parent / "migrations" / "007_candidate_expiry.sql").read_text(encoding="utf-8")
                    _apply_script(conn, migration_v7)
                    conn.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (7, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                        ("candidate_expiry",),
                    )
                    version = 7
                if version < 8:
                    migration_v8 = (Path(__file__).parent / "migrations" / "008_import_jobs.sql").read_text(encoding="utf-8")
                    _apply_script(conn, migration_v8)
                    conn.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (8, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                        ("import_jobs",),
                    )
                    version = 8
                if version < 9:
                    migration_v9 = (Path(__file__).parent / "migrations" / "009_lineage_ledger.sql").read_text(encoding="utf-8")
                    _apply_script(conn, migration_v9)
                    conn.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (9, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                        ("lineage_ledger",),
                    )
                    version = 9
                if version < 10:
                    migration_v10 = (Path(__file__).parent / "migrations" / "010_hybrid_retrieval.sql").read_text(encoding="utf-8")
                    _apply_script(conn, migration_v10)
                    conn.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (10, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                        ("hybrid_retrieval",),
                    )
        except sqlite3.Error as exc:
            raise StorageError(f"SQLite schema migration failed: {exc}") from exc
        finally:
            self.connection.execute("PRAGMA foreign_keys = ON")
        return backup_path

    def _ensure_rollback_guards(self, *, no_import_jobs: bool = False,
                                no_candidates: bool = False,
                                no_extraction_jobs: bool = False) -> None:
        """Preflight the whole downgrade chain so a refused rollback changes nothing."""
        version = self.schema_version()
        # V1.8 keeps its lineage columns and allocator; rolling back only removes
        # the v9 marker and relies on ledger_first_enabled=false at runtime.
        effective_version = 9 if version >= 10 else (8 if version >= 9 else version)
        if version >= 10:
            archived = int(self.connection.execute(
                "SELECT count(*) FROM memories WHERE status='ARCHIVED'",
            ).fetchone()[0])
            if archived:
                raise StorageError("cannot roll back while ARCHIVED memories exist")
            pending_jobs = int(self.connection.execute(
                "SELECT count(*) FROM index_jobs WHERE status IN ('PENDING','RUNNING')",
            ).fetchone()[0])
            if pending_jobs:
                raise StorageError("cannot roll back while pending index jobs exist")
        checks: list[tuple[bool, int, str, str]] = [
            (no_import_jobs, 8, "import_jobs",
             "cannot roll back while import_jobs contains data"),
            (no_candidates, 7, "memories WHERE status='CANDIDATE'",
             "cannot roll back while CANDIDATE memories exist"),
            (no_extraction_jobs, 6, "extraction_jobs",
             "cannot roll back while extraction_jobs contains data"),
        ]
        for enabled, minimum, target, message in checks:
            if not enabled or effective_version < minimum:
                continue
            if int(self.connection.execute(f"SELECT count(*) FROM {target}").fetchone()[0]):
                raise StorageError(message)

    def rollback_to_v9(self) -> None:
        """Roll v10 back to v9 only when no archive or pending vector job remains."""
        self._ensure_open()
        if self.schema_version() != 10:
            raise StorageError("v10 rollback requires schema version 10")
        self._ensure_rollback_guards()
        self.connection.execute("PRAGMA foreign_keys = OFF")
        try:
            with self.transaction() as conn:
                conn.execute("""
                    CREATE TABLE memories_v9 (
                        id TEXT PRIMARY KEY,
                        scope_id TEXT NOT NULL REFERENCES memory_scopes(id) ON DELETE RESTRICT,
                        subject_id TEXT NOT NULL,
                        memory_type TEXT NOT NULL CHECK (memory_type IN ('PREFERENCE','FACT','EVENT')),
                        content TEXT NOT NULL CHECK (length(trim(content)) > 0),
                        normalized_content TEXT NOT NULL CHECK (length(trim(normalized_content)) > 0),
                        normalization_version INTEGER NOT NULL DEFAULT 1,
                        status TEXT NOT NULL CHECK (status IN ('CANDIDATE','ACTIVE','SUPERSEDED','ARCHIVED','EXPIRED','DELETED','REJECTED')),
                        source_type TEXT NOT NULL CHECK (source_type IN ('EXPLICIT','INFERRED','OBSERVED','IMPORTED','MANUAL','AGENT')),
                        current_revision INTEGER NOT NULL CHECK (current_revision >= 1),
                        valid_from TEXT,
                        valid_until TEXT,
                        superseded_by TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        last_accessed_at TEXT,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        candidate_expires_at TEXT,
                        db_lineage_id TEXT NOT NULL,
                        creation_seq INTEGER NOT NULL CHECK (creation_seq >= 1),
                        CHECK ((status = 'SUPERSEDED' AND superseded_by IS NOT NULL) OR
                               (status <> 'SUPERSEDED' AND superseded_by IS NULL)),
                        UNIQUE (db_lineage_id, creation_seq)
                    )
                """)
                conn.execute("""
                    INSERT INTO memories_v9
                        (id, scope_id, subject_id, memory_type, content, normalized_content,
                         normalization_version, status, source_type, current_revision,
                         valid_from, valid_until, superseded_by, created_at, updated_at,
                         last_accessed_at, metadata_json, candidate_expires_at,
                         db_lineage_id, creation_seq)
                    SELECT id, scope_id, subject_id, memory_type, content, normalized_content,
                           normalization_version, status, source_type, current_revision,
                           valid_from, valid_until, superseded_by, created_at, updated_at,
                           last_accessed_at, metadata_json, candidate_expires_at,
                           db_lineage_id, creation_seq
                    FROM memories
                """)
                conn.execute("DROP TABLE memories")
                conn.execute("ALTER TABLE memories_v9 RENAME TO memories")
                conn.execute("""
                    CREATE UNIQUE INDEX uq_memories_active_dedup
                    ON memories(scope_id, subject_id, memory_type, normalized_content)
                    WHERE status IN ('ACTIVE','CANDIDATE')
                """)
                conn.execute(
                    "CREATE INDEX ix_memories_scope_status ON memories(scope_id, subject_id, status)",
                )
                conn.execute(
                    "CREATE INDEX ix_memories_candidate_expiry ON memories(status, candidate_expires_at)",
                )
                conn.execute("DROP TABLE IF EXISTS memory_embeddings")
                conn.execute("DROP TABLE IF EXISTS index_jobs")
                conn.execute("DROP TABLE IF EXISTS index_state")
                conn.execute("DELETE FROM schema_migrations WHERE version=10")
        except StorageError:
            raise
        except sqlite3.Error as exc:
            raise StorageError("could not roll back V2.0 schema to V1.8") from exc
        finally:
            self.connection.execute("PRAGMA foreign_keys = ON")

    def rollback_to_v8(self) -> None:
        """Remove the v9 schema marker while keeping lineage columns.

        SQLite cannot drop NOT NULL columns without rebuilding memories, and
        V1.8 explicitly keeps the columns and allocator on rollback. The
        application must also set ``ledger_first_enabled=false``.
        """
        self._ensure_open()
        if self.schema_version() >= 10:
            self.rollback_to_v9()
        try:
            with self.transaction() as conn:
                conn.execute("DELETE FROM schema_migrations WHERE version=9")
        except sqlite3.Error as exc:
            raise StorageError("could not roll back V1.8 schema marker") from exc

    def rollback_to_v05(self) -> None:
        """Roll back the V1.0-only reserved relation table to the V0.5 schema.

        Historical name: ``rollback_v1_to_v05`` reflects "V1.0 -> V0.5"; it must
        not be confused with schema v10 rollback (``rollback_to_v9``).

        Refuse rollback if relations exist; V1.0 deliberately exposes no relation
        write API, so an empty table is the expected safe downgrade condition.
        Later schema versions are rolled back first, each with its own guard.
        """
        self._ensure_open()
        self._ensure_rollback_guards(
            no_import_jobs=True, no_candidates=True, no_extraction_jobs=True,
        )
        if self.schema_version() >= 9:
            self.rollback_to_v8()
        if self.schema_version() == 8:
            self.rollback_to_v7()
        if self.schema_version() == 7:
            self.rollback_to_v6()
        if self.schema_version() == 6:
            self.rollback_to_v5()
        if self.schema_version() == 5:
            self.rollback_v1_to_v05()
        try:
            with self.transaction() as conn:
                version = int(conn.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_migrations",
                ).fetchone()[0])
                if version != 4:
                    raise StorageError("V0.5 rollback requires schema version 4")
                relation_count = int(conn.execute(
                    "SELECT count(*) FROM memory_relations",
                ).fetchone()[0])
                if relation_count:
                    raise StorageError("cannot roll back while memory_relations contains data")
                conn.execute("DROP TABLE memory_relations")
                conn.execute("DELETE FROM schema_migrations WHERE version=4")
        except sqlite3.Error as exc:
            raise StorageError("could not roll back V1.0 schema to V0.5") from exc

    def rollback_to_v5(self) -> None:
        """Roll v6 back to v5 only when no extraction job remains.

        A v7 database is first rolled back to v6 (requiring no candidate rows).
        """
        self._ensure_open()
        self._ensure_rollback_guards(
            no_import_jobs=True, no_candidates=True, no_extraction_jobs=True,
        )
        if self.schema_version() >= 9:
            self.rollback_to_v8()
        if self.schema_version() == 8:
            self.rollback_to_v7()
        if self.schema_version() == 7:
            self.rollback_to_v6()
        try:
            with self.transaction() as conn:
                version = int(conn.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_migrations",
                ).fetchone()[0])
                if version != 6:
                    raise StorageError("V1.5 rollback requires schema version 6")
                jobs = int(conn.execute(
                    "SELECT count(*) FROM extraction_jobs",
                ).fetchone()[0])
                if jobs:
                    raise StorageError("cannot roll back while extraction_jobs contains data")
                conn.execute("DROP TABLE extraction_jobs")
                conn.execute("DELETE FROM schema_migrations WHERE version=6")
        except sqlite3.Error as exc:
            raise StorageError("could not roll back V1.5 schema to V1.4") from exc

    def rollback_to_v6(self) -> None:
        """Roll v7 back to v6 only when no candidate row depends on the TTL column."""
        self._ensure_open()
        self._ensure_rollback_guards(no_import_jobs=True, no_candidates=True)
        if self.schema_version() >= 9:
            self.rollback_to_v8()
        if self.schema_version() == 8:
            self.rollback_to_v7()
        try:
            with self.transaction() as conn:
                version = int(conn.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_migrations",
                ).fetchone()[0])
                if version != 7:
                    raise StorageError("V1.6 rollback requires schema version 7")
                candidates = int(conn.execute(
                    "SELECT count(*) FROM memories WHERE status='CANDIDATE'",
                ).fetchone()[0])
                if candidates:
                    raise StorageError("cannot roll back while CANDIDATE memories exist")
                conn.execute("DROP INDEX ix_memories_candidate_expiry")
                conn.execute("ALTER TABLE memories DROP COLUMN candidate_expires_at")
                conn.execute("DELETE FROM schema_migrations WHERE version=7")
        except sqlite3.Error as exc:
            raise StorageError("could not roll back V1.6 schema to V1.5") from exc

    def rollback_to_v7(self) -> None:
        """Roll v8 back to v7 only when no import job remains."""
        self._ensure_open()
        version = self.schema_version()
        if version >= 9:
            jobs = int(self.connection.execute(
                "SELECT count(*) FROM import_jobs",
            ).fetchone()[0])
            if jobs:
                raise StorageError("cannot roll back while import_jobs contains data")
            self.rollback_to_v8()
        try:
            with self.transaction() as conn:
                version = int(conn.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_migrations",
                ).fetchone()[0])
                if version != 8:
                    raise StorageError("V1.7 rollback requires schema version 8")
                jobs = int(conn.execute(
                    "SELECT count(*) FROM import_jobs",
                ).fetchone()[0])
                if jobs:
                    raise StorageError("cannot roll back while import_jobs contains data")
                conn.execute("DROP INDEX ix_import_jobs_status")
                conn.execute("DROP INDEX ix_import_jobs_lookup")
                conn.execute("DROP INDEX ix_memory_sources_evidence_ref")
                conn.execute("DROP TABLE import_jobs")
                conn.execute("DELETE FROM schema_migrations WHERE version=8")
        except sqlite3.Error as exc:
            raise StorageError("could not roll back V1.7 schema to V1.6") from exc

    def rollback_preview(self) -> dict[str, object]:
        """Return a read-only report of what a v5→v4 rollback would remove."""
        self._ensure_open()
        try:
            version = self.schema_version()
            group_scopes = int(self.connection.execute(
                "SELECT count(*) FROM memory_scopes WHERE scope_type='GROUP'",
            ).fetchone()[0])
            group_memories = int(self.connection.execute(
                """SELECT count(*) FROM memories m
                   JOIN memory_scopes s ON s.id=m.scope_id
                   WHERE s.scope_type='GROUP'""",
            ).fetchone()[0])
            group_consents = int(self.connection.execute(
                "SELECT count(*) FROM memory_consents WHERE consent_type='group_memory'",
            ).fetchone()[0])
            group_rows = self.connection.execute(
                """SELECT id, platform, tenant_id, scope_external_id, is_enabled
                FROM memory_scopes WHERE scope_type='GROUP'
                ORDER BY platform, tenant_id, scope_external_id""",
            ).fetchall()
            extraction_jobs = 0
            if version >= 6:
                extraction_jobs = int(self.connection.execute(
                    "SELECT count(*) FROM extraction_jobs",
                ).fetchone()[0])
            candidates = 0
            if version >= 7:
                candidates = int(self.connection.execute(
                    "SELECT count(*) FROM memories WHERE status='CANDIDATE'",
                ).fetchone()[0])
            import_jobs = 0
            if version >= 8:
                import_jobs = int(self.connection.execute(
                    "SELECT count(*) FROM import_jobs",
                ).fetchone()[0])
            archived = 0
            pending_index_jobs = 0
            if version >= 10:
                archived = int(self.connection.execute(
                    "SELECT count(*) FROM memories WHERE status='ARCHIVED'",
                ).fetchone()[0])
                pending_index_jobs = int(self.connection.execute(
                    "SELECT count(*) FROM index_jobs WHERE status IN ('PENDING','RUNNING')",
                ).fetchone()[0])
        except sqlite3.Error as exc:
            raise StorageError("could not preview V1.1 rollback") from exc
        can_rollback = (
            version in (5, 6, 7, 8)
            and not group_scopes and not group_memories and not group_consents
            and not extraction_jobs and not candidates and not import_jobs
        )
        if version >= 9:
            can_rollback = False
        return {
            "schema_version": version,
            "group_scope_count": group_scopes,
            "group_memory_count": group_memories,
            "group_consent_count": group_consents,
            "extraction_job_count": extraction_jobs,
            "candidate_count": candidates,
            "import_job_count": import_jobs,
            "archived_count": archived,
            "pending_index_job_count": pending_index_jobs,
            "groups": [
                {
                    "scope_id": row["id"], "platform": row["platform"],
                    "tenant_id": row["tenant_id"], "group_id": row["scope_external_id"],
                    "enabled": bool(row["is_enabled"]),
                }
                for row in group_rows
            ],
            "can_rollback": can_rollback,
            "steps": [
                "（v10）先恢复或彻底删除所有 ARCHIVED 记忆，并清空 pending index_jobs",
                "（v8）清空 import_jobs 或接受 rollback_to_v7 拒绝",
                "（v7）归档/清理候选，或接受 rollback_to_v6 拒绝",
                "（v6）清空 extraction_jobs 或接受 rollback_to_v5 拒绝",
                "关闭 → [清空 →] 销毁每个群（会写入备份）",
                "确认无群 scope / 群记忆 / 群 consent / 候选 / 提取任务",
                "执行 rollback_v1_to_v05() 回到 v4",
            ],
        }

    def rollback_v1_to_v05(self, *, dry_run: bool = False) -> dict[str, object] | None:
        """Roll v5 back to v4 only when no group data remains.

        Newer schemas are rolled back first with their own guards.
        """
        if dry_run:
            return self.rollback_preview()
        self._ensure_open()
        if self.schema_version() >= 9:
            self.rollback_to_v8()
        if self.schema_version() == 7:
            self.rollback_to_v6()
        if self.schema_version() == 6:
            self.rollback_to_v5()
        self.connection.execute("PRAGMA foreign_keys = OFF")
        try:
            with self.transaction() as conn:
                version = int(conn.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_migrations",
                ).fetchone()[0])
                if version != 5:
                    raise StorageError("V1.0 rollback requires schema version 5")
                group_scopes = int(conn.execute(
                    "SELECT count(*) FROM memory_scopes WHERE scope_type='GROUP'",
                ).fetchone()[0])
                group_memories = int(conn.execute(
                    """SELECT count(*) FROM memories m
                       JOIN memory_scopes s ON s.id=m.scope_id
                       WHERE s.scope_type='GROUP'""",
                ).fetchone()[0])
                group_consents = int(conn.execute(
                    "SELECT count(*) FROM memory_consents WHERE consent_type='group_memory'",
                ).fetchone()[0])
                if group_scopes or group_memories or group_consents:
                    raise StorageError("cannot roll back while group scope/memory/consent exists")
                conn.execute("""
                    CREATE TABLE memory_scopes_v4 (
                        id TEXT PRIMARY KEY,
                        platform TEXT NOT NULL,
                        tenant_id TEXT NOT NULL,
                        scope_type TEXT NOT NULL CHECK (scope_type = 'PERSONAL'),
                        scope_external_id TEXT NOT NULL,
                        subject_type TEXT NOT NULL CHECK (subject_type = 'USER'),
                        subject_id TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        is_enabled INTEGER NOT NULL DEFAULT 1 CHECK (is_enabled IN (0, 1)),
                        disabled_at TEXT,
                        UNIQUE (platform, tenant_id, scope_type, scope_external_id, subject_type, subject_id)
                    )
                """)
                conn.execute("""
                    INSERT INTO memory_scopes_v4
                        (id, platform, tenant_id, scope_type, scope_external_id,
                         subject_type, subject_id, created_at, is_enabled, disabled_at)
                    SELECT id, platform, tenant_id, scope_type, scope_external_id,
                           subject_type, subject_id, created_at, is_enabled, disabled_at
                    FROM memory_scopes
                """)
                conn.execute("DROP TABLE memory_scopes")
                conn.execute("ALTER TABLE memory_scopes_v4 RENAME TO memory_scopes")
                conn.execute("DELETE FROM schema_migrations WHERE version=5")
        except sqlite3.Error as exc:
            raise StorageError("could not roll back V1.1 schema to V1.0") from exc
        finally:
            self.connection.execute("PRAGMA foreign_keys = ON")
        return None

    def backup_to(self, destination: str | Path) -> None:
        """Create a consistent SQLite backup for migration, group destroy, or rollback."""
        self._ensure_open()
        try:
            with self._lock:
                target = sqlite3.connect(str(destination))
                try:
                    self.connection.backup(target)
                    # A WAL-mode target can leave committed frames in -wal and
                    # sidecar files; normalize to a single self-contained file.
                    target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    target.execute("PRAGMA journal_mode=DELETE")
                finally:
                    target.close()
                for suffix in ("-wal", "-shm"):
                    Path(f"{destination}{suffix}").unlink(missing_ok=True)
        except (OSError, sqlite3.Error) as exc:
            raise StorageError("could not create SQLite backup") from exc

    def rollback_to_zero(self) -> None:
        """Destructively remove the V0 schema; intended for tests/dev rollback only."""
        try:
            with self.transaction() as conn:
                conn.execute("DROP TABLE IF EXISTS memory_fts")
                for table in (
                    "provider_call_audit", "index_state", "index_jobs",
                    "memory_embeddings",
                    "import_jobs", "extraction_jobs",
                    "memory_lineage_meta",
                    "memory_relations", "memory_consents", "memory_operations", "memory_tags", "memory_sources",
                    "memory_revisions", "memories", "memory_scopes",
                    "memory_meta", "schema_migrations",
                ):
                    conn.execute(f"DROP TABLE IF EXISTS {table}")
        except sqlite3.Error as exc:
            raise StorageError("could not roll back SQLite schema to empty V0 state") from exc

    def _checkpoint_truncate(self) -> None:
        row = self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if row is None:
            raise StorageError("WAL checkpoint returned no status")
        busy, log = int(row[0]), int(row[1])
        # log < 0 means the database is not in WAL mode (checkpoint is a no-op).
        if busy or log > 0:
            raise StorageError("WAL checkpoint is blocked by a reader; purge remains pending")

    @staticmethod
    def _mark_purge_complete(conn: sqlite3.Connection) -> None:
        for key, value in (
            (SECURE_PURGE_KEY, "0"),
            (SECURE_PURGE_BASELINE_KEY, "1"),
        ):
            conn.execute(
                "INSERT INTO memory_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def secure_purge(self) -> None:
        """Remove committed-deletion remnants from the database and WAL files.

        secure_delete overwrites deleted cells; checkpoint + VACUUM remove WAL
        frames and free pages that may still hold pre-existing content. A busy
        checkpoint leaves the pending marker set instead of falsely reporting
        completion. This is a SQLite-level purge, not a forensic SSD guarantee.
        """
        if self.path == ":memory:":
            with self.transaction() as conn:
                self._mark_purge_complete(conn)
            return
        self._ensure_open()
        try:
            with self._lock:
                self._ensure_open()
                self._checkpoint_truncate()
                self.connection.execute("VACUUM")
                self._checkpoint_truncate()
                with self.transaction() as conn:
                    self._mark_purge_complete(conn)
        except sqlite3.Error as exc:
            raise StorageError("secure purge failed") from exc

    def secure_purge_if_pending(self) -> bool:
        """Retry pending purge, and run a one-time baseline purge on upgrade."""
        if self.path == ":memory:":
            return False
        with self._lock:
            self._ensure_open()
            try:
                rows = self.connection.execute(
                    "SELECT key, value FROM memory_meta WHERE key IN (?, ?)",
                    (SECURE_PURGE_KEY, SECURE_PURGE_BASELINE_KEY),
                ).fetchall()
            except sqlite3.Error as exc:
                raise StorageError("could not read secure purge state") from exc
            state = {row["key"]: row["value"] for row in rows}
            if (state.get(SECURE_PURGE_KEY) != "1"
                    and state.get(SECURE_PURGE_BASELINE_KEY) == "1"):
                return False
            self.secure_purge()
            return True

    def schema_version(self) -> int:
        self._ensure_open()
        try:
            row = self.connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
            ).fetchone()
            return int(row["version"])
        except sqlite3.Error as exc:
            raise StorageError("could not read schema version") from exc

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self.connection.close()
            self._closed = True

    def __enter__(self) -> Self:
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
