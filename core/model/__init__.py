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


@dataclass(frozen=True, slots=True)
class Scope:
    id: str
    identity: Identity
    created_at: datetime

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValidationError("scope id must be non-empty")
        object.__setattr__(self, "created_at", require_aware_utc(self.created_at, "created_at"))
        if self.identity.scope_type is not ScopeType.PERSONAL:
            raise ValidationError("V0 only enables PERSONAL scopes")


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
    superseded_by: str | None = None
    last_accessed_at: datetime | None = None
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
        for name in ("valid_from", "valid_until", "last_accessed_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, require_aware_utc(value, name))
        if self.valid_from and self.valid_until and self.valid_until <= self.valid_from:
            raise ValidationError("valid_until must be later than valid_from")
        if self.status is MemoryStatus.SUPERSEDED and not self.superseded_by:
            raise ValidationError("SUPERSEDED memory requires superseded_by")
        if self.status is not MemoryStatus.SUPERSEDED and self.superseded_by is not None:
            raise ValidationError("superseded_by is only valid for SUPERSEDED memory")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


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
