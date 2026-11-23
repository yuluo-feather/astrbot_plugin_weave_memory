"""Provider audit persistence and AstrBot embedding adapter tests."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from data.plugins.astrbot_plugin_weave_memory.adapters.astrbot.embedding import (
    AstrBotEmbeddingProvider,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.database import (
    SQLiteDatabase,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.provider_audit import (
    SQLiteProviderCallAudit,
)
from data.plugins.astrbot_plugin_weave_memory.core.errors import IndexNotConfigured
from data.plugins.astrbot_plugin_weave_memory.core.provider import (
    ProviderCallRecord,
    ProviderCallResult,
    scope_id_hash,
)

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


def test_provider_audit_records_labels_only_and_prunes(tmp_path):
    db = SQLiteDatabase(tmp_path / "audit.db")
    db.migrate()
    audit = SQLiteProviderCallAudit(db)
    audit.record(ProviderCallRecord(
        provider="fake", model="m", scope_type="PERSONAL",
        scope_id_hash=scope_id_hash("raw-scope-id"),
        field_categories=("credential",), result=ProviderCallResult.OK,
        latency_ms=3, created_at=NOW,
    ))
    row = db.connection.execute("SELECT * FROM provider_call_audit").fetchone()
    assert row["scope_id_hash"] != "raw-scope-id"
    assert row["field_categories"] == '["credential"]'
    assert audit.counts_since(NOW - timedelta(days=1)) == {"ok": 1}
    assert audit.prune_before(NOW + timedelta(days=1)) == 1
    assert audit.ensure_available() is True
    db.close()


class HostProvider:
    def __init__(self, dim=2):
        self.calls = 0
        self.dim = dim

    def get_model(self):
        return "host-model"

    def get_dim(self):
        return self.dim

    async def get_embeddings(self, texts):
        self.calls += 1
        return [[1.0] + [0.0] * (self.dim - 1) for _ in texts]


class Context:
    def __init__(self, provider):
        self.provider = provider

    def get_provider_by_id(self, provider_id):
        return self.provider if provider_id == "fake" else None


def test_astrbot_embedding_provider_binds_and_batches():
    host = HostProvider()
    provider = AstrBotEmbeddingProvider(Context(host), "fake", "", batch_size=2)
    assert provider.model == "host-model"
    assert provider.dimension == 2
    vectors = asyncio.run(provider.embed_documents(("a", "b", "c")))
    assert len(vectors) == 3
    assert host.calls == 2


def test_astrbot_embedding_provider_rejects_unknown_or_wrong_type():
    with pytest.raises(IndexNotConfigured):
        AstrBotEmbeddingProvider(Context(HostProvider()), "missing")
    with pytest.raises(IndexNotConfigured):
        AstrBotEmbeddingProvider(Context(object()), "fake")
