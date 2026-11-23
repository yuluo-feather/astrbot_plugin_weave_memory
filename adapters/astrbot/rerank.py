"""Duck-typed AstrBot rerank provider binding (no AstrBot imports)."""

from __future__ import annotations

from typing import Any

from ...core.errors import IndexNotConfigured
from ...core.ports import RerankProvider


class AstrBotRerankProvider(RerankProvider):
    def __init__(self, context: Any, provider_id: str) -> None:
        self.provider_id = provider_id.strip()
        if not self.provider_id:
            raise IndexNotConfigured("rerank_provider_id is empty")
        resolver = getattr(context, "get_provider_by_id", None)
        provider = resolver(self.provider_id) if callable(resolver) else None
        if provider is None or not callable(getattr(provider, "rerank", None)):
            raise IndexNotConfigured("rerank provider is not registered")
        self._provider = provider

    async def rerank(self, query: str, documents: tuple[str, ...],
                     top_n: int) -> tuple[tuple[int, float], ...]:
        value = await self._provider.rerank(query, list(documents))
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
