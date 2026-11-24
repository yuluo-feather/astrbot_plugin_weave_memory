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
    """Resolves the host provider per call so plugin load order does not matter.

    An empty id means "use the host's first embedding provider"; a configured
    id is strict and never falls back to a different model.
    """

    def __init__(self, context: Any, provider_id: str = "", model: str = "",
                 *, batch_size: int = 50) -> None:
        self._context = context
        self._provider_id = provider_id.strip()
        self._model_override = model.strip()
        self._batch_size = max(1, int(batch_size))

    def resolve(self) -> Any | None:
        if self._provider_id:
            resolver = getattr(self._context, "get_provider_by_id", None)
            provider = resolver(self._provider_id) if callable(resolver) else None
            return provider if self._is_embedding_provider(provider) else None
        for getter_name in ("get_all_embedding_providers", "get_all_providers"):
            getter = getattr(self._context, getter_name, None)
            if not callable(getter):
                continue
            try:
                values = getter() or []
            except Exception:  # noqa: BLE001 - host API variance
                values = []
            for candidate in values:
                if self._is_embedding_provider(candidate):
                    return candidate
        return None

    @staticmethod
    def _is_embedding_provider(provider: Any) -> bool:
        if provider is None:
            return False
        return callable(getattr(provider, "get_embedding", None)) or callable(
            getattr(provider, "get_embeddings", None),
        )

    @property
    def provider_id(self) -> str:
        if self._provider_id:
            return self._provider_id
        provider = self.resolve()
        if provider is None:
            return "<auto>"
        config = getattr(provider, "provider_config", None)
        if isinstance(config, dict):
            value = config.get("id")
            if isinstance(value, str) and value.strip():
                return value.strip()
        return str(getattr(provider, "provider_id", "") or "<auto>")

    def _require(self) -> Any:
        provider = self.resolve()
        if provider is None:
            raise IndexNotConfigured("embedding provider is not registered")
        return provider

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
        return 0

    @property
    def model(self) -> str:
        provider = self.resolve()
        if provider is None:
            return self._model_override or "unknown"
        return self._model_override or self._provider_model(provider)

    @property
    def dimension(self) -> int:
        provider = self.resolve()
        return self._provider_dimension(provider) if provider is not None else 0

    @property
    def ready(self) -> bool:
        return self.dimension > 0

    async def embed_query(self, text: str) -> tuple[float, ...]:
        vectors = await self.embed_documents((text,))
        return vectors[0]

    async def embed_documents(
        self, texts: tuple[str, ...],
    ) -> tuple[tuple[float, ...], ...]:
        if not texts:
            return ()
        provider = self._require()
        results: list[tuple[float, ...]] = []
        batch_size = self._batch_size
        index = 0
        while index < len(texts):
            chunk = texts[index:index + batch_size]
            try:
                vectors = await self._embed_chunk(provider, chunk)
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

    async def _embed_chunk(self, provider: Any,
                           texts: tuple[str, ...]) -> list[list[float]]:
        batch = getattr(provider, "get_embeddings", None)
        if callable(batch):
            value = await batch(list(texts))
            if isinstance(value, list):
                return value
        single = getattr(provider, "get_embedding", None)
        if not callable(single):
            raise IndexNotConfigured("provider does not expose get_embedding")
        results: list[list[float]] = []
        for text in texts:
            value = await single(text)
            results.append(list(value))
        return results
