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


class SQLiteDatabase:
    """Single-owner, single-process SQLite connection.

    The connection is intentionally thread-affine. V0.4's coordinator will be
    the single write entry point; this adapter does not promise multi-thread or
    multi-process write coordination.
    """

    CURRENT_SCHEMA_VERSION = 6

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
                    for statement in _SCHEMA_V1.split(";"):
                        if statement.strip():
                            conn.execute(statement)
                    conn.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (1, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                        ("initial_v0_schema",),
                    )
                    version = 1
                if version < 2:
                    migration_v2 = (Path(__file__).parent / "migrations" / "002_scope_disable.sql").read_text(encoding="utf-8")
                    for statement in migration_v2.split(";"):
                        if statement.strip():
                            conn.execute(statement)
                    conn.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (2, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                        ("scope_disable_and_close",),
                    )
                    version = 2
                if version < 3:
                    migration_v3 = (Path(__file__).parent / "migrations" / "003_memory_consents.sql").read_text(encoding="utf-8")
                    for statement in migration_v3.split(";"):
                        if statement.strip():
                            conn.execute(statement)
                    conn.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (3, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                        ("personal_memory_consents",),
                    )
                    version = 3
                if version < 4:
                    migration_v4 = (Path(__file__).parent / "migrations" / "004_memory_relations.sql").read_text(encoding="utf-8")
                    for statement in migration_v4.split(";"):
                        if statement.strip():
                            conn.execute(statement)
                    conn.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (4, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                        ("reserved_memory_relations",),
                    )
                    version = 4
                if version < 5:
                    migration_v5 = (Path(__file__).parent / "migrations" / "005_group_scopes.sql").read_text(encoding="utf-8")
                    for statement in migration_v5.split(";"):
                        if statement.strip():
                            conn.execute(statement)
                    conn.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (5, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                        ("group_scopes_admin",),
                    )
                if version < 6:
                    migration_v6 = (Path(__file__).parent / "migrations" / "006_extraction_jobs.sql").read_text(encoding="utf-8")
                    for statement in migration_v6.split(";"):
                        if statement.strip():
                            conn.execute(statement)
                    conn.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (6, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                        ("extraction_jobs",),
                    )
        except sqlite3.Error as exc:
            raise StorageError("SQLite schema migration failed") from exc
        finally:
            self.connection.execute("PRAGMA foreign_keys = ON")
        return backup_path

    def rollback_to_v05(self) -> None:
        """Roll back the V1.0-only reserved relation table to the V0.5 schema.

        Refuse rollback if relations exist; V1.0 deliberately exposes no relation
        write API, so an empty table is the expected safe downgrade condition.
        """
        self._ensure_open()
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
        """Roll v6 back to v5 only when no extraction job remains."""
        self._ensure_open()
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
        except sqlite3.Error as exc:
            raise StorageError("could not preview V1.1 rollback") from exc
        can_rollback = (
            version == 5
            and not group_scopes and not group_memories and not group_consents
        )
        return {
            "schema_version": version,
            "group_scope_count": group_scopes,
            "group_memory_count": group_memories,
            "group_consent_count": group_consents,
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
                "关闭 → [清空 →] 销毁每个群（会写入备份）",
                "确认无群 scope / 群记忆 / 群 consent",
                "执行 rollback_to_v10() 回到 v4",
            ],
        }

    def rollback_to_v10(self, *, dry_run: bool = False) -> dict[str, object] | None:
        """Roll v5 back to v4 only when no group data remains."""
        if dry_run:
            return self.rollback_preview()
        self._ensure_open()
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
                finally:
                    target.close()
        except sqlite3.Error as exc:
            raise StorageError("could not create SQLite backup") from exc

    def rollback_to_zero(self) -> None:
        """Destructively remove the V0 schema; intended for tests/dev rollback only."""
        try:
            with self.transaction() as conn:
                conn.execute("DROP TABLE IF EXISTS memory_fts")
                for table in (
                    "extraction_jobs",
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
