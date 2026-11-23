"""Platform-independent V0 domain model."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from ..errors import ValidationError
from ..normalization import normalize_content


def require_aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


class ScopeType(StrEnum):
    PERSONAL = "PERSONAL"
    GROUP = "GROUP"       # Reserved; not enabled in V0.
    PROJECT = "PROJECT"   # Reserved; not enabled in V0.
    SESSION = "SESSION"   # Reserved; not enabled in V0.


class SubjectType(StrEnum):
    USER = "USER"
    GROUP = "GROUP"
    PROJECT = "PROJECT"
    SESSION = "SESSION"


class MemoryType(StrEnum):
    PREFERENCE = "PREFERENCE"
    FACT = "FACT"
    EVENT = "EVENT"


class MemoryStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    ARCHIVED = "ARCHIVED"
    EXPIRED = "EXPIRED"
    DELETED = "DELETED"
    REJECTED = "REJECTED"


class ArchiveReason(StrEnum):
    USER_ARCHIVE = "USER_ARCHIVE"
    CANDIDATE_EXPIRED = "CANDIDATE_EXPIRED"
    LIFECYCLE = "LIFECYCLE"


class IndexTarget(StrEnum):
    VECTOR = "VECTOR"


class IndexOperation(StrEnum):
    UPSERT = "UPSERT"
    DELETE = "DELETE"


class IndexJobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


class IndexStateStatus(StrEnum):
    HEALTHY = "HEALTHY"
    STALE = "STALE"
    REBUILDING = "REBUILDING"
    FAILED = "FAILED"


class SourceType(StrEnum):
    EXPLICIT = "EXPLICIT"
    INFERRED = "INFERRED"
    OBSERVED = "OBSERVED"
    IMPORTED = "IMPORTED"
    MANUAL = "MANUAL"
    AGENT = "AGENT"


class OperatorType(StrEnum):
    USER = "user"
    AGENT = "agent"
    ADMIN = "admin"
    SYSTEM = "system"


class ConsentState(StrEnum):
    GRANTED = "GRANTED"
    REVOKED = "REVOKED"


class AdminSource(StrEnum):
    PLATFORM_ROLE = "PLATFORM_ROLE"
    EXPLICIT_CONFIG = "EXPLICIT_CONFIG"
    NONE = "NONE"


class LedgerEventType(StrEnum):
    DELETE_MEMORY = "delete_memory"
    CLEAR_SCOPE = "clear_scope"
    CONSENT_CHANGE = "consent_change"
    REMOVE_ATTACHMENT = "remove_attachment"
    DESTROY_SCOPE = "destroy_scope"
    ARCHIVE_MEMORY = "archive_memory"
    RESTORE_MEMORY = "restore_memory"
    PURGE_MEMORY = "purge_memory"
    INDEX_INVALIDATE = "index_invalidate"
    UNKNOWN = "unknown"


class LedgerState(StrEnum):
    INTENT = "intent"
    COMMITTED = "committed"


@dataclass(frozen=True, slots=True)
class LedgerEvent:
    """One append-only ledger record; intent/committed share one event_id."""

    event_id: str
    seq: int
    event_type: LedgerEventType
    state: LedgerState
    source: str
    occurred_at: datetime
    payload: Mapping[str, Any] = field(default_factory=dict)
    scope_id: str | None = None
    subject_id: str | None = None
    memory_id: str | None = None
    raw_event_type: str | None = None

    def __post_init__(self) -> None:
        if not self.event_id.strip() or not self.source.strip():
            raise ValidationError("ledger event identifiers must be non-empty")
        if self.seq < 1:
            raise ValidationError("ledger seq must be >= 1")
        object.__setattr__(
            self, "occurred_at", require_aware_utc(self.occurred_at, "occurred_at"),
        )
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True, slots=True)
class AdminDecision:
    source: AdminSource
    admin_ids: tuple[str, ...]
    allowed: bool
    admin_available: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "admin_ids", tuple(self.admin_ids))
        if any(not admin_id.strip() for admin_id in self.admin_ids):
            raise ValidationError("admin ids must be non-empty")
        if self.source is AdminSource.NONE and self.admin_ids:
            raise ValidationError("NONE admin source cannot carry admin ids")
        if self.allowed and not self.admin_ids:
            raise ValidationError("allowed admin decision requires at least one admin")


@dataclass(frozen=True, slots=True)
class Identity:
    platform: str
    tenant_id: str
    scope_type: ScopeType
    scope_external_id: str
    subject_type: SubjectType
    subject_id: str

    def __post_init__(self) -> None:
        for name in ("platform", "tenant_id", "scope_external_id", "subject_id"):
            value = getattr(self, name)
            if not value or not value.strip():
                raise ValidationError(f"{name} must be non-empty")
        if self.scope_type is ScopeType.PERSONAL and self.subject_type is not SubjectType.USER:
            raise ValidationError("PERSONAL scope requires USER subject")
        if self.scope_type is ScopeType.GROUP and self.subject_type is not SubjectType.GROUP:
            raise ValidationError("GROUP scope requires GROUP subject")


@dataclass(frozen=True, slots=True)
class Scope:
    id: str
    identity: Identity
    created_at: datetime

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValidationError("scope id must be non-empty")
        object.__setattr__(self, "created_at", require_aware_utc(self.created_at, "created_at"))
        if self.identity.scope_type not in {ScopeType.PERSONAL, ScopeType.GROUP}:
            raise ValidationError("V1.1 only enables PERSONAL and GROUP scopes")


@dataclass(frozen=True, slots=True)
class MemorySource:
    id: str
    memory_id: str
    source_type: SourceType
    operator_type: OperatorType
    operator_id: str
    captured_at: datetime
    evidence_ref: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("id", "memory_id", "operator_id"):
            if not getattr(self, name).strip():
                raise ValidationError(f"{name} must be non-empty")
        object.__setattr__(self, "captured_at", require_aware_utc(self.captured_at, "captured_at"))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        if self.source_type is SourceType.EXPLICIT and self.operator_type is not OperatorType.USER:
            raise ValidationError("EXPLICIT source must be operated by USER")


@dataclass(frozen=True, slots=True)
class MemoryRevision:
    id: str
    memory_id: str
    revision: int
    content: str
    status: MemoryStatus
    changed_at: datetime
    change_reason: str
    snapshot: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise ValidationError("revision must be >= 1")
        if not self.id.strip() or not self.memory_id.strip():
            raise ValidationError("revision identifiers must be non-empty")
        if not self.change_reason.strip():
            raise ValidationError("change_reason must be non-empty")
        object.__setattr__(self, "changed_at", require_aware_utc(self.changed_at, "changed_at"))
        object.__setattr__(self, "snapshot", MappingProxyType(dict(self.snapshot)))


@dataclass(frozen=True, slots=True)
class MemoryOperation:
    """Audit-only operation record; metadata must never contain memory bodies."""

    id: str
    scope_id: str
    memory_id: str | None
    operation_type: str
    operator_type: OperatorType
    operator_id: str
    occurred_at: datetime
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("id", "scope_id", "operation_type", "operator_id"):
            if not getattr(self, name).strip():
                raise ValidationError(f"{name} must be non-empty")
        object.__setattr__(
            self, "occurred_at", require_aware_utc(self.occurred_at, "occurred_at"),
        )
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class MemoryEntity:
    id: str
    scope_id: str
    subject_id: str
    memory_type: MemoryType
    content: str
    normalized_content: str
    status: MemoryStatus
    source_type: SourceType
    current_revision: int
    created_at: datetime
    updated_at: datetime
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    candidate_expires_at: datetime | None = None
    superseded_by: str | None = None
    last_accessed_at: datetime | None = None
    archived_at: datetime | None = None
    purge_after: datetime | None = None
    archive_reason: ArchiveReason | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("id", "scope_id", "subject_id", "content"):
            value = getattr(self, name)
            if not value or not value.strip():
                raise ValidationError(f"{name} must be non-empty")
        if self.current_revision < 1:
            raise ValidationError("current_revision must be >= 1")
        expected = normalize_content(self.content)
        if not expected:
            raise ValidationError("content is empty after normalization")
        if self.normalized_content != expected:
            raise ValidationError("normalized_content does not match normalization v1")
        for name in ("created_at", "updated_at"):
            object.__setattr__(self, name, require_aware_utc(getattr(self, name), name))
        for name in ("valid_from", "valid_until", "candidate_expires_at", "last_accessed_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, require_aware_utc(value, name))
        for name in ("archived_at", "purge_after"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, require_aware_utc(value, name))
        if self.valid_from and self.valid_until and self.valid_until <= self.valid_from:
            raise ValidationError("valid_until must be later than valid_from")
        if self.candidate_expires_at is not None and self.status is not MemoryStatus.CANDIDATE:
            raise ValidationError("candidate_expires_at is only valid for CANDIDATE memory")
        archive_fields = (self.archived_at, self.purge_after, self.archive_reason)
        if self.status is MemoryStatus.ARCHIVED:
            if any(value is None for value in archive_fields):
                raise ValidationError("ARCHIVED memory requires archived_at, purge_after and archive_reason")
            assert self.archived_at is not None and self.purge_after is not None
            if self.purge_after <= self.archived_at:
                raise ValidationError("purge_after must be later than archived_at")
        elif any(value is not None for value in archive_fields):
            raise ValidationError("archive fields are only valid for ARCHIVED memory")
        if self.status is MemoryStatus.SUPERSEDED and not self.superseded_by:
            raise ValidationError("SUPERSEDED memory requires superseded_by")
        if self.status is not MemoryStatus.SUPERSEDED and self.superseded_by is not None:
            raise ValidationError("superseded_by is only valid for SUPERSEDED memory")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class IndexJob:
    id: str
    memory_id: str
    target: IndexTarget
    operation: IndexOperation
    content_hash: str
    status: IndexJobStatus
    attempts: int
    created_at: datetime
    updated_at: datetime
    last_error: str | None = None

    def __post_init__(self) -> None:
        for name in ("id", "memory_id", "content_hash"):
            if not getattr(self, name).strip():
                raise ValidationError(f"{name} must be non-empty")
        if self.attempts < 0:
            raise ValidationError("attempts must be >= 0")
        for name in ("created_at", "updated_at"):
            object.__setattr__(self, name, require_aware_utc(getattr(self, name), name))


@dataclass(frozen=True, slots=True)
class IndexStateRecord:
    target: IndexTarget
    status: IndexStateStatus
    updated_at: datetime
    dim: int | None = None
    config_hash: str | None = None

    def __post_init__(self) -> None:
        if self.dim is not None and self.dim < 1:
            raise ValidationError("index dimension must be >= 1")
        object.__setattr__(self, "updated_at", require_aware_utc(self.updated_at, "updated_at"))


class ExtractionJobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    SKIPPED_DUPLICATE_ACTIVE = "SKIPPED_DUPLICATE_ACTIVE"
    FAILED = "FAILED"
    # TODO(V2): reserved by the contract and the SQLite CHECK constraint for
    # manual cancellation; no producer exists yet. Do not remove as dead code.
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class ExtractionJob:
    id: str
    platform: str
    tenant_id: str
    scope_id: str
    subject_id: str
    conversation_id: str
    watermark: int
    content_hash: str
    status: ExtractionJobStatus
    attempts: int
    created_at: datetime
    updated_at: datetime
    last_error: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "id", "platform", "tenant_id", "scope_id", "subject_id",
            "conversation_id", "content_hash",
        ):
            value = getattr(self, name)
            if not value or not value.strip():
                raise ValidationError(f"{name} must be non-empty")
        if self.watermark < 0:
            raise ValidationError("watermark must be non-negative")
        if self.attempts < 0:
            raise ValidationError("attempts must be non-negative")
        for name in ("created_at", "updated_at"):
            object.__setattr__(self, name, require_aware_utc(getattr(self, name), name))
        if self.updated_at < self.created_at:
            raise ValidationError("updated_at must not be earlier than created_at")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class ExtractionWindow:
    scope_id: str
    subject_id: str
    conversation_id: str
    watermark: int
    content_hash: str
    messages: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("scope_id", "subject_id", "conversation_id", "content_hash"):
            value = getattr(self, name)
            if not value or not value.strip():
                raise ValidationError(f"{name} must be non-empty")
        if self.watermark < 0:
            raise ValidationError("watermark must be non-negative")
        messages = tuple(self.messages)
        if not messages:
            raise ValidationError("extraction window requires at least one message")
        if any(not message.strip() for message in messages):
            raise ValidationError("extraction window messages must be non-empty")
        object.__setattr__(self, "messages", messages)


@dataclass(frozen=True, slots=True)
class ExtractionCandidate:
    memory_type: MemoryType
    content: str
    confidence: float | None = None
    source_message_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValidationError("candidate content must be non-empty")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValidationError("confidence must be between 0 and 1")
        source_ids = tuple(self.source_message_ids)
        if any(not message_id.strip() for message_id in source_ids):
            raise ValidationError("source_message_ids must be non-empty")
        object.__setattr__(self, "source_message_ids", source_ids)


class ImportJobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    # TODO(V2): reserved by CONTRACTS/ADR-0017 and the SQLite CHECK; the
    # importer reports per-entry skips but never sets this job status.
    SKIPPED = "SKIPPED"


@dataclass(frozen=True, slots=True)
class ImportJob:
    id: str
    scope_id: str
    subject_id: str
    file_sha256: str
    dataset_version: str
    status: ImportJobStatus
    cursor: int
    created_memory_ids: tuple[str, ...]
    created_at: datetime
    updated_at: datetime
    last_error: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "id", "scope_id", "subject_id", "file_sha256", "dataset_version",
        ):
            if not getattr(self, name).strip():
                raise ValidationError(f"{name} must be non-empty")
        if self.cursor < 0:
            raise ValidationError("cursor must be non-negative")
        created_ids = tuple(self.created_memory_ids)
        if any(not memory_id.strip() for memory_id in created_ids):
            raise ValidationError("created_memory_ids must be non-empty")
        object.__setattr__(self, "created_memory_ids", created_ids)
        for name in ("created_at", "updated_at"):
            object.__setattr__(self, name, require_aware_utc(getattr(self, name), name))
        if self.updated_at < self.created_at:
            raise ValidationError("updated_at must not be earlier than created_at")


@dataclass(frozen=True, slots=True)
class MemoryConsent:
    id: str
    subject_id: str
    scope_id: str
    consent_type: str
    state: ConsentState
    source: str
    granted_at: datetime | None = None
    revoked_at: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("id", "subject_id", "scope_id", "consent_type", "source"):
            if not getattr(self, name).strip():
                raise ValidationError(f"{name} must be non-empty")
        if self.granted_at is not None:
            object.__setattr__(self, "granted_at", require_aware_utc(self.granted_at, "granted_at"))
        if self.revoked_at is not None:
            object.__setattr__(self, "revoked_at", require_aware_utc(self.revoked_at, "revoked_at"))
        if self.state is ConsentState.GRANTED and self.granted_at is None:
            raise ValidationError("GRANTED consent requires granted_at")
        if self.state is ConsentState.REVOKED and self.revoked_at is None:
            raise ValidationError("REVOKED consent requires revoked_at")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
