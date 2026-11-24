"""Duck-typed AstrBot embedding provider binding (no AstrBot imports)."""

from __future__ import annotations

from typing import Any

from ...core.errors import IndexNotConfigured
from ...core.ports import EmbeddingProvider


def _status_code(exc: BaseException) -> int | None:
    for attr in ("status_code", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None) if response is not None else None
    return value if isinstance(value, int) else None


def _is_too_large(exc: BaseException) -> bool:
    if _status_code(exc) == 413:
        return True
    text = str(exc).lower()
    return "413" in text or "batch" in text and "large" in text


class AstrBotEmbeddingProvider(EmbeddingProvider):
    def __init__(self, context: Any, provider_id: str, model: str = "",
                 *, batch_size: int = 50) -> None:
        self.provider_id = provider_id.strip()
        if not self.provider_id:
            raise IndexNotConfigured("embedding_provider_id is empty")
        resolver = getattr(context, "get_provider_by_id", None)
        provider = resolver(self.provider_id) if callable(resolver) else None
        if provider is None:
            raise IndexNotConfigured("embedding provider is not registered")
        if not (callable(getattr(provider, "get_embedding", None))
                or callable(getattr(provider, "get_embeddings", None))):
            raise IndexNotConfigured("provider does not expose embeddings")
        self._provider = provider
        self._batch_size = max(1, int(batch_size))
        self.model = (
            model.strip()
            or self._provider_model(provider)
        )
        self.dimension = self._provider_dimension(provider)

    @staticmethod
    def _provider_model(provider: Any) -> str:
        config = getattr(provider, "provider_config", None)
        if isinstance(config, dict):
            for key in ("embedding_model", "model"):
                value = config.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        getter = getattr(provider, "get_model", None)
        if callable(getter):
            try:
                value = getter()
            except Exception:
                value = None
            if isinstance(value, str) and value.strip():
                return value.strip()
        return "unknown"

    @staticmethod
    def _provider_dimension(provider: Any) -> int:
        getter = getattr(provider, "get_dim", None)
        if callable(getter):
            try:
                value = int(getter())
            except Exception:
                value = 0
            if value > 0:
                return value
        config = getattr(provider, "provider_config", None)
        if isinstance(config, dict):
            for key in ("embedding_dimension", "dimension", "dim"):
                configured = config.get(key)
                if isinstance(configured, int) and configured > 0:
                    return configured
        raise IndexNotConfigured("embedding provider dimension is unavailable")

    async def embed_query(self, text: str) -> tuple[float, ...]:
        vectors = await self.embed_documents((text,))
        return vectors[0]

    async def embed_documents(
        self, texts: tuple[str, ...],
    ) -> tuple[tuple[float, ...], ...]:
        if not texts:
            return ()
        results: list[tuple[float, ...]] = []
        batch_size = self._batch_size
        index = 0
        while index < len(texts):
            chunk = texts[index:index + batch_size]
            try:
                vectors = await self._embed_chunk(chunk)
            except Exception as exc:
                if _is_too_large(exc) and batch_size > 1:
                    batch_size = max(1, batch_size // 2)
                    continue
                raise
            if len(vectors) != len(chunk):
                raise IndexNotConfigured("embedding provider returned a partial batch")
            results.extend(tuple(float(value) for value in vector) for vector in vectors)
            index += len(chunk)
        return tuple(results)

    async def _embed_chunk(self, texts: tuple[str, ...]) -> list[list[float]]:
        batch = getattr(self._provider, "get_embeddings", None)
        if callable(batch):
            value = await batch(list(texts))
            if isinstance(value, list):
                return value
        single = getattr(self._provider, "get_embedding", None)
        if not callable(single):
            raise IndexNotConfigured("provider does not expose get_embedding")
        results: list[list[float]] = []
        for text in texts:
            value = await single(text)
            results.append(list(value))
        return results
