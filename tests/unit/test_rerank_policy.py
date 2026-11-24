import asyncio
from datetime import UTC, datetime

import pytest
from data.plugins.astrbot_plugin_weave_memory.app.rerank_policy import GuardedRerank
from data.plugins.astrbot_plugin_weave_memory.core.errors import IndexNotConfigured
from data.plugins.astrbot_plugin_weave_memory.core.provider import ProviderCallResult


class FakeClock:
    def now(self):
        return datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


class FakeAudit:
    def __init__(self, available=True):
        self._available = available
        self.records = []

    def ensure_available(self):
        return self._available

    def record(self, record):
        self.records.append(record)


class FakeProvider:
    def __init__(self, endpoint="http://127.0.0.1:9997", model="m"):
        self.endpoint = endpoint
        self.model = model
        self.calls = 0

    async def rerank(self, query, documents, top_n):
        self.calls += 1
        return tuple((index, 1.0) for index in range(len(documents)))


def test_local_provider_runs_and_audits_ok():
    audit = FakeAudit()
    provider = FakeProvider()
    guard = GuardedRerank(
        provider, provider_id="local", audit=audit, clock=FakeClock(),
    )
    result = asyncio.run(guard.rerank("q", ("a", "b"), 2))
    assert len(result) == 2
    assert audit.records[0].result is ProviderCallResult.OK


def test_sensitive_document_is_not_sent():
    audit = FakeAudit()
    provider = FakeProvider()
    guard = GuardedRerank(
        provider, provider_id="local", audit=audit, clock=FakeClock(),
    )
    with pytest.raises(IndexNotConfigured):
        asyncio.run(guard.rerank("q", ("我的密码是 abc123",), 1))
    assert provider.calls == 0
    assert audit.records[0].result is ProviderCallResult.SKIPPED


def test_external_provider_requires_opt_in():
    audit = FakeAudit()
    provider = FakeProvider(endpoint="https://rerank.example.com/v1")
    guard = GuardedRerank(
        provider, provider_id="remote", audit=audit, clock=FakeClock(),
    )
    with pytest.raises(IndexNotConfigured):
        asyncio.run(guard.rerank("q", ("别的内容",), 1))
    assert provider.calls == 0

    allowed = GuardedRerank(
        provider, provider_id="remote", audit=audit, clock=FakeClock(),
        allow_external=True,
    )
    assert len(asyncio.run(allowed.rerank("q", ("别的内容",), 1))) == 1


def test_external_provider_fails_closed_when_audit_unavailable():
    provider = FakeProvider(endpoint="https://rerank.example.com/v1")
    guard = GuardedRerank(
        provider, provider_id="remote", audit=FakeAudit(available=False),
        clock=FakeClock(), allow_external=True,
    )
    with pytest.raises(IndexNotConfigured):
        asyncio.run(guard.rerank("q", ("别的内容",), 1))
    assert provider.calls == 0
