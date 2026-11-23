from __future__ import annotations

import asyncio
from datetime import UTC, datetime

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
    AuthorizationDenied,
    Conflict,
    StorageError,
)
from data.plugins.astrbot_plugin_weave_memory.core.model import (
    ExtractionCandidate,
    ExtractionJob,
    ExtractionJobStatus,
    ExtractionWindow,
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

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


class FixedClock:
    def now(self) -> datetime:
        return NOW


class FakeExtractor:
    def __init__(self, candidates=(), error: Exception | None = None) -> None:
        self.candidates = tuple(candidates)
        self.error = error
        self.calls: list[ExtractionWindow] = []

    async def extract(self, window, *, allowed_types, sensitive_categories):
        self.calls.append(window)
        if self.error is not None:
            raise self.error
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


@pytest.fixture
def extraction_stack(tmp_path):
    db = SQLiteDatabase(tmp_path / "extraction.db")
    db.migrate()
    store = SQLiteMemoryStore(db)
    index = SQLiteFTSIndex(db)
    clock = FixedClock()
    ids = UUID4IdGenerator()
    coordinator = WriteCoordinator(store, index)
    consent = ConsentService(SQLiteConsentStore(db), coordinator, clock, ids)
    lifecycle = LifecycleService(coordinator, store, consent)
    jobs = SQLiteExtractionJobStore(db)
    identity = Identity("astrbot", "default", ScopeType.PERSONAL, "u1", SubjectType.USER, "u1")
    scope = Scope("scope-extract", identity, NOW)
    store.create_scope(scope)
    extractor = FakeExtractor()
    service = ExtractionService(
        store=store, jobs=jobs, coordinator=coordinator, consent=consent,
        scopes=store, lifecycle=lifecycle, extractor=extractor,
        clock=clock, ids=ids, global_enabled=True,
    )
    yield {
        "db": db, "store": store, "index": index, "coordinator": coordinator,
        "consent": consent, "scope": scope, "jobs": jobs,
        "extractor": extractor, "service": service, "clock": clock, "ids": ids,
    }
    db.close()


def _submit(stack, content: str, *, message_id: str = "msg-1"):
    return asyncio.run(stack["service"].submit(
        stack["scope"], conversation_id="conv-1", message_id=message_id,
        content=content,
    ))


def _active(store: SQLiteMemoryStore, scope: Scope, content: str, memory_type=MemoryType.PREFERENCE) -> MemoryEntity:
    now = NOW
    memory_id = "active-existing"
    memory = MemoryEntity(
        id=memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
        memory_type=memory_type, content=content,
        normalized_content=normalize_content(content), status=MemoryStatus.ACTIVE,
        source_type=SourceType.EXPLICIT, current_revision=1,
        created_at=now, updated_at=now,
    )
    revision = MemoryRevision(memory_id + "-r1", memory_id, 1, content, MemoryStatus.ACTIVE, now, "seed")
    source = MemorySource(memory_id + "-s1", memory_id, SourceType.EXPLICIT, OperatorType.USER, "u1", now)
    store.create(memory, revision, source)
    return memory


def test_extraction_requires_all_consent_layers(extraction_stack):
    stack = extraction_stack
    assert _submit(stack, "我喜欢黑咖啡") is None
    assert stack["extractor"].calls == []
    assert stack["jobs"].count_by_status(
        scope_id=stack["scope"].id, subject_id="u1",
    )[ExtractionJobStatus.PENDING.value] == 0


def test_missing_external_model_blocks_extract_without_rewriting_consent(extraction_stack):
    stack = extraction_stack
    consent = stack["consent"]
    scope = stack["scope"]
    consent.grant(scope, PersonalConsentType.PERSONAL_MEMORY, operator_id="u1")
    for consent_type in (
        ExtractionConsentType.CAPTURE,
        ExtractionConsentType.AUTO_EXTRACT,
        ExtractionConsentType.STORE_CANDIDATE,
    ):
        consent.grant(scope, consent_type, operator_id="u1")
    assert _submit(stack, "我喜欢黑咖啡") is None
    assert stack["extractor"].calls == []
    assert consent.is_granted(scope, ExtractionConsentType.AUTO_EXTRACT) is True
    assert consent.is_granted(scope, ExtractionConsentType.EXTERNAL_MODEL) is False


def test_sensitive_message_is_not_captured_before_model(extraction_stack):
    stack = extraction_stack
    _grant_all(stack["consent"], stack["scope"])
    assert _submit(stack, "我的密码是 hunter2") is None
    assert stack["extractor"].calls == []
    assert stack["jobs"].count_by_status(
        scope_id=stack["scope"].id, subject_id="u1",
    )[ExtractionJobStatus.PENDING.value] == 0


def test_sensitive_whitelist_allows_candidate(extraction_stack):
    stack = extraction_stack
    _grant_all(stack["consent"], stack["scope"])
    stack["consent"].grant(
        stack["scope"], ExtractionConsentType.SENSITIVE_CATEGORIES,
        operator_id="u1", metadata={"categories": ["credential"]},
    )
    stack["extractor"].candidates = (
        ExtractionCandidate(MemoryType.FACT, "用户密码是 hunter2", confidence=0.9),
    )
    job = _submit(stack, "我的密码是 hunter2")
    assert job is not None and job.status is ExtractionJobStatus.SUCCEEDED
    candidates, total = stack["service"].list_candidates(stack["scope"])
    assert total == 1
    assert candidates[0].status is MemoryStatus.CANDIDATE


def test_candidate_is_inferred_not_searchable_until_confirmed(extraction_stack):
    stack = extraction_stack
    _grant_all(stack["consent"], stack["scope"])
    stack["extractor"].candidates = (
        ExtractionCandidate(MemoryType.PREFERENCE, "用户喜欢黑咖啡", confidence=0.8),
    )
    job = _submit(stack, "我喜欢黑咖啡")
    assert job is not None and job.status is ExtractionJobStatus.SUCCEEDED
    candidates, total = stack["service"].list_candidates(stack["scope"])
    assert total == 1
    candidate = candidates[0]
    assert candidate.status is MemoryStatus.CANDIDATE
    assert candidate.source_type is SourceType.INFERRED
    assert stack["index"].search("黑咖啡", scope_id=stack["scope"].id, subject_id="u1") == ()

    confirmed = asyncio.run(stack["service"].confirm_candidate(
        stack["scope"], candidate.id, expected_revision=candidate.current_revision,
    ))
    assert confirmed.status is MemoryStatus.ACTIVE
    assert stack["index"].search("黑咖啡", scope_id=stack["scope"].id, subject_id="u1") == (candidate.id,)


def test_active_duplicate_skips_candidate(extraction_stack):
    stack = extraction_stack
    _grant_all(stack["consent"], stack["scope"])
    _active(stack["store"], stack["scope"], "用户喜欢黑咖啡")
    stack["extractor"].candidates = (
        ExtractionCandidate(MemoryType.PREFERENCE, "用户喜欢黑咖啡"),
    )
    job = _submit(stack, "我喜欢黑咖啡")
    assert job is not None and job.status is ExtractionJobStatus.SKIPPED_DUPLICATE_ACTIVE
    assert stack["service"].list_candidates(stack["scope"])[1] == 0


def test_repeated_submission_reuses_job_and_candidate(extraction_stack):
    stack = extraction_stack
    _grant_all(stack["consent"], stack["scope"])
    stack["extractor"].candidates = (
        ExtractionCandidate(MemoryType.PREFERENCE, "用户喜欢黑咖啡"),
    )
    first = _submit(stack, "我喜欢黑咖啡", message_id="msg-1")
    second = _submit(stack, "我喜欢黑咖啡", message_id="msg-2")
    assert first is not None and second is not None
    assert first.id == second.id
    assert stack["service"].list_candidates(stack["scope"])[1] == 1


def test_confirm_revision_conflict_and_reject(extraction_stack):
    stack = extraction_stack
    _grant_all(stack["consent"], stack["scope"])
    stack["extractor"].candidates = (
        ExtractionCandidate(MemoryType.FACT, "用户住在杭州"),
    )
    _submit(stack, "我住在杭州")
    candidate = stack["service"].list_candidates(stack["scope"])[0][0]
    with pytest.raises(Conflict):
        asyncio.run(stack["service"].confirm_candidate(
            stack["scope"], candidate.id, expected_revision=99,
        ))
    rejected = asyncio.run(stack["service"].reject_candidate(
        stack["scope"], candidate.id, expected_revision=candidate.current_revision,
        reason="不准确",
    ))
    assert rejected.status is MemoryStatus.REJECTED
    assert stack["service"].list_candidates(stack["scope"])[1] == 0


def test_extractor_failure_does_not_raise_and_job_can_retry(extraction_stack):
    stack = extraction_stack
    _grant_all(stack["consent"], stack["scope"])
    stack["extractor"].error = RuntimeError("provider down")
    failed = _submit(stack, "我喜欢黑咖啡")
    assert failed is not None and failed.status is ExtractionJobStatus.PENDING
    assert failed.last_error == "RuntimeError"
    stack["extractor"].error = None
    stack["extractor"].candidates = (
        ExtractionCandidate(MemoryType.PREFERENCE, "用户喜欢黑咖啡"),
    )
    retried = _submit(stack, "我喜欢黑咖啡")
    assert retried is not None and retried.status is ExtractionJobStatus.SUCCEEDED


def test_recover_interrupted_marks_running_job_failed(extraction_stack):
    stack = extraction_stack
    job = ExtractionJob(
        id="job-running", platform="astrbot", tenant_id="default",
        scope_id=stack["scope"].id, subject_id="u1", conversation_id="conv",
        watermark=0, content_hash="hash", status=ExtractionJobStatus.RUNNING,
        attempts=1, created_at=NOW, updated_at=NOW,
    )
    stack["jobs"].create_or_get(job)
    assert stack["service"].recover_interrupted() == 1
    assert stack["jobs"].get(job.id).status is ExtractionJobStatus.FAILED


def test_batch_messages_drops_blocked_sensitive_messages(extraction_stack):
    stack = extraction_stack
    _grant_all(stack["consent"], stack["scope"])
    stack["extractor"].candidates = (
        ExtractionCandidate(MemoryType.PREFERENCE, "用户喜欢黑咖啡"),
    )
    job = asyncio.run(stack["service"].submit_messages(
        stack["scope"], conversation_id="conv",
        messages=(("m1", "我喜欢黑咖啡"), ("m2", "我的密码是 hunter2")),
        watermark=2,
    ))
    assert job is not None and job.status is ExtractionJobStatus.SUCCEEDED
    assert stack["extractor"].calls[0].messages == ("我喜欢黑咖啡",)
    assert stack["service"].list_candidates(stack["scope"])[1] == 1


def test_global_flag_off_blocks_capture_even_when_user_opted_in(extraction_stack):
    stack = extraction_stack
    _grant_all(stack["consent"], stack["scope"])
    stack["service"]._global_enabled = False
    assert _submit(stack, "我喜欢黑咖啡") is None
    assert stack["extractor"].calls == []


def test_queue_full_marks_job_failed(extraction_stack, monkeypatch):
    stack = extraction_stack
    _grant_all(stack["consent"], stack["scope"])
    monkeypatch.setattr(stack["service"], "_pending_count", lambda scope: 999)
    job = _submit(stack, "我喜欢黑咖啡")
    assert job is not None
    assert job.status is ExtractionJobStatus.FAILED
    assert job.last_error == "queue_full"
    assert stack["extractor"].calls == []


def test_rollback_to_v5_refuses_nonempty_jobs(extraction_stack):
    stack = extraction_stack
    job = ExtractionJob(
        id="job-lock", platform="astrbot", tenant_id="default",
        scope_id=stack["scope"].id, subject_id="u1", conversation_id="conv",
        watermark=0, content_hash="hash", status=ExtractionJobStatus.PENDING,
        attempts=0, created_at=NOW, updated_at=NOW,
    )
    stack["jobs"].create_or_get(job)
    with pytest.raises(StorageError):
        stack["db"].rollback_to_v5()


def test_empty_extraction_jobs_rolls_back_to_v5(extraction_stack):
    stack = extraction_stack
    stack["db"].rollback_to_v5()
    assert stack["db"].schema_version() == 5
    assert stack["db"].connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='extraction_jobs'",
    ).fetchone() is None


def test_candidate_write_rechecks_consent_gate(extraction_stack, monkeypatch):
    stack = extraction_stack
    _grant_all(stack["consent"], stack["scope"])
    stack["extractor"].candidates = (
        ExtractionCandidate(MemoryType.PREFERENCE, "用户喜欢黑咖啡"),
    )

    calls = {"count": 0}

    def fail_on_write(scope):
        calls["count"] += 1
        raise AuthorizationDenied("revoked")

    monkeypatch.setattr(stack["service"], "_require_store_gate", fail_on_write)
    job = _submit(stack, "我喜欢黑咖啡")
    assert job is not None and job.status is ExtractionJobStatus.FAILED
    assert stack["service"].list_candidates(stack["scope"])[1] == 0
