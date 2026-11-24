"""SQLite-backed exact vector index over memory_embeddings (stdlib only)."""

from __future__ import annotations

import math
import sqlite3
import struct
from datetime import datetime

from ...core.errors import StorageError, VectorDimensionMismatch
from ...core.model import (
    IndexStateRecord,
    IndexStateStatus,
    IndexTarget,
    MemoryStatus,
)
from ...core.ports import SearchCandidate, VectorIndex
from .database import SQLiteDatabase
from .index_jobs import SQLiteIndexStateStore
from .store import _time_dump, _time_load

_FLOAT = struct.Struct("<f")


def _normalize(values: tuple[float, ...] | list[float]) -> tuple[float, ...]:
    if not values:
        raise VectorDimensionMismatch("embedding vector is empty")
    try:
        floats = tuple(float(value) for value in values)
    except (TypeError, ValueError) as exc:
        raise VectorDimensionMismatch("embedding vector is not numeric") from exc
    if not all(math.isfinite(value) for value in floats):
        raise VectorDimensionMismatch("embedding vector contains non-finite values")
    norm = math.sqrt(math.fsum(value * value for value in floats))
    if norm <= 0.0:
        raise VectorDimensionMismatch("embedding vector has zero norm")
    return tuple(value / norm for value in floats)


def _pack(vector: tuple[float, ...]) -> bytes:
    return b"".join(_FLOAT.pack(value) for value in vector)


def _unpack(blob: bytes, dim: int) -> tuple[float, ...]:
    if len(blob) != dim * _FLOAT.size:
        raise VectorDimensionMismatch("stored embedding has an invalid length")
    return tuple(_FLOAT.unpack_from(blob, index * _FLOAT.size)[0] for index in range(dim))


