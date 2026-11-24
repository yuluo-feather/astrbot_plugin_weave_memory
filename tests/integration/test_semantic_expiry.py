"""ACTIVE rows past valid_until stop being listed and eventually become EXPIRED."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.consents import (
    SQLiteConsentStore,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.database import (
    SQLiteDatabase,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.fts import SQLiteFTSIndex
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.store import (
    SQLiteMemoryStore,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.system import UUID4IdGenerator
from data.plugins.astrbot_plugin_weave_memory.app.consent_service import (
    ConsentService,
    PersonalConsentType,
)
from data.plugins.astrbot_plugin_weave_memory.app.lifecycle_service import (
    LifecycleService,
)
from data.plugins.astrbot_plugin_weave_memory.app.memory_service import (
    MemoryApplicationService,
)
from data.plugins.astrbot_plugin_weave_memory.app.retention_service import (
    RetentionService,
)
from data.plugins.astrbot_plugin_weave_memory.app.scope_service import (
    PersonalScopeService,
)
from data.plugins.astrbot_plugin_weave_memory.app.write_coordinator import (
    WriteCoordinator,
)
from data.plugins.astrbot_plugin_weave_memory.core.model import (
    Identity,
    MemoryEntity,
    MemoryRevision,
    MemorySource,
    MemoryStatus,
    MemoryType,
    OperatorType,
    ScopeType,
    SourceType,
    SubjectType,
)
from data.plugins.astrbot_plugin_weave_memory.core.normalization import (
    normalize_content,
)

START = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


class MutableClock:
    def __init__(self) -> None:
        self.value = START

    def now(self) -> datetime:
        return self.value

    def advance(self, **delta) -> None:
        self.value = self.value + timedelta(**delta)


@pytest.fixture
def stack(tmp_path):
    db = SQLiteDatabase(tmp_path / "expiry.db")
    db.migrate()
    store = SQLiteMemoryStore(db)
    index = SQLiteFTSIndex(db)
    clock = MutableClock()
    ids = UUID4IdGenerator()
    coordinator = WriteCoordinator(store, index)
    scopes = PersonalScopeService(store, coordinator, clock)
    consent = ConsentService(SQLiteConsentStore(db), coordinator, clock, ids)
    lifecycle = LifecycleService(coordinator, store, consent)
    memories = MemoryApplicationService(
        store, coordinator, consent, lifecycle, clock, ids,
    )
    retention = RetentionService(store, lifecycle, clock, ids, interval_seconds=60)
    identity = Identity(
        "astrbot", "default", ScopeType.PERSONAL, "u1", SubjectType.USER, "u1",
    )
    scope = scopes.resolve(identity)
    consent.grant(scope, PersonalConsentType.PERSONAL_MEMORY, operator_id="u1")
    yield SimpleStack(
        db, store, clock, ids, coordinator, scope, memories, retention,
    )
    db.close()


class SimpleStack:
    def __init__(self, db, store, clock, ids, coordinator, scope, memories,
                 retention) -> None:
        self.db = db
        self.store = store
        self.clock = clock
        self.ids = ids
        self.coordinator = coordinator
        self.scope = scope
        self.memories = memories
        self.retention = retention


def _create(stack, content: str, *, valid_until: datetime | None) -> MemoryEntity:
    memory_id = stack.ids.new_id("memory")
    now = stack.clock.now()
    memory = MemoryEntity(
        id=memory_id, scope_id=stack.scope.id, subject_id="u1",
        memory_type=MemoryType.FACT, content=content,
        normalized_content=normalize_content(content), status=MemoryStatus.ACTIVE,
        source_type=SourceType.EXPLICIT, current_revision=1,
        created_at=now, updated_at=now, valid_until=valid_until,
    )
    revision = MemoryRevision(
        id=stack.ids.new_id("revision"), memory_id=memory_id, revision=1,
        content=content, status=MemoryStatus.ACTIVE, changed_at=now,
        change_reason="test",
    )
    source = MemorySource(
        id=stack.ids.new_id("source"), memory_id=memory_id,
        source_type=SourceType.EXPLICIT, operator_type=OperatorType.USER,
        operator_id="u1", captured_at=now,
    )
    asyncio.run(stack.coordinator.create(memory, revision, source))
    return memory


def test_expired_active_row_is_hidden_from_reads_before_the_sweep(stack):
    expired = _create(stack, "这段有效期已过", valid_until=START + timedelta(hours=1))
    keep = _create(stack, "这段仍然有效", valid_until=START + timedelta(days=1))
    stack.clock.advance(hours=2)

    available = stack.memories.list_active(stack.scope)
    assert [item.id for item in available] == [keep.id]
    page, total = stack.memories.list_active_page(stack.scope, limit=10, offset=0)
    assert [item.id for item in page] == [keep.id]
    assert total == 1
    stats = stack.memories.statistics(stack.scope)
    assert stats["by_status"] == {"ACTIVE": 1, "EXPIRED": 1}
    exported = stack.memories.export_personal(stack.scope)
    assert [item["source_memory_id"] for item in exported["memories"]] == [keep.id]
    assert stack.store.get(
        expired.id, scope_id=stack.scope.id, subject_id="u1",
    ).status is MemoryStatus.ACTIVE


def test_retention_sweep_moves_overdue_rows_to_expired(stack):
    overdue = _create(stack, "到期事实", valid_until=START + timedelta(hours=1))
    stack.clock.advance(hours=2)

    assert asyncio.run(stack.retention.expire_overdue()) == 1
    row = stack.store.get(overdue.id, scope_id=stack.scope.id, subject_id="u1")
    assert row.status is MemoryStatus.EXPIRED
    assert asyncio.run(stack.retention.expire_overdue()) == 0


def test_export_all_keeps_truly_expired_rows_but_drops_overdue_active(stack):
    overdue = _create(stack, "过期未扫描", valid_until=START + timedelta(hours=1))
    stack.clock.advance(hours=2)
    payload = stack.memories.export_personal(stack.scope, include_all=True)
    assert payload["memories"] == []
    asyncio.run(stack.retention.expire_overdue())
    payload = stack.memories.export_personal(stack.scope, include_all=True)
    statuses = {
        item["source_memory_id"]: (item["status"], item["content"])
        for item in payload["memories"]
    }
    assert statuses[overdue.id] == ("EXPIRED", None)
