"""SQLite implementation of the authoritative MemoryStore port."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
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
    ArchiveReason,
    Identity,
    MemoryEntity,
    MemoryOperation,
    MemoryRevision,
    MemorySource,
    MemoryStatus,
    MemoryType,
    OperatorType,
    Scope,
    ScopeType,
    SubjectType,
)
from ...core.normalization import NORMALIZATION_VERSION
from ...core.policy.dedup import DedupKey
from .database import SECURE_PURGE_KEY, SQLiteDatabase

CURRENT_LINEAGE_KEY = "db_lineage_id"


def _time_dump(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("SQLite timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _content_hash(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


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
    def __init__(self, database: SQLiteDatabase, *,
                 vector_outbox: Callable[
                     [sqlite3.Connection, str, str, str], None,
                 ] | None = None) -> None:
        self.db = database
        self._vector_outbox = vector_outbox

    def _enqueue_vector(self, conn: sqlite3.Connection, memory_id: str,
                        operation: str, content_hash: str) -> None:
        if self._vector_outbox is not None:
            self._vector_outbox(conn, memory_id, operation, content_hash)

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

    @staticmethod
    def _current_lineage(conn: sqlite3.Connection) -> str:
        row = conn.execute(
            "SELECT value FROM memory_meta WHERE key=?", (CURRENT_LINEAGE_KEY,),
        ).fetchone()
        if row is None or not str(row["value"]).strip():
            raise StorageError("current db_lineage_id is missing")
        return str(row["value"])

    @staticmethod
    def _allocate_creation_seq(conn: sqlite3.Connection) -> tuple[str, int]:
        """Allocate the next lineage-scoped creation_seq inside the write transaction."""
        lineage = SQLiteMemoryStore._current_lineage(conn)
        row = conn.execute(
            "SELECT high_watermark FROM memory_lineage_meta WHERE db_lineage_id=?",
            (lineage,),
        ).fetchone()
        if row is None:
            next_seq = 1
            conn.execute(
                "INSERT INTO memory_lineage_meta(db_lineage_id, high_watermark) VALUES (?, ?)",
                (lineage, next_seq),
            )
        else:
            next_seq = int(row["high_watermark"]) + 1
            conn.execute(
                "UPDATE memory_lineage_meta SET high_watermark=? WHERE db_lineage_id=?",
                (next_seq, lineage),
            )
        return lineage, next_seq

    def current_lineage_id(self) -> str:
        try:
            return self._current_lineage(self.db.connection)
        except sqlite3.Error as exc:
            raise StorageError("could not read current db_lineage_id") from exc

    def lineage_high_watermarks(self) -> dict[str, int]:
        try:
            rows = self.db.connection.execute(
                "SELECT db_lineage_id, high_watermark FROM memory_lineage_meta",
            ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError("could not read lineage high watermarks") from exc
        return {row["db_lineage_id"]: int(row["high_watermark"]) for row in rows}

    def get_lineage(self, memory_id: str, *, scope_id: str,
                    subject_id: str) -> tuple[str, int]:
        try:
            row = self.db.connection.execute(
                """SELECT db_lineage_id, creation_seq FROM memories
                WHERE id=? AND scope_id=? AND subject_id=?""",
                (memory_id, scope_id, subject_id),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError("could not read memory lineage") from exc
        if row is None:
            raise NotFound("memory not found")
        return str(row["db_lineage_id"]), int(row["creation_seq"])

    def scope_lineage_boundaries(self, scope_id: str) -> dict[str, int]:
        """Current high watermark for every lineage that has rows in this scope.

        Reading the persisted allocator (not the maximum surviving row) keeps
        the clear boundary stable after rows were deleted.
        """
        try:
            lineages = [
                row["db_lineage_id"] for row in self.db.connection.execute(
                    "SELECT DISTINCT db_lineage_id FROM memories WHERE scope_id=?",
                    (scope_id,),
                ).fetchall()
            ]
            boundaries: dict[str, int] = {}
            for lineage in lineages:
                row = self.db.connection.execute(
                    "SELECT high_watermark FROM memory_lineage_meta WHERE db_lineage_id=?",
                    (lineage,),
                ).fetchone()
                if row is None:
                    raise StorageError(
                        f"missing high watermark for lineage {lineage}",
                    )
                boundaries[lineage] = int(row["high_watermark"])
        except sqlite3.Error as exc:
            raise StorageError("could not read scope lineage boundaries") from exc
        return boundaries

    def remove_attachment(self, *, memory_id: str, scope_id: str,
                          subject_id: str, attachment_type: str,
                          attachment_key: str) -> bool:
        """Delete one non-cascading source/tag/relation while the memory stays alive."""
        if attachment_type not in {"source", "tag", "relation"}:
            raise ValidationError("attachment_type must be source, tag or relation")
        try:
            with self.db.transaction() as conn:
                row = conn.execute(
                    """SELECT 1 FROM memories WHERE id=? AND scope_id=? AND subject_id=?""",
                    (memory_id, scope_id, subject_id),
                ).fetchone()
                if row is None:
                    raise NotFound("memory not found")
                if attachment_type == "source":
                    cursor = conn.execute(
                        "DELETE FROM memory_sources WHERE memory_id=? AND id=?",
                        (memory_id, attachment_key),
                    )
                elif attachment_type == "tag":
                    cursor = conn.execute(
                        "DELETE FROM memory_tags WHERE memory_id=? AND tag=?",
                        (memory_id, attachment_key),
                    )
                else:
                    cursor = conn.execute(
                        """DELETE FROM memory_relations
                        WHERE id=? AND (source_memory_id=? OR target_memory_id=?)""",
                        (attachment_key, memory_id, memory_id),
                    )
                return cursor.rowcount > 0
        except (NotFound, ValidationError):
            raise
        except sqlite3.Error as exc:
            raise StorageError("could not remove memory attachment") from exc

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
                        _time_dump(memory.candidate_expires_at),
                        memory.superseded_by, _time_dump(memory.created_at),
                        _time_dump(memory.updated_at), _time_dump(memory.last_accessed_at),
                        _json_dump(memory.metadata),
                        _time_dump(memory.archived_at), _time_dump(memory.purge_after),
                        memory.archive_reason.value if memory.archive_reason else None,
                    )
                    stored_memory = tuple(existing[key] for key in (
                        "id", "scope_id", "subject_id", "memory_type", "content",
                        "normalized_content", "normalization_version", "status", "source_type",
                        "current_revision", "valid_from", "valid_until",
                        "candidate_expires_at", "superseded_by",
                        "created_at", "updated_at", "last_accessed_at", "metadata_json",
                        "archived_at", "purge_after", "archive_reason",
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
                     valid_from, valid_until, candidate_expires_at, superseded_by,
                     created_at, updated_at, last_accessed_at, metadata_json,
                     archived_at, purge_after, archive_reason,
                     db_lineage_id, creation_seq)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (memory.id, memory.scope_id, memory.subject_id, memory.memory_type.value,
                     memory.content, memory.normalized_content, NORMALIZATION_VERSION,
                     memory.status.value, memory.source_type.value, memory.current_revision,
                     _time_dump(memory.valid_from), _time_dump(memory.valid_until),
                     _time_dump(memory.candidate_expires_at),
                     memory.superseded_by, _time_dump(memory.created_at),
                     _time_dump(memory.updated_at), _time_dump(memory.last_accessed_at),
                     _json_dump(memory.metadata),
                     _time_dump(memory.archived_at), _time_dump(memory.purge_after),
                     memory.archive_reason.value if memory.archive_reason else None,
                     *self._allocate_creation_seq(conn)),
                )
                self._insert_revision(conn, revision)
                self._insert_source(conn, source)
                self._mark_fts_dirty(conn, memory.id)
                if memory.status is MemoryStatus.ACTIVE:
                    self._enqueue_vector(
                        conn, memory.id, "UPSERT",
                        _content_hash(memory.normalized_content),
                    )
        except (AuthorizationDenied, Conflict, NotFound, ValidationError):
            raise
        except sqlite3.IntegrityError as exc:
            raise Conflict("memory violates an identifier or active/candidate deduplication constraint") from exc
        except sqlite3.Error as exc:
            raise StorageError("could not create memory transaction") from exc

    def update(self, memory: MemoryEntity, *, expected_revision: int,
               revision: MemoryRevision, source: MemorySource | None = None,
               operation: MemoryOperation | None = None) -> None:
        if memory.current_revision != expected_revision + 1:
            raise Conflict("updated entity revision must increment exactly once")
        if revision.memory_id != memory.id or revision.revision != memory.current_revision:
            raise Conflict("revision record does not match updated entity")
        if revision.content != memory.content or revision.status is not memory.status:
            raise Conflict("revision snapshot must match updated entity")
        if operation is not None and (
            operation.memory_id != memory.id or operation.scope_id != memory.scope_id
        ):
            raise ValidationError("operation must belong to the updated memory scope")
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
                    valid_from=?, valid_until=?, candidate_expires_at=?, superseded_by=?,
                    updated_at=?, last_accessed_at=?, metadata_json=?,
                    archived_at=?, purge_after=?, archive_reason=?
                    WHERE id=? AND current_revision=?""",
                    (memory.scope_id, memory.subject_id, memory.memory_type.value,
                     memory.content, memory.normalized_content, NORMALIZATION_VERSION,
                     memory.status.value, memory.source_type.value, memory.current_revision,
                     _time_dump(memory.valid_from), _time_dump(memory.valid_until),
                     _time_dump(memory.candidate_expires_at),
                     memory.superseded_by, _time_dump(memory.updated_at),
                     _time_dump(memory.last_accessed_at), _json_dump(memory.metadata),
                     _time_dump(memory.archived_at), _time_dump(memory.purge_after),
                     memory.archive_reason.value if memory.archive_reason else None,
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
                if operation is not None:
                    self._insert_operation(
                        conn, operation_id=operation.id, scope_id=operation.scope_id,
                        memory_id=operation.memory_id,
                        operation_type=operation.operation_type,
                        operator_type=operation.operator_type,
                        operator_id=operation.operator_id,
                        occurred_at=operation.occurred_at,
                        metadata=operation.metadata,
                    )
                self._mark_fts_dirty(conn, memory.id)
                if memory.status is MemoryStatus.ACTIVE:
                    self._enqueue_vector(
                        conn, memory.id, "UPSERT",
                        _content_hash(memory.normalized_content),
                    )
                else:
                    self._enqueue_vector(
                        conn, memory.id, "DELETE",
                        _content_hash(memory.normalized_content),
                    )
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

    def find_candidate_duplicate(self, key: DedupKey) -> MemoryEntity | None:
        try:
            row = self.db.connection.execute(
                """SELECT m.* FROM memories m JOIN memory_scopes s ON s.id=m.scope_id
                WHERE m.scope_id=? AND m.subject_id=? AND m.memory_type=?
                  AND m.normalized_content=? AND m.status='CANDIDATE' AND s.is_enabled=1
                ORDER BY m.created_at, m.id LIMIT 1""",
                (key.scope_id, key.subject_id, key.memory_type,
                 key.normalized_content),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError("could not find candidate duplicate memory") from exc
        return self._row_to_memory(row) if row is not None else None

    def list_candidates(
        self, *, scope_id: str, subject_id: str, limit: int, offset: int,
    ) -> tuple[tuple[MemoryEntity, ...], int]:
        if type(limit) is not int or limit < 1 or limit > 100:
            raise ValidationError("limit must be between 1 and 100")
        if type(offset) is not int or offset < 0:
            raise ValidationError("offset must be non-negative")
        try:
            with self.db.transaction(immediate=False) as conn:
                total = int(conn.execute(
                    """SELECT count(*) AS total FROM memories m
                    JOIN memory_scopes s ON s.id=m.scope_id
                    WHERE m.scope_id=? AND m.subject_id=? AND m.status='CANDIDATE'
                      AND s.is_enabled=1""",
                    (scope_id, subject_id),
                ).fetchone()["total"])
                rows = conn.execute(
                    """SELECT m.* FROM memories m JOIN memory_scopes s ON s.id=m.scope_id
                    WHERE m.scope_id=? AND m.subject_id=? AND m.status='CANDIDATE'
                      AND s.is_enabled=1
                    ORDER BY m.created_at, m.id LIMIT ? OFFSET ?""",
                    (scope_id, subject_id, limit, offset),
                ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError("could not list candidate memories") from exc
        return tuple(self._row_to_memory(row) for row in rows), total

    def list_expired_candidates(
        self, *, scope_id: str, subject_id: str, at: datetime, limit: int = 100,
    ) -> tuple[MemoryEntity, ...]:
        if type(limit) is not int or limit < 1 or limit > 1000:
            raise ValidationError("limit must be between 1 and 1000")
        try:
            rows = self.db.connection.execute(
                """SELECT m.* FROM memories m JOIN memory_scopes s ON s.id=m.scope_id
                WHERE m.scope_id=? AND m.subject_id=? AND m.status='CANDIDATE'
                  AND m.candidate_expires_at IS NOT NULL
                  AND m.candidate_expires_at <= ? AND s.is_enabled=1
                ORDER BY m.candidate_expires_at, m.id LIMIT ?""",
                (scope_id, subject_id, _time_dump(at), limit),
            ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError("could not list expired candidates") from exc
        return tuple(self._row_to_memory(row) for row in rows)

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

    def get_any(self, memory_id: str) -> MemoryEntity:
        """Internal index-worker lookup; never used for user-facing reads."""
        try:
            row = self.db.connection.execute(
                "SELECT * FROM memories WHERE id=?", (memory_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError("could not read memory for indexing") from exc
        if row is None:
            raise NotFound("memory not found")
        return self._row_to_memory(row)

    def list_active_all(self) -> tuple[MemoryEntity, ...]:
        try:
            rows = self.db.connection.execute(
                """SELECT m.* FROM memories m JOIN memory_scopes s ON s.id=m.scope_id
                   WHERE m.status='ACTIVE' AND s.is_enabled=1
                   ORDER BY m.created_at, m.id""",
            ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError("could not list active memories for rebuild") from exc
        return tuple(self._row_to_memory(row) for row in rows)

    def list_active_by_tag_text(self, query_text: str, *, scope_id: str,
                                subject_id: str,
                                limit: int = 20) -> tuple[MemoryEntity, ...]:
        text = (query_text or "").strip()
        if not text or limit < 1:
            return ()
        try:
            rows = self.db.connection.execute(
                """SELECT DISTINCT m.* FROM memories m
                   JOIN memory_scopes s ON s.id=m.scope_id
                   JOIN memory_tags t ON t.memory_id=m.id
                   WHERE m.scope_id=? AND m.subject_id=? AND m.status='ACTIVE'
                     AND s.is_enabled=1 AND INSTR(?, t.tag) > 0
                   ORDER BY m.created_at DESC, m.id LIMIT ?""",
                (scope_id, subject_id, text, limit),
            ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError("could not list active memories by tag") from exc
        return tuple(self._row_to_memory(row) for row in rows)

    def list_active_in_window(self, start: datetime, end: datetime, *,
                              scope_id: str, subject_id: str,
                              limit: int = 20) -> tuple[MemoryEntity, ...]:
        if limit < 1 or end <= start:
            return ()
        try:
            rows = self.db.connection.execute(
                """SELECT m.* FROM memories m JOIN memory_scopes s ON s.id=m.scope_id
                   WHERE m.scope_id=? AND m.subject_id=? AND m.status='ACTIVE'
                     AND s.is_enabled=1 AND m.created_at>=? AND m.created_at<?
                   ORDER BY m.created_at DESC, m.id LIMIT ?""",
                (scope_id, subject_id, _time_dump(start), _time_dump(end), limit),
            ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError("could not list active memories by time") from exc
        return tuple(self._row_to_memory(row) for row in rows)

    def list_for_export(
        self, *, scope_id: str, subject_id: str,
        statuses: tuple[MemoryStatus, ...],
    ) -> tuple[MemoryEntity, ...]:
        if not statuses:
            return ()
        targets = tuple(status.value for status in statuses)
        placeholders = ",".join("?" for _ in targets)
        try:
            rows = self.db.connection.execute(
                f"""SELECT m.* FROM memories m JOIN memory_scopes s ON s.id=m.scope_id
                WHERE m.scope_id=? AND m.subject_id=? AND s.is_enabled=1
                  AND m.status IN ({placeholders})
                ORDER BY m.created_at, m.id""",
                (scope_id, subject_id, *targets),
            ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError("could not list memories for export") from exc
        return tuple(self._row_to_memory(row) for row in rows)

    def find_import_source(self, evidence_ref: str) -> bool:
        try:
            row = self.db.connection.execute(
                """SELECT 1 FROM memory_sources
                WHERE source_type='IMPORTED' AND evidence_ref=? LIMIT 1""",
                (evidence_ref,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError("could not read imported source") from exc
        return row is not None

    def record_operation(
        self, *, operation_id: str, scope_id: str, memory_id: str | None,
        operation_type: str, operator_type: OperatorType, operator_id: str,
        occurred_at: datetime, metadata: Mapping[str, object],
    ) -> None:
        try:
            with self.db.transaction() as conn:
                self._insert_operation(
                    conn, operation_id=operation_id, scope_id=scope_id,
                    memory_id=memory_id, operation_type=operation_type,
                    operator_type=operator_type, operator_id=operator_id,
                    occurred_at=occurred_at, metadata=metadata,
                )
        except sqlite3.Error as exc:
            raise StorageError("could not record memory operation") from exc

    def list_active_page(
        self, *, scope_id: str, subject_id: str, limit: int, offset: int,
        memory_type: MemoryType | None = None,
    ) -> tuple[tuple[MemoryEntity, ...], int]:
        """Read one bounded page and its total from the same visible scope."""
        type_clause = " AND m.memory_type=?" if memory_type is not None else ""
        params: tuple[Any, ...] = (scope_id, subject_id)
        if memory_type is not None:
            params += (memory_type.value,)
        try:
            with self.db.transaction(immediate=False) as conn:
                total_row = conn.execute(
                    """SELECT count(*) AS total FROM memories m
                    JOIN memory_scopes s ON s.id=m.scope_id
                    WHERE m.scope_id=? AND m.subject_id=? AND m.status='ACTIVE'
                      AND s.is_enabled=1""" + type_clause,
                    params,
                ).fetchone()
                rows = conn.execute(
                    """SELECT m.* FROM memories m
                    JOIN memory_scopes s ON s.id=m.scope_id
                    WHERE m.scope_id=? AND m.subject_id=? AND m.status='ACTIVE'
                      AND s.is_enabled=1""" + type_clause +
                    " ORDER BY m.created_at, m.id LIMIT ? OFFSET ?",
                    params + (limit, offset),
                ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError("could not list active memory page") from exc
        return tuple(self._row_to_memory(row) for row in rows), int(total_row["total"])

    def list_revisions(
        self, memory_id: str, *, scope_id: str, subject_id: str,
    ) -> tuple[MemoryRevision, ...]:
        try:
            rows = self.db.connection.execute(
                """SELECT r.* FROM memory_revisions r
                JOIN memories m ON m.id=r.memory_id
                JOIN memory_scopes s ON s.id=m.scope_id
                WHERE r.memory_id=? AND m.scope_id=? AND m.subject_id=?
                  AND s.is_enabled=1
                ORDER BY r.revision""",
                (memory_id, scope_id, subject_id),
            ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError("could not read memory revisions") from exc
        if not rows:
            # Do not distinguish a foreign memory from a missing one.
            self.get(memory_id, scope_id=scope_id, subject_id=subject_id)
            return ()
        return tuple(MemoryRevision(
            id=row["id"], memory_id=row["memory_id"], revision=row["revision"],
            content=row["content"], status=MemoryStatus(row["status"]),
            changed_at=_time_load_required(row["changed_at"], "revision.changed_at"),
            change_reason=row["change_reason"], snapshot=_json_load(row["snapshot_json"]),
        ) for row in rows)

    def list_sources(
        self, memory_id: str, *, scope_id: str, subject_id: str,
    ) -> tuple[MemorySource, ...]:
        try:
            rows = self.db.connection.execute(
                """SELECT src.* FROM memory_sources src
                JOIN memories m ON m.id=src.memory_id
                JOIN memory_scopes s ON s.id=m.scope_id
                WHERE src.memory_id=? AND m.scope_id=? AND m.subject_id=?
                  AND s.is_enabled=1
                ORDER BY src.rowid""",
                (memory_id, scope_id, subject_id),
            ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError("could not read memory sources") from exc
        if not rows:
            self.get(memory_id, scope_id=scope_id, subject_id=subject_id)
            return ()
        from ...core.model import SourceType
        return tuple(MemorySource(
            id=row["id"], memory_id=row["memory_id"],
            source_type=SourceType(row["source_type"]),
            operator_type=OperatorType(row["operator_type"]),
            operator_id=row["operator_id"],
            captured_at=_time_load_required(row["captured_at"], "source.captured_at"),
            evidence_ref=row["evidence_ref"], metadata=_json_load(row["metadata_json"]),
        ) for row in rows)

    def statistics(self, *, scope_id: str, subject_id: str) -> dict[str, Any]:
        """Return counts only; never include memory bodies in management stats."""
        try:
            rows = self.db.connection.execute(
                """SELECT m.status, m.memory_type, m.source_type, count(*) AS total
                FROM memories m JOIN memory_scopes s ON s.id=m.scope_id
                WHERE m.scope_id=? AND m.subject_id=? AND s.is_enabled=1
                GROUP BY m.status, m.memory_type, m.source_type""",
                (scope_id, subject_id),
            ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError("could not read memory statistics") from exc
        by_status: dict[str, int] = {}
        by_type: dict[str, int] = {}
        by_source: dict[str, int] = {}
        total = 0
        for row in rows:
            count = int(row["total"])
            total += count
            by_status[row["status"]] = by_status.get(row["status"], 0) + count
            by_type[row["memory_type"]] = by_type.get(row["memory_type"], 0) + count
            by_source[row["source_type"]] = by_source.get(row["source_type"], 0) + count
        return {
            "total": total,
            "by_status": by_status,
            "by_type": by_type,
            "by_source": by_source,
        }

    def overview_counts(self) -> dict[str, object]:
        """Aggregate counts for the read-only management API; never includes bodies."""
        try:
            with self.db.transaction(immediate=False) as conn:
                scope_rows = conn.execute(
                    """SELECT scope_type, is_enabled, count(*) AS total
                    FROM memory_scopes GROUP BY scope_type, is_enabled""",
                ).fetchall()
                memory_rows = conn.execute(
                    """SELECT status, memory_type, source_type, count(*) AS total
                    FROM memories GROUP BY status, memory_type, source_type""",
                ).fetchall()
                candidate_rows = conn.execute(
                    """SELECT s.scope_type, count(*) AS total
                    FROM memories m JOIN memory_scopes s ON s.id=m.scope_id
                    WHERE m.status='CANDIDATE' GROUP BY s.scope_type""",
                ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError("could not read management overview") from exc
        scopes: dict[str, dict[str, int]] = {}
        for row in scope_rows:
            bucket = scopes.setdefault(row["scope_type"], {"total": 0, "enabled": 0})
            bucket["total"] += int(row["total"])
            if row["is_enabled"] == 1:
                bucket["enabled"] += int(row["total"])
        by_status: dict[str, int] = {}
        by_type: dict[str, int] = {}
        by_source: dict[str, int] = {}
        memory_total = 0
        for row in memory_rows:
            count = int(row["total"])
            memory_total += count
            by_status[row["status"]] = by_status.get(row["status"], 0) + count
            by_type[row["memory_type"]] = by_type.get(row["memory_type"], 0) + count
            by_source[row["source_type"]] = by_source.get(row["source_type"], 0) + count
        candidates = {row["scope_type"]: int(row["total"]) for row in candidate_rows}
        return {
            "scopes": scopes,
            "memories": {
                "total": memory_total,
                "by_status": by_status,
                "by_type": by_type,
                "by_source": by_source,
            },
            "candidates": {
                "total": sum(candidates.values()),
                "by_scope_type": candidates,
            },
        }

    def recent_operations(self, *, limit: int = 5) -> tuple[dict[str, object], ...]:
        if type(limit) is not int or limit < 1 or limit > 50:
            raise ValidationError("limit must be between 1 and 50")
        try:
            rows = self.db.connection.execute(
                """SELECT o.operation_type, o.operator_type, o.occurred_at,
                o.metadata_json, s.scope_type
                FROM memory_operations o JOIN memory_scopes s ON s.id=o.scope_id
                ORDER BY o.rowid DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError("could not read recent operations") from exc
        results: list[dict[str, object]] = []
        for row in rows:
            metadata = _json_load(row["metadata_json"])
            results.append({
                "operation_type": row["operation_type"],
                "operator_type": row["operator_type"],
                "scope_type": row["scope_type"],
                "occurred_at": row["occurred_at"],
                "result": metadata.get("result", "SUCCESS"),
            })
        return tuple(results)

    def list_group_scopes(self) -> tuple[dict[str, object], ...]:
        try:
            rows = self.db.connection.execute(
                """SELECT id, platform, tenant_id, scope_external_id, is_enabled,
                admin_source, created_at,
                (SELECT count(*) FROM memories m
                 WHERE m.scope_id=memory_scopes.id AND m.status='ACTIVE') AS active_count
                FROM memory_scopes WHERE scope_type='GROUP'
                ORDER BY platform, tenant_id, scope_external_id""",
            ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError("could not list group scopes") from exc
        return tuple({
            "scope_id": row["id"],
            "platform": row["platform"],
            "tenant_id": row["tenant_id"],
            "group_id": row["scope_external_id"],
            "enabled": bool(row["is_enabled"]),
            "admin_source": row["admin_source"],
            "active_count": int(row["active_count"]),
            "created_at": row["created_at"],
        } for row in rows)

    def maintenance_status(self) -> dict[str, object]:
        try:
            version = int(self.db.connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations",
            ).fetchone()[0])
            meta_rows = self.db.connection.execute(
                "SELECT key, value FROM memory_meta WHERE key IN (?, ?)",
                (SECURE_PURGE_KEY, "secure_purge_baseline_done"),
            ).fetchall()
            memory_total = int(self.db.connection.execute(
                "SELECT count(*) FROM memories",
            ).fetchone()[0])
            candidate_total = int(self.db.connection.execute(
                "SELECT count(*) FROM memories WHERE status='CANDIDATE'",
            ).fetchone()[0])
            operation_total = int(self.db.connection.execute(
                "SELECT count(*) FROM memory_operations",
            ).fetchone()[0])
            scope_counts = {
                row["scope_type"]: int(row["total"])
                for row in self.db.connection.execute(
                    "SELECT scope_type, count(*) AS total FROM memory_scopes GROUP BY scope_type",
                ).fetchall()
            }
            database_bytes = Path(self.db.path).stat().st_size if self.db.path != ":memory:" else 0
        except (OSError, sqlite3.Error) as exc:
            raise StorageError("could not read maintenance status") from exc
        meta = {row["key"]: row["value"] for row in meta_rows}
        return {
            "schema_version": version,
            "secure_purge_pending": meta.get(SECURE_PURGE_KEY) == "1",
            "secure_purge_baseline_done": meta.get("secure_purge_baseline_done") == "1",
            "memory_total": memory_total,
            "candidate_total": candidate_total,
            "operation_total": operation_total,
            "scope_counts": scope_counts,
            "database_bytes": database_bytes,
        }

    def list_operations(
        self, *, scope_id: str, subject_id: str, limit: int, offset: int,
    ) -> tuple[tuple[dict[str, object], ...], int]:
        """Read audit metadata only; operation payloads are intentionally omitted."""
        try:
            with self.db.transaction(immediate=False) as conn:
                total_row = conn.execute(
                    """SELECT count(*) AS total FROM memory_operations o
                    JOIN memory_scopes s ON s.id=o.scope_id
                    WHERE o.scope_id=? AND s.subject_id=? AND s.is_enabled=1""",
                    (scope_id, subject_id),
                ).fetchone()
                rows = conn.execute(
                    """SELECT o.id, o.memory_id, o.operation_type, o.operator_type,
                    o.operator_id, o.occurred_at FROM memory_operations o
                    JOIN memory_scopes s ON s.id=o.scope_id
                    WHERE o.scope_id=? AND s.subject_id=? AND s.is_enabled=1
                    ORDER BY o.rowid DESC LIMIT ? OFFSET ?""",
                    (scope_id, subject_id, limit, offset),
                ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError("could not read memory operations") from exc
        operations = tuple({
            "id": row["id"],
            "memory_id": row["memory_id"],
            "operation_type": row["operation_type"],
            "operator_type": row["operator_type"],
            "operator_id": row["operator_id"],
            "occurred_at": row["occurred_at"],
        } for row in rows)
        return operations, int(total_row["total"])

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
               occurred_at: datetime,
               expected_lineage: str | None = None,
               expected_creation_seq: int | None = None,
               idempotent: bool = False) -> bool:
        """Write a minimal tombstone audit record, then physically purge content."""
        try:
            with self.db.transaction() as conn:
                prior = conn.execute("SELECT * FROM memory_operations WHERE id=?", (operation_id,)).fetchone()
                if prior is not None:
                    if (prior["scope_id"], prior["memory_id"], prior["operation_type"],
                        prior["operator_type"], prior["operator_id"], prior["occurred_at"]) == (
                        scope_id, memory_id, "DELETE_MEMORY", operator_type.value,
                        operator_id, _time_dump(occurred_at)):
                        return False
                    raise Conflict("operation id already belongs to another operation")
                row = conn.execute(
                    """SELECT id, memory_type, source_type, current_revision,
                       normalized_content, db_lineage_id, creation_seq
                    FROM memories WHERE id=? AND scope_id=? AND subject_id=?""",
                    (memory_id, scope_id, subject_id),
                ).fetchone()
                if row is None:
                    if idempotent:
                        return False
                    raise NotFound("memory not found")
                if expected_lineage is not None and row["db_lineage_id"] != expected_lineage:
                    raise Conflict("delete event lineage does not match the stored row")
                if (expected_creation_seq is not None
                        and int(row["creation_seq"]) != expected_creation_seq):
                    raise Conflict("delete event creation_seq does not match the stored row")
                self._insert_operation(
                    conn, operation_id=operation_id, scope_id=scope_id,
                    memory_id=memory_id, operation_type="DELETE_MEMORY",
                    operator_type=operator_type, operator_id=operator_id,
                    occurred_at=occurred_at,
                    metadata={"status": "DELETED", "memory_type": row["memory_type"],
                              "source_type": row["source_type"],
                              "last_revision": row["current_revision"],
                              "db_lineage_id": row["db_lineage_id"],
                              "creation_seq": int(row["creation_seq"])},
                )
                self._mark_fts_dirty(conn, memory_id)
                self._enqueue_vector(
                    conn, memory_id, "DELETE",
                    _content_hash(row["normalized_content"]),
                )
                conn.execute(
                    "DELETE FROM memory_relations WHERE source_memory_id=? OR target_memory_id=?",
                    (memory_id, memory_id),
                )
                conn.execute("DELETE FROM memory_tags WHERE memory_id=?", (memory_id,))
                conn.execute("DELETE FROM memory_sources WHERE memory_id=?", (memory_id,))
                conn.execute("DELETE FROM memory_revisions WHERE memory_id=?", (memory_id,))
                self._delete_derived_rows(conn, (memory_id,))
                conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
                self._mark_secure_purge_pending(conn)
                return True
        except (Conflict, NotFound, ValidationError):
            raise
        except sqlite3.IntegrityError as exc:
            raise Conflict("delete operation identifier already exists") from exc
        except sqlite3.Error as exc:
            raise StorageError("could not delete memory transaction") from exc

    def list_expired_archives(self, *, at: datetime, limit: int) -> tuple[MemoryEntity, ...]:
        """Return ARCHIVED rows whose retention window has elapsed."""
        if limit < 1:
            raise ValidationError("archive cleanup limit must be >= 1")
        try:
            rows = self.db.connection.execute(
                """SELECT * FROM memories
                   WHERE status='ARCHIVED' AND purge_after IS NOT NULL AND purge_after<=?
                   ORDER BY purge_after, id LIMIT ?""",
                (_time_dump(at), limit),
            ).fetchall()
            return tuple(self._row_to_memory(row) for row in rows)
        except sqlite3.Error as exc:
            raise StorageError("could not list expired archives") from exc

    def purge_archived(self, memory_id: str, *, scope_id: str, subject_id: str,
                       operation_id: str, operator_type: OperatorType,
                       operator_id: str, occurred_at: datetime,
                       expected_lineage: str | None = None,
                       expected_creation_seq: int | None = None,
                       idempotent: bool = False) -> bool:
        """Permanently purge an ARCHIVED row and all derived/cascade data."""
        try:
            with self.db.transaction() as conn:
                prior = conn.execute(
                    "SELECT * FROM memory_operations WHERE id=?", (operation_id,),
                ).fetchone()
                if prior is not None:
                    if (prior["scope_id"], prior["memory_id"], prior["operation_type"],
                        prior["operator_type"], prior["operator_id"], prior["occurred_at"]) == (
                        scope_id, memory_id, "PURGE_MEMORY", operator_type.value,
                        operator_id, _time_dump(occurred_at)):
                        return False
                    raise Conflict("operation id already belongs to another operation")
                row = conn.execute(
                    """SELECT id, memory_type, source_type, current_revision, status,
                       normalized_content, archive_reason, db_lineage_id, creation_seq
                    FROM memories WHERE id=? AND scope_id=? AND subject_id=?""",
                    (memory_id, scope_id, subject_id),
                ).fetchone()
                if row is None:
                    if idempotent:
                        return False
                    raise NotFound("memory not found")
                if row["status"] != "ARCHIVED":
                    raise Conflict("purge requires an ARCHIVED memory")
                if expected_lineage is not None and row["db_lineage_id"] != expected_lineage:
                    raise Conflict("purge event lineage does not match the stored row")
                if (expected_creation_seq is not None
                        and int(row["creation_seq"]) != expected_creation_seq):
                    raise Conflict("purge event creation_seq does not match the stored row")
                self._insert_operation(
                    conn, operation_id=operation_id, scope_id=scope_id,
                    memory_id=memory_id, operation_type="PURGE_MEMORY",
                    operator_type=operator_type, operator_id=operator_id,
                    occurred_at=occurred_at,
                    metadata={"status": "DELETED", "memory_type": row["memory_type"],
                              "source_type": row["source_type"],
                              "last_revision": row["current_revision"],
                              "archive_reason": row["archive_reason"],
                              "db_lineage_id": row["db_lineage_id"],
                              "creation_seq": int(row["creation_seq"])},
                )
                self._mark_fts_dirty(conn, memory_id)
                self._enqueue_vector(
                    conn, memory_id, "DELETE",
                    _content_hash(row["normalized_content"]),
                )
                conn.execute(
                    "DELETE FROM memory_relations WHERE source_memory_id=? OR target_memory_id=?",
                    (memory_id, memory_id),
                )
                conn.execute("DELETE FROM memory_tags WHERE memory_id=?", (memory_id,))
                conn.execute("DELETE FROM memory_sources WHERE memory_id=?", (memory_id,))
                conn.execute("DELETE FROM memory_revisions WHERE memory_id=?", (memory_id,))
                self._delete_derived_rows(conn, (memory_id,))
                conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
                self._mark_secure_purge_pending(conn)
                return True
        except (Conflict, NotFound, ValidationError):
            raise
        except sqlite3.IntegrityError as exc:
            raise Conflict("purge operation identifier already exists") from exc
        except sqlite3.Error as exc:
            raise StorageError("could not purge archived memory transaction") from exc

    def replay_archive(self, memory_id: str, *, scope_id: str,
                       archived_at: datetime, purge_after: datetime,
                       archive_reason: ArchiveReason, occurred_at: datetime) -> bool:
        """Apply a pending archive intent during ledger replay (body-free)."""
        try:
            with self.db.transaction() as conn:
                cursor = conn.execute(
                    """UPDATE memories
                       SET status='ARCHIVED', archived_at=?, purge_after=?,
                           archive_reason=?, candidate_expires_at=NULL, updated_at=?
                       WHERE id=? AND scope_id=?""",
                    (_time_dump(archived_at), _time_dump(purge_after),
                     archive_reason.value, _time_dump(occurred_at),
                     memory_id, scope_id),
                )
                if cursor.rowcount != 1:
                    return False
                self._mark_fts_dirty(conn, memory_id)
                content = conn.execute(
                    "SELECT normalized_content FROM memories WHERE id=?", (memory_id,),
                ).fetchone()
                if content is not None:
                    self._enqueue_vector(
                        conn, memory_id, "DELETE",
                        _content_hash(content["normalized_content"]),
                    )
                return True
        except sqlite3.IntegrityError as exc:
            raise Conflict("archive replay violates a storage constraint") from exc
        except sqlite3.Error as exc:
            raise StorageError("could not replay archive event") from exc

    def replay_restore(self, memory_id: str, *, scope_id: str,
                       occurred_at: datetime) -> bool:
        """Apply a pending restore intent during ledger replay."""
        try:
            with self.db.transaction() as conn:
                cursor = conn.execute(
                    """UPDATE memories
                       SET status='ACTIVE', archived_at=NULL, purge_after=NULL,
                           archive_reason=NULL, updated_at=?
                       WHERE id=? AND scope_id=? AND status='ARCHIVED'""",
                    (_time_dump(occurred_at), memory_id, scope_id),
                )
                if cursor.rowcount != 1:
                    return False
                self._mark_fts_dirty(conn, memory_id)
                content = conn.execute(
                    "SELECT normalized_content FROM memories WHERE id=?", (memory_id,),
                ).fetchone()
                if content is not None:
                    self._enqueue_vector(
                        conn, memory_id, "UPSERT",
                        _content_hash(content["normalized_content"]),
                    )
                return True
        except sqlite3.IntegrityError as exc:
            raise Conflict("restore replay conflicts with an active duplicate") from exc
        except sqlite3.Error as exc:
            raise StorageError("could not replay restore event") from exc

    def reject_candidate(self, memory: MemoryEntity, *, expected_revision: int,
                         revision: MemoryRevision, operation: MemoryOperation,
                         purge_body: bool) -> None:
        """Persist CANDIDATE → REJECTED and its audit in one transaction.

        With purge_body the content, revisions, sources and memory row are removed
        in the same transaction, leaving only the body-free operation record.
        """
        if memory.status is not MemoryStatus.REJECTED:
            raise Conflict("reject_candidate requires a REJECTED entity")
        if memory.current_revision != expected_revision + 1:
            raise Conflict("rejected entity revision must increment exactly once")
        if revision.memory_id != memory.id or revision.revision != memory.current_revision:
            raise Conflict("revision record does not match rejected entity")
        if revision.content != memory.content or revision.status is not memory.status:
            raise Conflict("revision snapshot must match rejected entity")
        if operation.memory_id != memory.id or operation.scope_id != memory.scope_id:
            raise ValidationError("operation must belong to the rejected memory scope")
        try:
            with self.db.transaction() as conn:
                scope = conn.execute(
                    "SELECT subject_id, is_enabled FROM memory_scopes WHERE id=?",
                    (memory.scope_id,),
                ).fetchone()
                if scope is None:
                    raise NotFound("scope not found")
                if scope["subject_id"] != memory.subject_id:
                    raise Conflict("memory subject does not match target scope identity")
                if scope["is_enabled"] != 1:
                    raise AuthorizationDenied("scope is disabled")
                cursor = conn.execute(
                    """UPDATE memories SET status=?, current_revision=?, candidate_expires_at=?,
                    superseded_by=?, updated_at=?, metadata_json=?,
                    archived_at=NULL, purge_after=NULL, archive_reason=NULL
                    WHERE id=? AND current_revision=? AND status='CANDIDATE'""",
                    (memory.status.value, memory.current_revision,
                     _time_dump(memory.candidate_expires_at), memory.superseded_by,
                     _time_dump(memory.updated_at), _json_dump(memory.metadata),
                     memory.id, expected_revision),
                )
                if cursor.rowcount != 1:
                    exists = conn.execute("SELECT 1 FROM memories WHERE id=?", (memory.id,)).fetchone()
                    if exists is None:
                        raise NotFound("memory not found")
                    raise Conflict("revision conflict")
                self._insert_operation(
                    conn, operation_id=operation.id, scope_id=operation.scope_id,
                    memory_id=operation.memory_id,
                    operation_type=operation.operation_type,
                    operator_type=operation.operator_type,
                    operator_id=operation.operator_id,
                    occurred_at=operation.occurred_at,
                    metadata=operation.metadata,
                )
                self._mark_fts_dirty(conn, memory.id)
                self._enqueue_vector(
                    conn, memory.id, "DELETE",
                    _content_hash(memory.normalized_content),
                )
                if purge_body:
                    conn.execute(
                        "DELETE FROM memory_relations WHERE source_memory_id=? OR target_memory_id=?",
                        (memory.id, memory.id),
                    )
                    conn.execute("DELETE FROM memory_tags WHERE memory_id=?", (memory.id,))
                    conn.execute("DELETE FROM memory_sources WHERE memory_id=?", (memory.id,))
                    conn.execute("DELETE FROM memory_revisions WHERE memory_id=?", (memory.id,))
                    self._delete_derived_rows(conn, (memory.id,))
                    conn.execute("DELETE FROM memories WHERE id=?", (memory.id,))
                    self._mark_secure_purge_pending(conn)
                else:
                    self._insert_revision(conn, revision)
        except (AuthorizationDenied, Conflict, NotFound, ValidationError):
            raise
        except sqlite3.IntegrityError as exc:
            raise Conflict("candidate rejection violates a storage constraint") from exc
        except sqlite3.Error as exc:
            raise StorageError("could not reject candidate transaction") from exc

    def clear_scope(self, scope_id: str, *, operation_id: str,
                    operator_type: OperatorType, operator_id: str,
                    occurred_at: datetime,
                    boundaries: Mapping[str, int] | None = None,
                    ) -> tuple[str, ...]:
        """Atomically tombstone and purge scope memories.

        With ``boundaries`` the deletion is lineage-scoped (V1.8); without it
        the whole scope is cleared (V1.7 fallback when ledger-first is off).
        """
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
                if boundaries is None:
                    rows = conn.execute(
                        """SELECT id, normalized_content, db_lineage_id, creation_seq FROM memories
                        WHERE scope_id=? ORDER BY id""",
                        (scope_id,),
                    ).fetchall()
                elif boundaries:
                    clauses = " OR ".join(
                        "(db_lineage_id=? AND creation_seq<=?)" for _ in boundaries
                    )
                    params: list[object] = [scope_id]
                    for lineage, boundary in boundaries.items():
                        params.extend((lineage, boundary))
                    rows = conn.execute(
                        f"""SELECT id, normalized_content, db_lineage_id, creation_seq FROM memories
                        WHERE scope_id=? AND ({clauses}) ORDER BY id""",
                        tuple(params),
                    ).fetchall()
                else:
                    rows = []
                memory_ids = tuple(row["id"] for row in rows)
                for row in rows:
                    self._insert_operation(
                        conn, operation_id=f"{operation_id}:{row['id']}",
                        scope_id=scope_id, memory_id=row["id"],
                        operation_type="CLEAR_MEMORY", operator_type=operator_type,
                        operator_id=operator_id, occurred_at=occurred_at,
                        metadata={"status": "DELETED", "reason": "scope_clear",
                                  "db_lineage_id": row["db_lineage_id"],
                                  "creation_seq": int(row["creation_seq"])},
                    )
                    self._mark_fts_dirty(conn, row["id"])
                    self._enqueue_vector(
                        conn, row["id"], "DELETE",
                        _content_hash(row["normalized_content"]),
                    )
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
                    conn.execute(
                        f"DELETE FROM memory_tags WHERE memory_id IN ({placeholders})",
                        memory_ids,
                    )
                    conn.execute(
                        f"DELETE FROM memory_sources WHERE memory_id IN ({placeholders})",
                        memory_ids,
                    )
                    conn.execute(
                        f"DELETE FROM memory_revisions WHERE memory_id IN ({placeholders})",
                        memory_ids,
                    )
                    self._delete_derived_rows(conn, memory_ids)
                    conn.execute(
                        f"DELETE FROM memories WHERE id IN ({placeholders})",
                        memory_ids,
                    )
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

    def group_row_counts(self, scope_id: str) -> dict[str, int]:
        try:
            counts = {}
            for table, column in (
                ("memories", "scope_id"),
                ("memory_consents", "scope_id"),
                ("memory_operations", "scope_id"),
                ("memory_scopes", "id"),
            ):
                row = self.db.connection.execute(
                    f"SELECT count(*) FROM {table} WHERE {column}=?", (scope_id,),
                ).fetchone()
                counts[table] = int(row[0])
            return counts
        except sqlite3.Error as exc:
            raise StorageError("could not count group rows") from exc

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
                rows = conn.execute(
                    "SELECT id, normalized_content FROM memories WHERE scope_id=? ORDER BY id",
                    (scope_id,),
                ).fetchall()
                memory_ids = tuple(item["id"] for item in rows)
                for item in rows:
                    self._mark_fts_dirty(conn, item["id"])
                    self._enqueue_vector(
                        conn, item["id"], "DELETE",
                        _content_hash(item["normalized_content"]),
                    )
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
                self._delete_derived_rows(conn, memory_ids)
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
            raise StorageError(f"could not destroy group scope: {exc}") from exc

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
        record = dict(metadata)
        record.setdefault("result", "SUCCESS")
        conn.execute(
            """INSERT INTO memory_operations
            (id, scope_id, memory_id, operation_type, operator_type, operator_id,
             occurred_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (operation_id, scope_id, memory_id, operation_type, operator_type.value,
             operator_id, _time_dump(occurred_at), _json_dump(record)),
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
    def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
        ).fetchone() is not None

    @staticmethod
    def _delete_derived_rows(conn: sqlite3.Connection, memory_ids: tuple[str, ...]) -> None:
        if not memory_ids:
            return
        placeholders = ",".join("?" for _ in memory_ids)
        for table in ("memory_embeddings", "index_jobs"):
            if SQLiteMemoryStore._table_exists(conn, table):
                conn.execute(
                    f"DELETE FROM {table} WHERE memory_id IN ({placeholders})",
                    memory_ids,
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
        from ...core.model import ArchiveReason, MemoryType, SourceType
        stored_version = int(row["normalization_version"])
        if stored_version != NORMALIZATION_VERSION:
            raise StorageError(
                f"memory {row['id']} uses normalization version {stored_version}; "
                f"expected {NORMALIZATION_VERSION}"
            )
        return MemoryEntity(
            id=row["id"], scope_id=row["scope_id"], subject_id=row["subject_id"],
            memory_type=MemoryType(row["memory_type"]), content=row["content"],
            normalized_content=row["normalized_content"], status=MemoryStatus(row["status"]),
            source_type=SourceType(row["source_type"]), current_revision=row["current_revision"],
            created_at=_time_load_required(row["created_at"], "memory.created_at"),
            updated_at=_time_load_required(row["updated_at"], "memory.updated_at"),
            valid_from=_time_load(row["valid_from"]), valid_until=_time_load(row["valid_until"]),
            candidate_expires_at=_time_load(row["candidate_expires_at"]),
            superseded_by=row["superseded_by"], last_accessed_at=_time_load(row["last_accessed_at"]),
            archived_at=_time_load(row["archived_at"]),
            purge_after=_time_load(row["purge_after"]),
            archive_reason=(
                ArchiveReason(row["archive_reason"]) if row["archive_reason"] else None
            ),
            metadata=_json_load(row["metadata_json"]),
        )
