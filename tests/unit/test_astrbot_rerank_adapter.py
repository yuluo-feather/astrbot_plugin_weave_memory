from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from data.plugins.astrbot_plugin_weave_memory.adapters.astrbot.rerank import (
    AstrBotRerankProvider,
)
from data.plugins.astrbot_plugin_weave_memory.core.errors import IndexNotConfigured


class FakeRerankProvider:
    model_name = "bge-reranker-v2-m3"

    def get_model(self) -> str:
        return self.model_name

    async def rerank(self, query, documents):
        return [
            SimpleNamespace(index=index, relevance_score=1.0 - index / 10)
            for index in range(len(documents))
        ]


class LateContext:
    def __init__(self) -> None:
        self.provider = None

    def get_provider_by_id(self, provider_id):
        return self.provider


def test_provider_registered_after_startup_recovers_on_first_call():
    context = LateContext()
    adapter = AstrBotRerankProvider(context, "weave_rerank_local")

    with pytest.raises(IndexNotConfigured):
        asyncio.run(adapter.rerank("q", ("a",), 1))
    assert adapter.model == ""

    context.provider = FakeRerankProvider()
    assert adapter.model == "bge-reranker-v2-m3"
    ranked = asyncio.run(adapter.rerank("q", ("a", "b"), 2))
    assert ranked[0] == (0, 1.0)
    assert len(ranked) == 2


def test_provider_swap_is_picked_up_without_rebuilding_adapter():
    context = LateContext()
    context.provider = FakeRerankProvider()
    adapter = AstrBotRerankProvider(context, "weave_rerank_local")
    assert asyncio.run(adapter.rerank("q", ("a",), 1)) == ((0, 1.0),)

    class SwappedProvider(FakeRerankProvider):
        async def rerank(self, query, documents):
            return [SimpleNamespace(index=0, relevance_score=0.5)]

    context.provider = SwappedProvider()
    assert asyncio.run(adapter.rerank("q", ("a",), 1)) == ((0, 0.5),)
    assert adapter.model == "bge-reranker-v2-m3"


def test_empty_provider_id_is_rejected_at_construction():
    with pytest.raises(IndexNotConfigured):
        AstrBotRerankProvider(LateContext(), "   ")
