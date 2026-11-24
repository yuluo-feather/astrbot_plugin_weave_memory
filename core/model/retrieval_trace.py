"""ADR-0023 RetrievalTrace v1 DTO (body-free, bounded serialization)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from ..privacy import redact_query

_FILTER_REASONS = frozenset({
    "eligible", "scope_disabled", "consent_missing", "status_not_active",
    "not_yet_valid", "expired", "budget", "duplicate", "missing_after_search",
    "provider_failed",
})
_MAX_CANDIDATES = 100
_MAX_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class ChannelCandidate:
    memory_id: str
    rank: int
    raw_score: float
    normalized_score: float
    filter_reason: str

    def to_dict(self) -> dict[str, object]:
        reason = self.filter_reason if self.filter_reason in _FILTER_REASONS else "provider_failed"
        return {
            "memory_id": self.memory_id, "rank": self.rank,
            "raw_score": round(float(self.raw_score), 6),
            "normalized_score": round(float(self.normalized_score), 6),
            "filter_reason": reason,
        }


@dataclass(frozen=True, slots=True)
class ChannelTrace:
    name: str
    enabled: bool
    limit: int
    ok: bool
    latency_ms: int
    candidates: tuple[ChannelCandidate, ...] = ()
    degraded: bool = False
    error_code: str | None = None

    def to_dict(self) -> dict[str, object]:
        values = [item.to_dict() for item in self.candidates[:_MAX_CANDIDATES]]
        return {
            "name": self.name, "enabled": self.enabled, "limit": self.limit,
            "ok": self.ok, "latency_ms": self.latency_ms,
            "candidates": values, "degraded": self.degraded,
            "error_code": self.error_code,
        }


@dataclass(frozen=True, slots=True)
class RetrievalTrace:
    trace_id: str
    occurred_at: str
    identity: Mapping[str, object]
    scope: Mapping[str, object]
    query_hash: str
    query_length: int
    classifier: Mapping[str, object]
    channels: tuple[ChannelTrace, ...]
    fusion: Mapping[str, object]
    rerank: Mapping[str, object]
    final: Mapping[str, object]
    provider: Mapping[str, object]
    index_state: Mapping[str, object]
    latency_ms: Mapping[str, object]
    errors: tuple[Mapping[str, object], ...] = ()
    query_text: str | None = None
    trace_version: int = 1
    truncated: bool = False
    truncated_channels: tuple[str, ...] = ()

    def to_dict(self, *, include_query_text: bool = False) -> dict[str, Any]:
        channels: list[dict[str, object]] = []
        truncated_channels: list[str] = list(self.truncated_channels)
        for channel in self.channels:
            data = channel.to_dict()
            if len(channel.candidates) > _MAX_CANDIDATES:
                truncated_channels.append(channel.name)
            channels.append(data)
        final = dict(self.final)
        omitted = final.get("omitted")
        if isinstance(omitted, list) and len(omitted) > _MAX_CANDIDATES:
            final["omitted"] = omitted[:_MAX_CANDIDATES]
            truncated_channels.append("omitted")
        query: dict[str, object] = {
            "hash": self.query_hash, "length": self.query_length,
        }
        if include_query_text and self.query_text:
            query["text"] = redact_query(self.query_text)
        payload: dict[str, Any] = {
            "trace_version": self.trace_version,
            "trace_id": self.trace_id,
            "occurred_at": self.occurred_at,
            "identity": dict(self.identity),
            "scope": dict(self.scope),
            "query": query,
            "classifier": dict(self.classifier),
            "channels": channels,
            "fusion": dict(self.fusion),
            "rerank": dict(self.rerank),
            "final": final,
            "provider": dict(self.provider),
            "index_state": dict(self.index_state),
            "latency_ms": dict(self.latency_ms),
            "errors": [dict(item) for item in self.errors],
            "truncated": self.truncated or bool(truncated_channels),
            "truncated_channels": sorted(set(truncated_channels)),
        }
        encoded = json.dumps(payload, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > _MAX_BYTES:
            for channel in payload["channels"]:
                channel["candidates"] = channel["candidates"][:20]
            payload["truncated"] = True
        return payload
