"""SQLite persistence for the VECTOR outbox and index state (schema v10)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from hashlib import sha256

from ...core.errors import Conflict, StorageError
from ...core.model import (
    IndexJob,
    IndexJobStatus,
    IndexOperation,
    IndexStateRecord,
    IndexStateStatus,
    IndexTarget,
)
from ...core.ports import IndexJobStore, IndexStateStore
from .database import SQLiteDatabase
from .store import _time_dump, _time_load_required


class SQLiteVectorOutbox:
    """Enqueue VECTOR jobs inside the caller's authoritative transaction."""

    def __call__(self, conn: sqlite3.Connection, memory_id: str,
                 operation: str, content_hash: str) -> None:
        now = _time_dump(datetime.now(UTC))
        job_id = sha256(
            f"{memory_id}|{operation}|{content_hash}".encode("utf-8"),
        ).hexdigest()[:32]
        conn.execute(
            """INSERT INTO index_jobs
               (id, memory_id, target, operation, content_hash, status,
                attempts, last_error, created_at, updated_at)
               VALUES (?, ?, 'VECTOR', ?, ?, 'PENDING', 0, NULL, ?, ?)
               ON CONFLICT(memory_id, target, operation, content_hash)
               DO UPDATE SET status='PENDING', attempts=0, last_error=NULL,
                             updated_at=excluded.updated_at
               WHERE index_jobs.status IN ('DONE','FAILED')""",
            (job_id, memory_id, operation, content_hash, now, now),
        )

_PENDING_OR_RUNNING = ("PENDING", "RUNNING")


def _row_to_job(row: sqlite3.Row) -> IndexJob:
    return IndexJob(
        id=row["id"], memory_id=row["memory_id"],
        target=IndexTarget(row["target"]),
        operation=IndexOperation(row["operation"]),
        content_hash=row["content_hash"],
        status=IndexJobStatus(row["status"]),
        attempts=int(row["attempts"]),
        last_error=row["last_error"],
        created_at=_time_load_required(row["created_at"], "index_job.created_at"),
        updated_at=_time_load_required(row["updated_at"], "index_job.updated_at"),
    )


def _row_to_state(row: sqlite3.Row) -> IndexStateRecord:
    return IndexStateRecord(
        target=IndexTarget(row["target"]),
        status=IndexStateStatus(row["status"]),
        dim=row["dim"],
        config_hash=row["config_hash"],
        updated_at=_time_load_required(row["updated_at"], "index_state.updated_at"),
    )


