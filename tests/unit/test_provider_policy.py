"""Provider data-boundary policy tests."""

import asyncio
from datetime import UTC, datetime

import pytest
from data.plugins.astrbot_plugin_weave_memory.app.embedding_cache import (
    EmbeddingLRUCache,
)
from data.plugins.astrbot_plugin_weave_memory.app.provider_config import (
    ProviderConfig,
)
from data.plugins.astrbot_plugin_weave_memory.app.provider_policy import (
    ProviderPolicy,
)
from data.plugins.astrbot_plugin_weave_memory.core.errors import (
    IndexNotConfigured,
    SensitiveContentBlocked,
    StorageError,
)
from data.plugins.astrbot_plugin_weave_memory.core.provider import (
    ProviderCallResult,
)

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


class FixedClock:
    def now(self):
        return NOW


class FakeProvider:
    provider_id = "fake"
    model = "m"
    dimension = 2

    def __init__(self) -> None:
        self.calls = 0
        self.fail_with: BaseException | None = None

    async def embed_query(self, text):
        return (await self.embed_documents((text,)))[0]

    async def embed_documents(self, texts):
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return tuple((1.0, 0.0) for _ in texts)


class FakeAudit:
    def __init__(self) -> None:
        self.records: list[object] = []
        self.fail = False

    def record(self, record):
        if self.fail:
            raise StorageError("audit down")
        self.records.append(record)

    def ensure_available(self):
        return True

    def prune_before(self, cutoff):
        return 0

    def counts_since(self, since):
        return {}


def _config(**overrides):
    values = {
        "external_provider_opt_in": True,
        "external_provider_allowlist": ["fake"],
        "embedding_timeout_seconds": 1,
    }
    values.update(overrides)
    return ProviderConfig.from_mapping(values)


def _policy(provider, audit, config=None):
    return ProviderPolicy(
        provider, config or _config(), audit,
        EmbeddingLRUCache(max_mb=1, ttl_seconds=60), FixedClock(),
    )


def test_opt_in_off_makes_zero_provider_calls():
    provider = FakeProvider()
    audit = FakeAudit()
    policy = _policy(provider, audit, _config(external_provider_opt_in=False))
    with pytest.raises(IndexNotConfigured):
        asyncio.run(policy.embed_query("hello", scope_type="PERSONAL", scope_id="s"))
    assert provider.calls == 0
    assert audit.records == []


def test_empty_or_missed_allowlist_fails_closed():
    provider = FakeProvider()
    audit = FakeAudit()
    for allowlist in ([], ["other"]):
        policy = _policy(
            provider, audit, _config(external_provider_allowlist=allowlist),
        )
        with pytest.raises(IndexNotConfigured):
            asyncio.run(policy.embed_query("hello", scope_type="PERSONAL", scope_id="s"))
    assert provider.calls == 0
    assert audit.records == []


def test_allowlisted_call_is_audited_and_cached():
    provider = FakeProvider()
    audit = FakeAudit()
    policy = _policy(provider, audit)
    first = asyncio.run(
        policy.embed_query("hello", scope_type="PERSONAL", scope_id="scope-raw"),
    )
    second = asyncio.run(
        policy.embed_query("hello", scope_type="PERSONAL", scope_id="scope-raw"),
    )
    assert first == second
    assert provider.calls == 1
    assert len(audit.records) == 1
    record = audit.records[0]
    assert record.result is ProviderCallResult.OK
    assert record.scope_id_hash != "scope-raw"
    assert record.field_categories == ()


def test_sensitive_text_is_not_sent_and_is_audited_as_skipped():
    provider = FakeProvider()
    audit = FakeAudit()
    policy = _policy(provider, audit)
    with pytest.raises(SensitiveContentBlocked):
        asyncio.run(
            policy.embed_query(
                "密码: abc123", scope_type="PERSONAL", scope_id="s",
            ),
        )
    assert provider.calls == 0
    assert audit.records[0].result is ProviderCallResult.SKIPPED
    assert "credential" in audit.records[0].field_categories


def test_timeout_engages_cooldown_and_blocks_repeats():
    provider = FakeProvider()
    provider.fail_with = asyncio.TimeoutError()
    audit = FakeAudit()
    policy = _policy(provider, audit)
    with pytest.raises(IndexNotConfigured):
        asyncio.run(policy.embed_query("hello", scope_type="PERSONAL", scope_id="s"))
    assert provider.calls == 1
    with pytest.raises(IndexNotConfigured):
        asyncio.run(policy.embed_query("hello", scope_type="PERSONAL", scope_id="s"))
    assert provider.calls == 1


def test_audit_write_failure_discards_result_and_cools_down():
    provider = FakeProvider()
    audit = FakeAudit()
    audit.fail = True
    policy = _policy(provider, audit)
    with pytest.raises(IndexNotConfigured):
        asyncio.run(policy.embed_query("hello", scope_type="PERSONAL", scope_id="s"))
    assert provider.calls == 1
    with pytest.raises(IndexNotConfigured):
        asyncio.run(policy.embed_query("hello", scope_type="PERSONAL", scope_id="s"))
    assert provider.calls == 1
