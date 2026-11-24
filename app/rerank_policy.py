"""Provider boundary and audit wrapper for rerank calls (V2.0 §3.4)."""

from __future__ import annotations

from time import monotonic
from typing import Any
from urllib.parse import urlparse

from ..core.errors import IndexNotConfigured
from ..core.provider import ProviderCallRecord, ProviderCallResult
from .sensitive_filter import KNOWN_CATEGORIES, detect_sensitive_categories


def is_local_endpoint(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return not host or host in {"127.0.0.1", "localhost", "::1"}


class GuardedRerank:
    """Checks the provider boundary, filters sensitive text and audits calls."""

    def __init__(self, provider: Any, *, provider_id: str,
                 audit: Any | None, clock: Any | None,
                 allow_external: bool = False,
                 allowed_categories: tuple[str, ...] = ()) -> None:
        self._provider = provider
        self.provider_id = provider_id
        self._audit = audit
        self._clock = clock
        self._allow_external = allow_external
        self._allowed = frozenset(allowed_categories) & frozenset(KNOWN_CATEGORIES)

    @property
    def model(self) -> str:
        return str(getattr(self._provider, "model", "") or "")

    def _record(self, result: ProviderCallResult, categories: set[str],
                latency_ms: int = 0) -> None:
        if self._audit is None or self._clock is None:
            return
        try:
            self._audit.record(ProviderCallRecord(
                provider=self.provider_id, model=self.model,
                scope_type="RERANK", scope_id_hash="",
                field_categories=tuple(sorted(categories)) or ("text",),
                result=result, latency_ms=latency_ms,
                created_at=self._clock.now(),
            ))
        except Exception:  # noqa: BLE001 - audit must not break chat
            return

    async def rerank(self, query: str, documents: tuple[str, ...],
                     top_n: int) -> tuple[tuple[int, float], ...]:
        detected: set[str] = set()
        for document in documents:
            detected |= detect_sensitive_categories(document)
        blocked = detected - self._allowed
        if blocked:
            self._record(ProviderCallResult.SKIPPED, blocked)
            raise IndexNotConfigured("sensitive content is not sent to rerank")

        endpoint = str(getattr(self._provider, "endpoint", "") or "")
        external = not is_local_endpoint(endpoint)
        if external and not self._allow_external:
            self._record(ProviderCallResult.SKIPPED, detected)
            raise IndexNotConfigured("external rerank provider is not allowed")
        if external and self._audit is not None:
            ensure = getattr(self._audit, "ensure_available", None)
            if callable(ensure) and not ensure():
                raise IndexNotConfigured("provider audit is unavailable")

        started = monotonic()
        try:
            value = await self._provider.rerank(query, documents, top_n)
        except Exception:
            self._record(
                ProviderCallResult.FAILED, detected,
                int((monotonic() - started) * 1000),
            )
            raise
        self._record(
            ProviderCallResult.OK, detected,
            int((monotonic() - started) * 1000),
        )
        return value
