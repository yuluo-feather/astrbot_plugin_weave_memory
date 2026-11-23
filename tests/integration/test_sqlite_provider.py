import sqlite3
from datetime import UTC, datetime

import pytest
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.database import (
    SQLiteDatabase,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.fts import SQLiteFTSIndex
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.store import (
    SQLiteMemoryStore,
)
from data.plugins.astrbot_plugin_weave_memory.core.errors import (
    AuthorizationDenied,
    Conflict,
    IndexUnavailable,
    NotFound,
)
from data.plugins.astrbot_plugin_weave_memory.core.model import (
    AdminSource,
    Identity,
    MemoryEntity,
    MemoryRevision,
    MemorySource,
    MemoryStatus,
    MemoryType,
    OperatorType,
    Scope,
    ScopeType,
    SourceType,
    SubjectType,
)
from data.plugins.astrbot_plugin_weave_memory.core.normalization import (
    normalize_content,
)

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


@pytest.fixture
def sqlite_stack(tmp_path):
    db = SQLiteDatabase(tmp_path / "weave.db")
    db.migrate()
    store = SQLiteMemoryStore(db)
    index = SQLiteFTSIndex(db)
    identity = Identity("astrbot", "default", ScopeType.PERSONAL,
                        "platform-user-1", SubjectType.USER, "user-1")
    scope = Scope("scope-1", identity, NOW)
    store.create_scope(scope)
    yield db, store, index, scope
    db.close()


def make_memory(scope, *, memory_id="mem-1", content="喜欢黑咖啡。", status=MemoryStatus.ACTIVE):
    return MemoryEntity(
        id=memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
        memory_type=MemoryType.PREFERENCE, content=content,
        normalized_content=normalize_content(content), status=status,
        source_type=SourceType.EXPLICIT, current_revision=1,
        created_at=NOW, updated_at=NOW,
    )


def add_memory(store, scope, *, memory_id="mem-1", content="喜欢黑咖啡。", status=MemoryStatus.ACTIVE):
    memory = make_memory(scope, memory_id=memory_id, content=content, status=status)
    revision = MemoryRevision("rev-" + memory_id, memory_id, 1, content, status, NOW, "created")
    source = MemorySource("src-" + memory_id, memory_id, SourceType.EXPLICIT,
                          OperatorType.USER, "user-1", NOW)
    store.create(memory, revision, source)
    return memory


def add_group_scope(store, *, scope_id="scope-group", group_id="group-1",
                    platform="astrbot", tenant="default"):
    identity = Identity(platform, tenant, ScopeType.GROUP, group_id,
                        SubjectType.GROUP, group_id)
    scope = Scope(scope_id, identity, NOW)
    store.create_scope(scope)
    return scope


def add_group_memory(store, scope, *, memory_id="group-mem-1",
                     content="群约定：周五复盘"):
    memory = MemoryEntity(
        id=memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
        memory_type=MemoryType.PREFERENCE, content=content,
        normalized_content=normalize_content(content), status=MemoryStatus.ACTIVE,
        source_type=SourceType.MANUAL, current_revision=1,
        created_at=NOW, updated_at=NOW,
    )
    revision = MemoryRevision("rev-" + memory_id, memory_id, 1, content,
                              MemoryStatus.ACTIVE, NOW, "group admin save")
    source = MemorySource("src-" + memory_id, memory_id, SourceType.MANUAL,
                          OperatorType.ADMIN, "admin-1", NOW)
    store.create(memory, revision, source)
    return memory


def add_group_consent(db, scope, *, consent_id="group-consent-1"):
    db.connection.execute(
        """INSERT INTO memory_consents
        (id, subject_id, scope_id, consent_type, state, source, granted_at, revoked_at, metadata_json)
        VALUES (?, ?, ?, 'group_memory', 'GRANTED', 'test', ?, NULL, '{}')""",
        (consent_id, scope.identity.subject_id, scope.id, "2026-11-22T12:00:00.000000Z"),
    )


def test_group_scope_admin_metadata_and_lifecycle(sqlite_stack):
    _, store, _, _ = sqlite_stack
    scope = add_group_scope(store)
    assert store.get_scope(scope.id) == scope
    assert store.get_admin_metadata(scope.id) == ((), AdminSource.NONE)
    store.set_admin_metadata(scope.id, ("admin-1", "admin-2"), AdminSource.EXPLICIT_CONFIG)
    assert store.get_admin_metadata(scope.id) == (("admin-1", "admin-2"), AdminSource.EXPLICIT_CONFIG)
    assert store.enable_scope(scope.id, operation_id="op-group-enable",
                              operator_type=OperatorType.ADMIN, operator_id="admin-1",
                              occurred_at=NOW)
    assert store.is_scope_enabled(scope.id)
    assert not store.enable_scope(scope.id, operation_id="op-group-enable",
                                  operator_type=OperatorType.ADMIN, operator_id="admin-1",
                                  occurred_at=NOW)
    assert store.disable_group_scope(scope.id, operation_id="op-group-disable",
                                     operator_type=OperatorType.ADMIN, operator_id="admin-1",
                                     occurred_at=NOW)
    assert not store.is_scope_enabled(scope.id)
    assert store.count_active(scope.id) == 0


def test_group_backup_destroy_and_rollback_path(tmp_path):
    from data.plugins.astrbot_plugin_weave_memory.core.errors import StorageError

    db = SQLiteDatabase(tmp_path / "group-destroy.db")
    db.migrate()
    store = SQLiteMemoryStore(db)
    scope = add_group_scope(store)
    store.set_admin_metadata(scope.id, ("admin-1",), AdminSource.EXPLICIT_CONFIG)
    store.enable_scope(scope.id, operation_id="op-group-enable",
                       operator_type=OperatorType.ADMIN, operator_id="admin-1",
                       occurred_at=NOW)
    memory = add_group_memory(store, scope)
    add_group_consent(db, scope)
    assert store.count_active(scope.id) == 1

    backup = tmp_path / "group-backup.sqlite3"
    digest = store.backup_group_scope(scope.id, str(backup))
    assert backup.exists() and len(digest) == 64

    db.rollback_to_v5()
    with pytest.raises(StorageError):
        db.rollback_to_v10()
    assert db.schema_version() == 5

    store.disable_group_scope(scope.id, operation_id="op-group-disable",
                              operator_type=OperatorType.ADMIN, operator_id="admin-1",
                              occurred_at=NOW)
    removed = store.delete_group_scope(scope.id, operation_id="op-group-destroy",
                                       operator_type=OperatorType.ADMIN,
                                       operator_id="admin-1", occurred_at=NOW)
    assert removed == 1
    assert db.connection.execute("SELECT count(*) FROM memories WHERE scope_id=?", (scope.id,)).fetchone()[0] == 0
    assert db.connection.execute("SELECT count(*) FROM memory_consents WHERE scope_id=?", (scope.id,)).fetchone()[0] == 0
    assert db.connection.execute("SELECT count(*) FROM memory_operations WHERE scope_id=?", (scope.id,)).fetchone()[0] == 0
    assert db.connection.execute("SELECT count(*) FROM memory_scopes WHERE id=?", (scope.id,)).fetchone()[0] == 0
    pending = db.connection.execute(
        "SELECT value FROM memory_meta WHERE key='secure_purge_pending'"
    ).fetchone()[0]
    assert pending == "1"
    dirty = db.connection.execute(
        "SELECT value FROM memory_meta WHERE key='fts_dirty_ids'"
    ).fetchone()[0]
    assert memory.id in dirty

    db.rollback_to_v10()
    assert db.schema_version() == 4
    db.migrate()
    assert db.schema_version() == 6
    db.close()


def test_migration_is_idempotent_and_creates_required_tables(sqlite_stack):
    db, _, _, _ = sqlite_stack
    assert db.connection.execute("PRAGMA secure_delete").fetchone()[0] == 1
    assert db.schema_version() == 6
    db.migrate()
    tables = {row[0] for row in db.connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"memories", "memory_revisions", "memory_sources", "memory_scopes",
            "memory_tags", "memory_operations", "memory_meta", "memory_consents",
            "memory_relations", "schema_migrations",
            "memory_fts"}.issubset(tables)
    scope_columns = {row["name"] for row in db.connection.execute("PRAGMA table_info(memory_scopes)")}
    assert {"is_enabled", "admin_ids_json", "admin_source"}.issubset(scope_columns)
    assert db.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_create_persists_memory_revision_source_in_one_transaction(sqlite_stack):
    db, store, _, scope = sqlite_stack
    memory = add_memory(store, scope)
    assert store.get(memory.id, scope_id=scope.id, subject_id="user-1") == memory
    assert db.connection.execute("SELECT count(*) FROM memory_revisions WHERE memory_id=?", (memory.id,)).fetchone()[0] == 1
    assert db.connection.execute("SELECT count(*) FROM memory_sources WHERE memory_id=?", (memory.id,)).fetchone()[0] == 1


def test_create_rolls_back_all_authoritative_rows_on_association_failure(sqlite_stack):
    db, store, _, scope = sqlite_stack
    # A pre-existing source ID makes the final insert fail after memory + revision inserts.
    existing = add_memory(store, scope, memory_id="mem-existing", content="既有记忆")
    db.connection.execute("UPDATE memory_sources SET id=? WHERE memory_id=?", ("src-mem-1", existing.id))
    memory = make_memory(scope, memory_id="mem-1", content="另一条记忆")
    revision = MemoryRevision("rev-mem-1", memory.id, 1, memory.content,
                              memory.status, NOW, "created")
    source = MemorySource("src-mem-1", memory.id, SourceType.EXPLICIT,
                          OperatorType.USER, "user-1", NOW)
    with pytest.raises(Conflict):
        store.create(memory, revision, source)
    with pytest.raises(NotFound):
        store.get(memory.id, scope_id=scope.id, subject_id="user-1")
    assert db.connection.execute("SELECT count(*) FROM memory_revisions WHERE memory_id=?", (memory.id,)).fetchone()[0] == 0


def test_partial_unique_index_only_blocks_active_and_candidate(sqlite_stack):
    from data.plugins.astrbot_plugin_weave_memory.core.policy.lifecycle import (
        transition,
    )

    _, store, _, scope = sqlite_stack
    active = add_memory(store, scope, memory_id="active-1", status=MemoryStatus.ACTIVE)
    duplicate = make_memory(scope, memory_id="active-2")
    with pytest.raises(Conflict):
        store.create(duplicate,
                     MemoryRevision("rev-active-2", duplicate.id, 1, duplicate.content, duplicate.status, NOW, "created"),
                     MemorySource("src-active-2", duplicate.id, SourceType.EXPLICIT, OperatorType.USER, "user-1", NOW))
    archived = transition(active, MemoryStatus.ARCHIVED, at=NOW.replace(microsecond=1))
    store.update(archived, expected_revision=1,
                 revision=MemoryRevision("rev-active-1-2", active.id, 2, archived.content,
                                          archived.status, archived.updated_at, "archived"))
    store.create(duplicate,
                 MemoryRevision("rev-active-2", duplicate.id, 1, duplicate.content, duplicate.status, NOW, "created"),
                 MemorySource("src-active-2", duplicate.id, SourceType.EXPLICIT, OperatorType.USER, "user-1", NOW))


def test_exact_create_replay_is_rejected_after_scope_disable(sqlite_stack):
    db, store, _, scope = sqlite_stack
    add_memory(store, scope, memory_id="mem-disable-replay")
    store.disable_scope(scope.id, operation_id="op-disable-replay",
                        operator_type=OperatorType.USER, operator_id="user-1",
                        occurred_at=NOW)
    with pytest.raises(AuthorizationDenied):
        add_memory(store, scope, memory_id="mem-disable-replay")


def test_memory_relations_reject_dangling_references(sqlite_stack):
    import sqlite3

    db, _, _, scope = sqlite_stack
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            "INSERT INTO memory_relations(id,source_memory_id,target_memory_id,relation_type,created_at) "
            "VALUES(?,?,?,?,?)",
            ("rel-dangling", "missing-1", "missing-2", "RELATED", NOW.isoformat()),
        )


