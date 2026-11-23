from __future__ import annotations

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
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.extraction_jobs import (
    SQLiteExtractionJobStore,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.fts import SQLiteFTSIndex
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.store import (
    SQLiteMemoryStore,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.system import UUID4IdGenerator
from data.plugins.astrbot_plugin_weave_memory.app.consent_service import (
    ConsentService,
    ExtractionConsentType,
    PersonalConsentType,
)
from data.plugins.astrbot_plugin_weave_memory.app.extraction_service import (
    ExtractionService,
)
from data.plugins.astrbot_plugin_weave_memory.app.lifecycle_service import (
    LifecycleService,
)
from data.plugins.astrbot_plugin_weave_memory.app.write_coordinator import (
    WriteCoordinator,
)
from data.plugins.astrbot_plugin_weave_memory.core.errors import (
    NotFound,
    StorageError,
)
from data.plugins.astrbot_plugin_weave_memory.core.model import (
    ExtractionCandidate,
    ExtractionJobStatus,
    Identity,
    MemoryStatus,
    MemoryType,
    Scope,
    ScopeType,
    SubjectType,
)
from data.plugins.astrbot_plugin_weave_memory.core.normalization import (
    normalize_content,
)

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
_STAMP = "2026-11-23T12:00:00.000000Z"


class MutableClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.value = now

    def now(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


class FakeExtractor:
    def __init__(self, candidates=()) -> None:
        self.candidates = tuple(candidates)
        self.calls = 0

    async def extract(self, window, *, allowed_types, sensitive_categories):
        self.calls += 1
        return self.candidates


def _grant_all(consent: ConsentService, scope: Scope) -> None:
    consent.grant(scope, PersonalConsentType.PERSONAL_MEMORY, operator_id="u1")
    for consent_type in (
        ExtractionConsentType.CAPTURE,
        ExtractionConsentType.AUTO_EXTRACT,
        ExtractionConsentType.EXTERNAL_MODEL,
        ExtractionConsentType.STORE_CANDIDATE,
    ):
        consent.grant(scope, consent_type, operator_id="u1")


def make_stack(tmp_path, *, ttl_days: int = 7, keep_body: bool = False):
    db = SQLiteDatabase(tmp_path / "candidates.db")
    db.migrate()
    store = SQLiteMemoryStore(db)
    index = SQLiteFTSIndex(db)
    clock = MutableClock()
    ids = UUID4IdGenerator()
    coordinator = WriteCoordinator(store, index)
    consent = ConsentService(SQLiteConsentStore(db), coordinator, clock, ids)
    lifecycle = LifecycleService(coordinator, store, consent)
    identity = Identity(
        "astrbot", "default", ScopeType.PERSONAL, "u1", SubjectType.USER, "u1",
    )
    scope = Scope("scope-candidates", identity, NOW)
    store.create_scope(scope)
    extractor = FakeExtractor()
    service = ExtractionService(
        store=store, jobs=SQLiteExtractionJobStore(db), coordinator=coordinator,
        consent=consent, scopes=store, lifecycle=lifecycle, extractor=extractor,
        clock=clock, ids=ids, global_enabled=True,
        candidate_ttl_days=ttl_days, rejected_keep_body=keep_body,
    )
    _grant_all(consent, scope)
    return {
        "db": db, "store": store, "index": index, "clock": clock,
        "consent": consent, "scope": scope, "extractor": extractor,
        "service": service,
    }


def submit(stack, content: str = "用户喜欢黑咖啡", *, message_id: str = "m1",
           conversation_id: str = "conv") -> None:
    stack["extractor"].candidates = (
        ExtractionCandidate(MemoryType.PREFERENCE, content),
    )
    job = asyncio.run(stack["service"].submit(
        stack["scope"], conversation_id=conversation_id,
        message_id=message_id, content=content,
    ))
    assert job is not None and job.status is ExtractionJobStatus.SUCCEEDED


def operations(stack) -> list[dict[str, object]]:
    rows = stack["db"].connection.execute(
        "SELECT operation_type, operator_type, metadata_json FROM memory_operations "
        "ORDER BY rowid",
    ).fetchall()
    return [
        {
            "operation_type": row["operation_type"],
            "operator_type": row["operator_type"],
            "metadata": json.loads(row["metadata_json"]),
        }
        for row in rows
    ]


def test_candidate_created_with_ttl_and_lazily_archived(tmp_path):
    stack = make_stack(tmp_path, ttl_days=7)
    submit(stack)
    candidates, total = asyncio.run(stack["service"].list_candidates(stack["scope"]))
    assert total == 1
    candidate = candidates[0]
    assert candidate.candidate_expires_at == NOW + timedelta(days=7)

    stack["clock"].advance(timedelta(days=8))
    remaining, total = asyncio.run(stack["service"].list_candidates(stack["scope"]))
    assert total == 0 and remaining == ()
    archived = stack["store"].get(
        candidate.id, scope_id=stack["scope"].id, subject_id="u1",
    )
    assert archived.status is MemoryStatus.ARCHIVED
    assert archived.content == "用户喜欢黑咖啡"
    assert archived.candidate_expires_at is None
    assert stack["index"].search(
        "黑咖啡", scope_id=stack["scope"].id, subject_id="u1",
    ) == ()
    expire_ops = [
        item for item in operations(stack)
        if item["operation_type"] == "CANDIDATE_EXPIRE"
    ]
    assert len(expire_ops) == 1
    metadata = expire_ops[0]["metadata"]
    assert metadata["from_status"] == "CANDIDATE"
    assert metadata["to_status"] == "ARCHIVED"
    assert metadata["result"] == "SUCCESS"
    assert "用户喜欢黑咖啡" not in json.dumps(metadata, ensure_ascii=False)


def test_confirm_writes_candidate_audit(tmp_path):
    stack = make_stack(tmp_path)
    submit(stack)
    candidate = asyncio.run(stack["service"].list_candidates(stack["scope"]))[0][0]
    confirmed = asyncio.run(stack["service"].confirm_candidate(
        stack["scope"], candidate.id, expected_revision=candidate.current_revision,
        operator_id="u1",
    ))
    assert confirmed.status is MemoryStatus.ACTIVE
    assert confirmed.candidate_expires_at is None
    confirm_ops = [
        item for item in operations(stack)
        if item["operation_type"] == "CANDIDATE_CONFIRM"
    ]
    assert len(confirm_ops) == 1
    metadata = confirm_ops[0]["metadata"]
    assert metadata["from_status"] == "CANDIDATE"
    assert metadata["to_status"] == "ACTIVE"
    assert metadata["memory_type"] == "PREFERENCE"
    assert "用户喜欢黑咖啡" not in json.dumps(metadata, ensure_ascii=False)


def test_reject_default_purges_body_and_keeps_audit(tmp_path):
    stack = make_stack(tmp_path)
    submit(stack)
    candidate = asyncio.run(stack["service"].list_candidates(stack["scope"]))[0][0]
    rejected = asyncio.run(stack["service"].reject_candidate(
        stack["scope"], candidate.id, expected_revision=candidate.current_revision,
        reason="不准确", operator_id="u1",
    ))
    assert rejected is None
    with pytest.raises(NotFound):
        stack["store"].get(candidate.id, scope_id=stack["scope"].id, subject_id="u1")
    for table, column in (
        ("memories", "id"), ("memory_revisions", "memory_id"),
        ("memory_sources", "memory_id"),
    ):
        assert stack["db"].connection.execute(
            f"SELECT count(*) FROM {table} WHERE {column}=?", (candidate.id,),
        ).fetchone()[0] == 0
    reject_ops = [
        item for item in operations(stack)
        if item["operation_type"] == "CANDIDATE_REJECT"
    ]
    assert len(reject_ops) == 1
    metadata = reject_ops[0]["metadata"]
    assert metadata["reason"] == "不准确"
    assert metadata["body_retained"] is False
    assert "用户喜欢黑咖啡" not in json.dumps(metadata, ensure_ascii=False)
    assert stack["db"].connection.execute(
        "SELECT value FROM memory_meta WHERE key='secure_purge_pending'",
    ).fetchone()[0] == "1"

    submit(stack, message_id="m2", conversation_id="conv-2")
    candidates, total = asyncio.run(stack["service"].list_candidates(stack["scope"]))
    assert total == 1
    assert candidates[0].status is MemoryStatus.CANDIDATE
    assert candidates[0].id != candidate.id


def test_reject_keep_body_preserves_rejected_row(tmp_path):
    stack = make_stack(tmp_path, keep_body=True)
    submit(stack)
    candidate = asyncio.run(stack["service"].list_candidates(stack["scope"]))[0][0]
    rejected = asyncio.run(stack["service"].reject_candidate(
        stack["scope"], candidate.id, expected_revision=candidate.current_revision,
        reason="保留正文", operator_id="u1",
    ))
    assert rejected is not None
    assert rejected.status is MemoryStatus.REJECTED
    assert rejected.content == "用户喜欢黑咖啡"
    stored = stack["store"].get(
        candidate.id, scope_id=stack["scope"].id, subject_id="u1",
    )
    assert stored.status is MemoryStatus.REJECTED
    assert len(stack["store"].list_revisions(
        candidate.id, scope_id=stack["scope"].id, subject_id="u1",
    )) == 2
    reject_ops = [
        item for item in operations(stack)
        if item["operation_type"] == "CANDIDATE_REJECT"
    ]
    assert reject_ops[0]["metadata"]["body_retained"] is True


def test_candidate_ttl_days_configuration_is_used(tmp_path):
    stack = make_stack(tmp_path, ttl_days=1)
    submit(stack)
    candidate = asyncio.run(stack["service"].list_candidates(stack["scope"]))[0][0]
    assert candidate.candidate_expires_at == NOW + timedelta(days=1)


def _seed_v6_candidate(db, *, scope_id: str = "scope-v6",
                       subject_id: str = "u6", memory_id: str = "mem-v6",
                       content: str = "迁移前候选") -> None:
    with db.transaction() as conn:
        conn.execute(
            """INSERT INTO memory_scopes
            (id, platform, tenant_id, scope_type, scope_external_id, subject_type,
             subject_id, created_at, is_enabled, disabled_at, admin_ids_json,
             admin_source)
            VALUES (?, 'astrbot', 'default', 'PERSONAL', ?, 'USER', ?, ?, 1,
                    NULL, '[]', 'NONE')""",
            (scope_id, subject_id, subject_id, _STAMP),
        )
        conn.execute(
            """INSERT INTO memories
            (id, scope_id, subject_id, memory_type, content, normalized_content,
             normalization_version, status, source_type, current_revision,
             valid_from, valid_until, superseded_by, created_at, updated_at,
             last_accessed_at, metadata_json, db_lineage_id, creation_seq)
            VALUES (?, ?, ?, 'PREFERENCE', ?, ?, 1, 'CANDIDATE', 'INFERRED', 1,
                    NULL, NULL, NULL, ?, ?, NULL, '{}',
                    (SELECT value FROM memory_meta WHERE key='db_lineage_id'),
                    (SELECT COALESCE(MAX(creation_seq), 0) + 1 FROM memories))""",
            (memory_id, scope_id, subject_id, content,
             normalize_content(content), _STAMP, _STAMP),
        )


def test_v7_migration_backfills_candidate_expiry(tmp_path):
    db = SQLiteDatabase(tmp_path / "v6-backfill.db")
    db.migrate()
    db.rollback_to_v6()
    assert db.schema_version() == 6
    _seed_v6_candidate(db)
    db.migrate()
    assert db.schema_version() == 9
    store = SQLiteMemoryStore(db)
    memory = store.get("mem-v6", scope_id="scope-v6", subject_id="u6")
    assert memory.status is MemoryStatus.CANDIDATE
    assert memory.candidate_expires_at == NOW + timedelta(days=7)
    db.close()


def test_v7_rollback_requires_no_candidates(tmp_path):
    db = SQLiteDatabase(tmp_path / "v7-rollback.db")
    db.migrate()
    db.rollback_to_v6()
    _seed_v6_candidate(db)
    db.migrate()
    with pytest.raises(StorageError):
        db.rollback_to_v6()
    assert db.schema_version() == 9
    db.connection.execute("DELETE FROM memories WHERE id='mem-v6'")
    db.rollback_to_v6()
    assert db.schema_version() == 6
    columns = {
        row["name"] for row in db.connection.execute("PRAGMA table_info(memories)")
    }
    assert "candidate_expires_at" not in columns
    db.migrate()
    assert db.schema_version() == 9
    db.close()
