"""Single source of truth for V0 lifecycle transitions."""

from dataclasses import replace
from datetime import datetime, timedelta

from ..errors import InvalidTransition
from ..model import (
    ArchiveReason,
    MemoryEntity,
    MemoryStatus,
    OperatorType,
    require_aware_utc,
)

DEFAULT_ARCHIVE_RETENTION_DAYS = 30

# Deliberately conservative. Restore from SUPERSEDED is explicit and creates a revision.
_ALLOWED: dict[MemoryStatus, frozenset[MemoryStatus]] = {
    MemoryStatus.CANDIDATE: frozenset({MemoryStatus.ACTIVE, MemoryStatus.ARCHIVED, MemoryStatus.REJECTED, MemoryStatus.DELETED}),
    MemoryStatus.ACTIVE: frozenset({MemoryStatus.SUPERSEDED, MemoryStatus.ARCHIVED, MemoryStatus.EXPIRED, MemoryStatus.DELETED}),
    MemoryStatus.SUPERSEDED: frozenset({MemoryStatus.ACTIVE, MemoryStatus.ARCHIVED, MemoryStatus.DELETED}),
    MemoryStatus.ARCHIVED: frozenset({MemoryStatus.ACTIVE, MemoryStatus.DELETED}),
    MemoryStatus.EXPIRED: frozenset({MemoryStatus.DELETED}),
    MemoryStatus.REJECTED: frozenset({MemoryStatus.DELETED}),
    MemoryStatus.DELETED: frozenset(),
}


def transition(memory: MemoryEntity, target: MemoryStatus, *, at: datetime,
               superseded_by: str | None = None,
               operator_type: OperatorType | None = None,
               archive_reason: ArchiveReason | None = None,
               purge_after: datetime | None = None) -> MemoryEntity:
    at = require_aware_utc(at, "at")
    if target not in _ALLOWED[memory.status]:
        raise InvalidTransition(f"{memory.status} -> {target} is not allowed")
    if (
        memory.status in {MemoryStatus.SUPERSEDED, MemoryStatus.CANDIDATE}
        and target is MemoryStatus.ACTIVE
        and operator_type not in {OperatorType.USER, OperatorType.ADMIN}
    ):
        raise InvalidTransition("activating/restoring memory requires explicit USER or ADMIN action")
    if memory.status is MemoryStatus.ARCHIVED and target is MemoryStatus.ACTIVE:
        if memory.archive_reason is not ArchiveReason.USER_ARCHIVE:
            raise InvalidTransition("only USER_ARCHIVE memory can be restored")
        if operator_type not in {OperatorType.USER, OperatorType.ADMIN}:
            raise InvalidTransition("restoring archived memory requires explicit USER or ADMIN action")
    if target is MemoryStatus.SUPERSEDED:
        if not superseded_by or superseded_by == memory.id:
            raise InvalidTransition("SUPERSEDED requires a different replacement memory id")
    else:
        superseded_by = None
    if target is MemoryStatus.ARCHIVED:
        if archive_reason is None:
            raise InvalidTransition("archiving requires an archive_reason")
        if purge_after is None:
            purge_after = at + timedelta(days=DEFAULT_ARCHIVE_RETENTION_DAYS)
        purge_after = require_aware_utc(purge_after, "purge_after")
        if purge_after <= at:
            raise InvalidTransition("purge_after must be later than archived_at")
        archived_at: datetime | None = at
    else:
        archived_at = None
        purge_after = None
        archive_reason = None
    return replace(memory, status=target, superseded_by=superseded_by,
                   candidate_expires_at=(
                       memory.candidate_expires_at
                       if target is MemoryStatus.CANDIDATE else None
                   ),
                   archived_at=archived_at, purge_after=purge_after,
                   archive_reason=archive_reason,
                   current_revision=memory.current_revision + 1, updated_at=at)


def expire_if_due(memory: MemoryEntity, *, at: datetime) -> MemoryEntity:
    """Apply semantic valid_until only to ACTIVE memories."""
    at = require_aware_utc(at, "at")
    if memory.status is MemoryStatus.ACTIVE and memory.valid_until and memory.valid_until <= at:
        return transition(memory, MemoryStatus.EXPIRED, at=at)
    return memory


def archive_expired_candidate(memory: MemoryEntity, *, candidate_expires_at: datetime,
                              at: datetime,
                              purge_after: datetime | None = None) -> MemoryEntity:
    """Archive a candidate using its workflow TTL, not semantic valid_until."""
    at = require_aware_utc(at, "at")
    candidate_expires_at = require_aware_utc(candidate_expires_at, "candidate_expires_at")
    if memory.status is MemoryStatus.CANDIDATE and candidate_expires_at <= at:
        return transition(
            memory, MemoryStatus.ARCHIVED, at=at,
            archive_reason=ArchiveReason.CANDIDATE_EXPIRED,
            purge_after=purge_after,
        )
    return memory