class SQLiteIndexJobStore(IndexJobStore):
    def __init__(self, db: SQLiteDatabase) -> None:
        self._db = db

    def create_or_get(self, job: IndexJob) -> tuple[IndexJob, bool]:
        try:
            with self._db.transaction() as conn:
                existing = conn.execute(
                    """SELECT * FROM index_jobs
                       WHERE memory_id=? AND target=? AND operation=? AND content_hash=?""",
                    (job.memory_id, job.target.value, job.operation.value,
                     job.content_hash),
                ).fetchone()
                if existing is not None:
                    return _row_to_job(existing), False
                conn.execute(
                    """INSERT INTO index_jobs
                       (id, memory_id, target, operation, content_hash, status,
                        attempts, last_error, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (job.id, job.memory_id, job.target.value, job.operation.value,
                     job.content_hash, job.status.value, job.attempts,
                     job.last_error, _time_dump(job.created_at),
                     _time_dump(job.updated_at)),
                )
                return job, True
        except sqlite3.IntegrityError as exc:
            raise Conflict("index job identifier already exists") from exc
        except sqlite3.Error as exc:
            raise StorageError("could not enqueue index job") from exc

    def claim_next(self, *, at: datetime) -> IndexJob | None:
        try:
            with self._db.transaction() as conn:
                row = conn.execute(
                    """SELECT * FROM index_jobs WHERE status='PENDING'
                       ORDER BY created_at, id LIMIT 1""",
                ).fetchone()
                if row is None:
                    return None
                cursor = conn.execute(
                    """UPDATE index_jobs SET status='RUNNING', updated_at=?
                       WHERE id=? AND status='PENDING'""",
                    (_time_dump(at), row["id"]),
                )
                if cursor.rowcount != 1:
                    return None
                updated = conn.execute(
                    "SELECT * FROM index_jobs WHERE id=?", (row["id"],),
                ).fetchone()
                return _row_to_job(updated)
        except sqlite3.Error as exc:
            raise StorageError("could not claim index job") from exc

    def mark_done(self, job_id: str, *, at: datetime) -> None:
        try:
            with self._db.transaction() as conn:
                conn.execute(
                    """UPDATE index_jobs SET status='DONE', last_error=NULL, updated_at=?
                       WHERE id=?""",
                    (_time_dump(at), job_id),
                )
        except sqlite3.Error as exc:
            raise StorageError("could not complete index job") from exc

    def mark_failed(self, job_id: str, *, error_code: str,
                    max_attempts: int, at: datetime) -> IndexJobStatus:
        try:
            with self._db.transaction() as conn:
                row = conn.execute(
                    "SELECT attempts FROM index_jobs WHERE id=?", (job_id,),
                ).fetchone()
                if row is None:
                    return IndexJobStatus.FAILED
                attempts = int(row["attempts"]) + 1
                status = (
                    IndexJobStatus.FAILED if attempts >= max_attempts
                    else IndexJobStatus.PENDING
                )
                conn.execute(
                    """UPDATE index_jobs SET attempts=?, status=?, last_error=?,
                       updated_at=? WHERE id=?""",
                    (attempts, status.value, error_code, _time_dump(at), job_id),
                )
                return status
        except sqlite3.Error as exc:
            raise StorageError("could not fail index job") from exc

    def reset_running_to_pending(self, *, at: datetime) -> int:
        try:
            with self._db.transaction() as conn:
                cursor = conn.execute(
                    """UPDATE index_jobs SET status='PENDING', updated_at=?
                       WHERE status='RUNNING'""",
                    (_time_dump(at),),
                )
                return int(cursor.rowcount)
        except sqlite3.Error as exc:
            raise StorageError("could not reset running index jobs") from exc

    def list_by_status(self, statuses: tuple[IndexJobStatus, ...],
                       *, limit: int = 100) -> tuple[IndexJob, ...]:
        if not statuses:
            return ()
        placeholders = ",".join("?" for _ in statuses)
        try:
            rows = self._db.connection.execute(
                f"""SELECT * FROM index_jobs WHERE status IN ({placeholders})
                    ORDER BY created_at, id LIMIT ?""",
                (*[status.value for status in statuses], limit),
            ).fetchall()
            return tuple(_row_to_job(row) for row in rows)
        except sqlite3.Error as exc:
            raise StorageError("could not list index jobs") from exc

    def count_by_status(self) -> dict[str, int]:
        try:
            rows = self._db.connection.execute(
                "SELECT status, count(*) AS count FROM index_jobs GROUP BY status",
            ).fetchall()
            return {str(row["status"]): int(row["count"]) for row in rows}
        except sqlite3.Error as exc:
            raise StorageError("could not count index jobs") from exc


class SQLiteIndexStateStore(IndexStateStore):
    def __init__(self, db: SQLiteDatabase) -> None:
        self._db = db

    def get(self, target: IndexTarget) -> IndexStateRecord | None:
        try:
            row = self._db.connection.execute(
                "SELECT * FROM index_state WHERE target=?", (target.value,),
            ).fetchone()
            return _row_to_state(row) if row is not None else None
        except sqlite3.Error as exc:
            raise StorageError("could not read index state") from exc

    def upsert(self, record: IndexStateRecord) -> None:
        try:
            with self._db.transaction() as conn:
                conn.execute(
                    """INSERT INTO index_state(target, dim, config_hash, status, updated_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(target) DO UPDATE SET
                         dim=excluded.dim, config_hash=excluded.config_hash,
                         status=excluded.status, updated_at=excluded.updated_at""",
                    (record.target.value, record.dim, record.config_hash,
                     record.status.value, _time_dump(record.updated_at)),
                )
        except sqlite3.Error as exc:
            raise StorageError("could not write index state") from exc

    def set_status(self, target: IndexTarget, status: IndexStateStatus, *,
                   dim: int | None = None, config_hash: str | None = None,
                   at: datetime) -> IndexStateRecord:
        current = self.get(target)
        record = IndexStateRecord(
            target=target, status=status, updated_at=at,
            dim=dim if dim is not None else (current.dim if current else None),
            config_hash=(
                config_hash if config_hash is not None
                else (current.config_hash if current else None)
            ),
        )
        self.upsert(record)
        return record


class SQLiteMemoryEmbeddingStore:
    """Read/write helpers for the derived embedding rows."""

    def __init__(self, db: SQLiteDatabase) -> None:
        self._db = db

    def get_content_hash(self, memory_id: str) -> str | None:
        row = self._db.connection.execute(
            "SELECT content_hash FROM memory_embeddings WHERE memory_id=?",
            (memory_id,),
        ).fetchone()
        return str(row["content_hash"]) if row is not None else None

    def remove(self, memory_id: str) -> None:
        try:
            with self._db.transaction() as conn:
                conn.execute(
                    "DELETE FROM memory_embeddings WHERE memory_id=?", (memory_id,),
                )
        except sqlite3.Error as exc:
            raise StorageError("could not remove memory embedding") from exc

    def count(self) -> int:
        row = self._db.connection.execute(
            "SELECT count(*) FROM memory_embeddings",
        ).fetchone()
        return int(row[0]) if row is not None else 0
