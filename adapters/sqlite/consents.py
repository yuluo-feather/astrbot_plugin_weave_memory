"""Append-only SQLite persistence for consent state changes."""

from __future__ import annotations

import sqlite3

from ...core.errors import AuthorizationDenied, Conflict, NotFound, StorageError
from ...core.model import ConsentState, MemoryConsent
from .database import SQLiteDatabase
from .store import _json_dump, _json_load, _time_dump, _time_load


class SQLiteConsentStore:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.db = database

    def record(self, consent: MemoryConsent,
               *, allow_disabled_grant: bool = False) -> None:
        try:
            with self.db.transaction() as conn:
                scope = conn.execute(
                    "SELECT subject_id, scope_type, is_enabled FROM memory_scopes WHERE id=?",
                    (consent.scope_id,),
                ).fetchone()
                if scope is None:
                    raise NotFound("consent scope not found")
                if scope["subject_id"] != consent.subject_id and not (
                    scope["scope_type"] == "GROUP"
                    and consent.consent_type == "group_personal_member"
                ):
                    raise Conflict("consent subject does not match scope identity")
                if consent.consent_type == "group_personal_member" and scope["scope_type"] != "GROUP":
                    raise Conflict("group personal member consent requires a GROUP scope")
                if (
                    consent.state is ConsentState.GRANTED
                    and scope["is_enabled"] != 1
                    and not allow_disabled_grant
                ):
                    raise AuthorizationDenied("cannot grant consent to a disabled scope")
                existing = conn.execute(
                    "SELECT * FROM memory_consents WHERE id=?", (consent.id,)
                ).fetchone()
                expected = (
                    consent.id, consent.subject_id, consent.scope_id, consent.consent_type,
                    consent.state.value, consent.source, _time_dump(consent.granted_at),
                    _time_dump(consent.revoked_at), _json_dump(consent.metadata),
                )
                if existing is not None:
                    stored = tuple(existing[key] for key in (
                        "id", "subject_id", "scope_id", "consent_type", "state",
                        "source", "granted_at", "revoked_at", "metadata_json",
                    ))
                    if stored == expected:
                        return
                    raise Conflict("consent id already exists with different content")
                conn.execute(
                    """INSERT INTO memory_consents
                    (id, subject_id, scope_id, consent_type, state, source,
                     granted_at, revoked_at, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    expected,
                )
        except (AuthorizationDenied, Conflict, NotFound):
            raise
        except sqlite3.IntegrityError as exc:
            raise Conflict("consent violates a storage constraint") from exc
        except sqlite3.Error as exc:
            raise StorageError("could not persist consent event") from exc

    def current(self, *, subject_id: str, scope_id: str,
                consent_type: str) -> MemoryConsent | None:
        try:
            row = self.db.connection.execute(
                """SELECT * FROM memory_consents
                WHERE subject_id=? AND scope_id=? AND consent_type=?
                ORDER BY rowid DESC LIMIT 1""",
                (subject_id, scope_id, consent_type),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError("could not read consent state") from exc
        if row is None:
            return None
        return MemoryConsent(
            id=row["id"], subject_id=row["subject_id"], scope_id=row["scope_id"],
            consent_type=row["consent_type"], state=ConsentState(row["state"]),
            source=row["source"], granted_at=_time_load(row["granted_at"]),
            revoked_at=_time_load(row["revoked_at"]), metadata=_json_load(row["metadata_json"]),
        )

    def is_granted(self, *, subject_id: str, scope_id: str,
                   consent_type: str) -> bool:
        current = self.current(subject_id=subject_id, scope_id=scope_id,
                               consent_type=consent_type)
        return current is not None and current.state is ConsentState.GRANTED

    def history(self, *, subject_id: str, scope_id: str,
                consent_type: str) -> tuple[MemoryConsent, ...]:
        try:
            rows = self.db.connection.execute(
                """SELECT * FROM memory_consents
                WHERE subject_id=? AND scope_id=? AND consent_type=?
                ORDER BY rowid""",
                (subject_id, scope_id, consent_type),
            ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError("could not read consent history") from exc
        return tuple(MemoryConsent(
            id=row["id"], subject_id=row["subject_id"], scope_id=row["scope_id"],
            consent_type=row["consent_type"], state=ConsentState(row["state"]),
            source=row["source"], granted_at=_time_load(row["granted_at"]),
            revoked_at=_time_load(row["revoked_at"]), metadata=_json_load(row["metadata_json"]),
        ) for row in rows)
