"""Duck-typed AstrBot rerank provider binding (no AstrBot imports)."""

from __future__ import annotations

from typing import Any

from ...core.errors import IndexNotConfigured
from ...core.ports import RerankProvider


class AstrBotRerankProvider(RerankProvider):
    """Resolves the host provider per call so plugin load order does not matter.

    AstrBot loads plugins before provider instances are ready, so binding the
    provider in ``__init__`` would leave a permanent ``None`` until the plugin
    is reloaded. Resolving on each call costs one dict lookup and recovers as
    soon as the provider is registered.
    """

    def __init__(self, context: Any, provider_id: str) -> None:
        self.provider_id = provider_id.strip()
        if not self.provider_id:
            raise IndexNotConfigured("rerank_provider_id is empty")
        self._context = context

    def _resolve(self) -> Any:
        resolver = getattr(self._context, "get_provider_by_id", None)
        provider = resolver(self.provider_id) if callable(resolver) else None
        if provider is None or not callable(getattr(provider, "rerank", None)):
            raise IndexNotConfigured("rerank provider is not registered")
        return provider

    @property
    def model(self) -> str:
        try:
            provider = self._resolve()
        except IndexNotConfigured:
            return ""
        getter = getattr(provider, "get_model", None)
        if callable(getter):
            try:
                return str(getter() or "")
            except Exception:  # noqa: BLE001 - metadata only
                return ""
        return str(getattr(provider, "model_name", "") or "")

    async def rerank(self, query: str, documents: tuple[str, ...],
                     top_n: int) -> tuple[tuple[int, float], ...]:
        provider = self._resolve()
        value = await provider.rerank(query, list(documents))
        results: list[tuple[int, float]] = []
        for item in value or ():
            index = getattr(item, "index", None)
            if index is None and isinstance(item, (tuple, list)) and len(item) >= 2:
                index, score = item[0], item[1]
            else:
                score = getattr(item, "relevance_score", None)
            if isinstance(index, int) and isinstance(score, (int, float)):
                results.append((index, float(score)))
        results.sort(key=lambda pair: (-pair[1], pair[0]))
        return tuple(results[:max(1, top_n)])
