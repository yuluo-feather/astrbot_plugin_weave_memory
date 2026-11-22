import asyncio
import json
from datetime import UTC, datetime

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
from data.plugins.astrbot_plugin_weave_memory.app.export import to_json, to_markdown
from data.plugins.astrbot_plugin_weave_memory.app.group_lifecycle_service import (
    GroupLifecycleService,
)
from data.plugins.astrbot_plugin_weave_memory.app.group_memory_service import (
    GroupMemoryService,
)
from data.plugins.astrbot_plugin_weave_memory.app.lifecycle_service import (
    LifecycleService,
)
from data.plugins.astrbot_plugin_weave_memory.app.memory_service import (
    MemoryApplicationService,
)
from data.plugins.astrbot_plugin_weave_memory.app.scope_service import (
    GroupScopeService,
    PersonalScopeService,
)
from data.plugins.astrbot_plugin_weave_memory.app.write_coordinator import (
    WriteCoordinator,
)
from data.plugins.astrbot_plugin_weave_memory.core.errors import AuthorizationDenied
from data.plugins.astrbot_plugin_weave_memory.core.model import (
    AdminDecision,
    AdminSource,
    Identity,
    MemoryType,
    OperatorType,
    ScopeType,
    SubjectType,
)

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


class FixedClock:
    def now(self):
        return NOW


class FakeAdmins:
    def __init__(self, admin_ids=("admin-1",), source=AdminSource.EXPLICIT_CONFIG):
        self.admin_ids = tuple(admin_ids)
        self.source = source

    def resolve(self, identity, sender_id, event=None):
        return AdminDecision(
            self.source, self.admin_ids, sender_id in self.admin_ids,
            bool(self.admin_ids),
        )


def personal_identity(user_id="u1"):
    return Identity("astrbot", "instance-1", ScopeType.PERSONAL,
                    user_id, SubjectType.USER, user_id)


def group_identity(group_id="group-1"):
    return Identity("astrbot", "instance-1", ScopeType.GROUP,
                    group_id, SubjectType.GROUP, group_id)


@pytest.fixture
def personal_stack(tmp_path):
    db = SQLiteDatabase(tmp_path / "export-personal.db")
    db.migrate()
    store = SQLiteMemoryStore(db)
    index = SQLiteFTSIndex(db)
    clock = FixedClock()
    ids = UUID4IdGenerator()
    coordinator = WriteCoordinator(store, index)
    scopes = PersonalScopeService(store, coordinator, clock)
    consent = ConsentService(SQLiteConsentStore(db), coordinator, clock, ids)
    lifecycle = LifecycleService(coordinator, store, consent)
    memories = MemoryApplicationService(store, coordinator, consent, lifecycle, clock, ids)
    yield db, store, scopes, consent, memories
    db.close()


@pytest.fixture
def group_stack(tmp_path):
    db = SQLiteDatabase(tmp_path / "export-group.db")
    db.migrate()
    store = SQLiteMemoryStore(db)
    index = SQLiteFTSIndex(db)
    clock = FixedClock()
    ids = UUID4IdGenerator()
    coordinator = WriteCoordinator(store, index)
    admins = FakeAdmins()
    scopes = GroupScopeService(store, admins, coordinator, clock)
    consent = ConsentService(SQLiteConsentStore(db), coordinator, clock, ids)
    lifecycle = GroupLifecycleService(
        store, coordinator, consent, index, clock, ids, tmp_path / "backups",
    )
    memories = GroupMemoryService(store, store, coordinator, consent, clock, ids)
    yield {"db": db, "scopes": scopes, "lifecycle": lifecycle, "memories": memories}
    db.close()


def make_group_scope(stack):
    decision = stack["scopes"].admin_decision(group_identity(), "admin-1")
    return stack["scopes"].ensure_scope(group_identity(), decision), decision


def test_personal_export_is_scope_scoped_and_excludes_deleted(personal_stack):
    db, store, scopes, consent, memories = personal_stack
    scope = scopes.resolve(personal_identity())
    consent.grant(scope, PersonalConsentType.PERSONAL_MEMORY, operator_id="u1")

    async def save(text):
        memory, _ = await memories.save_explicit(
            scope, text, operator_id="u1", memory_type=MemoryType.PREFERENCE,
        )
        return memory

    first = asyncio.run(save("喜欢黑咖啡"))
    second = asyncio.run(save("住在东京"))

    other = scopes.resolve(personal_identity("u2"))
    consent.grant(other, PersonalConsentType.PERSONAL_MEMORY, operator_id="u2")
    asyncio.run(memories.save_explicit(other, "别人的秘密", operator_id="u2"))

    payload = memories.export_personal(scope)
    text = to_json(payload)
    assert "喜欢黑咖啡" in text
    assert "住在东京" in text
    assert "别人的秘密" not in text
    assert payload["scope"]["subject_id"] == "u1"
    assert {memory["scope_id"] for memory in payload["memories"]} == {scope.id}

    store.delete(
        first.id, scope_id=scope.id, subject_id="u1",
        operation_id="op-export-delete", operator_type=OperatorType.USER,
        operator_id="u1", occurred_at=NOW,
    )
    after_delete = memories.export_personal(scope)
    assert "喜欢黑咖啡" not in to_json(after_delete)
    assert "住在东京" in to_markdown(after_delete)
    assert second.id in {memory["id"] for memory in after_delete["memories"]}

    consent.revoke(scope, PersonalConsentType.PERSONAL_MEMORY, operator_id="u1")
    with pytest.raises(AuthorizationDenied):
        memories.export_personal(scope)


def test_group_export_requires_admin_and_current_group(group_stack):
    scope, decision = make_group_scope(group_stack)
    lifecycle = group_stack["lifecycle"]
    memories = group_stack["memories"]
    asyncio.run(lifecycle.enable(scope, decision, operator_id="admin-1"))
    asyncio.run(memories.save_group(
        scope, "周五复盘", group_label="agreement",
        operator_id="admin-1", decision=decision,
    ))

    payload = memories.export_group(scope, decision)
    assert "周五复盘" in to_json(payload)
    assert payload["scope"]["scope_type"] == "GROUP"

    denied = AdminDecision(AdminSource.EXPLICIT_CONFIG, ("admin-1",), False, True)
    with pytest.raises(AuthorizationDenied):
        memories.export_group(scope, denied)

    asyncio.run(lifecycle.disable(scope, decision, operator_id="admin-1"))
    with pytest.raises(AuthorizationDenied):
        memories.export_group(scope, decision)


def test_operations_record_result_and_no_content(personal_stack):
    db, store, scopes, consent, memories = personal_stack
    scope = scopes.resolve(personal_identity())
    consent.grant(scope, PersonalConsentType.PERSONAL_MEMORY, operator_id="u1")
    memory, _ = asyncio.run(memories.save_explicit(
        scope, "秘密内容", operator_id="u1", memory_type=MemoryType.FACT,
    ))

    store.delete(
        memory.id, scope_id=scope.id, subject_id="u1",
        operation_id="op-audit-delete", operator_type=OperatorType.USER,
        operator_id="u1", occurred_at=NOW,
    )
    row = db.connection.execute(
        "SELECT operation_type, metadata_json FROM memory_operations WHERE id=?",
        ("op-audit-delete",),
    ).fetchone()
    assert row["operation_type"] == "DELETE_MEMORY"
    metadata = json.loads(row["metadata_json"])
    assert metadata["result"] == "SUCCESS"
    assert "content" not in metadata
    assert "秘密内容" not in row["metadata_json"]
