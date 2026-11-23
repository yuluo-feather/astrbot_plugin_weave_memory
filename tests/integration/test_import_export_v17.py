import asyncio
import json
from datetime import UTC, datetime, timedelta

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
from data.plugins.astrbot_plugin_weave_memory.adapters.system import UUID4IdGenerator
from data.plugins.astrbot_plugin_weave_memory.app.consent_service import (
    ConsentService,
    PersonalConsentType,
)
from data.plugins.astrbot_plugin_weave_memory.app.export import (
    EXPORT_FORMAT,
    build_export,
)
from data.plugins.astrbot_plugin_weave_memory.app.group_lifecycle_service import (
    GroupLifecycleService,
)
from data.plugins.astrbot_plugin_weave_memory.app.group_memory_service import (
    GroupMemoryService,
)
from data.plugins.astrbot_plugin_weave_memory.app.import_service import (
    ImportOutcome,
    ImportService,
    parse_export_payload,
    payload_sha256,
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
from data.plugins.astrbot_plugin_weave_memory.core.errors import (
    AuthorizationDenied,
    Conflict,
    StorageError,
    ValidationError,
)
from data.plugins.astrbot_plugin_weave_memory.core.model import (
    AdminDecision,
    AdminSource,
    Identity,
    ImportJob,
    ImportJobStatus,
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
from data.plugins.astrbot_plugin_weave_memory.core.policy.dedup import DedupKey

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
    db = SQLiteDatabase(tmp_path / "import-personal.db")
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
    imports = ImportService(
        store, SQLiteImportJobStore(db), coordinator, lifecycle, clock, ids,
    )
    scope = scopes.resolve(personal_identity())
    consent.grant(scope, PersonalConsentType.PERSONAL_MEMORY, operator_id="u1")
    yield {
        "db": db, "store": store, "index": index, "scopes": scopes,
        "consent": consent, "lifecycle": lifecycle, "memories": memories,
        "imports": imports, "scope": scope,
    }
    db.close()


@pytest.fixture
def group_stack(tmp_path):
    db = SQLiteDatabase(tmp_path / "import-group.db")
    db.migrate()
    store = SQLiteMemoryStore(db)
    index = SQLiteFTSIndex(db)
    clock = FixedClock()
    ids = UUID4IdGenerator()
    coordinator = WriteCoordinator(store, index)
    scopes = GroupScopeService(store, FakeAdmins(), coordinator, clock)
    consent = ConsentService(SQLiteConsentStore(db), coordinator, clock, ids)
    lifecycle = GroupLifecycleService(
        store, coordinator, consent, index, clock, ids, tmp_path / "backups",
    )
    memories = GroupMemoryService(store, store, coordinator, consent, clock, ids)
    imports = ImportService(
        store, SQLiteImportJobStore(db), coordinator, lifecycle, clock, ids,
    )
    decision = scopes.admin_decision(group_identity(), "admin-1")
    scope = scopes.ensure_scope(group_identity(), decision)
    yield {
        "db": db, "store": store, "scopes": scopes, "consent": consent,
        "lifecycle": lifecycle, "memories": memories, "imports": imports,
        "decision": decision, "scope": scope,
    }
    db.close()


def entry(source_memory_id="src-1", revision=1, content="喜欢黑咖啡",
          status="ACTIVE", memory_type="PREFERENCE", metadata=None):
    return {
        "source_memory_id": source_memory_id,
        "source_revision": revision,
        "memory_type": memory_type,
        "status": status,
        "content": content,
        "body_omitted": status != "ACTIVE",
        "source_type": "EXPLICIT",
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
        "valid_from": None,
        "valid_until": None,
        "metadata": metadata or {},
        "exported_at": NOW.isoformat(),
        "schema_version": EXPORT_FORMAT,
    }


def payload(*entries, export_id="exp-1", platform="astrbot",
            scope_type="PERSONAL", scope_external_id="u1", subject_id="u1"):
    return {
        "schema_version": EXPORT_FORMAT,
        "export_id": export_id,
        "source_platform": platform,
        "exported_at": NOW.isoformat(),
        "scope": {
            "platform": platform,
            "tenant_id": "instance-1",
            "scope_type": scope_type,
            "scope_external_id": scope_external_id,
            "subject_type": "GROUP" if scope_type == "GROUP" else "USER",
            "subject_id": subject_id,
        },
        "memories": list(entries),
    }


def run_import(stack, scope, data, *, dry_run=False, guard=None):
    if guard is None:
        def guard():
            stack["consent"].require(scope, PersonalConsentType.PERSONAL_MEMORY)
    return asyncio.run(stack["imports"].import_payload(
        scope, data, operator_id="u1", file_sha256=payload_sha256(data),
        guard=guard, dry_run=dry_run,
    ))


def add_candidate(store, scope, content):
    now = NOW
    memory_id = "candidate-1"
    memory = MemoryEntity(
        id=memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
        memory_type=MemoryType.FACT, content=content,
        normalized_content=normalize_content(content),
        status=MemoryStatus.CANDIDATE, source_type=SourceType.INFERRED,
        current_revision=1, created_at=now, updated_at=now,
        candidate_expires_at=now + timedelta(days=7),
    )
    revision = MemoryRevision(
        id="candidate-rev", memory_id=memory_id, revision=1, content=content,
        status=MemoryStatus.CANDIDATE, changed_at=now, change_reason="test candidate",
    )
    source = MemorySource(
        id="candidate-src", memory_id=memory_id, source_type=SourceType.INFERRED,
        operator_type=OperatorType.SYSTEM, operator_id="extractor", captured_at=now,
    )
    store.create(memory, revision, source)


def test_import_creates_imported_memory_and_ignores_forged_scope(personal_stack):
    stack = personal_stack
    scope = stack["scope"]
    forged = payload(entry(metadata={"tags": ["coffee"]}))
    forged["memories"][0]["scope_id"] = "someone-else"
    forged["memories"][0]["subject_id"] = "intruder"

    result = run_import(stack, scope, forged)

    assert result.imported == 1
    assert result.job_id is not None
    memories = stack["store"].list_active(
        scope_id=scope.id, subject_id=scope.identity.subject_id,
    )
    assert len(memories) == 1
    memory = memories[0]
    assert memory.content == "喜欢黑咖啡"
    assert memory.source_type is SourceType.IMPORTED
    assert memory.scope_id == scope.id
    assert memory.subject_id == "u1"
    assert memory.metadata["tags"] == ["coffee"]
    assert memory.metadata["import"]["source_memory_id"] == "src-1"
    sources = stack["store"].list_sources(
        memory.id, scope_id=scope.id, subject_id="u1",
    )
    assert sources[0].source_type is SourceType.IMPORTED
    assert json.loads(sources[0].evidence_ref) == {
        "export_id": "exp-1",
        "source_memory_id": "src-1",
        "source_platform": "astrbot",
        "source_revision": 1,
    }
    job = SQLiteImportJobStore(stack["db"]).get(result.job_id)
    assert job.status is ImportJobStatus.DONE
    assert job.created_memory_ids == (memory.id,)


def test_same_export_is_idempotent(personal_stack):
    stack = personal_stack
    scope = stack["scope"]
    data = payload(entry())

    first = run_import(stack, scope, data)
    second = run_import(stack, scope, data)

    assert first.imported == 1
    assert second.imported == 0
    assert second.note
    assert second.total == 1
    assert len(stack["store"].list_active(scope_id=scope.id, subject_id="u1")) == 1


def test_import_resumes_from_job_cursor(personal_stack):
    stack = personal_stack
    scope = stack["scope"]
    data = payload(entry("src-1", content="第一条"), entry("src-2", content="第二条"))
    jobs = SQLiteImportJobStore(stack["db"])
    jobs.create(ImportJob(
        id="job-resume", scope_id=scope.id, subject_id="u1",
        file_sha256=payload_sha256(data), dataset_version=EXPORT_FORMAT,
        status=ImportJobStatus.RUNNING, cursor=1, created_memory_ids=(),
        created_at=NOW, updated_at=NOW,
    ))

    result = run_import(stack, scope, data)

    assert result.resumed_from == 1
    assert result.imported == 1
    contents = {
        memory.content for memory in stack["store"].list_active(
            scope_id=scope.id, subject_id="u1",
        )
    }
    assert contents == {"第二条"}
    job = jobs.get("job-resume")
    assert job.status is ImportJobStatus.DONE
    assert job.cursor == 2


def test_dry_run_reports_without_writing(personal_stack):
    stack = personal_stack
    scope = stack["scope"]

    result = run_import(stack, scope, payload(entry()), dry_run=True)

    assert result.dry_run is True
    assert result.imported == 1
    assert stack["store"].list_active(scope_id=scope.id, subject_id="u1") == ()
    assert stack["db"].connection.execute(
        "SELECT count(*) FROM import_jobs",
    ).fetchone()[0] == 0


def test_non_active_statuses_are_metadata_only(personal_stack):
    stack = personal_stack
    scope = stack["scope"]
    statuses = ["ARCHIVED", "EXPIRED", "SUPERSEDED", "REJECTED", "DELETED"]
    data = payload(*[
        entry(f"src-{status.lower()}", content=None, status=status)
        for status in statuses
    ] + [entry("src-candidate", status="CANDIDATE")])

    result = run_import(stack, scope, data)

    assert result.metadata_only == len(statuses)
    assert result.failed == 1
    failed = [item for item in result.entries if item.outcome is ImportOutcome.FAILED]
    assert failed[0].source_memory_id == "src-candidate"
    assert stack["store"].list_active(scope_id=scope.id, subject_id="u1") == ()
    assert stack["db"].connection.execute(
        "SELECT count(*) FROM memories",
    ).fetchone()[0] == 0
    rows = stack["db"].connection.execute(
        "SELECT operation_type, metadata_json FROM memory_operations",
    ).fetchall()
    assert len(rows) == len(statuses)
    for row in rows:
        assert row["operation_type"] == "IMPORT_METADATA"
        metadata = json.loads(row["metadata_json"])
        assert "content" not in metadata
        assert metadata["source_status"] in statuses
        assert metadata["result"] == "SUCCESS"


def test_candidate_collision_is_reported_as_conflict(personal_stack):
    stack = personal_stack
    scope = stack["scope"]
    add_candidate(stack["store"], scope, "喜欢黑咖啡")

    result = run_import(
        stack, scope,
        payload(entry(content="喜欢黑咖啡", memory_type="FACT")),
    )

    assert result.conflicts == 1
    assert result.imported == 0
    outcomes = {item.outcome for item in result.entries}
    assert ImportOutcome.CONFLICT in outcomes
    assert stack["store"].find_candidate_duplicate(
        DedupKey(
            scope.id, "u1", MemoryType.FACT.value, normalize_content("喜欢黑咖啡"),
        ),
    ) is not None


def test_scope_type_mismatch_is_rejected(personal_stack, group_stack):
    personal = personal_stack
    group = group_stack
    personal_data = payload(entry())
    group_data = payload(
        entry(), scope_type="GROUP", scope_external_id="group-1", subject_id="group-1",
    )

    with pytest.raises(ValidationError):
        run_import(personal, personal["scope"], group_data)
    with pytest.raises(ValidationError):
        asyncio.run(group["imports"].import_payload(
            group["scope"], personal_data, operator_id="admin-1",
            file_sha256=payload_sha256(personal_data),
            guard=group["memories"].write_guard(group["scope"]),
        ))


def test_group_import_requires_enabled_and_stays_isolated(group_stack, personal_stack):
    group = group_stack
    personal = personal_stack
    data = payload(
        entry("g-src", content="群内约定"), scope_type="GROUP",
        scope_external_id="group-1", subject_id="group-1",
    )

    with pytest.raises(AuthorizationDenied):
        asyncio.run(group["imports"].import_payload(
            group["scope"], data, operator_id="admin-1",
            file_sha256=payload_sha256(data),
            guard=group["memories"].write_guard(group["scope"]),
        ))

    asyncio.run(group["lifecycle"].enable(
        group["scope"], group["decision"], operator_id="admin-1",
    ))
    result = asyncio.run(group["imports"].import_payload(
        group["scope"], data, operator_id="admin-1",
        file_sha256=payload_sha256(data),
        guard=group["memories"].write_guard(group["scope"]),
    ))
    assert result.imported == 1
    group_memories = group["store"].list_active(
        scope_id=group["scope"].id, subject_id="group-1",
    )
    assert [memory.content for memory in group_memories] == ["群内约定"]
    assert personal["store"].list_active(
        scope_id=personal["scope"].id, subject_id="u1",
    ) == ()
    assert "群内约定" not in json.dumps(
        personal["memories"].export_personal(personal["scope"]), ensure_ascii=False,
    )


def test_concurrent_same_file_import_is_rejected(personal_stack, monkeypatch):
    stack = personal_stack
    scope = stack["scope"]
    imports = stack["imports"]
    data = payload(entry("src-1", content="并发导入"))
    file_sha = payload_sha256(data)
    original = imports._import_entry

    async def yielding_entry(*args, **kwargs):
        await asyncio.sleep(0)
        return await original(*args, **kwargs)

    monkeypatch.setattr(imports, "_import_entry", yielding_entry)

    async def run_twice():
        return await asyncio.gather(
            imports.import_payload(
                scope, data, operator_id="u1", file_sha256=file_sha,
                guard=lambda: None,
            ),
            imports.import_payload(
                scope, data, operator_id="u1", file_sha256=file_sha,
                guard=lambda: None,
            ),
            return_exceptions=True,
        )

    results = asyncio.run(run_twice())
    assert any(isinstance(result, Conflict) for result in results)
    assert any(not isinstance(result, Exception) for result in results)


def test_rollback_deletes_only_created_memories_and_allows_new_job(personal_stack):
    stack = personal_stack
    scope = stack["scope"]
    existing, _ = asyncio.run(stack["memories"].save_explicit(
        scope, "原有记忆", operator_id="u1",
    ))
    data = payload(entry("src-1", content="导入一"), entry("src-2", content="导入二"))

    result = run_import(stack, scope, data)
    assert result.imported == 2

    rollback = asyncio.run(stack["imports"].rollback(
        scope, result.job_id, operator_id="u1",
    ))
    assert rollback.imported == 2
    remaining = stack["store"].list_active(scope_id=scope.id, subject_id="u1")
    assert [memory.id for memory in remaining] == [existing.id]
    job = SQLiteImportJobStore(stack["db"]).get(result.job_id)
    assert job.status is ImportJobStatus.FAILED

    again = run_import(stack, scope, data)
    assert again.imported == 2
    assert again.job_id != result.job_id


def test_export_default_and_all_filters(personal_stack):
    stack = personal_stack
    scope = stack["scope"]
    active, _ = asyncio.run(stack["memories"].save_explicit(
        scope, "活跃记忆", operator_id="u1",
    ))
    archived, _ = asyncio.run(stack["memories"].save_explicit(
        scope, "归档记忆", operator_id="u1",
    ))
    asyncio.run(stack["lifecycle"].transition_memory(
        archived, MemoryStatus.ARCHIVED,
        revision_id="archive-rev", at=NOW, reason="test archive",
        operator_type=OperatorType.USER,
    ))
    deleted, _ = asyncio.run(stack["memories"].save_explicit(
        scope, "删除记忆", operator_id="u1",
    ))
    asyncio.run(stack["lifecycle"].delete_memory(
        deleted.id, scope_id=scope.id, subject_id="u1",
        operation_id="delete-op", operator_type=OperatorType.USER,
        operator_id="u1", at=NOW,
    ))

    default = stack["memories"].export_personal(scope)
    assert [item["source_memory_id"] for item in default["memories"]] == [active.id]
    assert default["memories"][0]["status"] == "ACTIVE"

    full = stack["memories"].export_personal(scope, include_all=True)
    by_id = {item["source_memory_id"]: item for item in full["memories"]}
    assert set(by_id) == {active.id, archived.id}
    assert by_id[archived.id]["status"] == "ARCHIVED"
    assert by_id[archived.id]["content"] is None
    assert by_id[archived.id]["body_omitted"] is True
    assert deleted.id not in by_id

    first = stack["memories"].export_personal(scope)
    second = stack["memories"].export_personal(scope)
    assert first["export_id"] != second["export_id"]


def test_export_payload_round_trips_between_scopes(personal_stack, tmp_path):
    source = personal_stack
    memory, _ = asyncio.run(source["memories"].save_explicit(
        source["scope"], "跨库导入事实", operator_id="u1",
    ))
    exported = source["memories"].export_personal(source["scope"])

    other_db = SQLiteDatabase(tmp_path / "other.db")
    other_db.migrate()
    store = SQLiteMemoryStore(other_db)
    index = SQLiteFTSIndex(other_db)
    ids = UUID4IdGenerator()
    coordinator = WriteCoordinator(store, index)
    scopes = PersonalScopeService(store, coordinator, FixedClock())
    consent = ConsentService(SQLiteConsentStore(other_db), coordinator, FixedClock(), ids)
    lifecycle = LifecycleService(coordinator, store, consent)
    imports = ImportService(
        store, SQLiteImportJobStore(other_db), coordinator, lifecycle, FixedClock(), ids,
    )
    other_scope = scopes.resolve(personal_identity("u2"))
    consent.grant(other_scope, PersonalConsentType.PERSONAL_MEMORY, operator_id="u2")

    result = asyncio.run(imports.import_payload(
        other_scope, json.loads(json.dumps(exported, ensure_ascii=False)),
        operator_id="u2", file_sha256=payload_sha256(exported),
        guard=lambda: consent.require(other_scope, PersonalConsentType.PERSONAL_MEMORY),
    ))
    assert result.imported == 1
    imported = store.list_active(scope_id=other_scope.id, subject_id="u2")[0]
    assert imported.content == "跨库导入事实"
    assert imported.id != memory.id
    other_db.close()


def test_export_and_import_share_schema_version(personal_stack):
    stack = personal_stack
    scope = stack["scope"]
    memory, _ = asyncio.run(stack["memories"].save_explicit(
        scope, "格式校验", operator_id="u1",
    ))
    payload_obj = build_export(
        scope, (memory,), exported_at=NOW, export_id="exp-format",
    )
    envelope = parse_export_payload(payload_obj)
    assert envelope.schema_version == EXPORT_FORMAT
    assert envelope.export_id == "exp-format"
    assert len(envelope.entries) == 1

    with pytest.raises(ValidationError):
        parse_export_payload({**payload_obj, "schema_version": "weave-memory-export-v1"})
    bad = {**payload_obj, "memories": [{**payload_obj["memories"][0], "content": None}]}
    result = run_import(stack, scope, bad)
    assert result.failed == 1
    assert result.entries[0].outcome is ImportOutcome.FAILED


def test_v8_migration_creates_import_jobs_and_rolls_back(tmp_path):
    db = SQLiteDatabase(tmp_path / "v8.db")
    db.migrate()
    assert db.schema_version() == 9
    tables = {
        row[0] for row in db.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'",
        )
    }
    assert "import_jobs" in tables
    indexes = {
        row[0] for row in db.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index'",
        )
    }
    assert "ix_memory_sources_evidence_ref" in indexes

    db.rollback_to_v7()
    assert db.schema_version() == 7
    tables = {
        row[0] for row in db.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'",
        )
    }
    assert "import_jobs" not in tables
    db.migrate()
    assert db.schema_version() == 9
    db.close()


def test_v8_rollback_is_refused_while_jobs_exist(tmp_path):
    db = SQLiteDatabase(tmp_path / "v8-jobs.db")
    db.migrate()
    db.connection.execute(
        """INSERT INTO memory_scopes
        (id, platform, tenant_id, scope_type, scope_external_id, subject_type,
         subject_id, created_at, is_enabled)
        VALUES ('scope-1', 'p', 't', 'PERSONAL', 'u1', 'USER', 'u1',
                '2026-11-23T12:00:00Z', 1)""",
    )
    db.connection.execute(
        """INSERT INTO import_jobs
        (id, scope_id, subject_id, file_sha256, dataset_version, status,
         cursor, created_memory_ids_json, created_at, updated_at)
        VALUES ('job-1', 'scope-1', 'u1', 'sha', ?, 'DONE', 0, '[]', ?, ?)""",
        (EXPORT_FORMAT, NOW.isoformat(), NOW.isoformat()),
    )
    with pytest.raises(StorageError):
        db.rollback_to_v7()
    assert db.schema_version() == 9
    db.close()
