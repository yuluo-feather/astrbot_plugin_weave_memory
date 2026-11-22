"""SQLite implementation of the authoritative MemoryStore port."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from ...core.errors import (
    AuthorizationDenied,
    Conflict,
    NotFound,
    StorageError,
    ValidationError,
)
from ...core.model import (
    AdminSource,
    Identity,
    MemoryEntity,
    MemoryRevision,
    MemorySource,
    MemoryStatus,
    OperatorType,
    Scope,
    ScopeType,
    SubjectType,
)
from ...core.normalization import NORMALIZATION_VERSION
from ...core.policy.dedup import DedupKey
from .database import SECURE_PURGE_KEY, SQLiteDatabase


def _time_dump(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("SQLite timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _time_load(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value).astimezone(UTC)


def _time_load_required(value: str | None, field_name: str) -> datetime:
    loaded = _time_load(value)
    if loaded is None:
        raise StorageError(f"stored {field_name} is missing")
    return loaded


def _json_dump(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValidationError("metadata must contain JSON-compatible values") from exc


def _json_load(value: str) -> dict[str, Any]:
    try:
        loaded = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise StorageError("stored JSON object is invalid") from exc
    if not isinstance(loaded, dict):
        raise StorageError("stored JSON object has an invalid shape")
    return loaded


class SQLiteMemoryStore:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.db = database

    def create_scope(self, scope: Scope) -> None:
        identity = scope.identity
        try:
            with self.db.transaction() as conn:
                existing = conn.execute("SELECT * FROM memory_scopes WHERE id = ?", (scope.id,)).fetchone()
                values = (
                    identity.platform, identity.tenant_id, identity.scope_type.value,
                    identity.scope_external_id, identity.subject_type.value,
                    identity.subject_id,
                )
                if existing is not None:
                    stored = tuple(existing[k] for k in (
                        "platform", "tenant_id", "scope_type", "scope_external_id",
                        "subject_type", "subject_id",
                    ))
                    if stored != values:
                        raise Conflict("scope id already belongs to another identity")
                    return
                conn.execute(
                    """INSERT INTO memory_scopes
                    (id, platform, tenant_id, scope_type, scope_external_id,
                     subject_type, subject_id, created_at, is_enabled)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (scope.id, *values, _time_dump(scope.created_at),
                     0 if identity.scope_type is ScopeType.GROUP else 1),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("scope identity already exists") from exc
        except sqlite3.Error as exc:
            raise StorageError("could not create scope") from exc

    def get_scope(self, scope_id: str) -> Scope:
        try:
            row = self.db.connection.execute(
                "SELECT * FROM memory_scopes WHERE id = ?", (scope_id,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError("could not read scope") from exc
        if row is None:
            raise NotFound("scope not found")
        identity = Identity(
            platform=row["platform"], tenant_id=row["tenant_id"],
            scope_type=ScopeType(row["scope_type"]),
            scope_external_id=row["scope_external_id"],
            subject_type=SubjectType(row["subject_type"]), subject_id=row["subject_id"],
        )
        return Scope(row["id"], identity, _time_load_required(row["created_at"], "scope.created_at"))

    def is_scope_enabled(self, scope_id: str) -> bool:
        try:
            row = self.db.connection.execute(
                "SELECT is_enabled FROM memory_scopes WHERE id=?", (scope_id,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError("could not read scope state") from exc
        if row is None:
            raise NotFound("scope not found")
        return row["is_enabled"] == 1

    def create(self, memory: MemoryEntity, revision: MemoryRevision,
               source: MemorySource) -> None:
        if memory.status not in {MemoryStatus.ACTIVE, MemoryStatus.CANDIDATE}:
            raise Conflict("new memories must start as ACTIVE or CANDIDATE")
        if revision.memory_id != memory.id or revision.revision != 1 or memory.current_revision != 1:
            raise Conflict("initial revision and entity must both be revision 1")
        if revision.content != memory.content or revision.status is not memory.status:
            raise Conflict("initial revision snapshot must match memory")
        if source.memory_id != memory.id or source.source_type is not memory.source_type:
            raise Conflict("source must belong to memory and match source_type")
        try:
            with self.db.transaction() as conn:
                scope = conn.execute(
                    "SELECT subject_id, is_enabled FROM memory_scopes WHERE id=?",
                    (memory.scope_id,),
                ).fetchone()
                if scope is None:
                    raise NotFound("scope not found")
                if scope["subject_id"] != memory.subject_id:
                    raise Conflict("memory subject does not match scope identity")
                if scope["is_enabled"] != 1:
                    raise AuthorizationDenied("scope is disabled")
                existing = conn.execute("SELECT * FROM memories WHERE id=?", (memory.id,)).fetchone()
                if existing is not None:
                    expected_memory = (
                        memory.id, memory.scope_id, memory.subject_id, memory.memory_type.value,
                        memory.content, memory.normalized_content, NORMALIZATION_VERSION,
                        memory.status.value, memory.source_type.value, memory.current_revision,
                        _time_dump(memory.valid_from), _time_dump(memory.valid_until),
                        memory.superseded_by, _time_dump(memory.created_at),
                        _time_dump(memory.updated_at), _time_dump(memory.last_accessed_at),
                        _json_dump(memory.metadata),
                    )
                    stored_memory = tuple(existing[key] for key in (
                        "id", "scope_id", "subject_id", "memory_type", "content",
                        "normalized_content", "normalization_version", "status", "source_type",
                        "current_revision", "valid_from", "valid_until", "superseded_by",
                        "created_at", "updated_at", "last_accessed_at", "metadata_json",
                    ))
                    existing_revision = conn.execute(
                        "SELECT * FROM memory_revisions WHERE id=?", (revision.id,)
                    ).fetchone()
                    expected_revision = (
                        revision.id, revision.memory_id, revision.revision, revision.content,
                        revision.status.value, _time_dump(revision.changed_at),
                        revision.change_reason, _json_dump(revision.snapshot),
                    )
                    stored_revision = tuple(existing_revision[key] for key in (
                        "id", "memory_id", "revision", "content", "status", "changed_at",
                        "change_reason", "snapshot_json",
                    )) if existing_revision else None
                    existing_source = conn.execute(
                        "SELECT * FROM memory_sources WHERE id=?", (source.id,)
                    ).fetchone()
                    expected_source = (
                        source.id, source.memory_id, source.source_type.value,
                        source.operator_type.value, source.operator_id, source.evidence_ref,
                        _time_dump(source.captured_at), _json_dump(source.metadata),
                    )
                    stored_source = tuple(existing_source[key] for key in (
                        "id", "memory_id", "source_type", "operator_type", "operator_id",
                        "evidence_ref", "captured_at", "metadata_json",
                    )) if existing_source else None
                    if (stored_memory == expected_memory and
                            stored_revision == expected_revision and
                            stored_source == expected_source):
                        return  # Exact replay: idempotent success.
                    raise Conflict("memory id already exists with different content")

                conn.execute(
                    """INSERT INTO memories
                    (id, scope_id, subject_id, memory_type, content, normalized_content,
                     normalization_version, status, source_type, current_revision,
                     valid_from, valid_until, superseded_by, created_at, updated_at,
                     last_accessed_at, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (memory.id, memory.scope_id, memory.subject_id, memory.memory_type.value,
                     memory.content, memory.normalized_content, NORMALIZATION_VERSION,
                     memory.status.value, memory.source_type.value, memory.current_revision,
                     _time_dump(memory.valid_from), _time_dump(memory.valid_until),
                     memory.superseded_by, _time_dump(memory.created_at),
                     _time_dump(memory.updated_at), _time_dump(memory.last_accessed_at),
                     _json_dump(memory.metadata)),
                )
                self._insert_revision(conn, revision)
                self._insert_source(conn, source)
                self._mark_fts_dirty(conn, memory.id)
        except (AuthorizationDenied, Conflict, NotFound, ValidationError):
            raise
        except sqlite3.IntegrityError as exc:
            raise Conflict("memory violates an identifier or active/candidate deduplication constraint") from exc
        except sqlite3.Error as exc:
            raise StorageError("could not create memory transaction") from exc

    def update(self, memory: MemoryEntity, *, expected_revision: int,
               revision: MemoryRevision, source: MemorySource | None = None) -> None:
        if memory.current_revision != expected_revision + 1:
            raise Conflict("updated entity revision must increment exactly once")
        if revision.memory_id != memory.id or revision.revision != memory.current_revision:
            raise Conflict("revision record does not match updated entity")
        if revision.content != memory.content or revision.status is not memory.status:
            raise Conflict("revision snapshot must match updated entity")
        try:
            with self.db.transaction() as conn:
                scope = conn.execute("SELECT subject_id, is_enabled FROM memory_scopes WHERE id = ?", (memory.scope_id,)).fetchone()
                if scope is None:
                    raise NotFound("scope not found")
                if scope["subject_id"] != memory.subject_id:
                    raise Conflict("memory subject does not match target scope identity")
                if scope["is_enabled"] != 1:
                    raise AuthorizationDenied("scope is disabled")
                current = conn.execute("SELECT source_type FROM memories WHERE id=?", (memory.id,)).fetchone()
                if current is not None and memory.source_type.value != current["source_type"]:
                    if source is None or source.memory_id != memory.id or source.source_type is not memory.source_type:
                        raise Conflict("source change requires a matching new source record")
                elif source is not None and (source.memory_id != memory.id or source.source_type is not memory.source_type):
                    raise Conflict("new source record must match updated memory")
                cursor = conn.execute(
                    """UPDATE memories SET scope_id=?, subject_id=?, memory_type=?, content=?, normalized_content=?,
                    normalization_version=?, status=?, source_type=?, current_revision=?,
                    valid_from=?, valid_until=?, superseded_by=?, updated_at=?,
                    last_accessed_at=?, metadata_json=?
                    WHERE id=? AND current_revision=?""",
                    (memory.scope_id, memory.subject_id, memory.memory_type.value,
                     memory.content, memory.normalized_content, NORMALIZATION_VERSION,
                     memory.status.value, memory.source_type.value, memory.current_revision,
                     _time_dump(memory.valid_from), _time_dump(memory.valid_until),
                     memory.superseded_by, _time_dump(memory.updated_at),
                     _time_dump(memory.last_accessed_at), _json_dump(memory.metadata),
                     memory.id, expected_revision),
                )
                if cursor.rowcount != 1:
                    exists = conn.execute("SELECT 1 FROM memories WHERE id=?", (memory.id,)).fetchone()
                    if exists is None:
                        raise NotFound("memory not found")
                    raise Conflict("revision conflict")
                self._insert_revision(conn, revision)
                if source is not None:
                    self._insert_source(conn, source)
                self._mark_fts_dirty(conn, memory.id)
        except (AuthorizationDenied, Conflict, NotFound, ValidationError):
            raise
        except sqlite3.IntegrityError as exc:
            raise Conflict("memory update violates active/candidate deduplication constraint") from exc
        except sqlite3.Error as exc:
            raise StorageError("could not update memory transaction") from exc

    def get(self, memory_id: str, *, scope_id: str, subject_id: str) -> MemoryEntity:
        try:
            row = self.db.connection.execute(
                """SELECT m.* FROM memories m JOIN memory_scopes s ON s.id=m.scope_id
                WHERE m.id=? AND m.scope_id=? AND m.subject_id=? AND s.is_enabled=1""",
                (memory_id, scope_id, subject_id),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError("could not read memory") from exc
        if row is None:
            raise NotFound("memory not found")
        return self._row_to_memory(row)

    def find_active_duplicate(self, key: DedupKey) -> MemoryEntity | None:
        try:
            row = self.db.connection.execute(
                """SELECT m.* FROM memories m JOIN memory_scopes s ON s.id=m.scope_id
                WHERE m.scope_id=? AND m.subject_id=? AND m.memory_type=?
                  AND m.normalized_content=? AND m.status='ACTIVE' AND s.is_enabled=1
                ORDER BY m.created_at, m.id LIMIT 1""",
                (key.scope_id, key.subject_id, key.memory_type,
                 key.normalized_content),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError("could not find active duplicate memory") from exc
        return self._row_to_memory(row) if row is not None else None

    def list_active(self, *, scope_id: str, subject_id: str) -> tuple[MemoryEntity, ...]:
        try:
            rows = self.db.connection.execute(
                """SELECT m.* FROM memories m JOIN memory_scopes s ON s.id=m.scope_id
                WHERE m.scope_id=? AND m.subject_id=? AND m.status='ACTIVE' AND s.is_enabled=1
                ORDER BY m.created_at, m.id""",
                (scope_id, subject_id),
            ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError("could not list active memories") from exc
        return tuple(self._row_to_memory(row) for row in rows)

    def record_access(self, memory_ids: tuple[str, ...], *, scope_id: str,
                      subject_id: str, at: datetime) -> None:
        if not memory_ids:
            return
        placeholders = ",".join("?" for _ in memory_ids)
        try:
            with self.db.transaction() as conn:
                conn.execute(
                    f"""UPDATE memories SET last_accessed_at=?
                    WHERE id IN ({placeholders}) AND scope_id=? AND subject_id=?
                      AND status='ACTIVE'
                      AND EXISTS (SELECT 1 FROM memory_scopes s
                                  WHERE s.id=memories.scope_id AND s.is_enabled=1)""",
                    (_time_dump(at), *memory_ids, scope_id, subject_id),
                )
        except sqlite3.Error as exc:
            raise StorageError("could not record memory access") from exc

    def delete(self, memory_id: str, *, scope_id: str, subject_id: str,
               operation_id: str, operator_type: OperatorType, operator_id: str,
               occurred_at: datetime) -> None:
        """Write a minimal tombstone audit record, then physically purge content."""
        try:
            with self.db.transaction() as conn:
                prior = conn.execute("SELECT * FROM memory_operations WHERE id=?", (operation_id,)).fetchone()
                if prior is not None:
                    if (prior["scope_id"], prior["memory_id"], prior["operation_type"],
                        prior["operator_type"], prior["operator_id"], prior["occurred_at"]) == (
                        scope_id, memory_id, "DELETE_MEMORY", operator_type.value,
                        operator_id, _time_dump(occurred_at)):
                        return
                    raise Conflict("operation id already belongs to another operation")
                row = conn.execute(
                    """SELECT id, memory_type, source_type, current_revision
                    FROM memories WHERE id=? AND scope_id=? AND subject_id=?""",
                    (memory_id, scope_id, subject_id),
                ).fetchone()
                if row is None:
                    raise NotFound("memory not found")
                self._insert_operation(
                    conn, operation_id=operation_id, scope_id=scope_id,
                    memory_id=memory_id, operation_type="DELETE_MEMORY",
                    operator_type=operator_type, operator_id=operator_id,
                    occurred_at=occurred_at,
                    metadata={"status": "DELETED", "memory_type": row["memory_type"],
                              "source_type": row["source_type"],
                              "last_revision": row["current_revision"]},
                )
                self._mark_fts_dirty(conn, memory_id)
                conn.execute(
                    "DELETE FROM memory_relations WHERE source_memory_id=? OR target_memory_id=?",
                    (memory_id, memory_id),
                )
                conn.execute("DELETE FROM memory_tags WHERE memory_id=?", (memory_id,))
                conn.execute("DELETE FROM memory_sources WHERE memory_id=?", (memory_id,))
                conn.execute("DELETE FROM memory_revisions WHERE memory_id=?", (memory_id,))
                conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
                self._mark_secure_purge_pending(conn)
        except (Conflict, NotFound, ValidationError):
            raise
        except sqlite3.IntegrityError as exc:
            raise Conflict("delete operation identifier already exists") from exc
        except sqlite3.Error as exc:
            raise StorageError("could not delete memory transaction") from exc

    def clear_scope(self, scope_id: str, *, operation_id: str,
                    operator_type: OperatorType, operator_id: str,
                    occurred_at: datetime) -> tuple[str, ...]:
        """Atomically tombstone and purge every memory in one Personal scope."""
        try:
            with self.db.transaction() as conn:
                prior = conn.execute("SELECT * FROM memory_operations WHERE id=?", (operation_id,)).fetchone()
                if prior is not None:
                    if (prior["scope_id"], prior["memory_id"], prior["operation_type"],
                        prior["operator_type"], prior["operator_id"], prior["occurred_at"]) == (
                        scope_id, None, "CLEAR_SCOPE", operator_type.value,
                        operator_id, _time_dump(occurred_at)):
                        return ()
                    raise Conflict("operation id already belongs to another operation")
                scope = conn.execute("SELECT id FROM memory_scopes WHERE id=?", (scope_id,)).fetchone()
                if scope is None:
                    raise NotFound("scope not found")
                rows = conn.execute(
                    "SELECT id FROM memories WHERE scope_id=? ORDER BY id", (scope_id,)
                ).fetchall()
                memory_ids = tuple(row["id"] for row in rows)
                for memory_id in memory_ids:
                    self._insert_operation(
                        conn, operation_id=f"{operation_id}:{memory_id}",
                        scope_id=scope_id, memory_id=memory_id,
                        operation_type="CLEAR_MEMORY", operator_type=operator_type,
                        operator_id=operator_id, occurred_at=occurred_at,
                        metadata={"status": "DELETED", "reason": "scope_clear"},
                    )
                    self._mark_fts_dirty(conn, memory_id)
                self._insert_operation(
                    conn, operation_id=operation_id, scope_id=scope_id,
                    memory_id=None, operation_type="CLEAR_SCOPE",
                    operator_type=operator_type, operator_id=operator_id,
                    occurred_at=occurred_at, metadata={"deleted_count": len(memory_ids)},
                )
                if memory_ids:
                    placeholders = ",".join("?" for _ in memory_ids)
                    conn.execute(
                        f"DELETE FROM memory_relations WHERE source_memory_id IN ({placeholders}) OR target_memory_id IN ({placeholders})",
                        (*memory_ids, *memory_ids),
                    )
                conn.execute("DELETE FROM memory_tags WHERE memory_id IN (SELECT id FROM memories WHERE scope_id=?)", (scope_id,))
                conn.execute("DELETE FROM memory_sources WHERE memory_id IN (SELECT id FROM memories WHERE scope_id=?)", (scope_id,))
                conn.execute("DELETE FROM memory_revisions WHERE memory_id IN (SELECT id FROM memories WHERE scope_id=?)", (scope_id,))
                conn.execute("DELETE FROM memories WHERE scope_id=?", (scope_id,))
                self._mark_secure_purge_pending(conn)
                return memory_ids
        except (Conflict, NotFound, ValidationError):
            raise
        except sqlite3.IntegrityError as exc:
            raise Conflict("clear operation identifier already exists") from exc
        except sqlite3.Error as exc:
            raise StorageError("could not clear scope transaction") from exc

    def disable_scope(self, scope_id: str, *, operation_id: str,
                      operator_type: OperatorType, operator_id: str,
                      occurred_at: datetime) -> bool:
        """Close a scope: stop writes and ordinary reads without deleting data."""
        try:
            with self.db.transaction() as conn:
                prior = conn.execute("SELECT * FROM memory_operations WHERE id=?", (operation_id,)).fetchone()
                if prior is not None:
                    if (prior["scope_id"], prior["memory_id"], prior["operation_type"],
                        prior["operator_type"], prior["operator_id"], prior["occurred_at"]) == (
                        scope_id, None, "DISABLE_SCOPE", operator_type.value,
                        operator_id, _time_dump(occurred_at)):
                        return False
                    raise Conflict("operation id already belongs to another operation")
                row = conn.execute(
                    "SELECT is_enabled FROM memory_scopes WHERE id=?", (scope_id,)
                ).fetchone()
                if row is None:
                    raise NotFound("scope not found")
                if row["is_enabled"] == 0:
                    return False
                conn.execute(
                    "UPDATE memory_scopes SET is_enabled=0, disabled_at=? WHERE id=?",
                    (_time_dump(occurred_at), scope_id),
                )
                self._insert_operation(
                    conn, operation_id=operation_id, scope_id=scope_id,
                    memory_id=None, operation_type="DISABLE_SCOPE",
                    operator_type=operator_type, operator_id=operator_id,
                    occurred_at=occurred_at, metadata={},
                )
                return True
        except (Conflict, NotFound, ValidationError):
            raise
        except sqlite3.IntegrityError as exc:
            raise Conflict("disable operation identifier already exists") from exc
        except sqlite3.Error as exc:
            raise StorageError("could not disable scope") from exc

    def purge_deleted_payload(self) -> None:
        """Physically purge remnants left by committed delete/clear operations."""
        self.db.secure_purge()

    def get_admin_metadata(self, scope_id: str) -> tuple[tuple[str, ...], AdminSource]:
        try:
            row = self.db.connection.execute(
                "SELECT admin_ids_json, admin_source FROM memory_scopes WHERE id=?",
                (scope_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError("could not read group admin metadata") from exc
        if row is None:
            raise NotFound("scope not found")
        try:
            raw = json.loads(row["admin_ids_json"])
            if not isinstance(raw, list) or not all(
                isinstance(item, str) and item.strip() for item in raw
            ):
                raise ValueError("admin ids must be a list of strings")
            source = AdminSource(row["admin_source"])
        except (TypeError, ValueError) as exc:
            raise StorageError("stored group admin metadata is invalid") from exc
        return tuple(raw), source

    def set_admin_metadata(self, scope_id: str, admin_ids: tuple[str, ...],
                           source: AdminSource) -> None:
        ids = tuple(admin_ids)
        if any(not item.strip() for item in ids):
            raise ValidationError("admin ids must be non-empty")
        payload = json.dumps(list(ids), ensure_ascii=False, separators=(",", ":"))
        try:
            with self.db.transaction() as conn:
                row = conn.execute(
                    "SELECT scope_type FROM memory_scopes WHERE id=?", (scope_id,),
                ).fetchone()
                if row is None:
                    raise NotFound("scope not found")
                if row["scope_type"] != "GROUP":
                    raise Conflict("admin metadata is only valid for GROUP scopes")
                conn.execute(
                    "UPDATE memory_scopes SET admin_ids_json=?, admin_source=? WHERE id=?",
                    (payload, source.value, scope_id),
                )
        except (Conflict, NotFound, ValidationError):
            raise
        except sqlite3.Error as exc:
            raise StorageError("could not persist group admin metadata") from exc

    def enable_scope(self, scope_id: str, *, operation_id: str,
                     operator_type: OperatorType, operator_id: str,
                     occurred_at: datetime) -> bool:
        return self._set_group_enabled(
            scope_id, enabled=True, operation_type="GROUP_ENABLE",
            operation_id=operation_id, operator_type=operator_type,
            operator_id=operator_id, occurred_at=occurred_at,
        )

    def disable_group_scope(self, scope_id: str, *, operation_id: str,
                            operator_type: OperatorType, operator_id: str,
                            occurred_at: datetime) -> bool:
        return self._set_group_enabled(
            scope_id, enabled=False, operation_type="GROUP_DISABLE",
            operation_id=operation_id, operator_type=operator_type,
            operator_id=operator_id, occurred_at=occurred_at,
        )

    def _set_group_enabled(self, scope_id: str, *, enabled: bool,
                           operation_type: str, operation_id: str,
                           operator_type: OperatorType, operator_id: str,
                           occurred_at: datetime) -> bool:
        try:
            with self.db.transaction() as conn:
                prior = conn.execute(
                    "SELECT * FROM memory_operations WHERE id=?", (operation_id,),
                ).fetchone()
                if prior is not None:
                    if (prior["scope_id"], prior["memory_id"], prior["operation_type"],
                        prior["operator_type"], prior["operator_id"], prior["occurred_at"]) == (
                        scope_id, None, operation_type, operator_type.value,
                        operator_id, _time_dump(occurred_at)):
                        return False
                    raise Conflict("operation id already belongs to another operation")
                row = conn.execute(
                    "SELECT scope_type, is_enabled FROM memory_scopes WHERE id=?",
                    (scope_id,),
                ).fetchone()
                if row is None:
                    raise NotFound("scope not found")
                if row["scope_type"] != "GROUP":
                    raise Conflict("group lifecycle requires a GROUP scope")
                if bool(row["is_enabled"]) is enabled:
                    return False
                conn.execute(
                    "UPDATE memory_scopes SET is_enabled=?, disabled_at=? WHERE id=?",
                    (1 if enabled else 0, None if enabled else _time_dump(occurred_at), scope_id),
                )
                self._insert_operation(
                    conn, operation_id=operation_id, scope_id=scope_id,
                    memory_id=None, operation_type=operation_type,
                    operator_type=operator_type, operator_id=operator_id,
                    occurred_at=occurred_at, metadata={"enabled": enabled},
                )
                return True
        except (Conflict, NotFound, ValidationError):
            raise
        except sqlite3.IntegrityError as exc:
            raise Conflict("group lifecycle operation identifier already exists") from exc
        except sqlite3.Error as exc:
            raise StorageError("could not change group scope state") from exc

    def count_active(self, scope_id: str) -> int:
        try:
            return int(self.db.connection.execute(
                "SELECT count(*) FROM memories WHERE scope_id=? AND status='ACTIVE'",
                (scope_id,),
            ).fetchone()[0])
        except sqlite3.Error as exc:
            raise StorageError("could not count active memories") from exc

    def backup_group_scope(self, scope_id: str, destination: str) -> str:
        try:
            row = self.db.connection.execute(
                "SELECT scope_type FROM memory_scopes WHERE id=?", (scope_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError("could not inspect group scope for backup") from exc
        if row is None:
            raise NotFound("scope not found")
        if row["scope_type"] != "GROUP":
            raise Conflict("backup_group_scope requires a GROUP scope")
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db.backup_to(path)
        try:
            check = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
            try:
                check.row_factory = sqlite3.Row
                scope = check.execute(
                    "SELECT scope_type FROM memory_scopes WHERE id=?", (scope_id,),
                ).fetchone()
                if scope is None or scope["scope_type"] != "GROUP":
                    raise StorageError("backup does not contain the target group scope")
                check.execute(
                    "SELECT count(*) FROM memories WHERE scope_id=?", (scope_id,),
                ).fetchone()
                check.execute(
                    "SELECT count(*) FROM memory_consents WHERE scope_id=?", (scope_id,),
                ).fetchone()
            finally:
                check.close()
        except sqlite3.Error as exc:
            raise StorageError("could not verify group backup") from exc
        return sha256(path.read_bytes()).hexdigest()

    def delete_group_scope(self, scope_id: str, *, operation_id: str,
                           operator_type: OperatorType, operator_id: str,
                           occurred_at: datetime) -> int:
        """Atomically remove all group rows in an FK-safe order."""
        try:
            with self.db.transaction() as conn:
                row = conn.execute(
                    "SELECT scope_type FROM memory_scopes WHERE id=?", (scope_id,),
                ).fetchone()
                if row is None:
                    return 0
                if row["scope_type"] != "GROUP":
                    raise Conflict("delete_group_scope requires a GROUP scope")
                memory_ids = tuple(
                    item["id"] for item in conn.execute(
                        "SELECT id FROM memories WHERE scope_id=? ORDER BY id",
                        (scope_id,),
                    ).fetchall()
                )
                for memory_id in memory_ids:
                    self._mark_fts_dirty(conn, memory_id)
                conn.execute(
                    """DELETE FROM memory_relations
                       WHERE source_memory_id IN (SELECT id FROM memories WHERE scope_id=?)
                          OR target_memory_id IN (SELECT id FROM memories WHERE scope_id=?)""",
                    (scope_id, scope_id),
                )
                conn.execute(
                    "DELETE FROM memory_tags WHERE memory_id IN (SELECT id FROM memories WHERE scope_id=?)",
                    (scope_id,),
                )
                conn.execute(
                    "DELETE FROM memory_sources WHERE memory_id IN (SELECT id FROM memories WHERE scope_id=?)",
                    (scope_id,),
                )
                conn.execute(
                    "DELETE FROM memory_revisions WHERE memory_id IN (SELECT id FROM memories WHERE scope_id=?)",
                    (scope_id,),
                )
                conn.execute("DELETE FROM memories WHERE scope_id=?", (scope_id,))
                conn.execute("DELETE FROM memory_consents WHERE scope_id=?", (scope_id,))
                conn.execute("DELETE FROM memory_operations WHERE scope_id=?", (scope_id,))
                conn.execute("DELETE FROM memory_scopes WHERE id=?", (scope_id,))
                self._mark_secure_purge_pending(conn)
                return len(memory_ids)
        except (Conflict, NotFound, ValidationError):
            raise
        except sqlite3.IntegrityError as exc:
            raise Conflict("could not destroy group scope due to a constraint") from exc
        except sqlite3.Error as exc:
            raise StorageError("could not destroy group scope") from exc

    @staticmethod
    def _mark_secure_purge_pending(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO memory_meta(key,value) VALUES(?, '1') "
            "ON CONFLICT(key) DO UPDATE SET value='1'",
            (SECURE_PURGE_KEY,),
        )

    @staticmethod
    def _insert_operation(conn: sqlite3.Connection, *, operation_id: str,
                          scope_id: str, memory_id: str | None,
                          operation_type: str, operator_type: OperatorType,
                          operator_id: str, occurred_at: datetime,
                          metadata: Mapping[str, Any]) -> None:
        if not operation_id.strip() or not operator_id.strip():
            raise ValidationError("operation_id and operator_id must be non-empty")
        conn.execute(
            """INSERT INTO memory_operations
            (id, scope_id, memory_id, operation_type, operator_type, operator_id,
             occurred_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (operation_id, scope_id, memory_id, operation_type, operator_type.value,
             operator_id, _time_dump(occurred_at), _json_dump(metadata)),
        )

    @staticmethod
    def _insert_revision(conn: sqlite3.Connection, revision: MemoryRevision) -> None:
        conn.execute(
            """INSERT INTO memory_revisions
            (id, memory_id, revision, content, status, changed_at, change_reason, snapshot_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (revision.id, revision.memory_id, revision.revision, revision.content,
             revision.status.value, _time_dump(revision.changed_at), revision.change_reason,
             _json_dump(revision.snapshot)),
        )

    @staticmethod
    def _insert_source(conn: sqlite3.Connection, source: MemorySource) -> None:
        conn.execute(
            """INSERT INTO memory_sources
            (id, memory_id, source_type, operator_type, operator_id, evidence_ref,
             captured_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (source.id, source.memory_id, source.source_type.value,
             source.operator_type.value, source.operator_id, source.evidence_ref,
             _time_dump(source.captured_at), _json_dump(source.metadata)),
        )

    @staticmethod
    def _mark_fts_dirty(conn: sqlite3.Connection, memory_id: str) -> None:
        row = conn.execute("SELECT value FROM memory_meta WHERE key='fts_dirty_ids'").fetchone()
        try:
            raw_dirty = json.loads(row["value"]) if row else []
            if not isinstance(raw_dirty, list) or not all(isinstance(item, str) for item in raw_dirty):
                raise ValueError("dirty marker must be a list of memory IDs")
            dirty = set(raw_dirty)
        except (TypeError, ValueError) as exc:
            raise StorageError("FTS dirty marker is invalid") from exc
        dirty.add(memory_id)
        conn.execute(
            "INSERT INTO memory_meta(key,value) VALUES('fts_dirty_ids',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(sorted(dirty)),),
        )

    @staticmethod
    def _row_to_memory(row: sqlite3.Row) -> MemoryEntity:
        from ...core.model import MemoryType, SourceType
        return MemoryEntity(
            id=row["id"], scope_id=row["scope_id"], subject_id=row["subject_id"],
            memory_type=MemoryType(row["memory_type"]), content=row["content"],
            normalized_content=row["normalized_content"], status=MemoryStatus(row["status"]),
            source_type=SourceType(row["source_type"]), current_revision=row["current_revision"],
            created_at=_time_load_required(row["created_at"], "memory.created_at"),
            updated_at=_time_load_required(row["updated_at"], "memory.updated_at"),
            valid_from=_time_load(row["valid_from"]), valid_until=_time_load(row["valid_until"]),
            superseded_by=row["superseded_by"], last_accessed_at=_time_load(row["last_accessed_at"]),
            metadata=_json_load(row["metadata_json"]),
        )
