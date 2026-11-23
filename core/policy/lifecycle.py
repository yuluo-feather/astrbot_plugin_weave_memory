"""Single source of truth for V0 lifecycle transitions."""

from dataclasses import replace
from datetime import datetime

from ..errors import InvalidTransition
from ..model import MemoryEntity, MemoryStatus, OperatorType, require_aware_utc

# Deliberately conservative. Restore from SUPERSEDED is explicit and creates a revision.
_ALLOWED: dict[MemoryStatus, frozenset[MemoryStatus]] = {
    MemoryStatus.CANDIDATE: frozenset({MemoryStatus.ACTIVE, MemoryStatus.ARCHIVED, MemoryStatus.REJECTED, MemoryStatus.DELETED}),
    MemoryStatus.ACTIVE: frozenset({MemoryStatus.SUPERSEDED, MemoryStatus.ARCHIVED, MemoryStatus.EXPIRED, MemoryStatus.DELETED}),
    MemoryStatus.SUPERSEDED: frozenset({MemoryStatus.ACTIVE, MemoryStatus.ARCHIVED, MemoryStatus.DELETED}),
    MemoryStatus.ARCHIVED: frozenset({MemoryStatus.DELETED}),
    MemoryStatus.EXPIRED: frozenset({MemoryStatus.DELETED}),
    MemoryStatus.REJECTED: frozenset({MemoryStatus.DELETED}),
    MemoryStatus.DELETED: frozenset(),
}


def transition(memory: MemoryEntity, target: MemoryStatus, *, at: datetime,
               superseded_by: str | None = None,
               operator_type: OperatorType | None = None) -> MemoryEntity:
    at = require_aware_utc(at, "at")
    if target not in _ALLOWED[memory.status]:
        raise InvalidTransition(f"{memory.status} -> {target} is not allowed")
    if (
        memory.status in {MemoryStatus.SUPERSEDED, MemoryStatus.CANDIDATE}
        and target is MemoryStatus.ACTIVE
        and operator_type not in {OperatorType.USER, OperatorType.ADMIN}
    ):
        raise InvalidTransition("activating/restoring memory requires explicit USER or ADMIN action")
    if target is MemoryStatus.SUPERSEDED:
        if not superseded_by or superseded_by == memory.id:
            raise InvalidTransition("SUPERSEDED requires a different replacement memory id")
    else:
        superseded_by = None
    return replace(memory, status=target, superseded_by=superseded_by,
                   candidate_expires_at=(
                       memory.candidate_expires_at
                       if target is MemoryStatus.CANDIDATE else None
                   ),
                   current_revision=memory.current_revision + 1, updated_at=at)


def expire_if_due(memory: MemoryEntity, *, at: datetime) -> MemoryEntity:
    """Apply semantic valid_until only to ACTIVE memories."""
    at = require_aware_utc(at, "at")
    if memory.status is MemoryStatus.ACTIVE and memory.valid_until and memory.valid_until <= at:
        return transition(memory, MemoryStatus.EXPIRED, at=at)
    return memory


def archive_expired_candidate(memory: MemoryEntity, *, candidate_expires_at: datetime,
                              at: datetime) -> MemoryEntity:
    """Archive a candidate using its workflow TTL, not semantic valid_until."""
    at = require_aware_utc(at, "at")
    candidate_expires_at = require_aware_utc(candidate_expires_at, "candidate_expires_at")
    if memory.status is MemoryStatus.CANDIDATE and candidate_expires_at <= at:
        return transition(memory, MemoryStatus.ARCHIVED, at=at)
    return memory