def test_fts_sync_search_and_scope_filter(sqlite_stack):
    _, store, index, scope = sqlite_stack
    memory = add_memory(store, scope)
    assert not index.is_healthy()
    index.sync_memory(memory.id)
    assert index.is_healthy()
    assert index.search("黑咖啡", scope_id=scope.id, subject_id="user-1") == (memory.id,)
    assert index.search("黑咖啡", scope_id=scope.id, subject_id="another-user") == ()


def test_index_failure_keeps_authoritative_memory_and_fails_closed(sqlite_stack):
    db, store, index, scope = sqlite_stack
    memory = add_memory(store, scope)
    db.connection.execute("DROP TABLE memory_fts")
    with pytest.raises(IndexUnavailable):
        index.sync_memory(memory.id)
    assert store.get(memory.id, scope_id=scope.id, subject_id="user-1") == memory
    assert not index.is_healthy()
    with pytest.raises(IndexUnavailable):
        index.search("黑咖啡", scope_id=scope.id, subject_id="user-1")
    db.connection.execute("CREATE VIRTUAL TABLE memory_fts USING fts5(memory_id UNINDEXED, content, tokenize='unicode61 remove_diacritics 2')")
    assert index.rebuild() == 1
    assert index.is_healthy()
    assert index.search("黑咖啡", scope_id=scope.id, subject_id="user-1") == (memory.id,)


