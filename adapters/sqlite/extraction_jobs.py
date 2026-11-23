"""SQLite implementation of the V1.5 extraction job store (schema v6)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from ...core.errors import Conflict, NotFound, StorageError, ValidationError
from ...core.model import ExtractionJob, ExtractionJobStatus
from .database import SQLiteDatabase


def _time_dump(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("SQLite timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _time_load(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _json_dump(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValidationError("metadata must contain JSON-compatible values") from exc


def _json_load(value: str) -> dict[str, Any]:
    try:
        loaded = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise StorageError("stored extraction job metadata is invalid") from exc
    if not isinstance(loaded, dict):
        raise StorageError("stored extraction job metadata is not an object")
    return loaded


class SQLiteExtractionJobStore:
    def __init__(self, db: SQLiteDatabase) -> None:
        self.db = db

    def _row_to_job(self, row: sqlite3.Row) -> ExtractionJob:
        return ExtractionJob(
            id=row["id"], platform=row["platform"], tenant_id=row["tenant_id"],
            scope_id=row["scope_id"], subject_id=row["subject_id"],
            conversation_id=row["conversation_id"], watermark=int(row["watermark"]),
            content_hash=row["content_hash"],
            status=ExtractionJobStatus(row["status"]), attempts=int(row["attempts"]),
            last_error=row["last_error"],
            created_at=_time_load(row["created_at"]),
            updated_at=_time_load(row["updated_at"]),
            metadata=_json_load(row["metadata_json"]),
        )

    def create_or_get(self, job: ExtractionJob) -> tuple[ExtractionJob, bool]:
        try:
            with self.db.transaction() as conn:
                existing = conn.execute(
                    """SELECT * FROM extraction_jobs
                    WHERE platform=? AND tenant_id=? AND scope_id=? AND conversation_id=?
                      AND watermark=? AND content_hash=?""",
                    (job.platform, job.tenant_id, job.scope_id, job.conversation_id,
                     job.watermark, job.content_hash),
                ).fetchone()
                if existing is not None:
                    return self._row_to_job(existing), False
                conn.execute(
                    """INSERT INTO extraction_jobs
                    (id, platform, tenant_id, scope_id, subject_id, conversation_id,
                     watermark, content_hash, status, attempts, last_error,
                     created_at, updated_at, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (job.id, job.platform, job.tenant_id, job.scope_id, job.subject_id,
                     job.conversation_id, job.watermark, job.content_hash,
                     job.status.value, job.attempts, job.last_error,
                     _time_dump(job.created_at), _time_dump(job.updated_at),
                     _json_dump(job.metadata)),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("extraction job already exists with different data") from exc
        except sqlite3.Error as exc:
            raise StorageError("could not create extraction job") from exc
        return job, True

    def get(self, job_id: str) -> ExtractionJob:
        try:
            row = self.db.connection.execute(
                "SELECT * FROM extraction_jobs WHERE id=?", (job_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError("could not read extraction job") from exc
        if row is None:
            raise NotFound("extraction job not found")
        return self._row_to_job(row)

    def update(self, job: ExtractionJob, *, expected_status: ExtractionJobStatus) -> None:
        try:
            with self.db.transaction() as conn:
                cursor = conn.execute(
                    """UPDATE extraction_jobs
                    SET status=?, attempts=?, last_error=?, updated_at=?, metadata_json=?
                    WHERE id=? AND status=?""",
                    (job.status.value, job.attempts, job.last_error,
                     _time_dump(job.updated_at), _json_dump(job.metadata),
                     job.id, expected_status.value),
                )
                if cursor.rowcount != 1:
                    raise Conflict("extraction job status changed concurrently")
        except sqlite3.Error as exc:
            raise StorageError("could not update extraction job") from exc

    def list_by_status(
        self, *, statuses: tuple[ExtractionJobStatus, ...], limit: int = 100,
    ) -> tuple[ExtractionJob, ...]:
        if not statuses:
            return ()
        if type(limit) is not int or limit < 1 or limit > 1000:
            raise ValidationError("limit must be between 1 and 1000")
        placeholders = ",".join("?" for _ in statuses)
        try:
            rows = self.db.connection.execute(
                f"""SELECT * FROM extraction_jobs WHERE status IN ({placeholders})
                ORDER BY created_at, id LIMIT ?""",
                (*[status.value for status in statuses], limit),
            ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError("could not list extraction jobs") from exc
        return tuple(self._row_to_job(row) for row in rows)

    def count_by_status(self, *, scope_id: str, subject_id: str) -> dict[str, int]:
        try:
            rows = self.db.connection.execute(
                """SELECT status, count(*) AS total FROM extraction_jobs
                WHERE scope_id=? AND subject_id=? GROUP BY status""",
                (scope_id, subject_id),
            ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError("could not count extraction jobs") from exc
        counts = {status.value: 0 for status in ExtractionJobStatus}
        for row in rows:
            counts[row["status"]] = int(row["total"])
        return counts
