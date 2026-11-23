"""SQLite implementation of the V1.7 import job store (schema v8, ADR-0017)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from ...core.errors import Conflict, NotFound, StorageError
from ...core.model import ImportJob, ImportJobStatus
from .database import SQLiteDatabase


def _time_dump(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _time_load(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _ids_dump(ids: tuple[str, ...]) -> str:
    return json.dumps(list(ids), ensure_ascii=False, separators=(",", ":"))


def _ids_load(value: str) -> tuple[str, ...]:
    try:
        loaded = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise StorageError("stored import job ids are invalid") from exc
    if not isinstance(loaded, list) or any(not isinstance(item, str) for item in loaded):
        raise StorageError("stored import job ids are not a string list")
    return tuple(loaded)


class SQLiteImportJobStore:
    def __init__(self, db: SQLiteDatabase) -> None:
        self.db = db

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> ImportJob:
        return ImportJob(
            id=row["id"], scope_id=row["scope_id"], subject_id=row["subject_id"],
            file_sha256=row["file_sha256"], dataset_version=row["dataset_version"],
            status=ImportJobStatus(row["status"]), cursor=int(row["cursor"]),
            created_memory_ids=_ids_load(row["created_memory_ids_json"]),
            created_at=_time_load(row["created_at"]),
            updated_at=_time_load(row["updated_at"]),
            last_error=row["last_error"],
        )

    def find_latest(self, *, scope_id: str, file_sha256: str) -> ImportJob | None:
        try:
            row = self.db.connection.execute(
                """SELECT * FROM import_jobs
                WHERE scope_id=? AND file_sha256=?
                ORDER BY created_at DESC LIMIT 1""",
                (scope_id, file_sha256),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError("could not read import job") from exc
        return self._row_to_job(row) if row is not None else None

    def create(self, job: ImportJob) -> ImportJob:
        try:
            with self.db.transaction() as conn:
                conn.execute(
                    """INSERT INTO import_jobs
                    (id, scope_id, subject_id, file_sha256, dataset_version, status,
                     cursor, created_memory_ids_json, last_error, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (job.id, job.scope_id, job.subject_id, job.file_sha256,
                     job.dataset_version, job.status.value, job.cursor,
                     _ids_dump(job.created_memory_ids), job.last_error,
                     _time_dump(job.created_at), _time_dump(job.updated_at)),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("import job already exists") from exc
        except sqlite3.Error as exc:
            raise StorageError("could not create import job") from exc
        return job

    def update(self, job: ImportJob) -> None:
        try:
            with self.db.transaction() as conn:
                cursor = conn.execute(
                    """UPDATE import_jobs
                    SET status=?, cursor=?, created_memory_ids_json=?, last_error=?,
                        updated_at=?
                    WHERE id=?""",
                    (job.status.value, job.cursor, _ids_dump(job.created_memory_ids),
                     job.last_error, _time_dump(job.updated_at), job.id),
                )
                if cursor.rowcount == 0:
                    raise NotFound("import job not found")
        except sqlite3.Error as exc:
            raise StorageError("could not update import job") from exc

    def get(self, job_id: str) -> ImportJob:
        try:
            row = self.db.connection.execute(
                "SELECT * FROM import_jobs WHERE id=?", (job_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError("could not read import job") from exc
        if row is None:
            raise NotFound("import job not found")
        return self._row_to_job(row)
