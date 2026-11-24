"""SQLite provider_call_audit repository (no memory body columns)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from uuid import uuid4

from ...core.errors import StorageError
from ...core.ports import ProviderCallAuditRepository
from ...core.provider import ProviderCallRecord
from .database import SQLiteDatabase
from .store import _time_dump


class SQLiteProviderCallAudit(ProviderCallAuditRepository):
    def __init__(self, db: SQLiteDatabase) -> None:
        self._db = db

    def record(self, record: ProviderCallRecord) -> None:
        try:
            with self._db.transaction() as conn:
                conn.execute(
                    """INSERT INTO provider_call_audit
                       (id, provider, model, scope_type, scope_id_hash,
                        field_categories, result, latency_ms, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (uuid4().hex, record.provider, record.model, record.scope_type,
                     record.scope_id_hash,
                     json.dumps(list(record.field_categories), ensure_ascii=False),
                     record.result.value, record.latency_ms,
                     _time_dump(record.created_at)),
                )
        except sqlite3.Error as exc:
            raise StorageError("could not write provider audit") from exc

    def ensure_available(self) -> bool:
        try:
            self._db.connection.execute(
                "SELECT 1 FROM provider_call_audit LIMIT 1",
            ).fetchone()
            return True
        except sqlite3.Error:
            return False

    def prune_before(self, cutoff: datetime) -> int:
        try:
            with self._db.transaction() as conn:
                cursor = conn.execute(
                    "DELETE FROM provider_call_audit WHERE created_at < ?",
                    (_time_dump(cutoff),),
                )
                return int(cursor.rowcount)
        except sqlite3.Error as exc:
            raise StorageError("could not prune provider audit") from exc

    def counts_since(self, since: datetime) -> dict[str, int]:
        try:
            rows = self._db.connection.execute(
                """SELECT result, count(*) AS count FROM provider_call_audit
                   WHERE created_at >= ? GROUP BY result""",
                (_time_dump(since),),
            ).fetchall()
            return {str(row["result"]): int(row["count"]) for row in rows}
        except sqlite3.Error as exc:
            raise StorageError("could not count provider audit") from exc
