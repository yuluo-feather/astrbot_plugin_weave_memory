"""Independent append-only intent ledger (V1.8, ADR-0018).

The ledger lives in its own SQLite file so a main-database backup never carries
it. ``intent`` is written and fsynced before the main mutation; ``committed`` is
appended after it. Both share one ``event_id``. Rotation only moves the active
file into an immutable segment; event retention is never trimmed.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from ...core.errors import Conflict, StorageError, ValidationError
from ...core.model import LedgerEvent, LedgerEventType, LedgerState
from .store import _json_dump, _json_load, _time_dump, _time_load_required

_LEDGER_READER_VERSION = 2
_LEDGER_WRITER_VERSION = 2

_LEDGER_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS ledger_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ledger_events (
        event_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('intent','committed')),
        seq INTEGER NOT NULL UNIQUE CHECK (seq >= 1),
        event_type TEXT NOT NULL,
        scope_id TEXT,
        subject_id TEXT,
        memory_id TEXT,
        source TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY (event_id, state)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_ledger_events_seq ON ledger_events(seq)",
    "CREATE INDEX IF NOT EXISTS ix_ledger_events_type ON ledger_events(event_type, seq)",
)


def _row_to_event(row: sqlite3.Row) -> LedgerEvent:
    raw_type = str(row["event_type"])
    try:
        event_type = LedgerEventType(raw_type)
    except ValueError:
        event_type = LedgerEventType.UNKNOWN
    return LedgerEvent(
        event_id=row["event_id"],
        seq=int(row["seq"]),
        event_type=event_type,
        state=LedgerState(row["state"]),
        source=row["source"],
        occurred_at=_time_load_required(row["occurred_at"], "ledger.occurred_at"),
        payload=_json_load(row["payload_json"]),
        scope_id=row["scope_id"],
        subject_id=row["subject_id"],
        memory_id=row["memory_id"],
        raw_event_type=None if event_type is not LedgerEventType.UNKNOWN else raw_type,
    )


class SQLiteLedger:
    """Single-owner ledger connection with immutable rotated segments."""

    def __init__(self, path: str | Path, *, segments_dir: str | Path | None = None) -> None:
        self.path = Path(path)
        self.segments_dir = (
            Path(segments_dir) if segments_dir is not None
            else self.path.parent / "memory_ledger_segments"
        )
        self._lock = RLock()
        self._connection: sqlite3.Connection | None = None
        self._ledger_id = ""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._open_active()
        self._ledger_id = self._meta()["ledger_id"]

    def _connect(self, path: Path) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                str(path), isolation_level=None, timeout=5.0,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA secure_delete = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
        except sqlite3.Error as exc:
            raise StorageError("could not open memory ledger") from exc
        return connection

    def _open_active(self) -> None:
        connection = self._connect(self.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            for statement in _LEDGER_SCHEMA:
                connection.execute(statement)
            connection.execute("COMMIT")
        except sqlite3.Error as exc:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            connection.close()
            raise StorageError("could not initialize memory ledger") from exc
        self._connection = connection

    def _conn(self) -> sqlite3.Connection:
        if self._connection is None:
            raise StorageError("memory ledger is closed")
        return self._connection

    @staticmethod
    def _read_meta(connection: sqlite3.Connection) -> dict[str, str]:
        rows = connection.execute("SELECT key, value FROM ledger_meta").fetchall()
        return {row["key"]: row["value"] for row in rows}

    @staticmethod
    def _write_meta(connection: sqlite3.Connection, **values: object) -> None:
        for key, value in values.items():
            connection.execute(
                "INSERT INTO ledger_meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    def _meta(self) -> dict[str, str]:
        with self._lock:
            meta = self._read_meta(self._conn())
            if "ledger_id" not in meta:
                meta = {
                    "ledger_id": uuid4().hex,
                    "last_seq": "0",
                    "earliest_retained_seq": "1",
                    "base_seq": "1",
                    "end_seq": "0",
                    "ledger_format_version": str(_LEDGER_WRITER_VERSION),
                    "min_reader_version": str(_LEDGER_READER_VERSION),
                }
                with self._lock:
                    self._conn().execute("BEGIN IMMEDIATE")
                    try:
                        self._write_meta(self._conn(), **meta)
                        self._conn().execute("COMMIT")
                    except sqlite3.Error as exc:
                        if self._conn().in_transaction:
                            self._conn().execute("ROLLBACK")
                        raise StorageError("could not initialize ledger metadata") from exc
            elif "ledger_format_version" not in meta:
                # V1.8 ledgers become format 2 on first V2 open. Existing event
                # semantics are unchanged; only the registry version advances.
                with self._lock:
                    self._conn().execute("BEGIN IMMEDIATE")
                    try:
                        self._write_meta(
                            self._conn(),
                            ledger_format_version="2",
                            min_reader_version="2",
                        )
                        self._conn().execute("COMMIT")
                    except sqlite3.Error as exc:
                        if self._conn().in_transaction:
                            self._conn().execute("ROLLBACK")
                        raise StorageError("could not upgrade ledger metadata") from exc
                meta = self._read_meta(self._conn())
            required = int(meta.get("min_reader_version", "1"))
            if required > _LEDGER_READER_VERSION:
                raise StorageError(
                    "ledger requires a newer reader; refusal is fail-closed",
                )
            return meta

    def format_version(self) -> int:
        return int(self._meta().get("ledger_format_version", "1"))

    def min_reader_version(self) -> int:
        return int(self._meta().get("min_reader_version", "1"))

    def ledger_id(self) -> str:
        return self._meta()["ledger_id"]

    def last_seq(self) -> int:
        return int(self._meta()["last_seq"])

    def earliest_retained_seq(self) -> int:
        return int(self._meta()["earliest_retained_seq"])

    def _segment_files(self) -> list[Path]:
        if not self.segments_dir.exists():
            return []
        return sorted(self.segments_dir.glob("memory_ledger_*.sqlite3"))

    @staticmethod
    def _read_segment_meta(path: Path) -> dict[str, str]:
        try:
            connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
            try:
                connection.row_factory = sqlite3.Row
                rows = connection.execute("SELECT key, value FROM ledger_meta").fetchall()
                return {row["key"]: row["value"] for row in rows}
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise StorageError(f"could not read ledger segment {path.name}") from exc

    def segment_report(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            reports: list[dict[str, object]] = []
            for path in self._segment_files():
                meta = self._read_segment_meta(path)
                reports.append({
                    "path": str(path),
                    "ledger_id": meta.get("ledger_id", ""),
                    "base_seq": int(meta.get("base_seq", "0")),
                    "end_seq": int(meta.get("end_seq", "0")),
                    "active": False,
                })
            meta = self._meta()
            reports.append({
                "path": str(self.path),
                "ledger_id": meta["ledger_id"],
                "base_seq": int(meta["base_seq"]),
                "end_seq": int(meta["end_seq"]),
                "active": True,
            })
            return tuple(reports)

    def coverage_report(self) -> dict[str, Any]:
        reports = self.segment_report()
        ledger_ids = {str(report["ledger_id"]) for report in reports}
        contiguous = True
        for previous, current in zip(reports, reports[1:]):
            if int(current["base_seq"]) != int(previous["end_seq"]) + 1:
                contiguous = False
                break
        return {
            "ledger_id": self.ledger_id(),
            "ledger_ids": sorted(ledger_ids),
            "segments": list(reports),
            "contiguous": contiguous and len(ledger_ids) <= 1,
            "earliest_retained_seq": int(reports[0]["base_seq"]) if reports else 1,
            "last_seq": self.last_seq(),
        }

    def _find_event(self, event_id: str,
                    state: LedgerState) -> sqlite3.Row | None:
        row = self._conn().execute(
            "SELECT * FROM ledger_events WHERE event_id=? AND state=?",
            (event_id, state.value),
        ).fetchone()
        if row is not None:
            return row
        for path in self._segment_files():
            try:
                connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
                try:
                    connection.row_factory = sqlite3.Row
                    row = connection.execute(
                        "SELECT * FROM ledger_events WHERE event_id=? AND state=?",
                        (event_id, state.value),
                    ).fetchone()
                finally:
                    connection.close()
            except sqlite3.Error as exc:
                raise StorageError("could not check ledger segment for replay") from exc
            if row is not None:
                return row
        return None

    def append(self, *, event_id: str, event_type: LedgerEventType,
               state: LedgerState, source: str, occurred_at: datetime,
               payload: Mapping[str, object],
               scope_id: str | None = None, subject_id: str | None = None,
               memory_id: str | None = None) -> LedgerEvent:
        if not event_id.strip() or not source.strip():
            raise ValidationError("ledger event identifiers must be non-empty")
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValidationError("ledger occurred_at must be timezone-aware")
        with self._lock:
            connection = self._conn()
            existing = self._find_event(event_id, state)
            if existing is not None:
                return _row_to_event(existing)
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT value FROM ledger_meta WHERE key='last_seq'",
                ).fetchone()
                seq = int(row["value"]) + 1 if row is not None else 1
                connection.execute(
                    """INSERT INTO ledger_events
                    (event_id, state, seq, event_type, scope_id, subject_id,
                     memory_id, source, occurred_at, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (event_id, state.value, seq, event_type.value, scope_id,
                     subject_id, memory_id, source, _time_dump(occurred_at),
                     _json_dump(payload)),
                )
                meta = self._read_meta(connection)
                self._write_meta(
                    connection,
                    last_seq=seq,
                    end_seq=seq,
                    earliest_retained_seq=meta.get(
                        "earliest_retained_seq", meta.get("base_seq", "1"),
                    ),
                )
                connection.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                connection.execute("ROLLBACK")
                raise Conflict("ledger event identifier already exists") from exc
            except sqlite3.Error as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise StorageError("could not append ledger event") from exc
            return LedgerEvent(
                event_id=event_id, seq=seq, event_type=event_type, state=state,
                source=source, occurred_at=occurred_at.astimezone(UTC),
                payload=payload, scope_id=scope_id, subject_id=subject_id,
                memory_id=memory_id,
            )

    def _events_from_file(self, path: Path, *, after: int,
                          active: bool) -> list[LedgerEvent]:
        try:
            connection = (
                self._conn() if active
                else sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
            )
            try:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    "SELECT * FROM ledger_events WHERE seq > ? ORDER BY seq",
                    (after,),
                ).fetchall()
                return [_row_to_event(row) for row in rows]
            finally:
                if not active:
                    connection.close()
        except sqlite3.Error as exc:
            raise StorageError(f"could not read ledger events from {path.name}") from exc

    def events_after(self, seq: int) -> tuple[LedgerEvent, ...]:
        with self._lock:
            events: list[LedgerEvent] = []
            for path in self._segment_files():
                events.extend(self._events_from_file(path, after=seq, active=False))
            events.extend(self._events_from_file(self.path, after=seq, active=True))
            events.sort(key=lambda event: event.seq)
            return tuple(events)

    def all_events(self) -> tuple[LedgerEvent, ...]:
        return self.events_after(0)

    def pending_intents(self) -> tuple[LedgerEvent, ...]:
        events = self.all_events()
        committed = {event.event_id for event in events
                     if event.state is LedgerState.COMMITTED}
        return tuple(
            event for event in events
            if event.state is LedgerState.INTENT and event.event_id not in committed
        )

    def latest_by_key(self, *, after: int = 0) -> dict[tuple[str, str, str], LedgerEvent]:
        """Latest consent_change event per (scope, subject, consent_type)."""
        latest: dict[tuple[str, str, str], LedgerEvent] = {}
        for event in self.events_after(after):
            if event.event_type is not LedgerEventType.CONSENT_CHANGE:
                continue
            key = (
                str(event.scope_id or ""), str(event.subject_id or ""),
                str(event.payload.get("consent_type", "")),
            )
            if not all(key):
                continue
            current = latest.get(key)
            if current is None or event.seq > current.seq:
                latest[key] = event
        return latest

    def rotate(self) -> str | None:
        """Move the active ledger into an immutable segment file."""
        with self._lock:
            meta = self._meta()
            last = int(meta["last_seq"])
            if last == 0:
                return None
            base = int(meta["base_seq"])
            connection = self._conn()
            try:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.close()
            except sqlite3.Error as exc:
                raise StorageError("could not checkpoint ledger before rotation") from exc
            self.segments_dir.mkdir(parents=True, exist_ok=True)
            target = self.segments_dir / (
                f"memory_ledger_{base:012d}_{last:012d}.sqlite3"
            )
            if target.exists():
                raise StorageError("ledger rotation target already exists")
            try:
                os.replace(self.path, target)
                for suffix in ("-wal", "-shm"):
                    Path(str(self.path) + suffix).unlink(missing_ok=True)
            except OSError as exc:
                raise StorageError("could not rotate memory ledger") from exc
            self._open_active()
            connection = self._conn()
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._write_meta(
                    connection,
                    ledger_id=meta["ledger_id"],
                    last_seq=last,
                    earliest_retained_seq=meta.get("earliest_retained_seq", base),
                    base_seq=last + 1,
                    end_seq=last,
                )
                connection.execute("COMMIT")
            except sqlite3.Error as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise StorageError("could not initialize rotated ledger segment") from exc
            return str(target)

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
