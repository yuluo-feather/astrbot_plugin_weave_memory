"""SQLite state for V2.4 Group personal policies and the WebUI identity binding.

Policy rows and sensitivity decisions are user-data (backed up with the main
database). Binding rows only link a Dashboard username to a platform member;
they carry no memory content.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Mapping

from ...core.errors import StorageError
from ...core.ports import Clock
from .database import SQLiteDatabase


class SQLiteGroupPersonalPolicyStore:
    def __init__(self, db: SQLiteDatabase, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    def get_policy(self, scope_id: str) -> dict[str, object] | None:
        try:
            row = self._db.connection.execute(
                "SELECT enabled, epoch, updated_at, updated_by "
                "FROM group_personal_policies WHERE scope_id=?",
                (scope_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError("could not read group personal policy") from exc
        if row is None:
            return None
        return {
            "enabled": bool(row["enabled"]),
            "epoch": int(row["epoch"]),
            "updated_at": row["updated_at"],
            "updated_by": row["updated_by"],
        }

    def set_enabled(self, scope_id: str, *, enabled: bool,
                    operator_id: str) -> dict[str, object]:
        now = self._clock.now().isoformat()
        try:
            with self._db.transaction() as conn:
                row = conn.execute(
                    "SELECT enabled, epoch FROM group_personal_policies WHERE scope_id=?",
                    (scope_id,),
                ).fetchone()
                if row is None:
                    epoch = 0
                    conn.execute(
                        "INSERT INTO group_personal_policies"
                        "(scope_id, enabled, epoch, updated_at, updated_by) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (scope_id, int(enabled), epoch, now, operator_id),
                    )
                else:
                    previous = bool(row["enabled"])
                    epoch = int(row["epoch"])
                    # Re-enabling bumps the epoch so earlier opt-ins do not
                    # silently come back after an administrator closed the group.
                    if enabled and not previous:
                        epoch += 1
                    conn.execute(
                        "UPDATE group_personal_policies SET enabled=?, epoch=?, "
                        "updated_at=?, updated_by=? WHERE scope_id=?",
                        (int(enabled), epoch, now, operator_id, scope_id),
                    )
        except sqlite3.Error as exc:
            raise StorageError("could not write group personal policy") from exc
        return {
            "enabled": bool(enabled), "epoch": epoch,
            "updated_at": now, "updated_by": operator_id,
        }

    def delete(self, scope_id: str) -> None:
        try:
            with self._db.transaction() as conn:
                conn.execute(
                    "DELETE FROM group_personal_policies WHERE scope_id=?",
                    (scope_id,),
                )
        except sqlite3.Error as exc:
            raise StorageError("could not delete group personal policy") from exc


class SQLiteSensitivityStore:
    def __init__(self, db: SQLiteDatabase, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    def get(self, memory_id: str) -> dict[str, object] | None:
        try:
            row = self._db.connection.execute(
                "SELECT category, source, decided_at FROM memory_sensitivity "
                "WHERE memory_id=?",
                (memory_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError("could not read sensitivity decision") from exc
        if row is None:
            return None
        return {
            "category": row["category"], "source": row["source"],
            "decided_at": row["decided_at"],
        }

    def record(self, memory_id: str, *, category: str, source: str,
               decided_at: datetime | None = None) -> None:
        when = (decided_at or self._clock.now()).isoformat()
        try:
            with self._db.transaction() as conn:
                conn.execute(
                    "INSERT INTO memory_sensitivity(memory_id, category, source, decided_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(memory_id) DO UPDATE SET "
                    "category=excluded.category, source=excluded.source, "
                    "decided_at=excluded.decided_at",
                    (memory_id, category, source, when),
                )
        except sqlite3.Error as exc:
            raise StorageError("could not record sensitivity decision") from exc


class SQLiteIdentityBindingStore:
    def __init__(self, db: SQLiteDatabase, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    def find_by_username(self, username: str) -> dict[str, object] | None:
        try:
            row = self._db.connection.execute(
                "SELECT username, platform, tenant_id, subject_id, bound_at, last_used_at "
                "FROM webui_identity_bindings WHERE username=?",
                (username,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError("could not read identity binding") from exc
        if row is None:
            return None
        return dict(row)

    def bind(self, username: str, *, platform: str, tenant_id: str,
             subject_id: str) -> dict[str, object]:
        now = self._clock.now().isoformat()
        try:
            with self._db.transaction() as conn:
                conn.execute(
                    "INSERT INTO webui_identity_bindings"
                    "(username, platform, tenant_id, subject_id, bound_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(username) DO UPDATE SET "
                    "platform=excluded.platform, tenant_id=excluded.tenant_id, "
                    "subject_id=excluded.subject_id, bound_at=excluded.bound_at",
                    (username, platform, tenant_id, subject_id, now),
                )
        except sqlite3.IntegrityError as exc:
            raise StorageError("identity is already bound to another dashboard user") from exc
        except sqlite3.Error as exc:
            raise StorageError("could not store identity binding") from exc
        return {
            "username": username, "platform": platform, "tenant_id": tenant_id,
            "subject_id": subject_id, "bound_at": now, "last_used_at": None,
        }

    def touch(self, username: str) -> None:
        try:
            with self._db.transaction() as conn:
                conn.execute(
                    "UPDATE webui_identity_bindings SET last_used_at=? WHERE username=?",
                    (self._clock.now().isoformat(), username),
                )
        except sqlite3.Error:
            # A failed usage stamp must not break the read request itself.
            return

    def unbind(self, username: str) -> None:
        try:
            with self._db.transaction() as conn:
                conn.execute(
                    "DELETE FROM webui_identity_bindings WHERE username=?",
                    (username,),
                )
        except sqlite3.Error as exc:
            raise StorageError("could not remove identity binding") from exc

    def create_code(self, code: str, *, platform: str, tenant_id: str,
                    subject_id: str, expires_at: datetime) -> None:
        now = self._clock.now().isoformat()
        try:
            with self._db.transaction() as conn:
                conn.execute(
                    "INSERT INTO webui_binding_codes"
                    "(code, platform, tenant_id, subject_id, created_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (code, platform, tenant_id, subject_id, now, expires_at.isoformat()),
                )
        except sqlite3.Error as exc:
            raise StorageError("could not store binding code") from exc

    def consume_code(self, code: str) -> Mapping[str, object] | None:
        """Atomically mark a code used; returns None when missing/expired/used."""
        now = self._clock.now().isoformat()
        try:
            with self._db.transaction() as conn:
                row = conn.execute(
                    "SELECT platform, tenant_id, subject_id, expires_at, used_at "
                    "FROM webui_binding_codes WHERE code=?",
                    (code,),
                ).fetchone()
                if row is None or row["used_at"] is not None:
                    return None
                if str(row["expires_at"]) <= now:
                    return None
                conn.execute(
                    "UPDATE webui_binding_codes SET used_at=? WHERE code=?",
                    (now, code),
                )
        except sqlite3.Error as exc:
            raise StorageError("could not consume binding code") from exc
        return {
            "platform": row["platform"], "tenant_id": row["tenant_id"],
            "subject_id": row["subject_id"],
        }

    def purge_expired(self) -> int:
        try:
            with self._db.transaction() as conn:
                cursor = conn.execute(
                    "DELETE FROM webui_binding_codes WHERE expires_at <= ?",
                    (self._clock.now().isoformat(),),
                )
                return int(cursor.rowcount or 0)
        except sqlite3.Error as exc:
            raise StorageError("could not purge binding codes") from exc
