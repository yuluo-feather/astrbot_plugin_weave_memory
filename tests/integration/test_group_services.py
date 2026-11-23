import asyncio
import json
import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from data.plugins.astrbot_plugin_weave_memory.adapters.astrbot.group_commands import (
    AstrBotGroupCommands,
)
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
from data.plugins.astrbot_plugin_weave_memory.adapters.system import (
    SystemClock,
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
    GroupContextComposer,
    GroupRecallService,
)
from data.plugins.astrbot_plugin_weave_memory.app.recall_service import RecallService
from data.plugins.astrbot_plugin_weave_memory.app.scope_service import (
    GroupScopeService,
    PersonalScopeService,
)
from data.plugins.astrbot_plugin_weave_memory.app.write_coordinator import (
    WriteCoordinator,
)
from data.plugins.astrbot_plugin_weave_memory.core.errors import (
    AuthorizationDenied,
    GroupAdminUnavailable,
    GroupNotAdmin,
    GroupNotCreated,
    InvalidTransition,
    StorageError,
)
from data.plugins.astrbot_plugin_weave_memory.core.model import (
    AdminDecision,
    AdminSource,
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


class FakeAdmins:
    def __init__(self, admin_ids=("admin-1",), source=AdminSource.EXPLICIT_CONFIG):
        self.admin_ids = tuple(admin_ids)
        self.source = source

    def resolve(self, identity, sender_id, event=None):
        allowed = sender_id in self.admin_ids
        return AdminDecision(self.source, self.admin_ids, allowed, bool(self.admin_ids))


def group_identity(group_id="group-1", tenant="instance-1"):
    return Identity("astrbot", tenant, ScopeType.GROUP, group_id,
                    SubjectType.GROUP, group_id)


@pytest.fixture
def group_stack(tmp_path):
    db = SQLiteDatabase(tmp_path / "group.db")
    db.migrate()
    store = SQLiteMemoryStore(db)
    index = SQLiteFTSIndex(db)
    consent_repo = SQLiteConsentStore(db)
    clock = SystemClock()
    ids = UUID4IdGenerator()
    coordinator = WriteCoordinator(store, index)
    admins = FakeAdmins()
    scopes = GroupScopeService(store, admins, coordinator, clock)
    consent = ConsentService(consent_repo, coordinator, clock, ids)
    lifecycle = GroupLifecycleService(
        store, coordinator, consent, index, clock, ids, tmp_path / "backups",
    )
    memories = GroupMemoryService(store, store, coordinator, consent, clock, ids)
    recall = GroupRecallService(store, index, consent, clock, coordinator, store)
    composer = GroupContextComposer(recall)
    personal = PersonalScopeService(store, coordinator, clock)
    personal_recall = RecallService(store, index, consent, clock, coordinator, store)
    yield {
        "db": db, "store": store, "index": index, "scopes": scopes,
        "consent": consent, "lifecycle": lifecycle, "memories": memories,
        "recall": recall, "composer": composer, "admins": admins,
        "personal": personal, "personal_recall": personal_recall,
    }
    db.close()


def make_scope(stack, identity):
    decision = stack["scopes"].admin_decision(identity, "admin-1")
    scope = stack["scopes"].ensure_scope(identity, decision)
    return scope, decision


def test_group_enable_save_recall_disable_clear_destroy(group_stack):
    identity = group_identity()
    scopes = group_stack["scopes"]
    assert scopes.status(identity).created is False

    scope, decision = make_scope(group_stack, identity)
    status = scopes.status(identity)
    assert status.created is True and status.enabled is False
    assert status.admin_source is AdminSource.EXPLICIT_CONFIG

    lifecycle = group_stack["lifecycle"]
    asyncio.run(lifecycle.enable(scope, decision, operator_id="admin-1"))
    assert scopes.status(identity).enabled is True

    memories = group_stack["memories"]
    memory, receipt = asyncio.run(memories.save_group(
        scope, "周五复盘", group_label="agreement",
        operator_id="admin-1", decision=decision,
    ))
    assert memory.memory_type is MemoryType.PREFERENCE
    assert memory.source_type is SourceType.MANUAL
    assert memory.metadata["group_label"] == "agreement"
    assert receipt.index_state.value == "HEALTHY"
    assert [item.id for item in memories.list_group(scope)] == [memory.id]

    composer = group_stack["composer"]
    context = asyncio.run(composer.compose("周五复盘", scope))
    assert context is not None and "周五复盘" in context.content
    assert "weave_memory_group_context" in context.content
    assert "（2026-11-23）" in context.content

    diagnostics = []
    diagnostic_recall = GroupRecallService(
        group_stack["store"], group_stack["index"], group_stack["consent"],
        SystemClock(), WriteCoordinator(group_stack["store"], group_stack["index"]),
        group_stack["store"], diagnostics.append,
    )
    diagnostic_context = asyncio.run(
        GroupContextComposer(diagnostic_recall).compose("周五复盘", scope),
    )
    assert diagnostic_context is not None
    assert diagnostics[-1]["scope"]["type"] == "GROUP"
    assert diagnostics[-1]["final_injected_ids"] == [memory.id]
    assert diagnostics[-1]["trace_id"]
    assert group_stack["index"].search(
        "周五复盘", scope_id=scope.id, subject_id=scope.identity.subject_id,
        scope_type="GROUP",
    ) == (memory.id,)
    assert group_stack["index"].search(
        "周五复盘", scope_id=scope.id, subject_id=scope.identity.subject_id,
        scope_type="PERSONAL",
    ) == ()

    asyncio.run(lifecycle.disable(scope, decision, operator_id="admin-1"))
    with pytest.raises(AuthorizationDenied):
        memories.list_group(scope)
    with pytest.raises(AuthorizationDenied):
        asyncio.run(composer.compose("周五复盘", scope))

    asyncio.run(lifecycle.enable(scope, decision, operator_id="admin-1"))
    assert [item.id for item in memories.list_group(scope)] == [memory.id]
    asyncio.run(lifecycle.clear(scope, decision, operator_id="admin-1"))
    assert memories.list_group(scope) == ()
    asyncio.run(lifecycle.disable(scope, decision, operator_id="admin-1"))

    receipt = asyncio.run(lifecycle.destroy(scope, decision, operator_id="admin-1"))
    assert receipt.backup_path
    assert receipt.backup_sha256
    assert scopes.status(identity).created is False
    group_stack["db"].rollback_to_v5()
    group_stack["db"].rollback_to_v10()
    assert group_stack["db"].schema_version() == 4


def test_group_destroy_backs_up_memory_and_requires_no_group_data_for_rollback(group_stack):
    identity = group_identity()
    scope, decision = make_scope(group_stack, identity)
    lifecycle = group_stack["lifecycle"]
    memories = group_stack["memories"]
    asyncio.run(lifecycle.enable(scope, decision, operator_id="admin-1"))
    memory, _ = asyncio.run(memories.save_group(
        scope, "群约定：周五复盘", group_label="agreement",
        operator_id="admin-1", decision=decision,
    ))

    group_stack["db"].rollback_to_v5()
    with pytest.raises(StorageError):
        group_stack["db"].rollback_to_v10()

    asyncio.run(lifecycle.disable(scope, decision, operator_id="admin-1"))
    receipt = asyncio.run(lifecycle.destroy(scope, decision, operator_id="admin-1"))
    assert receipt.affected_count == 1
    backup = sqlite3.connect(f"file:{receipt.backup_path}?mode=ro", uri=True)
    try:
        assert backup.execute(
            "SELECT count(*) FROM memories WHERE id=?", (memory.id,),
        ).fetchone()[0] == 1
    finally:
        backup.close()
    sidecar = receipt.backup_path.replace(".sqlite3", ".json")
    manifest = json.loads(open(sidecar, encoding="utf-8").read())
    assert manifest["backup_sha256"] == receipt.backup_sha256
    assert manifest["affected_count"] == 1

    group_stack["db"].rollback_to_v10()
    assert group_stack["db"].schema_version() == 4


def test_group_destroy_reuses_existing_backup_and_records_row_counts(group_stack):
    from pathlib import Path

    identity = group_identity()
    scope, decision = make_scope(group_stack, identity)
    lifecycle = group_stack["lifecycle"]
    memories = group_stack["memories"]
    store = group_stack["store"]
    asyncio.run(lifecycle.enable(scope, decision, operator_id="admin-1"))
    asyncio.run(memories.save_group(
        scope, "群约定：周五复盘", group_label="agreement",
        operator_id="admin-1", decision=decision,
    ))
    asyncio.run(lifecycle.disable(scope, decision, operator_id="admin-1"))

    op = "op-retry"
    backups = Path(group_stack["db"].path).parent / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    preset = backups / f"group-{scope.id}-{op}-20260101T000000000000.sqlite3"
    digest = store.backup_group_scope(scope.id, str(preset))

    receipt = asyncio.run(lifecycle.destroy(
        scope, decision, operator_id="admin-1", operation_id=op,
    ))
    assert receipt.backup_path == str(preset)
    assert receipt.backup_sha256 == digest
    backup = sqlite3.connect(f"file:{preset}?mode=ro", uri=True)
    try:
        assert backup.execute(
            "SELECT count(*) FROM memories WHERE scope_id=?", (scope.id,),
        ).fetchone()[0] == 1
    finally:
        backup.close()
    manifest = json.loads(preset.with_suffix(".json").read_text(encoding="utf-8"))
    assert manifest["row_counts"]["memories"] == 1
    assert manifest["row_counts"]["memory_scopes"] == 1


def test_group_clear_then_destroy_rolls_back_and_remigrates(group_stack):
    identity = group_identity()
    scope, decision = make_scope(group_stack, identity)
    lifecycle = group_stack["lifecycle"]
    memories = group_stack["memories"]
    asyncio.run(lifecycle.enable(scope, decision, operator_id="admin-1"))
    asyncio.run(memories.save_group(
        scope, "群约定：周五复盘", group_label="agreement",
        operator_id="admin-1", decision=decision,
    ))
    asyncio.run(lifecycle.disable(scope, decision, operator_id="admin-1"))
    asyncio.run(lifecycle.clear(scope, decision, operator_id="admin-1"))
    receipt = asyncio.run(lifecycle.destroy(scope, decision, operator_id="admin-1"))
    assert receipt.affected_count == 0

    db = group_stack["db"]
    assert db.schema_version() == 6
    db.rollback_to_v5()
    db.rollback_to_v10()
    assert db.schema_version() == 4
    db.migrate()
    assert db.schema_version() == 6


def test_group_enable_does_not_enable_when_grant_fails(group_stack, monkeypatch):
    identity = group_identity()
    scope, decision = make_scope(group_stack, identity)

    def fail(*args, **kwargs):
        raise StorageError("consent write failed")

    monkeypatch.setattr(group_stack["consent"], "grant", fail)
    with pytest.raises(StorageError):
        asyncio.run(group_stack["lifecycle"].enable(
            scope, decision, operator_id="admin-1",
        ))
    assert group_stack["store"].is_scope_enabled(scope.id) is False


def test_group_multiple_admins_can_manage(group_stack):
    group_stack["admins"].admin_ids = ("admin-1", "admin-2")
    identity = group_identity()
    first = group_stack["scopes"].admin_decision(identity, "admin-1")
    second = group_stack["scopes"].admin_decision(identity, "admin-2")
    assert first.allowed is True
    assert second.allowed is True
    scope = group_stack["scopes"].ensure_scope(identity, first)
    asyncio.run(group_stack["lifecycle"].enable(scope, first, operator_id="admin-1"))
    asyncio.run(group_stack["memories"].save_group(
        scope, "管理员二写入", group_label="topic",
        operator_id="admin-2", decision=second,
    ))
    assert group_stack["scopes"].admin_decision(identity, "member").allowed is False


def test_group_member_cannot_manage_or_write(group_stack):
    identity = group_identity()
    scopes = group_stack["scopes"]
    member = scopes.admin_decision(identity, "member")
    assert member.allowed is False
    with pytest.raises(AuthorizationDenied):
        scopes.ensure_scope(identity, member)
    assert scopes.status(identity).created is False

    scope, decision = make_scope(group_stack, identity)
    lifecycle = group_stack["lifecycle"]
    with pytest.raises(AuthorizationDenied):
        asyncio.run(lifecycle.enable(scope, member, operator_id="member"))
    asyncio.run(lifecycle.enable(scope, decision, operator_id="admin-1"))
    with pytest.raises(AuthorizationDenied):
        asyncio.run(group_stack["memories"].save_group(
            scope, "成员写入", group_label="topic",
            operator_id="member", decision=member,
        ))
    with pytest.raises(AuthorizationDenied):
        asyncio.run(lifecycle.disable(scope, member, operator_id="member"))
    with pytest.raises(AuthorizationDenied):
        asyncio.run(lifecycle.clear(scope, member, operator_id="member"))
    with pytest.raises(AuthorizationDenied):
        asyncio.run(lifecycle.destroy(scope, member, operator_id="member"))


def test_group_admin_source_none_fails_closed(group_stack):
    identity = group_identity()
    scope, _ = make_scope(group_stack, identity)
    group_stack["admins"].admin_ids = ()
    group_stack["admins"].source = AdminSource.NONE
    decision = group_stack["scopes"].admin_decision(identity, "admin-1")
    assert decision.source is AdminSource.NONE
    assert decision.allowed is False
    with pytest.raises(AuthorizationDenied):
        asyncio.run(group_stack["lifecycle"].enable(
            scope, decision, operator_id="admin-1",
        ))


def test_group_labels_map_to_memory_types(group_stack):
    identity = group_identity()
    scope, decision = make_scope(group_stack, identity)
    asyncio.run(group_stack["lifecycle"].enable(scope, decision, operator_id="admin-1"))
    expected = {
        "topic": MemoryType.FACT,
        "agreement": MemoryType.PREFERENCE,
        "atmosphere": MemoryType.PREFERENCE,
        "experience": MemoryType.EVENT,
    }
    for index, (label, memory_type) in enumerate(expected.items()):
        memory, _ = asyncio.run(group_stack["memories"].save_group(
            scope, f"{label}-内容-{index}", group_label=label,
            operator_id="admin-1", decision=decision,
        ))
        assert memory.memory_type is memory_type


def test_group_destroy_requires_disabled_scope(group_stack):
    identity = group_identity()
    scope, decision = make_scope(group_stack, identity)
    lifecycle = group_stack["lifecycle"]
    asyncio.run(lifecycle.enable(scope, decision, operator_id="admin-1"))
    asyncio.run(group_stack["memories"].save_group(
        scope, "群约定", group_label="agreement",
        operator_id="admin-1", decision=decision,
    ))
    with pytest.raises(InvalidTransition):
        asyncio.run(lifecycle.destroy(scope, decision, operator_id="admin-1"))


def test_group_admin_change_takes_effect(group_stack):
    identity = group_identity()
    scope, decision = make_scope(group_stack, identity)
    asyncio.run(group_stack["lifecycle"].enable(scope, decision, operator_id="admin-1"))
    group_stack["admins"].admin_ids = ("admin-2",)
    removed = group_stack["scopes"].admin_decision(identity, "admin-1")
    assert removed.allowed is False
    with pytest.raises(AuthorizationDenied):
        asyncio.run(group_stack["memories"].save_group(
            scope, "旧管理员写入", group_label="topic",
            operator_id="admin-1", decision=removed,
        ))
    promoted = group_stack["scopes"].admin_decision(identity, "admin-2")
    assert promoted.allowed is True
    asyncio.run(group_stack["memories"].save_group(
        scope, "新管理员写入", group_label="topic",
        operator_id="admin-2", decision=promoted,
    ))


def test_group_and_personal_memory_do_not_cross(group_stack):
    identity = group_identity()
    scope, decision = make_scope(group_stack, identity)
    asyncio.run(group_stack["lifecycle"].enable(scope, decision, operator_id="admin-1"))
    group_memory, _ = asyncio.run(group_stack["memories"].save_group(
        scope, "群级约定", group_label="agreement",
        operator_id="admin-1", decision=decision,
    ))

    personal_identity = Identity(
        "astrbot", "instance-1", ScopeType.PERSONAL,
        "user-1", SubjectType.USER, "user-1",
    )
    personal_scope = group_stack["personal"].resolve(personal_identity)
    group_stack["consent"].grant(
        personal_scope, PersonalConsentType.PERSONAL_MEMORY, operator_id="user-1",
    )
    now = datetime.now(UTC)
    personal_memory = MemoryEntity(
        id="personal-1", scope_id=personal_scope.id, subject_id="user-1",
        memory_type=MemoryType.FACT, content="个人秘密",
        normalized_content="个人秘密", status=MemoryStatus.ACTIVE,
        source_type=SourceType.EXPLICIT, current_revision=1,
        created_at=now, updated_at=now,
    )
    group_stack["store"].create(
        personal_memory,
        MemoryRevision("rev-personal-1", "personal-1", 1, "个人秘密",
                       MemoryStatus.ACTIVE, now, "created"),
        MemorySource("src-personal-1", "personal-1", SourceType.EXPLICIT,
                     OperatorType.USER, "user-1", now),
    )
    group_stack["index"].sync_memory(personal_memory.id)

    group_context = asyncio.run(group_stack["composer"].compose("个人秘密", scope))
    assert group_context is None
    assert group_stack["personal_recall"].recall("群级约定", personal_scope) == ()
    personal_hits = group_stack["personal_recall"].recall("个人秘密", personal_scope)
    assert [item.id for item in personal_hits] == ["personal-1"]
    assert group_memory.id not in [item.id for item in personal_hits]


def test_group_destroy_without_backup_records_skipped_mode(group_stack):
    identity = group_identity()
    scope, decision = make_scope(group_stack, identity)
    lifecycle = group_stack["lifecycle"]
    asyncio.run(lifecycle.enable(scope, decision, operator_id="admin-1"))
    asyncio.run(group_stack["memories"].save_group(
        scope, "无备份销毁测试", group_label="topic", operator_id="admin-1",
        decision=decision,
    ))
    asyncio.run(lifecycle.disable(scope, decision, operator_id="admin-1"))
    receipt = asyncio.run(lifecycle.destroy(
        scope, decision, operator_id="admin-1", with_backup=False,
    ))
    assert receipt.backup_mode == "SKIPPED"
    assert receipt.backup_path == ""
    assert receipt.backup_sha256 == ""
    backups = Path(group_stack["db"].path).parent / "backups"
    sqlite_backups = list(backups.glob("group-*.sqlite3"))
    assert sqlite_backups == []
    manifests = list(backups.glob("group-*.json"))
    assert len(manifests) == 1
    payload = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert payload["backup_mode"] == "SKIPPED"
    assert payload["backup_path"] is None


def test_cleanup_destroy_backups_keeps_newest(group_stack):
    backups = Path(group_stack["db"].path).parent / "backups"
    backups.mkdir(exist_ok=True)
    old = backups / "group-old-op-20260101T000000000000.sqlite3"
    old.write_bytes(b"old")
    old.with_suffix(".json").write_text("{}", encoding="utf-8")
    os.utime(old, (0, 0))
    os.utime(old.with_suffix(".json"), (0, 0))
    new = backups / "group-new-op-20261123T000000000000.sqlite3"
    new.write_bytes(b"new")
    new.with_suffix(".json").write_text("{}", encoding="utf-8")
    os.utime(new, (time.time(), time.time()))
    removed = group_stack["lifecycle"].cleanup_destroy_backups(30)
    assert removed >= 1
    assert not old.exists()
    assert new.exists()


def test_rollback_preview_reports_group_data_without_modifying(group_stack):
    identity = group_identity()
    scope, decision = make_scope(group_stack, identity)
    asyncio.run(group_stack["lifecycle"].enable(scope, decision, operator_id="admin-1"))
    asyncio.run(group_stack["memories"].save_group(
        scope, "回滚预览测试", group_label="topic", operator_id="admin-1",
        decision=decision,
    ))
    db = group_stack["db"]
    db.rollback_to_v5()
    preview = db.rollback_preview()
    assert preview["schema_version"] == 5
    assert preview["group_scope_count"] >= 1
    assert preview["group_memory_count"] >= 1
    assert preview["can_rollback"] is False
    assert db.rollback_to_v10(dry_run=True) == preview
    assert db.schema_version() == 5


def test_group_command_error_text_distinguishes_admin_reasons():
    unavailable = AstrBotGroupCommands._error_text(
        "群开启", GroupAdminUnavailable("no admin source"),
    )
    not_admin = AstrBotGroupCommands._error_text(
        "群开启", GroupNotAdmin("not admin"),
    )
    not_created = AstrBotGroupCommands._error_text(
        "群开启", GroupNotCreated("not created"),
    )
    assert "group_admin_ids" in unavailable
    assert "不是本群管理员" in not_admin
    assert "尚未创建" in not_created
