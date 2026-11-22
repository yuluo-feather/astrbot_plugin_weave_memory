"""FTS5 derived index. The memories table remains the authority."""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from datetime import UTC, datetime

from ...core.errors import IndexUnavailable, ValidationError
from .database import SQLiteDatabase


def _fts_text(value: str) -> str:
    """Insert token boundaries between CJK characters for substring recall.

    unicode61 treats a contiguous Han run as one token. Character boundaries
    allow short Chinese/Japanese/Korean queries without changing stored text.
    """
    value = unicodedata.normalize("NFKC", value)
    pieces: list[str] = []
    for char in value:
        name = unicodedata.name(char, "")
        is_cjk = name.startswith(("CJK ", "HIRAGANA", "KATAKANA", "HANGUL"))
        if is_cjk:
            pieces.extend((" ", char, " "))
        else:
            pieces.append(char)
    return " ".join("".join(pieces).split())


_STOP_WORDS = {"the", "a", "an", "and", "or", "is", "are", "what", "which",
               "who", "when", "where", "why", "how", "do", "did", "does",
               "can", "could", "would", "you", "your", "i", "me", "my",
               "we", "our", "please", "remember", "about"}


def _fts_query(value: str) -> str:
    """Build a parameter-safe OR query from phrases, CJK bigrams and words."""
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized:
        return ""
    clauses: list[str] = []

    def add_phrase(text: str) -> None:
        phrase = _fts_text(text)
        if phrase:
            clauses.append('"' + phrase.replace('"', '""') + '"')

    add_phrase(normalized)
    cjk_run = ""
    for char in normalized:
        name = unicodedata.name(char, "")
        if name.startswith(("CJK ", "HIRAGANA", "KATAKANA", "HANGUL")):
            cjk_run += char
        else:
            if len(cjk_run) >= 2:
                for index in range(len(cjk_run) - 1):
                    add_phrase(cjk_run[index:index + 2])
            cjk_run = ""
    if len(cjk_run) >= 2:
        for index in range(len(cjk_run) - 1):
            add_phrase(cjk_run[index:index + 2])

    for word in re.findall(r"[A-Za-z0-9_]+", normalized):
        if len(word) >= 3 and word.casefold() not in _STOP_WORDS:
            add_phrase(word)
    return " OR ".join(dict.fromkeys(clauses))