def test_schema_can_roll_back_to_empty_and_reapply(tmp_path):
    db = SQLiteDatabase(tmp_path / "rollback.db")
    db.migrate()
    db.rollback_to_zero()
    assert db.connection.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='memories'").fetchone()[0] == 0
    db.migrate()
    assert db.schema_version() == 6
    db.close()


def test_candidate_and_active_same_key_conflict(sqlite_stack):
    _, store, _, scope = sqlite_stack
    add_memory(store, scope, memory_id="m-active", content="同名事实")
    with pytest.raises(Conflict):
        add_memory(
            store, scope, memory_id="m-candidate", content="同名事实",
            status=MemoryStatus.CANDIDATE,
        )


def test_in_memory_secure_purge_marks_complete():
    db = SQLiteDatabase(":memory:")
    db.migrate()
    db.secure_purge()
    state = {
        row["key"]: row["value"]
        for row in db.connection.execute(
            "SELECT key, value FROM memory_meta WHERE key LIKE 'secure_purge_%'"
        ).fetchall()
    }
    assert state["secure_purge_pending"] == "0"
    assert state["secure_purge_baseline_done"] == "1"
    # No file/WAL exists for :memory:, so there is nothing to retry at startup.
    assert db.secure_purge_if_pending() is False
    db.close()


