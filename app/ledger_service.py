"""Ledger-first intent protocol and pending replay (V1.8, ADR-0018).

Every destructive action appends an ``intent`` to the independent ledger before
touching the main database, and a ``committed`` event after the main write
succeeds. A pending intent is replayed at startup and before backups, so a crash
between the two stores cannot lose the deletion.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from ..core.errors import DomainError, NotFound, StorageError, ValidationError
from ..core.model import (
    ArchiveReason,
    ConsentState,
    LedgerEvent,
    LedgerEventType,
    LedgerState,
    MemoryConsent,
    OperatorType,
)
from ..core.ports import (
    Clock,
    ConsentRepository,
    IdGenerator,
    LedgerReplayStore,
    LedgerStore,
)


def time_from_payload(value: object, name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"ledger payload {name} is required")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"ledger payload {name} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError(f"ledger payload {name} must be timezone-aware")
    return parsed


def boundaries_from_payload(payload: Mapping[str, object]) -> dict[str, int]:
    raw = payload.get("lineage_boundaries")
    if not isinstance(raw, list):
        raise ValidationError("clear_scope intent requires lineage_boundaries")
    boundaries: dict[str, int] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValidationError("lineage_boundaries entries must be objects")
        lineage = item.get("db_lineage_id")
        boundary = item.get("clear_creation_seq")
        high = item.get("high_watermark")
        if not isinstance(lineage, str) or not lineage.strip():
            raise ValidationError("lineage boundary requires db_lineage_id")
        if type(boundary) is not int or boundary < 0:
            raise ValidationError("lineage boundary requires clear_creation_seq")
        if type(high) is not int or high < boundary:
            raise ValidationError("lineage boundary high_watermark is invalid")
        boundaries[lineage] = boundary
    return boundaries


def lineage_fields_from_payload(payload: Mapping[str, object]) -> tuple[str, int]:
    lineage = payload.get("db_lineage_id")
    creation_seq = payload.get("creation_seq")
    if not isinstance(lineage, str) or not lineage.strip():
        raise ValidationError("ledger event requires db_lineage_id")
    if type(creation_seq) is not int or creation_seq < 1:
        raise ValidationError("ledger event requires creation_seq")
    return lineage, creation_seq


class LedgerService:
    def __init__(self, ledger: LedgerStore, clock: Clock, ids: IdGenerator, *,
                 enabled: bool = True, rotate_events: int = 0) -> None:
        self._ledger = ledger
        self._clock = clock
        self._ids = ids
        self._enabled = enabled
        self._rotate_events = max(0, rotate_events)

    def _maybe_rotate(self) -> None:
        if self._rotate_events and self._ledger.last_seq() % self._rotate_events == 0:
            self._ledger.rotate()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def ledger(self) -> LedgerStore:
        return self._ledger

    def record_intent(self, event_type: LedgerEventType, *, payload: Mapping[str, object],
                      source: str, occurred_at: datetime | None = None,
                      scope_id: str | None = None, subject_id: str | None = None,
                      memory_id: str | None = None,
                      event_id: str | None = None) -> LedgerEvent | None:
        if not self._enabled:
            return None
        event = self._ledger.append(
            event_id=event_id or self._ids.new_id("ledger"),
            event_type=event_type, state=LedgerState.INTENT, source=source,
            occurred_at=occurred_at or self._clock.now(), payload=payload,
            scope_id=scope_id, subject_id=subject_id, memory_id=memory_id,
        )
        self._maybe_rotate()
        return event

    def record_committed(self, intent: LedgerEvent | None, *,
                         occurred_at: datetime | None = None,
                         source: str | None = None) -> LedgerEvent | None:
        if not self._enabled or intent is None:
            return None
        event = self._ledger.append(
            event_id=intent.event_id, event_type=intent.event_type,
            state=LedgerState.COMMITTED, source=source or intent.source,
            occurred_at=occurred_at or self._clock.now(), payload=intent.payload,
            scope_id=intent.scope_id, subject_id=intent.subject_id,
            memory_id=intent.memory_id,
        )
        self._maybe_rotate()
        return event

    def drain_pending(self, store: LedgerReplayStore,
                      consents: ConsentRepository) -> tuple[int, tuple[str, ...]]:
        """Replay pending intents idempotently; return (applied, failed event ids)."""
        if not self._enabled:
            return 0, ()
        applied = 0
        failed: list[str] = []
        for intent in self._ledger.pending_intents():
            try:
                self._apply(intent, store, consents)
            except (DomainError, ValidationError):
                failed.append(intent.event_id)
                continue
            self.record_committed(intent)
            applied += 1
        return applied, tuple(failed)

    @staticmethod
    def _operator(value: object) -> OperatorType:
        try:
            return OperatorType(str(value))
        except ValueError as exc:
            raise ValidationError("ledger payload operator_type is invalid") from exc

    def _apply(self, intent: LedgerEvent, store: LedgerReplayStore,
               consents: ConsentRepository) -> None:
        payload = intent.payload
        if intent.event_type is LedgerEventType.DELETE_MEMORY:
            lineage, creation_seq = lineage_fields_from_payload(payload)
            memory_id = intent.memory_id or payload.get("memory_id")
            scope_id = intent.scope_id
            subject_id = intent.subject_id
            if not isinstance(memory_id, str) or not scope_id or not subject_id:
                raise ValidationError("delete_memory intent is missing identity")
            store.delete(
                memory_id, scope_id=scope_id, subject_id=subject_id,
                operation_id=str(payload.get("operation_id", "")),
                operator_type=self._operator(payload.get("operator_type")),
                operator_id=str(payload.get("operator_id", "")),
                occurred_at=time_from_payload(payload.get("occurred_at"), "occurred_at"),
                expected_lineage=lineage, expected_creation_seq=creation_seq,
                idempotent=True,
            )
            return
        if intent.event_type is LedgerEventType.CLEAR_SCOPE:
            if not intent.scope_id:
                raise ValidationError("clear_scope intent requires scope_id")
            store.clear_scope(
                intent.scope_id,
                operation_id=str(payload.get("operation_id", "")),
                operator_type=self._operator(payload.get("operator_type")),
                operator_id=str(payload.get("operator_id", "")),
                occurred_at=time_from_payload(payload.get("occurred_at"), "occurred_at"),
                boundaries=boundaries_from_payload(payload),
            )
            return
        if intent.event_type is LedgerEventType.CONSENT_CHANGE:
            self._apply_consent(payload, consents)
            return
        if intent.event_type is LedgerEventType.REMOVE_ATTACHMENT:
            lineage, creation_seq = lineage_fields_from_payload(payload)
            memory_id = intent.memory_id or payload.get("memory_id")
            if not isinstance(memory_id, str) or not intent.scope_id or not intent.subject_id:
                raise ValidationError("remove_attachment intent is missing identity")
            # The stored generation must still match; otherwise a reused ID is
            # never allowed to lose an attachment.
            try:
                stored = store.get_lineage(
                    memory_id, scope_id=intent.scope_id,
                    subject_id=intent.subject_id,
                )
            except NotFound:
                return
            if stored != (lineage, creation_seq):
                raise ValidationError("remove_attachment lineage does not match")
            store.remove_attachment(
                memory_id=memory_id, scope_id=intent.scope_id,
                subject_id=intent.subject_id,
                attachment_type=str(payload.get("attachment_type", "")),
                attachment_key=str(payload.get("attachment_key", "")),
            )
            return
        if intent.event_type is LedgerEventType.DESTROY_SCOPE:
            scope_id = intent.scope_id or payload.get("scope_id")
            if not isinstance(scope_id, str) or not scope_id.strip():
                raise ValidationError("destroy_scope intent requires scope_id")
            store.delete_group_scope(
                scope_id,
                operation_id=str(payload.get("operation_id", "")),
                operator_type=OperatorType.ADMIN,
                operator_id=str(payload.get("operator_id", "ledger_replay")),
                occurred_at=time_from_payload(payload.get("occurred_at"), "occurred_at"),
            )
            return
        if intent.event_type is LedgerEventType.ARCHIVE_MEMORY:
            lineage, creation_seq = lineage_fields_from_payload(payload)
            memory_id = intent.memory_id or payload.get("memory_id")
            if not isinstance(memory_id, str) or not intent.scope_id or not intent.subject_id:
                raise ValidationError("archive_memory intent is missing identity")
            try:
                reason = ArchiveReason(str(payload.get("archive_reason")))
            except ValueError as exc:
                raise ValidationError("archive_memory reason is invalid") from exc
            try:
                stored = store.get_lineage(
                    memory_id, scope_id=intent.scope_id,
                    subject_id=intent.subject_id,
                )
            except NotFound:
                return
            if stored != (lineage, creation_seq):
                raise ValidationError("archive_memory lineage does not match")
            store.replay_archive(
                memory_id, scope_id=intent.scope_id,
                archived_at=time_from_payload(payload.get("archived_at"), "archived_at"),
                purge_after=time_from_payload(payload.get("purge_after"), "purge_after"),
                archive_reason=reason,
                occurred_at=time_from_payload(payload.get("occurred_at"), "occurred_at"),
            )
            return
        if intent.event_type is LedgerEventType.RESTORE_MEMORY:
            lineage, creation_seq = lineage_fields_from_payload(payload)
            memory_id = intent.memory_id or payload.get("memory_id")
            if not isinstance(memory_id, str) or not intent.scope_id or not intent.subject_id:
                raise ValidationError("restore_memory intent is missing identity")
            try:
                stored = store.get_lineage(
                    memory_id, scope_id=intent.scope_id,
                    subject_id=intent.subject_id,
                )
            except NotFound:
                return
            if stored != (lineage, creation_seq):
                raise ValidationError("restore_memory lineage does not match")
            store.replay_restore(
                memory_id, scope_id=intent.scope_id,
                occurred_at=time_from_payload(payload.get("occurred_at"), "occurred_at"),
            )
            return
        if intent.event_type is LedgerEventType.PURGE_MEMORY:
            lineage, creation_seq = lineage_fields_from_payload(payload)
            memory_id = intent.memory_id or payload.get("memory_id")
            if not isinstance(memory_id, str) or not intent.scope_id or not intent.subject_id:
                raise ValidationError("purge_memory intent is missing identity")
            store.purge_archived(
                memory_id, scope_id=intent.scope_id, subject_id=intent.subject_id,
                operation_id=str(payload.get("operation_id", "")),
                operator_type=self._operator(payload.get("operator_type")),
                operator_id=str(payload.get("operator_id", "")),
                occurred_at=time_from_payload(payload.get("occurred_at"), "occurred_at"),
                expected_lineage=lineage, expected_creation_seq=creation_seq,
                idempotent=True,
            )
            return
        raise ValidationError("unknown ledger event type")

    @staticmethod
    def _apply_consent(payload: Mapping[str, object],
                       consents: ConsentRepository) -> None:
        scope_id = payload.get("scope_id")
        subject_id = payload.get("subject_id")
        consent_type = payload.get("consent_type")
        state_raw = payload.get("new_state")
        source = payload.get("source")
        consent_id = payload.get("consent_id")
        if not all(isinstance(value, str) and value.strip() for value in (
            scope_id, subject_id, consent_type, state_raw, source, consent_id,
        )):
            raise ValidationError("consent_change intent is missing fields")
        try:
            state = ConsentState(str(state_raw))
        except ValueError as exc:
            raise ValidationError("consent_change new_state is invalid") from exc
        granted_at = payload.get("granted_at")
        revoked_at = payload.get("revoked_at")
        metadata = payload.get("metadata")
        consent = MemoryConsent(
            id=str(consent_id), subject_id=str(subject_id), scope_id=str(scope_id),
            consent_type=str(consent_type), state=state, source=str(source),
            granted_at=time_from_payload(granted_at, "granted_at") if granted_at else None,
            revoked_at=time_from_payload(revoked_at, "revoked_at") if revoked_at else None,
            metadata=metadata if isinstance(metadata, Mapping) else {},
        )
        try:
            consents.record(consent, allow_disabled_grant=True)
        except NotFound:
            # The scope was destroyed later; the consent state is moot.
            return

    def strict_drain(self, store: LedgerReplayStore,
                     consents: ConsentRepository) -> None:
        _applied, failed = self.drain_pending(store, consents)
        if failed:
            raise StorageError(
                f"ledger replay failed for {len(failed)} pending intent(s)",
            ) from None

    def latest_consent_states(self, after: int = 0) -> dict[tuple[str, str, str], LedgerEvent]:
        return self._ledger.latest_by_key(after=after)


def utc_now(clock: Clock) -> datetime:
    return clock.now().astimezone(UTC)