def _dot(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    return math.fsum(a * b for a, b in zip(left, right))


class SQLiteVectorIndex(VectorIndex):
    def __init__(self, db: SQLiteDatabase, *, provider_id: str, model: str,
                 dim: int, config_hash: str,
                 states: SQLiteIndexStateStore | None = None,
                 snapshot_cache_mb: int = 64) -> None:
        self._db = db
        self.provider_id = provider_id
        self.model = model
        self.dim = dim
        self.config_hash = config_hash
        self._states = states or SQLiteIndexStateStore(db)
        self._snapshot_cache: dict[tuple[str, str, str], tuple[int, tuple]] = {}
        self._snapshot_bytes = 0
        self._snapshot_limit = max(0, snapshot_cache_mb) * 1024 * 1024
        self._cache_hits = 0
        self._cache_misses = 0

    def _invalidate_snapshot(self) -> None:
        self._snapshot_cache.clear()
        self._snapshot_bytes = 0

    def snapshot_cache_stats(self) -> dict[str, int]:
        return {
            "hits": self._cache_hits,
            "misses": self._cache_misses,
            "bytes": self._snapshot_bytes,
        }

    # ------------------------------------------------------------------ state
    def status(self) -> IndexStateRecord | None:
        return self._states.get(IndexTarget.VECTOR)

    def is_healthy(self) -> bool:
        record = self.status()
        return (
            record is not None
            and record.status is IndexStateStatus.HEALTHY
            and record.dim == self.dim
            and record.config_hash == self.config_hash
        )

    def mark_stale(self, *, at: datetime, dim: int | None = None,
                   config_hash: str | None = None) -> None:
        self._states.set_status(
            IndexTarget.VECTOR, IndexStateStatus.STALE, at=at,
            dim=dim if dim is not None else self.dim,
            config_hash=config_hash if config_hash is not None else self.config_hash,
        )

    def mark_failed(self, *, at: datetime) -> None:
        self._states.set_status(IndexTarget.VECTOR, IndexStateStatus.FAILED, at=at)

    def mark_rebuilding(self, *, at: datetime) -> None:
        self._states.set_status(IndexTarget.VECTOR, IndexStateStatus.REBUILDING, at=at)

    def mark_healthy(self, *, at: datetime) -> None:
        self._states.set_status(
            IndexTarget.VECTOR, IndexStateStatus.HEALTHY, at=at,
            dim=self.dim, config_hash=self.config_hash,
        )

    # ------------------------------------------------------------------ writes
    def upsert(self, memory_id: str, vector: tuple[float, ...], *, dim: int,
               provider: str, model: str, content_hash: str,
               db_lineage_id: str, creation_seq: int, revision: int,
               created_at: datetime) -> None:
        if dim != self.dim:
            raise VectorDimensionMismatch("embedding dimension does not match the index")
        normalized = _normalize(vector)
        if len(normalized) != self.dim:
            raise VectorDimensionMismatch("embedding dimension does not match the index")
        try:
            with self._db.transaction() as conn:
                conn.execute(
                    """INSERT INTO memory_embeddings
                       (memory_id, db_lineage_id, creation_seq, revision, provider,
                        model, dim, content_hash, vector, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(memory_id) DO UPDATE SET
                         db_lineage_id=excluded.db_lineage_id,
                         creation_seq=excluded.creation_seq,
                         revision=excluded.revision,
                         provider=excluded.provider, model=excluded.model,
                         dim=excluded.dim, content_hash=excluded.content_hash,
                         vector=excluded.vector, created_at=excluded.created_at""",
                    (memory_id, db_lineage_id, creation_seq, revision, provider,
                     model, self.dim, content_hash, _pack(normalized),
                     _time_dump(created_at)),
                )
        except sqlite3.Error as exc:
            raise StorageError("could not write memory embedding") from exc
        self._invalidate_snapshot()

    def remove(self, memory_id: str) -> None:
        try:
            with self._db.transaction() as conn:
                conn.execute(
                    "DELETE FROM memory_embeddings WHERE memory_id=?", (memory_id,),
                )
        except sqlite3.Error as exc:
            raise StorageError("could not remove memory embedding") from exc
        self._invalidate_snapshot()

    def clear(self, *, scope_id: str | None = None,
              subject_id: str | None = None) -> None:
        try:
            with self._db.transaction() as conn:
                if scope_id is None or subject_id is None:
                    conn.execute("DELETE FROM memory_embeddings")
                else:
                    conn.execute(
                        """DELETE FROM memory_embeddings WHERE memory_id IN (
                             SELECT id FROM memories
                             WHERE scope_id=? AND subject_id=?)""",
                        (scope_id, subject_id),
                    )
        except sqlite3.Error as exc:
            raise StorageError("could not clear memory embeddings") from exc
        self._invalidate_snapshot()

    def count(self, *, scope_id: str | None = None,
              subject_id: str | None = None) -> int:
        try:
            if scope_id is None or subject_id is None:
                row = self._db.connection.execute(
                    "SELECT count(*) FROM memory_embeddings",
                ).fetchone()
            else:
                row = self._db.connection.execute(
                    """SELECT count(*) FROM memory_embeddings e
                       JOIN memories m ON m.id=e.memory_id
                         AND m.db_lineage_id=e.db_lineage_id
                         AND m.creation_seq=e.creation_seq
                         AND m.current_revision=e.revision
                       WHERE m.scope_id=? AND m.subject_id=?""",
                    (scope_id, subject_id),
                ).fetchone()
            return int(row[0]) if row is not None else 0
        except sqlite3.Error as exc:
            raise StorageError("could not count memory embeddings") from exc

    # ------------------------------------------------------------------ search
    def _candidates(self, query: tuple[float, ...], *, scope_id: str,
                    subject_id: str, limit: int, at: datetime,
                    scope_type: str) -> tuple[SearchCandidate, ...]:
        normalized = _normalize(query)
        if len(normalized) != self.dim:
            raise VectorDimensionMismatch("query dimension does not match the index")
        key = (scope_id, subject_id, scope_type)
        cached = self._snapshot_cache.get(key)
        if cached is not None:
            self._cache_hits += 1
            rows = cached[1]
        else:
            self._cache_misses += 1
            raw_rows = self._db.connection.execute(
                """SELECT e.memory_id, e.vector, e.dim, m.status,
                          m.valid_from, m.valid_until, s.is_enabled
                   FROM memory_embeddings e
                   JOIN memories m ON m.id=e.memory_id
                     AND m.db_lineage_id=e.db_lineage_id
                     AND m.creation_seq=e.creation_seq
                     AND m.current_revision=e.revision
                   JOIN memory_scopes s ON s.id=m.scope_id
                   WHERE m.scope_id=? AND m.subject_id=? AND s.scope_type=?""",
                (scope_id, subject_id, scope_type),
            ).fetchall()
            parsed: list[tuple] = []
            size = 0
            for row in raw_rows:
                dim = int(row["dim"])
                if dim != self.dim:
                    raise VectorDimensionMismatch("stored embedding dimension mismatch")
                vector = _unpack(row["vector"], dim)
                parsed.append((
                    str(row["memory_id"]), vector, MemoryStatus(row["status"]),
                    _time_load(row["valid_from"]), _time_load(row["valid_until"]),
                    bool(row["is_enabled"]),
                ))
                size += len(vector) * 8 + 64
            rows = tuple(parsed)
            if self._snapshot_limit and 0 < size <= self._snapshot_limit:
                if self._snapshot_bytes + size > self._snapshot_limit:
                    self._invalidate_snapshot()
                self._snapshot_cache[key] = (size, rows)
                self._snapshot_bytes += size
        scored = [
            SearchCandidate(
                memory_id=memory_id, score=_dot(normalized, vector),
                status=status, valid_from=valid_from,
                valid_until=valid_until, scope_enabled=scope_enabled,
            )
            for memory_id, vector, status, valid_from, valid_until, scope_enabled in rows
        ]
        scored.sort(key=lambda item: (-item.score, item.memory_id))
        return tuple(scored[:max(0, limit)])

    def search_diagnostics(
        self, vector: tuple[float, ...], *, scope_id: str, subject_id: str,
        limit: int, at: datetime, scope_type: str = "PERSONAL",
    ) -> tuple[SearchCandidate, ...]:
        return self._candidates(
            vector, scope_id=scope_id, subject_id=subject_id,
            limit=limit, at=at, scope_type=scope_type,
        )

    def search(self, vector: tuple[float, ...], *, scope_id: str,
               subject_id: str, limit: int, at: datetime,
               scope_type: str = "PERSONAL") -> tuple[SearchCandidate, ...]:
        candidates = self._candidates(
            vector, scope_id=scope_id, subject_id=subject_id,
            limit=limit * 4 + 20, at=at, scope_type=scope_type,
        )
        eligible = [
            candidate for candidate in candidates
            if candidate.status is MemoryStatus.ACTIVE
            and candidate.scope_enabled
            and (candidate.valid_from is None or candidate.valid_from <= at)
            and (candidate.valid_until is None or candidate.valid_until > at)
        ]
        return tuple(eligible[:max(0, limit)])