def test_close_is_idempotent_and_operations_after_close_fail(tmp_path):
    db = SQLiteDatabase(tmp_path / "close.db")
    db.migrate()
    db.close()
    db.close()
    from data.plugins.astrbot_plugin_weave_memory.core.errors import ServiceClosed
    with pytest.raises(ServiceClosed):
        db.schema_version()


def test_optimistic_update_and_index_removal(sqlite_stack):
    from data.plugins.astrbot_plugin_weave_memory.core.policy.lifecycle import (
        transition,
    )

    db, store, index, scope = sqlite_stack
    memory = add_memory(store, scope)
    index.sync_memory(memory.id)
    archived = transition(memory, MemoryStatus.ARCHIVED, at=NOW.replace(microsecond=1))
    revision = MemoryRevision("rev-mem-1-2", memory.id, 2, archived.content,
                              archived.status, archived.updated_at, "archived")
    store.update(archived, expected_revision=1, revision=revision)
    assert not index.is_healthy()
    index.sync_memory(memory.id)
    assert index.search("黑咖啡", scope_id=scope.id, subject_id="user-1") == ()
    with pytest.raises(Conflict):
        store.update(archived, expected_revision=1, revision=revision)
    assert db.connection.execute("SELECT count(*) FROM memory_revisions WHERE memory_id=?", (memory.id,)).fetchone()[0] == 2


def test_source_change_appends_source_record_in_same_transaction(sqlite_stack):
    from dataclasses import replace

    db, store, _, scope = sqlite_stack
    memory = add_memory(store, scope)
    updated = replace(memory, source_type=SourceType.MANUAL, current_revision=2,
                      updated_at=NOW.replace(microsecond=2))
    revision = MemoryRevision("rev-mem-1-2", memory.id, 2, updated.content,
                              updated.status, updated.updated_at, "source changed")
    source = MemorySource("src-mem-1-2", memory.id, SourceType.MANUAL,
                          OperatorType.USER, "user-1", updated.updated_at)
    store.update(updated, expected_revision=1, revision=revision, source=source)
    assert store.get(memory.id, scope_id=scope.id, subject_id="user-1").source_type is SourceType.MANUAL
    assert db.connection.execute("SELECT count(*) FROM memory_sources WHERE memory_id=?", (memory.id,)).fetchone()[0] == 2


