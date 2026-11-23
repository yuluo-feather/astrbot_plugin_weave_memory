"""V1.8 ledger-first deletion and backup/restore acceptance tests."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.backup import (
    BackupService,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.consents import (
    SQLiteConsentStore,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.database import (
    SQLiteDatabase,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.fts import SQLiteFTSIndex
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.ledger import SQLiteLedger
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.store import (
    SQLiteMemoryStore,
)
from data.plugins.astrbot_plugin_weave_memory.app.consent_service import (
    ConsentService,
    GroupConsentType,
    PersonalConsentType,
)
from data.plugins.astrbot_plugin_weave_memory.app.group_lifecycle_service import (
    GroupLifecycleService,
)
from data.plugins.astrbot_plugin_weave_memory.app.group_memory_service import (
    GroupMemoryService,
)
from data.plugins.astrbot_plugin_weave_memory.app.ledger_service import LedgerService
from data.plugins.astrbot_plugin_weave_memory.app.lifecycle_service import (
    LifecycleService,
)
from data.plugins.astrbot_plugin_weave_memory.app.memory_service import (
    MemoryApplicationService,
)
from data.plugins.astrbot_plugin_weave_memory.app.scope_service import (
    PersonalScopeService,
)
from data.plugins.astrbot_plugin_weave_memory.app.write_coordinator import (
    WriteCoordinator,
)
from data.plugins.astrbot_plugin_weave_memory.core.errors import (
    Conflict,
    StorageError,
)
from data.plugins.astrbot_plugin_weave_memory.core.model import (
    AdminDecision,
    AdminSource,
    Identity,
    LedgerEventType,
    LedgerState,
    MemoryStatus,
    OperatorType,
    Scope,
    ScopeType,
    SubjectType,
)

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


class FixedClock:
    def __init__(self) -> None:
        self.value = NOW

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: int = 1) -> None:
        self.value = self.value + timedelta(seconds=seconds)


class SeqIds:
    def __init__(self) -> None:
        self._next = 0

    def new_id(self, entity_kind: str) -> str:
        self._next += 1
        return f"{entity_kind}-{self._next:04d}"


def _personal_identity() -> Identity:
    return Identity(
        "astrbot", "default", ScopeType.PERSONAL, "u1", SubjectType.USER, "u1",
    )


def _group_identity() -> Identity:
    return Identity(
        "astrbot", "default", ScopeType.GROUP, "g1", SubjectType.GROUP, "g1",
    )


def _build(tmp_path, bus, *, data_dir=None, ledger_enabled=True):
    db = SQLiteDatabase(tmp_path / f"main-{bus}.sqlite3")
    db.migrate()
    store = SQLiteMemoryStore(db)
    index = SQLiteFTSIndex(db)
    ledger_store = SQLiteLedger(tmp_path / "memory_ledger.sqlite3")
    clock = FixedClock()
    ids = SeqIds()
    ledger = LedgerService(ledger_store, clock, ids, enabled=ledger_enabled)
    coordinator = WriteCoordinator(store, index, ledger)
    scopes = PersonalScopeService(store, coordinator, clock)
    consent_repo = SQLiteConsentStore(db)
    consent = ConsentService(consent_repo, coordinator, clock, ids)
    lifecycle = LifecycleService(coordinator, store, consent)
    memories = MemoryApplicationService(
        store, coordinator, consent, lifecycle, clock, ids,
    )
    group_memories = GroupMemoryService(
        store, store, coordinator, consent, clock, ids,
    )
    group_lifecycle = GroupLifecycleService(
        store, coordinator, consent, index, clock, ids,
        tmp_path / "group_backups", ledger=ledger,
    )
    backup = BackupService(
        database=db, store=store, index=index, consents=consent_repo,
        coordinator=coordinator, ledger=ledger, clock=clock, ids=ids,
        data_dir=data_dir or tmp_path,
        config={"api_token": "secret", "nested": {"password": "x", "keep": 1}},
        keep_last=5, retention_days=30, enabled=ledger_enabled,
    )
    return SimpleNamespace(
        db=db, store=store, index=index, ledger_store=ledger_store,
        clock=clock, ids=ids, ledger=ledger, coordinator=coordinator,
        scopes=scopes, consent_repo=consent_repo, consent=consent,
        lifecycle=lifecycle, memories=memories, group_memories=group_memories,
        group_lifecycle=group_lifecycle, backup=backup,
    )


@pytest.fixture
def stack(tmp_path):
    built = _build(tmp_path, "primary")
    scope = built.scopes.resolve(_personal_identity())
    built.scope = scope
    built.consent.grant(
        scope, PersonalConsentType.PERSONAL_MEMORY, operator_id="u1",
    )
    yield built
    built.ledger_store.close()
    built.db.close()


def save(stack, content: str = "喜欢黑咖啡"):
    memory, _ = asyncio.run(
        stack.memories.save_explicit(stack.scope, content, operator_id="u1"),
    )
    return memory


def delete(stack, memory_id: str) -> None:
    asyncio.run(stack.lifecycle.delete_memory(
        memory_id, scope_id=stack.scope.id, subject_id="u1",
        operation_id=stack.ids.new_id("operation"),
        operator_type=OperatorType.USER, operator_id="u1", at=stack.clock.now(),
    ))


def clear(stack) -> None:
    asyncio.run(stack.lifecycle.clear_scope(
        stack.scope.id, operation_id=stack.ids.new_id("operation"),
        operator_type=OperatorType.USER, operator_id="u1", at=stack.clock.now(),
    ))


def make_backup(stack) -> str:
    return str(stack.backup.create_backup()["backup_id"])


def row_count(stack, table: str, where: str = "", params: tuple = ()) -> int:
    sql = f"SELECT count(*) FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return int(stack.db.connection.execute(sql, params).fetchone()[0])


def memory_ids(stack) -> set[str]:
    return {
        row[0] for row in stack.db.connection.execute("SELECT id FROM memories")
    }


def test_v9_schema_allocates_lineage_scoped_sequence(stack):
    first = save(stack, "第一条")
    second = save(stack, "第二条")
    rows = stack.db.connection.execute(
        "SELECT id, db_lineage_id, creation_seq FROM memories ORDER BY creation_seq",
    ).fetchall()
    assert [row["id"] for row in rows] == [first.id, second.id]
    assert [row["creation_seq"] for row in rows] == [1, 2]
    assert {row["db_lineage_id"] for row in rows} == {stack.store.current_lineage_id()}
    high = stack.db.connection.execute(
        "SELECT high_watermark FROM memory_lineage_meta WHERE db_lineage_id=?",
        (stack.store.current_lineage_id(),),
    ).fetchone()[0]
    assert high == 2


def test_delete_writes_intent_and_committed(stack):
    memory = save(stack)
    delete(stack, memory.id)
    events = [
        event for event in stack.ledger.ledger.all_events()
        if event.event_type is LedgerEventType.DELETE_MEMORY
    ]
    assert [event.state for event in events] == [
        LedgerState.INTENT, LedgerState.COMMITTED,
    ]
    assert events[0].event_id == events[1].event_id
    assert row_count(stack, "memories") == 0
    tombstone = stack.db.connection.execute(
        "SELECT metadata_json FROM memory_operations WHERE operation_type='DELETE_MEMORY'",
    ).fetchone()
    assert json.loads(tombstone[0])["creation_seq"] == 1


def test_pending_intent_is_replayed_without_main_write(stack):
    memory = save(stack)
    lineage, creation_seq = stack.store.get_lineage(
        memory.id, scope_id=stack.scope.id, subject_id="u1",
    )
    stack.ledger.record_intent(
        LedgerEventType.DELETE_MEMORY,
        payload={
            "db_lineage_id": lineage,
            "creation_seq": creation_seq,
            "memory_id": memory.id,
            "operation_id": "op-pending",
            "operator_type": "user",
            "operator_id": "u1",
            "occurred_at": stack.clock.now().isoformat(),
        },
        source="user", scope_id=stack.scope.id, subject_id="u1",
        memory_id=memory.id,
    )
    assert memory.id in memory_ids(stack)
    applied, failed = stack.ledger.drain_pending(
        stack.store, stack.consent_repo,
    )
    assert applied == 1 and failed == ()
    assert memory.id not in memory_ids(stack)
    committed = [
        event for event in stack.ledger.ledger.all_events()
        if event.event_type is LedgerEventType.DELETE_MEMORY
        and event.state is LedgerState.COMMITTED
    ]
    assert len(committed) == 1


def test_old_backup_does_not_revive_deleted_memory(stack):
    memory = save(stack)
    backup_id = make_backup(stack)
    delete(stack, memory.id)
    stack.backup.restore(backup_id, mode="merge", confirm=True)
    assert memory.id not in memory_ids(stack)
    assert row_count(stack, "memory_revisions", "memory_id=?", (memory.id,)) == 0


def test_clear_excludes_pre_clear_and_keeps_post_clear(stack):
    before = save(stack, "清空前")
    backup_id = make_backup(stack)
    clear(stack)
    after = save(stack, "清空后")
    stack.backup.restore(backup_id, mode="merge", confirm=True)
    ids = memory_ids(stack)
    assert before.id not in ids
    assert after.id in ids


def test_merge_keeps_post_backup_row_and_newer_revision(stack):
    memory = save(stack, "旧内容")
    backup_id = make_backup(stack)
    asyncio.run(stack.memories.update_explicit(
        stack.scope, memory.id, "新内容", expected_revision=1, operator_id="u1",
    ))
    after = save(stack, "备份后新建")
    stack.backup.restore(backup_id, mode="merge", confirm=True)
    row = stack.db.connection.execute(
        "SELECT content, current_revision FROM memories WHERE id=?",
        (memory.id,),
    ).fetchone()
    assert row["content"] == "新内容"
    assert row["current_revision"] == 2
    assert after.id in memory_ids(stack)


def test_revert_overwrites_same_id_with_backup_version(stack):
    memory = save(stack, "旧内容")
    backup_id = make_backup(stack)
    asyncio.run(stack.memories.update_explicit(
        stack.scope, memory.id, "新内容", expected_revision=1, operator_id="u1",
    ))
    stack.backup.restore(backup_id, mode="revert", confirm=True)
    row = stack.db.connection.execute(
        "SELECT content, current_revision FROM memories WHERE id=?",
        (memory.id,),
    ).fetchone()
    assert row["content"] == "旧内容"
    assert row["current_revision"] == 1


def test_disaster_rebuild_requires_empty_target_and_rotates_lineage(tmp_path):
    source = _build(tmp_path, "source")
    scope = source.scopes.resolve(_personal_identity())
    source.scope = scope
    source.consent.grant(scope, PersonalConsentType.PERSONAL_MEMORY, operator_id="u1")
    try:
        memory = save(source, "灾难备份")
        backup_id = make_backup(source)
        backup_lineage = source.store.current_lineage_id()
        with pytest.raises(Conflict):
            source.backup.restore(backup_id, mode="disaster-rebuild", confirm=True)
    finally:
        source.ledger_store.close()
        source.db.close()

    target = _build(tmp_path, "target")
    try:
        result = target.backup.restore(
            backup_id, mode="disaster-rebuild", confirm=True,
        )
        assert result["db_lineage_id"] != backup_lineage
        assert target.store.current_lineage_id() == result["db_lineage_id"]
        assert memory.id in memory_ids(target)
        imported = target.db.connection.execute(
            "SELECT db_lineage_id, creation_seq FROM memories WHERE id=?",
            (memory.id,),
        ).fetchone()
        assert imported["db_lineage_id"] == backup_lineage
        with pytest.raises(Conflict):
            target.backup.restore(backup_id, mode="merge", confirm=True)
    finally:
        target.ledger_store.close()
        target.db.close()


def test_consent_revoke_is_not_revived_by_old_backup(stack):
    backup_id = make_backup(stack)
    stack.consent.revoke(
        stack.scope, PersonalConsentType.PERSONAL_MEMORY, operator_id="u1",
    )
    stack.backup.restore(backup_id, mode="merge", confirm=True)
    assert not stack.consent.is_granted(
        stack.scope, PersonalConsentType.PERSONAL_MEMORY,
    )


def test_dry_run_lists_ledger_exclusions(stack):
    memory = save(stack)
    backup_id = make_backup(stack)
    delete(stack, memory.id)
    report = stack.backup.restore(backup_id, mode="merge", dry_run=True)
    assert report["will_not_revive"] == 1
    assert report["will_revive"] == 0
    assert memory.id in report["will_not_revive_ids"]
    assert row_count(stack, "memories") == 0


def test_restore_journal_resumes_after_failure(stack, monkeypatch):
    memory = save(stack, "恢复中断")
    backup_id = make_backup(stack)

    def fail(*_args, **_kwargs):
        raise StorageError("injected restore failure")

    monkeypatch.setattr(stack.backup, "_apply_exclusions", fail)
    with pytest.raises(StorageError):
        stack.backup.restore(backup_id, mode="merge", confirm=True)
    assert (stack.backup._journal_path).exists()  # noqa: SLF001 - acceptance check
    monkeypatch.undo()
    result = stack.backup.recover_if_needed()
    assert result is not None and result["restored"] is True
    assert memory.id in memory_ids(stack)
    assert not stack.backup._journal_path.exists()  # noqa: SLF001


def test_ledger_rotation_preserves_global_sequence_and_restore(stack):
    save(stack, "轮转前")
    stack.ledger_store.rotate()
    memory = save(stack, "轮转后")
    report = stack.ledger_store.coverage_report()
    assert report["contiguous"] is True
    assert report["ledger_ids"] == [stack.ledger.ledger.ledger_id()]
    assert int(report["earliest_retained_seq"]) == 1
    backup_id = make_backup(stack)
    delete(stack, memory.id)
    stack.backup.restore(backup_id, mode="merge", confirm=True)
    assert memory.id not in memory_ids(stack)


def test_backup_is_disabled_when_ledger_first_is_off(stack, tmp_path):
    disabled = BackupService(
        database=stack.db, store=stack.store, index=stack.index,
        consents=stack.consent_repo, coordinator=stack.coordinator,
        ledger=stack.ledger, clock=stack.clock, ids=stack.ids,
        data_dir=tmp_path, enabled=False,
    )
    with pytest.raises(StorageError):
        disabled.create_backup()


def test_ledger_off_keeps_creation_seq_and_delete_semantics(tmp_path):
    disabled = _build(tmp_path, "ledger-off", ledger_enabled=False)
    scope = disabled.scopes.resolve(_personal_identity())
    disabled.scope = scope
    disabled.consent.grant(
        scope, PersonalConsentType.PERSONAL_MEMORY, operator_id="u1",
    )
    try:
        memory = save(disabled)
        row = disabled.db.connection.execute(
            "SELECT creation_seq FROM memories WHERE id=?", (memory.id,),
        ).fetchone()
        assert row["creation_seq"] == 1
        delete(disabled, memory.id)
        assert row_count(disabled, "memories") == 0
        assert disabled.ledger.ledger.all_events() == ()
        with pytest.raises(StorageError):
            disabled.backup.create_backup()
    finally:
        disabled.ledger_store.close()
        disabled.db.close()


def test_auto_enroll_does_not_override_restored_revocation(stack):
    backup_id = make_backup(stack)
    stack.consent.revoke(
        stack.scope, PersonalConsentType.PERSONAL_MEMORY, operator_id="u1",
    )
    stack.backup.restore(backup_id, mode="merge", confirm=True)
    assert stack.consent.ensure_auto_enrolled(
        stack.scope, PersonalConsentType.PERSONAL_MEMORY, operator_id="u1",
    ) is False
    assert not stack.consent.is_granted(
        stack.scope, PersonalConsentType.PERSONAL_MEMORY,
    )


def test_backup_ledger_id_mismatch_fails_closed(stack):
    save(stack)
    backup_id = make_backup(stack)
    sidecar = stack.backup._backup_dir(backup_id) / "sidecar.json"  # noqa: SLF001
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["ledger_id"] = "different-ledger"
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(StorageError):
        stack.backup.restore(backup_id, mode="merge", confirm=True)


def test_corrupt_backup_fails_closed(stack):
    save(stack)
    backup_id = make_backup(stack)
    directory = stack.backup._backup_dir(backup_id)  # noqa: SLF001
    (directory / "main.sqlite3").write_bytes(b"not a database")
    with pytest.raises(StorageError):
        stack.backup.restore(backup_id, mode="merge", confirm=True)


def test_config_file_is_redacted(stack):
    save(stack)
    backup_id = make_backup(stack)
    config = json.loads((
        stack.backup._backup_dir(backup_id) / "config.json"  # noqa: SLF001
    ).read_text(encoding="utf-8"))
    assert config["api_token"] == "***"
    assert config["nested"]["password"] == "***"
    assert config["nested"]["keep"] == 1


def test_attachment_removal_is_not_revived(stack):
    memory = save(stack)
    stack.db.connection.execute(
        "INSERT INTO memory_tags(memory_id, tag, created_at) VALUES (?, 't1', ?)",
        (memory.id, stack.clock.now().isoformat()),
    )
    backup_id = make_backup(stack)
    asyncio.run(stack.coordinator.remove_attachment(
        memory_id=memory.id, scope_id=stack.scope.id, subject_id="u1",
        attachment_type="tag", attachment_key="t1",
        operator_type=OperatorType.USER, operator_id="u1",
        occurred_at=stack.clock.now(),
    ))
    assert row_count(stack, "memory_tags", "memory_id=?", (memory.id,)) == 0
    stack.backup.restore(backup_id, mode="merge", confirm=True)
    assert row_count(stack, "memory_tags", "memory_id=?", (memory.id,)) == 0


def test_group_destroy_is_excluded_from_backup_restore(stack):
    identity = _group_identity()
    group_scope = Scope(
        "group-scope-1", identity, stack.clock.now(),
    )
    stack.store.create_scope(group_scope)
    stack.store.set_admin_metadata(
        group_scope.id, ("admin-1",), AdminSource.EXPLICIT_CONFIG,
    )
    stack.store.enable_scope(
        group_scope.id, operation_id="op-group-enable",
        operator_type=OperatorType.ADMIN, operator_id="admin-1",
        occurred_at=stack.clock.now(),
    )
    stack.consent.grant(
        group_scope, GroupConsentType.GROUP_MEMORY, operator_id="admin-1",
    )
    decision = AdminDecision(
        AdminSource.EXPLICIT_CONFIG, ("admin-1",), True, True,
    )
    group_memory, _ = asyncio.run(stack.group_memories.save_group(
        group_scope, "群约定：周五复盘", group_label="agreement",
        operator_id="admin-1", decision=decision,
    ))
    backup_id = make_backup(stack)
    asyncio.run(stack.group_lifecycle.disable(
        group_scope, decision, operator_id="admin-1",
    ))
    asyncio.run(stack.group_lifecycle.destroy(
        group_scope, decision, operator_id="admin-1",
    ))
    assert row_count(stack, "memory_scopes", "id=?", (group_scope.id,)) == 0
    stack.backup.restore(backup_id, mode="merge", confirm=True)
    assert row_count(stack, "memory_scopes", "id=?", (group_scope.id,)) == 0
    assert group_memory.id not in memory_ids(stack)


def test_clear_event_without_boundaries_fails_closed(stack):
    save(stack)
    backup_id = make_backup(stack)
    stack.ledger.ledger.append(
        event_id=stack.ids.new_id("ledger"),
        event_type=LedgerEventType.CLEAR_SCOPE, state=LedgerState.INTENT,
        source="user", occurred_at=stack.clock.now(),
        payload={"operation_id": "op-bad-clear"},
        scope_id=stack.scope.id,
    )
    with pytest.raises(StorageError):
        stack.backup.restore(backup_id, mode="merge", confirm=True)


def test_multi_lineage_clear_records_each_boundary(tmp_path):
    source = _build(tmp_path, "multi-source")
    scope = source.scopes.resolve(_personal_identity())
    source.scope = scope
    source.consent.grant(scope, PersonalConsentType.PERSONAL_MEMORY, operator_id="u1")
    try:
        old_memory = save(source, "旧谱系记忆")
        backup_id = make_backup(source)
    finally:
        source.ledger_store.close()
        source.db.close()

    target = _build(tmp_path, "multi-target")
    try:
        target.backup.restore(backup_id, mode="disaster-rebuild", confirm=True)
        target.scope = target.scopes.resolve(_personal_identity())
        new_memory = save(target, "新谱系记忆")
        new_backup = make_backup(target)
        clear(target)
        clear_event = [
            event for event in target.ledger.ledger.all_events()
            if event.event_type is LedgerEventType.CLEAR_SCOPE
        ][-1]
        boundaries = clear_event.payload["lineage_boundaries"]
        assert isinstance(boundaries, list)
        assert len(boundaries) == 2
        assert len({item["db_lineage_id"] for item in boundaries}) == 2
        target.backup.restore(new_backup, mode="merge", confirm=True)
        ids = memory_ids(target)
        assert old_memory.id not in ids
        assert new_memory.id not in ids
    finally:
        target.ledger_store.close()
        target.db.close()


def test_high_watermark_advances_after_restore(stack):
    save(stack, "备份前")
    backup_id = make_backup(stack)
    clear(stack)
    stack.backup.restore(backup_id, mode="merge", confirm=True)
    new_memory = save(stack, "恢复后新建")
    row = stack.db.connection.execute(
        "SELECT db_lineage_id, creation_seq FROM memories WHERE id=?",
        (new_memory.id,),
    ).fetchone()
    assert row["creation_seq"] == 2
    high = stack.db.connection.execute(
        "SELECT high_watermark FROM memory_lineage_meta WHERE db_lineage_id=?",
        (row["db_lineage_id"],),
    ).fetchone()[0]
    assert high >= 2


def test_memory_type_status_preserved_on_restore(stack):
    memory = save(stack, "状态保持")
    stack.db.connection.execute(
        "UPDATE memories SET status='ARCHIVED' WHERE id=?", (memory.id,),
    )
    backup_id = make_backup(stack)
    for table in ("memory_relations", "memory_tags", "memory_sources", "memory_revisions"):
        column = "source_memory_id" if table == "memory_relations" else "memory_id"
        stack.db.connection.execute(
            f"DELETE FROM {table} WHERE {column}=?", (memory.id,),
        )
    stack.db.connection.execute("DELETE FROM memories WHERE id=?", (memory.id,))
    stack.backup.restore(backup_id, mode="merge", confirm=True)
    row = stack.db.connection.execute(
        "SELECT status FROM memories WHERE id=?", (memory.id,),
    ).fetchone()
    assert row["status"] == "ARCHIVED"
    assert memory.status is MemoryStatus.ACTIVE