class SQLiteFTSIndex:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.db = database

    def is_healthy(self) -> bool:
        try:
            row = self.db.connection.execute(
                "SELECT value FROM memory_meta WHERE key='fts_dirty_ids'"
            ).fetchone()
            if row is None:
                return False
            dirty = json.loads(row["value"])
            if not isinstance(dirty, list) or not all(isinstance(item, str) for item in dirty):
                raise ValueError("dirty marker must be a list of memory IDs")
            return not dirty
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise IndexUnavailable("FTS health state cannot be verified") from exc

    def sync_memory(self, memory_id: str) -> None:
        """Synchronize one changed memory and clear its durable dirty marker."""
        try:
            with self.db.transaction() as conn:
                row = conn.execute(
                    """SELECT m.id, m.content, m.status, s.is_enabled
                    FROM memories m JOIN memory_scopes s ON s.id=m.scope_id
                    WHERE m.id=?""", (memory_id,)
                ).fetchone()
                conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
                if row is not None and row["status"] == "ACTIVE" and row["is_enabled"] == 1:
                    conn.execute(
                        "INSERT INTO memory_fts(memory_id, content) VALUES (?, ?)",
                        (memory_id, _fts_text(row["content"])),
                    )
                dirty_row = conn.execute(
                    "SELECT value FROM memory_meta WHERE key='fts_dirty_ids'"
                ).fetchone()
                raw_dirty = json.loads(dirty_row["value"]) if dirty_row else []
                if not isinstance(raw_dirty, list) or not all(isinstance(item, str) for item in raw_dirty):
                    raise ValueError("dirty marker must be a list of memory IDs")
                dirty = set(raw_dirty)
                dirty.discard(memory_id)
                conn.execute(
                    "INSERT INTO memory_meta(key,value) VALUES('fts_dirty_ids',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps(sorted(dirty)),),
                )
        except (sqlite3.Error, TypeError, ValueError) as exc:
            # The marker was committed with the authoritative write and remains
            # present because this entire index transaction rolled back.
            raise IndexUnavailable("FTS synchronization failed; rebuild is required") from exc

    def document_count(self) -> int:
        try:
            return int(self.db.connection.execute("SELECT count(*) FROM memory_fts").fetchone()[0])
        except sqlite3.Error as exc:
            raise IndexUnavailable("FTS document count unavailable") from exc

    def integrity_check(self) -> None:
        try:
            with self.db.transaction() as conn:
                conn.execute("INSERT INTO memory_fts(memory_fts) VALUES('integrity-check')")
        except sqlite3.Error as exc:
            raise IndexUnavailable("FTS integrity check failed") from exc

    def recreate_and_rebuild(self) -> int:
        """Recreate the derived FTS table and repopulate it from authority."""
        try:
            with self.db.transaction() as conn:
                conn.execute("DROP TABLE IF EXISTS memory_fts")
                conn.execute("""CREATE VIRTUAL TABLE memory_fts USING fts5(
                    memory_id UNINDEXED, content,
                    tokenize='unicode61 remove_diacritics 2')""")
                rows = conn.execute("""SELECT m.id, m.content FROM memories m
                    JOIN memory_scopes s ON s.id=m.scope_id
                    WHERE m.status='ACTIVE' AND s.is_enabled=1""").fetchall()
                conn.executemany(
                    "INSERT INTO memory_fts(memory_id, content) VALUES (?, ?)",
                    [(row["id"], _fts_text(row["content"])) for row in rows],
                )
                count = conn.execute("SELECT count(*) FROM memory_fts").fetchone()[0]
                conn.execute(
                    "INSERT INTO memory_meta(key,value) VALUES('fts_dirty_ids','[]') "
                    "ON CONFLICT(key) DO UPDATE SET value='[]'"
                )
            return int(count)
        except sqlite3.Error as exc:
            raise IndexUnavailable("FTS recreation and rebuild failed") from exc

    def rebuild(self) -> int:
        """Rebuild all ACTIVE documents and clear dirty markers atomically."""
        try:
            with self.db.transaction() as conn:
                conn.execute("DELETE FROM memory_fts")
                rows = conn.execute("""SELECT m.id, m.content FROM memories m
                    JOIN memory_scopes s ON s.id=m.scope_id
                    WHERE m.status='ACTIVE' AND s.is_enabled=1""").fetchall()
                conn.executemany(
                    "INSERT INTO memory_fts(memory_id, content) VALUES (?, ?)",
                    [(row["id"], _fts_text(row["content"])) for row in rows],
                )
                count = conn.execute("SELECT count(*) FROM memory_fts").fetchone()[0]
                conn.execute(
                    "INSERT INTO memory_meta(key,value) VALUES('fts_dirty_ids','[]') "
                    "ON CONFLICT(key) DO UPDATE SET value='[]'"
                )
            return int(count)
        except sqlite3.Error as exc:
            raise IndexUnavailable("FTS rebuild failed") from exc

    def search(self, query: str, *, scope_id: str, subject_id: str,
               limit: int = 20, at: datetime | None = None) -> tuple[str, ...]:
        if not query or not query.strip():
            return ()
        if limit < 1 or limit > 100:
            raise ValidationError("limit must be between 1 and 100")
        if len(query) > 256:
            raise ValidationError("search query must not exceed 256 characters")
        if not self.is_healthy():
            raise IndexUnavailable("FTS index is dirty; rebuild before searching")
        # Evaluate semantic validity at the caller's clock instant. Filtering
        # before LIMIT prevents expired hits from crowding out valid memories.
        at = at or datetime.now(UTC)
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValidationError("search evaluation time must be timezone-aware")
        at_key = at.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
        # Every user term is escaped into a quoted FTS phrase before OR composition.
        fts_query = _fts_query(query)
        if not fts_query:
            return ()
        try:
            rows = self.db.connection.execute(
                """SELECT m.id FROM memory_fts f
                JOIN memories m ON m.id=f.memory_id
                JOIN memory_scopes s ON s.id=m.scope_id
                WHERE memory_fts MATCH ? AND m.scope_id=? AND m.subject_id=?
                  AND m.status='ACTIVE' AND s.is_enabled=1
                  AND (m.valid_from IS NULL OR m.valid_from <= ?)
                  AND (m.valid_until IS NULL OR m.valid_until > ?)
                ORDER BY bm25(memory_fts) ASC,
                  COALESCE(m.last_accessed_at, m.created_at) DESC,
                  CASE m.source_type WHEN 'EXPLICIT' THEN 0 ELSE 1 END ASC,
                  m.memory_type ASC, m.created_at DESC, m.id ASC
                LIMIT ?""",
                (fts_query, scope_id, subject_id, at_key, at_key, limit),
            ).fetchall()
            return tuple(row["id"] for row in rows)
        except sqlite3.Error as exc:
            raise IndexUnavailable("FTS query failed") from exc