def test_exact_replay_is_idempotent_but_changed_payload_conflicts(sqlite_stack):
    _, store, _, scope = sqlite_stack
    memory = make_memory(scope)
    revision = MemoryRevision("rev-mem-1", memory.id, 1, memory.content,
                              memory.status, NOW, "created")
    source = MemorySource("src-mem-1", memory.id, SourceType.EXPLICIT,
                          OperatorType.USER, "user-1", NOW)
    store.create(memory, revision, source)
    store.create(memory, revision, source)
    assert store.get(memory.id, scope_id=scope.id, subject_id="user-1") == memory
    assert store.db.connection.execute("SELECT count(*) FROM memory_revisions WHERE memory_id=?", (memory.id,)).fetchone()[0] == 1
    changed = make_memory(scope, content="不同正文")
    with pytest.raises(Conflict):
        store.create(changed, revision, source)


def test_fts_query_matches_relevant_cjk_bigrams_in_natural_question(sqlite_stack):
    _, store, index, scope = sqlite_stack
    memory = add_memory(store, scope, content="喜欢黑咖啡")
    index.sync_memory(memory.id)
    assert index.search("你还记得我喜欢喝什么咖啡吗？", scope_id=scope.id,
                        subject_id="user-1") == (memory.id,)


def test_fts_rejects_out_of_range_limit_and_oversized_query(sqlite_stack):
    from data.plugins.astrbot_plugin_weave_memory.core.errors import ValidationError

    _, _, index, scope = sqlite_stack
    with pytest.raises(ValidationError):
        index.search("咖啡", scope_id=scope.id, subject_id="user-1", limit=0)
    with pytest.raises(ValidationError):
        index.search("咖" * 257, scope_id=scope.id, subject_id="user-1")


def test_v4_migration_failure_rolls_back_ddl_and_version_marker(tmp_path, monkeypatch):
    from pathlib import Path

    from data.plugins.astrbot_plugin_weave_memory.core.errors import StorageError

    db = SQLiteDatabase(tmp_path / "failed-v4-migration.db")
    db.migrate()
    db.rollback_to_v5()
    db.rollback_to_v10()
    db.rollback_to_v05()
    assert db.schema_version() == 3

    original_read_text = Path.read_text

    def broken_migration_read_text(path, *args, **kwargs):
        if path.name == "004_memory_relations.sql":
            # The first DDL succeeds; the second deliberately fails. Both DDL
            # and the schema marker must remain inside the same transaction.
            return "CREATE TABLE memory_relations (id TEXT PRIMARY KEY);\nCREATE TABLE memory_relations (id TEXT PRIMARY KEY);"
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", broken_migration_read_text)
    with pytest.raises(StorageError):
        db.migrate()

    assert db.schema_version() == 3
    assert db.connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_relations'",
    ).fetchone() is None
    db.close()


def test_v3_database_upgrades_to_v5_group_scopes(tmp_path):
    db = SQLiteDatabase(tmp_path / "upgrade-v3.db")
    db.migrate()
    db.rollback_to_v5()
    db.rollback_to_v10()
    db.rollback_to_v05()
    assert db.schema_version() == 3
    db.migrate()
    assert db.schema_version() == 6
    assert db.connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_relations'",
    ).fetchone() is not None
    scope_columns = {row["name"] for row in db.connection.execute("PRAGMA table_info(memory_scopes)")}
    assert {"admin_ids_json", "admin_source"}.issubset(scope_columns)
    db.close()


