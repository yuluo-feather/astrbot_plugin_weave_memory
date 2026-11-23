"""Hybrid FTS + VECTOR retrieval with RRF and optional rerank (Personal V2.0)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from time import monotonic
from uuid import uuid4

from ..core.errors import AuthorizationDenied, DomainError, NotFound
from ..core.model import MemoryEntity, MemoryStatus, Scope
from ..core.model.retrieval_trace import (
    ChannelCandidate,
    ChannelTrace,
    RetrievalTrace,
)
from ..core.policy.query_classifier import classify
from ..core.ports import (
    Clock,
    MemoryStore,
    RerankProvider,
    ScopeStore,
    SearchCandidate,
    SearchIndex,
    VectorIndex,
)
from ..core.retrieval import reciprocal_rank_fusion
from .consent_service import ConsentService, PersonalConsentType
from .provider_policy import ProviderPolicy
from .write_coordinator import WriteCoordinator

logger = logging.getLogger(__name__)


def _filter_reason(candidate: SearchCandidate, now: datetime) -> str:
    if not candidate.scope_enabled:
        return "scope_disabled"
    if candidate.status is not MemoryStatus.ACTIVE:
        return "status_not_active"
    if candidate.valid_from is not None and candidate.valid_from > now:
        return "not_yet_valid"
    if candidate.valid_until is not None and candidate.valid_until <= now:
        return "expired"
    return "eligible"


def _hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()[:16]


class HybridRecallService:
    def __init__(self, store: MemoryStore, index: SearchIndex,
                 consent: ConsentService, clock: Clock,
                 scopes: ScopeStore, coordinator: WriteCoordinator,
                 vector: VectorIndex | None = None,
                 provider: ProviderPolicy | None = None,
                 rerank: RerankProvider | None = None, *,
                 vector_candidate_limit: int = 20,
                 hybrid_candidate_limit: int = 24,
                 classifier_mode: str = "rules",
                 rerank_enabled: bool = False,
                 rerank_timeout_seconds: int = 5,
                 recorder: object | None = None,
                 include_query_text: bool = False) -> None:
        self._store = store
        self._index = index
        self._consent = consent
        self._clock = clock
        self._scopes = scopes
        self._coordinator = coordinator
        self._vector = vector
        self._provider = provider
        self._rerank = rerank
        self._vector_limit = max(1, vector_candidate_limit)
        self._hybrid_limit = max(1, hybrid_candidate_limit)
        self._classifier_mode = classifier_mode
        self._rerank_enabled = rerank_enabled
        self._rerank_timeout = max(1, rerank_timeout_seconds)
        self._recorder = recorder
        self._include_query_text = include_query_text

    async def recall_with_trace(self, query: str, scope: Scope, *,
                                limit: int = 12,
                                ) -> tuple[tuple[MemoryEntity, ...], RetrievalTrace]:
        self._consent.require(scope, PersonalConsentType.PERSONAL_MEMORY)
        now = self._clock.now()
        total_started = monotonic()
        classify_started = monotonic()
        classification = classify(query, mode=self._classifier_mode)
        classify_ms = int((monotonic() - classify_started) * 1000)

        fts_started = monotonic()
        fts_candidates = self._index.search_diagnostics(
            query, scope_id=scope.id, subject_id=scope.identity.subject_id,
            limit=100, scope_type=scope.identity.scope_type.value,
        )
        fts_ms = int((monotonic() - fts_started) * 1000)
        fts_items = self._channel_items(fts_candidates, now)
        fts_ids = [
            item.memory_id for item in fts_items if item.filter_reason == "eligible"
        ]

        vector_items: list[ChannelCandidate] = []
        vector_ids: list[str] = []
        vector_ms = 0
        vector_degraded = False
        vector_error: str | None = None
        provider_status: dict[str, object] = {}
        index_state: dict[str, object] = {}
        if self._vector is not None:
            record = self._vector.status()
            if record is not None:
                index_state = {
                    "target": record.target.value, "status": record.status.value,
                }
            if not self._vector.is_healthy():
                vector_degraded = True
                vector_error = "vector_stale"
            elif self._provider is None or not self._provider.available:
                vector_degraded = True
                vector_error = "provider_unavailable"
            else:
                provider_status = self._provider.status()
                vector_started = monotonic()
                try:
                    query_vector = await self._provider.embed_query(
                        query, scope_type=scope.identity.scope_type.value,
                        scope_id=scope.id,
                    )
                    vector_candidates = self._vector.search_diagnostics(
                        query_vector, scope_id=scope.id,
                        subject_id=scope.identity.subject_id,
                        limit=self._vector_limit, at=now,
                        scope_type=scope.identity.scope_type.value,
                    )
                    vector_items = self._channel_items(vector_candidates, now)
                    vector_ids = [
                        item.memory_id for item in vector_items
                        if item.filter_reason == "eligible"
                    ]
                except DomainError as exc:
                    vector_degraded = True
                    vector_error = type(exc).__name__
                except asyncio.TimeoutError:
                    vector_degraded = True
                    vector_error = "provider_timeout"
                finally:
                    vector_ms = int((monotonic() - vector_started) * 1000)
        else:
            vector_degraded = True
            vector_error = "vector_disabled"

        fuse_started = monotonic()
        channels: dict[str, list[str]] = {"FTS": fts_ids}
        if vector_ids:
            channels["VECTOR"] = vector_ids
        fused = reciprocal_rank_fusion(channels, k=60)
        fuse_ms = int((monotonic() - fuse_started) * 1000)

        selected: list[MemoryEntity] = []
        omitted: list[dict[str, object]] = []
        seen: set[str] = set()
        for candidate in fused.candidates[:self._hybrid_limit]:
            if candidate.memory_id in seen:
                omitted.append({"memory_id": candidate.memory_id, "reason": "duplicate"})
                continue
            seen.add(candidate.memory_id)
            try:
                memory = self._store.get(
                    candidate.memory_id, scope_id=scope.id,
                    subject_id=scope.identity.subject_id,
                )
            except NotFound:
                omitted.append({
                    "memory_id": candidate.memory_id,
                    "reason": "missing_after_search",
                })
                continue
            if memory.status is not MemoryStatus.ACTIVE:
                omitted.append({
                    "memory_id": candidate.memory_id,
                    "reason": "status_not_active",
                })
                continue
            selected.append(memory)
            if len(selected) >= limit:
                break
        if len(fused.candidates) > self._hybrid_limit:
            omitted.append({
                "memory_id": "*", "reason": "budget",
                "count": len(fused.candidates) - self._hybrid_limit,
            })

        rerank_ms = 0
        rerank_info: dict[str, object] = {
            "enabled": self._rerank_enabled, "provider": None,
            "model": None, "candidates": [],
        }
        if self._rerank_enabled and self._rerank is not None and selected:
            rerank_started = monotonic()
            try:
                scores = await asyncio.wait_for(
                    self._rerank.rerank(
                        query, tuple(memory.content for memory in selected),
                        len(selected),
                    ),
                    timeout=self._rerank_timeout,
                )
                by_index = {index: score for index, score in scores}
                ranked = [
                    (by_index.get(index, 0.0), memory)
                    for index, memory in enumerate(selected)
                ]
                ranked.sort(key=lambda pair: (-pair[0], pair[1].id))
                selected = [memory for _, memory in ranked]
                rerank_info["candidates"] = [
                    {"memory_id": memory.id, "score": score}
                    for score, memory in ranked
                ]
            except Exception:  # noqa: BLE001 - rerank is optional
                rerank_info["degraded"] = True
            finally:
                rerank_ms = int((monotonic() - rerank_started) * 1000)

        trace = RetrievalTrace(
            trace_id=uuid4().hex,
            occurred_at=now.astimezone(UTC).isoformat(),
            identity={
                "platform": scope.identity.platform,
                "tenant_hash": _hash(scope.identity.tenant_id),
                "subject_hash": _hash(scope.identity.subject_id),
            },
            scope={"id": scope.id, "type": scope.identity.scope_type.value},
            query_hash=sha256(query.encode("utf-8")).hexdigest(),
            query_length=len(query),
            classifier={
                "mode": classification.mode,
                "query_type": (
                    classification.query_type.value
                    if classification.query_type else None
                ),
                "confidence": classification.confidence,
                "rule_id": classification.rule_id,
            },
            channels=(
                ChannelTrace(
                    name="FTS", enabled=True, limit=100, ok=True,
                    latency_ms=fts_ms, candidates=tuple(fts_items),
                ),
                ChannelTrace(
                    name="VECTOR",
                    enabled=self._vector is not None,
                    limit=self._vector_limit,
                    ok=not vector_degraded,
                    latency_ms=vector_ms,
                    candidates=tuple(vector_items),
                    degraded=vector_degraded,
                    error_code=vector_error,
                ),
            ),
            fusion={
                "method": fused.method, "k": fused.k,
                "candidates": [
                    {
                        "memory_id": item.memory_id, "rank": item.rank,
                        "score": item.score,
                    }
                    for item in fused.candidates[:self._hybrid_limit]
                ],
            },
            rerank=rerank_info,
            final={
                "selected_ids": [memory.id for memory in selected],
                "injected_ids": [],
                "omitted": omitted,
                "estimated_tokens": 0,
                "budget": 0,
            },
            provider=provider_status,
            index_state=index_state,
            latency_ms={
                "classify": classify_ms, "fts": fts_ms, "vector": vector_ms,
                "fuse": fuse_ms, "rerank": rerank_ms,
                "total": int((monotonic() - total_started) * 1000),
            },
            errors=(
                ({"stage": "vector", "code": vector_error},)
                if vector_error else ()
            ),
            query_text=query,
        )
        return tuple(selected), trace

    def _channel_items(self, candidates: tuple[SearchCandidate, ...],
                       now: datetime) -> list[ChannelCandidate]:
        return [
            ChannelCandidate(
                memory_id=candidate.memory_id, rank=rank,
                raw_score=candidate.score, normalized_score=candidate.score,
                filter_reason=_filter_reason(candidate, now),
            )
            for rank, candidate in enumerate(candidates, start=1)
        ]

    def record_diagnostics(self, trace: RetrievalTrace, *,
                           injected_ids: tuple[str, ...] = (),
                           estimated_tokens: int = 0,
                           omitted: tuple[dict[str, object], ...] = ()) -> None:
        final = dict(trace.final)
        final["injected_ids"] = list(injected_ids)
        final["estimated_tokens"] = estimated_tokens
        if omitted:
            final["omitted"] = list(omitted)
        updated = replace(trace, final=final)
        if self._recorder is not None:
            writer = getattr(self._recorder, "write", None)
            if callable(writer):
                writer(updated.to_dict(include_query_text=self._include_query_text))

    async def record_access(self, memory_ids: tuple[str, ...], scope: Scope) -> None:
        def require_access() -> None:
            self._consent.require(scope, PersonalConsentType.PERSONAL_MEMORY)
            if not self._scopes.is_scope_enabled(scope.id):
                raise AuthorizationDenied("personal memory scope is disabled")

        await self._coordinator.record_access(
            memory_ids, scope_id=scope.id,
            subject_id=scope.identity.subject_id, at=self._clock.now(),
            guard=require_access,
        )
        require_access()
