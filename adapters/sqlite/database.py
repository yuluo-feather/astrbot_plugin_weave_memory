"""SQLite connection, schema migrations, and transaction boundary (V0.3)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Self

from ...core.errors import ServiceClosed, StorageError

_SCHEMA_V1 = (Path(__file__).parent / "migrations" / "001_initial.sql").read_text(encoding="utf-8")
SECURE_PURGE_KEY = "secure_purge_pending"
SECURE_PURGE_BASELINE_KEY = "secure_purge_baseline_done"


class SQLiteDatabase:
    """Single-owner, single-process SQLite connection.

    The connection is intentionally thread-affine. V0.4's coordinator will be
    the single write entry point; this adapter does not promise multi-thread or
    multi-process write coordination.
    """

    CURRENT_SCHEMA_VERSION = 4

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

    def migrate(self) -> None:
        self._ensure_open()
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
        except sqlite3.Error as exc:
            raise StorageError("SQLite schema migration failed") from exc

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

    def rollback_to_zero(self) -> None:
        """Destructively remove the V0 schema; intended for tests/dev rollback only."""
        try:
            with self.transaction() as conn:
                conn.execute("DROP TABLE IF EXISTS memory_fts")
                for table in (
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
