"""Provider call DTOs and failure taxonomy (no host imports)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from types import MappingProxyType
from typing import Mapping

from .errors import ValidationError


class ProviderCallResult(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    FAILED = "failed"
    SKIPPED = "skipped"


class ProviderFailure(StrEnum):
    TIMEOUT = "provider_timeout"
    RATE_LIMITED = "provider_rate_limited"
    AUTH_FAILED = "provider_auth_failed"
    UNAVAILABLE = "provider_unavailable"
    DIM_MISMATCH = "vector_dim_mismatch"
    AUDIT_FAILED = "provider_audit_failed"


def scope_id_hash(scope_id: str) -> str:
    return sha256(scope_id.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class ProviderCallRecord:
    provider: str
    model: str
    scope_type: str
    scope_id_hash: str
    field_categories: tuple[str, ...]
    result: ProviderCallResult
    latency_ms: int
    created_at: datetime
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.latency_ms < 0:
            raise ValidationError("provider latency must be >= 0")
        if self.created_at.tzinfo is None:
            raise ValidationError("provider audit timestamp must be timezone-aware")
        object.__setattr__(self, "field_categories", tuple(self.field_categories))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
