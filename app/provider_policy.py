"""Provider data-boundary policy: opt-in, allowlist, sensitivity, audit, cache."""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta
from hashlib import sha256
from time import monotonic

from ..core.errors import (
    IndexNotConfigured,
    SensitiveContentBlocked,
    StorageError,
    VectorDimensionMismatch,
)
from ..core.ports import (
    Clock,
    EmbeddingProvider,
    ProviderCallAuditRepository,
)
from ..core.provider import ProviderCallRecord, ProviderCallResult, scope_id_hash
from .embedding_cache import EmbeddingLRUCache
from .observability import count_metric, emit_event
from .provider_config import ProviderConfig
from .sensitive_filter import detect_sensitive_categories

logger = logging.getLogger(__name__)


class ProviderPolicy:
    def __init__(self, provider: EmbeddingProvider | None,
                 config: ProviderConfig, audit: ProviderCallAuditRepository | None,
                 cache: EmbeddingLRUCache, clock: Clock) -> None:
        self._provider = provider
        self._config = config
        self._audit = audit
        self._cache = cache
        self._clock = clock
        self._failures = 0
        self._cooldown_until: datetime | None = None

    @property
    def available(self) -> bool:
        return self._provider is not None

    @property
    def provider_id(self) -> str:
        return self._provider.provider_id if self._provider is not None else ""

    @property
    def model(self) -> str:
        return self._provider.model if self._provider is not None else ""

    @property
    def dimension(self) -> int:
        if self._provider is None:
            raise IndexNotConfigured("embedding provider is not configured")
        return self._provider.dimension

    @property
    def degraded(self) -> bool:
        return self._cooldown_until is not None and self._cooldown_until > self._clock.now()

    def status(self) -> dict[str, object]:
        provider = self._provider
        return {
            "available": provider is not None,
            "provider_id": provider.provider_id if provider else None,
            "model": provider.model if provider else None,
            "dim": provider.dimension if provider else None,
            "degraded": self.degraded,
            "cache_hits": self._cache.hits,
            "external_opt_in": self._config.external_provider_opt_in,
            "allowlist": list(self._config.external_provider_allowlist),
        }

    def _allowed(self) -> bool:
        provider = self._provider
        if provider is None:
            return False
        if not self._config.external_provider_opt_in:
            return False
        allowlist = self._config.external_provider_allowlist
        if not allowlist:
            return False
        candidates = {
            provider.provider_id,
            provider.model,
            f"{provider.provider_id}/{provider.model}",
        }
        return any(item in candidates for item in allowlist)

    def _check_gates(self) -> None:
        if self._provider is None:
            raise IndexNotConfigured("embedding provider is not configured")
        if not self._config.external_provider_opt_in:
            raise IndexNotConfigured("external provider is not opted in")
        if not self._allowed():
            logger.warning(
                "embedding provider %s is not in external_provider_allowlist",
                self._provider.provider_id,
            )
            raise IndexNotConfigured("embedding provider is not allowlisted")
        if self.degraded:
            raise IndexNotConfigured("embedding provider is cooling down")
        if self._audit is not None and not self._audit.ensure_available():
            raise IndexNotConfigured("provider audit is unavailable")

    def _record(self, *, scope_type: str, scope_id: str,
                categories: tuple[str, ...], result: ProviderCallResult,
                latency_ms: int, model: str) -> None:
        if self._audit is None:
            return
        provider = self._provider
        assert provider is not None
        record = ProviderCallRecord(
            provider=provider.provider_id, model=model,
            scope_type=scope_type, scope_id_hash=scope_id_hash(scope_id),
            field_categories=categories, result=result,
            latency_ms=latency_ms, created_at=self._clock.now(),
        )
        try:
            self._audit.record(record)
        except StorageError:
            self._failures += 1
            self._cooldown_until = (
                self._clock.now()
                + timedelta(seconds=self._config.provider_failure_cooldown_seconds)
            )
            raise IndexNotConfigured("provider audit write failed") from None
        count_metric(
            "weave_provider_calls_total", provider=provider.provider_id,
            result=result.value,
        )

    def _cooldown(self) -> None:
        self._failures += 1
        self._cooldown_until = (
            self._clock.now()
            + timedelta(seconds=self._config.provider_failure_cooldown_seconds)
        )
        emit_event(
            "provider.failed", component="provider", result="failed",
            error_code="provider_unavailable",
        )

    async def embed_query(self, text: str, *, scope_type: str,
                          scope_id: str) -> tuple[float, ...]:
        vectors = await self.embed_documents(
            (text,), scope_type=scope_type, scope_id=scope_id,
        )
        return vectors[0]

    async def embed_documents(
        self, texts: tuple[str, ...], *, scope_type: str, scope_id: str,
    ) -> tuple[tuple[float, ...], ...]:
        self._check_gates()
        provider = self._provider
        assert provider is not None
        max_chars = self._config.embedding_max_text_chars
        truncated = tuple(text[:max_chars] for text in texts)
        categories: set[str] = set()
        for text in truncated:
            detected = detect_sensitive_categories(text)
            if detected:
                categories.update(detected)
        if categories:
            self._record(
                scope_type=scope_type, scope_id=scope_id,
                categories=tuple(sorted(categories)),
                result=ProviderCallResult.SKIPPED, latency_ms=0,
                model=provider.model,
            )
            raise SensitiveContentBlocked("text matched a sensitive category")

        hashes = [sha256(text.encode("utf-8")).hexdigest() for text in truncated]
        cached = [
            self._cache.get(provider.provider_id, provider.model, digest)
            for digest in hashes
        ]
        missing = [
            index for index, vector in enumerate(cached) if vector is None
        ]
        if missing:
            started = monotonic()
            try:
                produced = await asyncio.wait_for(
                    provider.embed_documents(tuple(truncated[index] for index in missing)),
                    timeout=self._config.embedding_timeout_seconds,
                )
            except asyncio.TimeoutError:
                self._cooldown()
                self._record(
                    scope_type=scope_type, scope_id=scope_id,
                    categories=(), result=ProviderCallResult.FAILED,
                    latency_ms=int((monotonic() - started) * 1000),
                    model=provider.model,
                )
                raise IndexNotConfigured("embedding provider timed out") from None
            except Exception as exc:
                self._cooldown()
                self._record(
                    scope_type=scope_type, scope_id=scope_id,
                    categories=(), result=ProviderCallResult.FAILED,
                    latency_ms=int((monotonic() - started) * 1000),
                    model=provider.model,
                )
                raise IndexNotConfigured("embedding provider failed") from exc
            if len(produced) != len(missing):
                self._cooldown()
                raise VectorDimensionMismatch("embedding provider returned wrong batch size")
            for slot, vector in zip(missing, produced):
                if len(vector) != provider.dimension:
                    self._cooldown()
                    raise VectorDimensionMismatch("embedding dimension mismatch")
                if not all(math.isfinite(value) for value in vector):
                    self._cooldown()
                    raise VectorDimensionMismatch("embedding contains non-finite values")
                normalized = tuple(float(value) for value in vector)
                cached[slot] = normalized
                self._cache.put(
                    provider.provider_id, provider.model, hashes[slot], normalized,
                )
            latency = int((monotonic() - started) * 1000)
            self._record(
                scope_type=scope_type, scope_id=scope_id, categories=(),
                result=ProviderCallResult.OK, latency_ms=latency,
                model=provider.model,
            )
        self._failures = 0
        self._cooldown_until = None
        return tuple(vector for vector in cached if vector is not None)
