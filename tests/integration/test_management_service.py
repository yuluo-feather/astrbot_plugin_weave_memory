from __future__ import annotations

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
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.import_jobs import (
    SQLiteImportJobStore,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.store import (
    SQLiteMemoryStore,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.system import (
    UUID4IdGenerator,
)
from data.plugins.astrbot_plugin_weave_memory.app.consent_service import (
    ConsentService,
    PersonalConsentType,
)
from data.plugins.astrbot_plugin_weave_memory.app.group_lifecycle_service import (
    GroupLifecycleService,
)
from data.plugins.astrbot_plugin_weave_memory.app.group_memory_service import (
    GroupMemoryService,
)
from data.plugins.astrbot_plugin_weave_memory.app.group_recall_service import (
    GroupRecallService,
)
from data.plugins.astrbot_plugin_weave_memory.app.import_service import ImportService
from data.plugins.astrbot_plugin_weave_memory.app.lifecycle_service import (
    LifecycleService,
)
from data.plugins.astrbot_plugin_weave_memory.app.management_service import (
    ManagementService,
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
from data.plugins.astrbot_plugin_weave_memory.core.errors import (
    AuthorizationDenied,
    NotFound,
)
from data.plugins.astrbot_plugin_weave_memory.core.model import (
    AdminDecision,
    AdminSource,
    Identity,
    ScopeType,
    SubjectType,
)

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


class FakeAdmins:
    def __init__(self, admin_ids=("admin-1",)) -> None:
        self.admin_ids = tuple(admin_ids)

    def resolve(self, identity, sender_id, event=None):
        allowed = sender_id in self.admin_ids
        return AdminDecision(
            AdminSource.EXPLICIT_CONFIG, self.admin_ids, allowed, bool(self.admin_ids),
        )


class FixedClock:
    def now(self) -> datetime:
        return NOW


@pytest.fixture
def management_stack(tmp_path):
    db = SQLiteDatabase(tmp_path / "management.db")
    db.migrate()
    store = SQLiteMemoryStore(db)
    index = SQLiteFTSIndex(db)
    clock = FixedClock()
    ids = UUID4IdGenerator()
    coordinator = WriteCoordinator(store, index)
    consent = ConsentService(SQLiteConsentStore(db), coordinator, clock, ids)
    diagnostics: list[dict] = []
    group_scopes = GroupScopeService(store, FakeAdmins(), coordinator, clock)
    group_lifecycle = GroupLifecycleService(
        store, coordinator, consent, index, clock, ids, tmp_path / "backups",
    )
    group_memories = GroupMemoryService(store, store, coordinator, consent, clock, ids)
    group_recall = GroupRecallService(
        store, index, consent, clock, coordinator, store,
        diagnostics.append,
    )
    personal_scopes = PersonalScopeService(store, coordinator, clock)
    lifecycle = LifecycleService(coordinator, store, consent)
    memories = MemoryApplicationService(
        store, coordinator, consent, lifecycle, clock, ids,
    )
    imports = ImportService(
        store, SQLiteImportJobStore(db), coordinator, lifecycle, clock, ids,
    )

    personal_identity = Identity(
        "astrbot", "instance-1", ScopeType.PERSONAL, "u1", SubjectType.USER, "u1",
    )
    personal_scope = personal_scopes.resolve(personal_identity)
    consent.grant(personal_scope, PersonalConsentType.PERSONAL_MEMORY, operator_id="u1")
    asyncio.run(memories.save_explicit(
        personal_scope, "个人秘密内容", operator_id="u1",
    ))

    group_identity = Identity(
        "astrbot", "instance-1", ScopeType.GROUP, "g1", SubjectType.GROUP, "g1",
    )
    decision = group_scopes.admin_decision(group_identity, "admin-1")
    group_scope = group_scopes.ensure_scope(group_identity, decision)
    asyncio.run(group_lifecycle.enable(group_scope, decision, operator_id="admin-1"))
    asyncio.run(group_memories.save_group(
        group_scope, "每周五复盘项目进度", group_label="topic",
        operator_id="admin-1", decision=decision,
    ))
    asyncio.run(group_memories.save_group(
        group_scope, "群内默认使用中文交流", group_label="agreement",
        operator_id="admin-1", decision=decision,
    ))

    service = ManagementService(
        repository=store, store=store, index=index, groups=group_scopes,
        group_recall=group_recall, consent=consent,
        imports=imports, group_memories=group_memories,
        settings={
            "candidate_ttl_days": 7,
            "rejected_keep_body": False,
            "api_admin_whitelist_enabled": False,
        },
        group_max_tokens=192, group_max_memories=2,
    )
    yield {
        "db": db, "store": store, "index": index, "consent": consent,
        "groups": group_scopes, "decision": decision, "service": service,
        "personal_scope": personal_scope, "group_scope": group_scope,
        "diagnostics": diagnostics, "group_recall": group_recall,
        "group_lifecycle": group_lifecycle,
    }
    db.close()


def _group_kwargs(**overrides):
    values = {
        "platform": "astrbot", "tenant_id": "instance-1", "group_id": "g1",
    }
    values.update(overrides)
    return values


def test_overview_is_aggregate_only(management_stack):
    overview = management_stack["service"].overview()
    payload = json.dumps(overview, ensure_ascii=False)
    assert "个人秘密内容" not in payload
    assert "每周五复盘项目进度" not in payload
    assert overview["memories"]["total"] == 3
    assert overview["candidates"]["total"] == 0
    assert overview["index"]["healthy"] is True
    assert "content" not in payload


def test_group_memories_detail_and_history(management_stack):
    service = management_stack["service"]
    listing = service.list_memories(**_group_kwargs())
    assert listing["total"] == 2
    assert {item["memory_type"] for item in listing["items"]} == {"FACT", "PREFERENCE"}
    first = listing["items"][0]
    detail = service.get_memory(**_group_kwargs(), memory_id=first["id"])
    assert detail["content"] == first["content"]
    history = service.memory_history(**_group_kwargs(), memory_id=first["id"])
    assert history["memory"]["id"] == first["id"]
    assert [revision["revision"] for revision in history["revisions"]] == [1]
    assert history["sources"][0]["operator_type"] == "admin"


def test_group_candidates_only_return_candidates(management_stack):
    result = management_stack["service"].list_candidates(**_group_kwargs())
    assert result["total"] == 0
    assert result["items"] == []


def test_disabled_or_unknown_group_is_rejected(management_stack):
    service = management_stack["service"]
    asyncio.run(management_stack["group_lifecycle"].disable(
        management_stack["group_scope"], management_stack["decision"],
        operator_id="admin-1",
    ))
    with pytest.raises(AuthorizationDenied):
        service.list_memories(**_group_kwargs())
    with pytest.raises(NotFound):
        service.list_memories(**_group_kwargs(group_id="missing"))


def test_retrieval_debug_has_no_content_and_no_side_effects(management_stack):
    service = management_stack["service"]
    store = management_stack["store"]
    scope = management_stack["group_scope"]
    before = {
        memory.id: memory.last_accessed_at
        for memory in store.list_active(
            scope_id=scope.id, subject_id=scope.identity.subject_id,
        )
    }
    result = service.retrieval_debug(**_group_kwargs(), query="复盘", limit=8)
    payload = json.dumps(result, ensure_ascii=False)
    assert "每周五复盘项目进度" not in payload
    assert result["candidates"]
    assert all(
        set(candidate) == {"id", "rank", "bm25_score", "filter_reason"}
        for candidate in result["candidates"]
    )
    assert result["injected_ids"]
    assert result["estimated_tokens"] > 0
    assert management_stack["diagnostics"] == []
    after = {
        memory.id: memory.last_accessed_at
        for memory in store.list_active(
            scope_id=scope.id, subject_id=scope.identity.subject_id,
        )
    }
    assert after == before


def test_groups_and_maintenance_are_read_only_views(management_stack):
    service = management_stack["service"]
    groups = service.list_groups()["items"]
    assert groups == [{
        "scope_id": management_stack["group_scope"].id,
        "platform": "astrbot", "tenant_id": "instance-1", "group_id": "g1",
        "enabled": True, "admin_source": "EXPLICIT_CONFIG", "active_count": 2,
        "created_at": groups[0]["created_at"],
    }]
    status = service.maintenance_status()
    assert status["schema_version"] == 9
    assert status["memory_total"] == 3
    assert status["candidate_total"] == 0
    assert status["index"]["healthy"] is True
    assert service.settings() == {
        "candidate_ttl_days": 7,
        "rejected_keep_body": False,
        "api_admin_whitelist_enabled": False,
    }


def test_group_export_and_import_payloads(management_stack):
    stack = management_stack
    exported = stack["service"].export_group_payload(
        platform="astrbot", tenant_id="instance-1", group_id="g1",
        username="admin-1",
    )
    assert exported["schema_version"] == "weave-memory-export-v2"
    assert "每周五复盘项目进度" in {
        item["content"] for item in exported["memories"]
    }

    payload = {
        "schema_version": "weave-memory-export-v2",
        "export_id": "mgmt-exp",
        "source_platform": "astrbot",
        "exported_at": NOW.isoformat(),
        "scope": {
            "platform": "astrbot", "tenant_id": "instance-1",
            "scope_type": "GROUP", "scope_external_id": "g1",
            "subject_type": "GROUP", "subject_id": "g1",
        },
        "memories": [{
            "source_memory_id": "mgmt-src", "source_revision": 1,
            "memory_type": "FACT", "status": "ACTIVE", "content": "管理 API 导入",
            "source_type": "EXPLICIT",
            "created_at": NOW.isoformat(), "updated_at": NOW.isoformat(),
            "valid_from": None, "valid_until": None, "metadata": {},
            "exported_at": NOW.isoformat(),
            "schema_version": "weave-memory-export-v2",
        }],
    }
    result = asyncio.run(stack["service"].import_group_payload(
        platform="astrbot", tenant_id="instance-1", group_id="g1",
        username="admin-1", payload=payload,
    ))
    assert result["imported"] == 1
    after = stack["store"].list_active(
        scope_id=stack["group_scope"].id, subject_id="g1",
    )
    assert "管理 API 导入" in {memory.content for memory in after}


def test_management_import_export_reject_disabled_group(management_stack):
    stack = management_stack
    asyncio.run(stack["group_lifecycle"].disable(
        stack["group_scope"], stack["decision"], operator_id="admin-1",
    ))
    with pytest.raises(AuthorizationDenied):
        stack["service"].export_group_payload(
            platform="astrbot", tenant_id="instance-1", group_id="g1",
            username="admin-1",
        )
    with pytest.raises(AuthorizationDenied):
        asyncio.run(stack["service"].import_group_payload(
            platform="astrbot", tenant_id="instance-1", group_id="g1",
            username="admin-1", payload={},
        ))