def test_v4_to_v5_migration_creates_pre_upgrade_backup(tmp_path):
    db_path = tmp_path / "backup-before-v5.db"
    db = SQLiteDatabase(db_path)
    db.migrate()
    db.rollback_to_v5()
    db.rollback_to_v10()
    assert db.schema_version() == 4
    store = SQLiteMemoryStore(db)
    identity = Identity("astrbot", "default", ScopeType.PERSONAL,
                        "platform-backup-user", SubjectType.USER, "backup-user")
    scope = Scope("scope-backup", identity, NOW)
    store.create_scope(scope)
    memory = add_memory(store, scope, memory_id="mem-backup", content="迁移前必须保留")
    db.close()

    upgraded = SQLiteDatabase(db_path)
    backup_path = upgraded.migrate()
    assert backup_path is not None
    assert backup_path.exists()
    assert backup_path.parent.name == "migration_backups"
    assert upgraded.schema_version() == 6
    assert SQLiteMemoryStore(upgraded).get(
        memory.id, scope_id=scope.id, subject_id="backup-user",
    ) == memory

    backup = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    try:
        version = backup.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations",
        ).fetchone()[0]
        content = backup.execute(
            "SELECT content FROM memories WHERE id=?", (memory.id,),
        ).fetchone()[0]
    finally:
        backup.close()
    assert version == 4
    assert content == "迁移前必须保留"
    upgraded.close()


def test_migration_backup_failure_aborts_upgrade(tmp_path, monkeypatch):
    from data.plugins.astrbot_plugin_weave_memory.core.errors import StorageError

    db_path = tmp_path / "backup-failure.db"
    db = SQLiteDatabase(db_path)
    db.migrate()
    db.rollback_to_v5()
    db.rollback_to_v10()
    store = SQLiteMemoryStore(db)
    identity = Identity("astrbot", "default", ScopeType.PERSONAL,
                        "platform-failure-user", SubjectType.USER, "failure-user")
    scope = Scope("scope-failure", identity, NOW)
    store.create_scope(scope)
    memory = add_memory(store, scope, memory_id="mem-failure", content="备份失败也要保留")
    db.close()

    upgraded = SQLiteDatabase(db_path)

    def fail_backup(self, destination):
        raise StorageError("forced backup failure")

    monkeypatch.setattr(SQLiteDatabase, "backup_to", fail_backup)
    with pytest.raises(StorageError):
        upgraded.migrate()
    assert upgraded.schema_version() == 4
    assert SQLiteMemoryStore(upgraded).get(
        memory.id, scope_id=scope.id, subject_id="failure-user",
    ) == memory
    upgraded.close()


def test_fresh_and_current_database_skip_migration_backup(tmp_path):
    db = SQLiteDatabase(tmp_path / "fresh.db")
    assert db.migrate() is None
    assert not (tmp_path / "migration_backups").exists()
    assert db.migrate() is None
    db.close()


def test_v1_schema_rolls_back_to_v05_only_when_reserved_relations_empty(tmp_path):
    from data.plugins.astrbot_plugin_weave_memory.core.errors import StorageError

    db = SQLiteDatabase(tmp_path / "rollback-v05.db")
    db.migrate()
    identity = Identity("astrbot", "default", ScopeType.PERSONAL,
                        "platform-u1", SubjectType.USER, "u1")
    scope = Scope("scope-rollback", identity, NOW)
    store = SQLiteMemoryStore(db)
    store.create_scope(scope)
    existing = add_memory(store, scope, memory_id="rollback-existing", content="回滚后保留")
    db.rollback_to_v5()
    db.rollback_to_v10()
    db.rollback_to_v05()
    assert db.schema_version() == 3
    assert db.connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_relations'",
    ).fetchone() is None
    assert store.get(existing.id, scope_id=scope.id, subject_id="u1") == existing
    db.migrate()
    assert db.schema_version() == 6
    assert store.get(existing.id, scope_id=scope.id, subject_id="u1") == existing
    db.rollback_to_v5()
    db.rollback_to_v10()
    first = add_memory(store, scope, memory_id="rollback-a", content="第一条")
    second = add_memory(store, scope, memory_id="rollback-b", content="第二条")
    db.connection.execute(
        "INSERT INTO memory_relations(id,source_memory_id,target_memory_id,relation_type,created_at) VALUES(?,?,?,?,?)",
        ("rollback-rel", first.id, second.id, "RELATED_TO", "2026-11-22T12:00:00.000000Z"),
    )
    with pytest.raises(StorageError):
        db.rollback_to_v05()
    assert db.schema_version() == 4
    db.close()
