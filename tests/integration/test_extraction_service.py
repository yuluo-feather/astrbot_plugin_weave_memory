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
from data.plugins.astrbot_plugin_weave_memory.app.memory_service import (
    MemoryApplicationService,
)
from data.plugins.astrbot_plugin_weave_memory.app.write_coordinator import (
    WriteCoordinator,
)
from data.plugins.astrbot_plugin_weave_memory.core.errors import (
    AuthorizationDenied,
    Conflict,
    NotFound,
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
    def __init__(self, candidates=(), error: Exception | None = None,
                 fail_times: int = 0) -> None:
        self.candidates = tuple(candidates)
        self.error = error
        self.fail_times = fail_times
        self.calls: list[ExtractionWindow] = []
        self.summary_candidate: ExtractionCandidate | None = None
        self.summary_calls: list[ExtractionWindow] = []

    async def extract(self, window, *, allowed_types, sensitive_categories):
        self.calls.append(window)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("transient failure")
        if self.error is not None:
            raise self.error
        return self.candidates

    async def summarize(self, window, *, sensitive_categories):
        self.summary_calls.append(window)
        return self.summary_candidate


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


def _seed_active(store, scope: Scope, content: str, index: int,
                 memory_type=MemoryType.PREFERENCE) -> MemoryEntity:
    memory_id = f"seed-{index}"
    memory = MemoryEntity(
        id=memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
        memory_type=memory_type, content=content,
        normalized_content=normalize_content(content), status=MemoryStatus.ACTIVE,
        source_type=SourceType.EXPLICIT, current_revision=1,
        created_at=NOW, updated_at=NOW,
    )
    revision = MemoryRevision(
        memory_id + "-r1", memory_id, 1, content, MemoryStatus.ACTIVE, NOW, "seed",
    )
    source = MemorySource(
        memory_id + "-s1", memory_id, SourceType.EXPLICIT,
        OperatorType.USER, "u1", NOW,
    )
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
    consent.revoke(scope, ExtractionConsentType.EXTERNAL_MODEL, operator_id="u1")
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
    candidates, total = asyncio.run(stack["service"].list_candidates(stack["scope"]))
    assert total == 1
    assert candidates[0].status is MemoryStatus.CANDIDATE


def test_candidate_is_inferred_not_searchable_until_confirmed(extraction_stack):
    stack = extraction_stack
    _grant_all(stack["consent"], stack["scope"])
    stack["extractor"].candidates = (
        ExtractionCandidate(MemoryType.PREFERENCE, "用户喜欢黑咖啡", confidence=0.5),
    )
    job = _submit(stack, "我喜欢黑咖啡")
    assert job is not None and job.status is ExtractionJobStatus.SUCCEEDED
    candidates, total = asyncio.run(stack["service"].list_candidates(stack["scope"]))
    assert total == 1
    candidate = candidates[0]
    assert candidate.status is MemoryStatus.CANDIDATE
    assert candidate.source_type is SourceType.INFERRED
    assert stack["index"].search("黑咖啡", scope_id=stack["scope"].id, subject_id="u1") == ()

    confirmed = asyncio.run(stack["service"].confirm_candidate(
        stack["scope"], candidate.id, expected_revision=candidate.current_revision,
    ))
    assert confirmed.status is MemoryStatus.ACTIVE
    assert confirmed.metadata["auto"] is False
    assert confirmed.metadata["user_confirmed"] is True
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
    assert asyncio.run(stack["service"].list_candidates(stack["scope"]))[1] == 0


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
    assert asyncio.run(stack["service"].list_candidates(stack["scope"]))[1] == 1


def test_confirm_revision_conflict_and_reject(extraction_stack):
    stack = extraction_stack
    _grant_all(stack["consent"], stack["scope"])
    stack["extractor"].candidates = (
        ExtractionCandidate(MemoryType.FACT, "用户住在杭州"),
    )
    _submit(stack, "我住在杭州")
    candidate = asyncio.run(stack["service"].list_candidates(stack["scope"]))[0][0]
    with pytest.raises(Conflict):
        asyncio.run(stack["service"].confirm_candidate(
            stack["scope"], candidate.id, expected_revision=99,
        ))
    rejected = asyncio.run(stack["service"].reject_candidate(
        stack["scope"], candidate.id, expected_revision=candidate.current_revision,
        reason="不准确",
    ))
    assert rejected is None
    with pytest.raises(NotFound):
        stack["store"].get(
            candidate.id, scope_id=stack["scope"].id, subject_id="u1",
        )
    operations = stack["store"].db.connection.execute(
        "SELECT operation_type, metadata_json FROM memory_operations "
        "WHERE memory_id=? ORDER BY rowid", (candidate.id,),
    ).fetchall()
    assert [row["operation_type"] for row in operations] == ["CANDIDATE_REJECT"]
    assert "不准确" in operations[0]["metadata_json"]
    assert "用户住在杭州" not in operations[0]["metadata_json"]
    assert asyncio.run(stack["service"].list_candidates(stack["scope"]))[1] == 0


def test_extractor_failure_retries_in_process_then_succeeds(extraction_stack):
    stack = extraction_stack
    _grant_all(stack["consent"], stack["scope"])
    stack["extractor"].fail_times = 1
    stack["extractor"].candidates = (
        ExtractionCandidate(MemoryType.PREFERENCE, "用户喜欢黑咖啡"),
    )
    job = _submit(stack, "我喜欢黑咖啡")
    assert job is not None and job.status is ExtractionJobStatus.SUCCEEDED
    assert len(stack["extractor"].calls) == 2
    assert stack["jobs"].get(job.id).attempts == 2


def test_extractor_failure_exhausts_retries_and_marks_failed(extraction_stack):
    stack = extraction_stack
    _grant_all(stack["consent"], stack["scope"])
    stack["extractor"].error = RuntimeError("provider down")
    job = _submit(stack, "我喜欢黑咖啡")
    assert job is not None and job.status is ExtractionJobStatus.FAILED
    assert job.last_error == "RuntimeError"
    assert len(stack["extractor"].calls) == 3
    assert stack["jobs"].count_by_status(
        scope_id=stack["scope"].id, subject_id="u1",
    )[ExtractionJobStatus.PENDING.value] == 0


def test_recover_interrupted_marks_running_and_pending_jobs_failed(extraction_stack):
    stack = extraction_stack
    running = ExtractionJob(
        id="job-running", platform="astrbot", tenant_id="default",
        scope_id=stack["scope"].id, subject_id="u1", conversation_id="conv",
        watermark=0, content_hash="hash", status=ExtractionJobStatus.RUNNING,
        attempts=1, created_at=NOW, updated_at=NOW,
    )
    pending = ExtractionJob(
        id="job-pending", platform="astrbot", tenant_id="default",
        scope_id=stack["scope"].id, subject_id="u1", conversation_id="conv",
        watermark=1, content_hash="hash-2", status=ExtractionJobStatus.PENDING,
        attempts=0, created_at=NOW, updated_at=NOW,
    )
    stack["jobs"].create_or_get(running)
    stack["jobs"].create_or_get(pending)
    assert stack["service"].recover_interrupted() == 2
    assert stack["jobs"].get(running.id).status is ExtractionJobStatus.FAILED
    assert stack["jobs"].get(pending.id).status is ExtractionJobStatus.FAILED


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
    assert asyncio.run(stack["service"].list_candidates(stack["scope"]))[1] == 1


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
    assert asyncio.run(stack["service"].list_candidates(stack["scope"]))[1] == 0


def test_explicit_save_rejects_same_content_candidate_with_friendly_message(extraction_stack):
    stack = extraction_stack
    _grant_all(stack["consent"], stack["scope"])
    stack["extractor"].candidates = (
        ExtractionCandidate(MemoryType.PREFERENCE, "用户喜欢黑咖啡"),
    )
    _submit(stack, "我喜欢黑咖啡")
    app = MemoryApplicationService(
        stack["store"], stack["coordinator"], stack["consent"],
        stack["service"]._lifecycle, stack["clock"], stack["ids"],
    )
    with pytest.raises(Conflict, match="已存在同内容候选记忆"):
        asyncio.run(app.save_explicit(
            stack["scope"], "用户喜欢黑咖啡", operator_id="u1",
            memory_type=MemoryType.PREFERENCE,
        ))


def test_revoked_consent_before_model_call_marks_job_failed(extraction_stack):
    stack = extraction_stack
    scope = stack["scope"]
    _grant_all(stack["consent"], scope)
    now = stack["clock"].now()
    job = ExtractionJob(
        id="job-revoked", platform="astrbot", tenant_id="default",
        scope_id=scope.id, subject_id="u1", conversation_id="conv-1",
        watermark=1, content_hash="hash-1",
        status=ExtractionJobStatus.PENDING, attempts=0,
        created_at=now, updated_at=now,
    )
    stack["jobs"].create_or_get(job)
    stack["consent"].revoke(
        scope, ExtractionConsentType.CAPTURE, operator_id="u1",
    )
    window = ExtractionWindow(
        scope_id=scope.id, subject_id="u1", conversation_id="conv-1",
        watermark=1, content_hash="hash-1", messages=("我喜欢黑咖啡",),
    )
    result = asyncio.run(stack["service"]._run(
        scope, job, window, stack["service"].gates(scope), None,
    ))
    assert result.status is ExtractionJobStatus.FAILED
    assert result.last_error == "consent_revoked"
    assert stack["extractor"].calls == []


def test_cancel_job_stops_further_model_calls(extraction_stack):
    stack = extraction_stack
    scope = stack["scope"]
    _grant_all(stack["consent"], scope)
    now = stack["clock"].now()
    stack["jobs"].create_or_get(ExtractionJob(
        id="job-cancel", platform="astrbot", tenant_id="default",
        scope_id=scope.id, subject_id="u1", conversation_id="conv-1",
        watermark=1, content_hash="hash-1",
        status=ExtractionJobStatus.PENDING, attempts=0,
        created_at=now, updated_at=now,
    ))

    cancelled = stack["service"].cancel_job(scope, "job-cancel", operator_id="u1")
    assert cancelled.status is ExtractionJobStatus.CANCELLED
    assert stack["jobs"].get("job-cancel").status is ExtractionJobStatus.CANCELLED

    window = ExtractionWindow(
        scope_id=scope.id, subject_id="u1", conversation_id="conv-1",
        watermark=1, content_hash="hash-1", messages=("我喜欢黑咖啡",),
    )
    result = asyncio.run(stack["service"]._run(
        scope, cancelled, window, stack["service"].gates(scope), None,
    ))
    assert result.status is ExtractionJobStatus.CANCELLED
    assert stack["extractor"].calls == []

    with pytest.raises(Conflict):
        stack["service"].cancel_job(scope, "job-cancel", operator_id="u1")


def _service_with(stack, **overrides) -> ExtractionService:
    options = {
        "store": stack["store"], "jobs": stack["jobs"],
        "coordinator": stack["coordinator"], "consent": stack["consent"],
        "scopes": stack["store"], "lifecycle": stack["service"]._lifecycle,
        "extractor": stack["extractor"], "clock": stack["clock"],
        "ids": stack["ids"], "global_enabled": True,
    }
    options.update(overrides)
    return ExtractionService(**options)


def test_default_grants_enable_extraction_without_explicit_consents(extraction_stack):
    stack = extraction_stack
    stack["consent"].grant(
        stack["scope"], PersonalConsentType.PERSONAL_MEMORY, operator_id="u1",
    )
    job = _submit(stack, "我喜欢黑咖啡")
    assert job is not None and job.status is ExtractionJobStatus.SUCCEEDED
    assert len(stack["extractor"].calls) == 1
    gates = stack["service"].gates(stack["scope"])
    assert gates.capture_consent and gates.auto_extract_consent
    assert gates.store_candidate_consent and gates.external_model_consent


def test_explicit_revoke_beats_default_grant(extraction_stack):
    stack = extraction_stack
    _grant_all(stack["consent"], stack["scope"])
    stack["consent"].revoke(
        stack["scope"], ExtractionConsentType.CAPTURE, operator_id="u1",
    )
    assert _submit(stack, "我喜欢黑咖啡") is None
    assert stack["extractor"].calls == []


def test_direct_write_creates_active_memory_and_audit(extraction_stack):
    stack = extraction_stack
    _grant_all(stack["consent"], stack["scope"])
    stack["extractor"].candidates = (
        ExtractionCandidate(MemoryType.FACT, "用户住在杭州", confidence=0.9),
    )
    job = _submit(stack, "我住在杭州")
    assert job is not None and job.status is ExtractionJobStatus.SUCCEEDED
    rows, total = stack["store"].list_active_page(
        scope_id=stack["scope"].id, subject_id="u1", limit=10, offset=0,
    )
    assert total == 1
    memory = rows[0]
    assert memory.status is MemoryStatus.ACTIVE
    assert memory.source_type is SourceType.INFERRED
    assert memory.metadata["auto"] is True
    assert memory.metadata["direct_write"] is True
    assert memory.metadata["commit_rule"] == "explicit_first_person_stable_attribute"
    assert stack["index"].search(
        "杭州", scope_id=stack["scope"].id, subject_id="u1",
    ) == (memory.id,)
    operations = stack["store"].db.connection.execute(
        "SELECT operation_type, metadata_json FROM memory_operations "
        "WHERE memory_id=? ORDER BY rowid", (memory.id,),
    ).fetchall()
    assert [row["operation_type"] for row in operations] == ["AUTO_COMMIT"]
    assert "杭州" not in operations[0]["metadata_json"]


def test_event_and_low_confidence_stay_candidates(extraction_stack):
    stack = extraction_stack
    _grant_all(stack["consent"], stack["scope"])
    stack["extractor"].candidates = (
        ExtractionCandidate(MemoryType.EVENT, "用户今天去了杭州", confidence=0.9),
        ExtractionCandidate(MemoryType.PREFERENCE, "用户喜欢黑咖啡", confidence=0.5),
    )
    _submit(stack, "我今天去了杭州，也喜欢黑咖啡")
    candidates, total = asyncio.run(
        stack["service"].list_candidates(stack["scope"]),
    )
    assert total == 2
    assert all(item.status is MemoryStatus.CANDIDATE for item in candidates)


def test_conflicting_active_memory_forces_candidate(extraction_stack):
    stack = extraction_stack
    _grant_all(stack["consent"], stack["scope"])
    _active(stack["store"], stack["scope"], "用户喜欢黑咖啡")
    stack["extractor"].candidates = (
        ExtractionCandidate(MemoryType.PREFERENCE, "用户喜欢绿茶", confidence=0.9),
    )
    _submit(stack, "我喜欢绿茶")
    candidates, total = asyncio.run(
        stack["service"].list_candidates(stack["scope"]),
    )
    assert total == 1
    assert candidates[0].metadata["direct_write"] is False


def test_near_duplicate_active_memory_is_skipped(extraction_stack):
    stack = extraction_stack
    _grant_all(stack["consent"], stack["scope"])
    _active(stack["store"], stack["scope"], "用户喜欢喝黑咖啡")
    stack["extractor"].candidates = (
        ExtractionCandidate(MemoryType.PREFERENCE, "用户喜欢黑咖啡", confidence=0.9),
    )
    job = _submit(stack, "我喜欢黑咖啡")
    assert job is not None
    assert job.status is ExtractionJobStatus.SKIPPED_DUPLICATE_ACTIVE
    rows, total = stack["store"].list_active_page(
        scope_id=stack["scope"].id, subject_id="u1", limit=10, offset=0,
    )
    assert total == 1
    survivor = rows[0]
    assert survivor.content == "用户喜欢喝黑咖啡"
    assert survivor.current_revision == 2
    assert survivor.metadata["merged_sources"] == [f"extraction_job:{job.id}"]
    operations = stack["store"].db.connection.execute(
        "SELECT operation_type, metadata_json FROM memory_operations "
        "WHERE memory_id=? ORDER BY rowid", (survivor.id,),
    ).fetchall()
    assert [row["operation_type"] for row in operations] == ["MEMORY_MERGE"]
    assert "黑咖啡" not in operations[0]["metadata_json"]


def test_auto_commit_disabled_is_candidate_only(extraction_stack):
    stack = extraction_stack
    _grant_all(stack["consent"], stack["scope"])
    stack["extractor"].candidates = (
        ExtractionCandidate(MemoryType.FACT, "用户住在杭州", confidence=0.99),
    )
    service = _service_with(stack, auto_commit_enabled=False)
    job = asyncio.run(service.submit(
        stack["scope"], conversation_id="conv-1", message_id="msg-1",
        content="我住在杭州",
    ))
    assert job is not None and job.status is ExtractionJobStatus.SUCCEEDED
    assert asyncio.run(service.list_candidates(stack["scope"]))[1] == 1


def test_summary_creates_active_event_memory(extraction_stack):
    stack = extraction_stack
    _grant_all(stack["consent"], stack["scope"])
    stack["extractor"].summary_candidate = ExtractionCandidate(
        MemoryType.EVENT, "用户讨论了咖啡偏好和周末去杭州的计划", confidence=0.9,
    )
    job = asyncio.run(stack["service"].submit_summary(
        stack["scope"], conversation_id="conv-1",
        messages=("我喜欢黑咖啡", "周末想去杭州"), watermark=2,
    ))
    assert job is not None and job.status is ExtractionJobStatus.SUCCEEDED
    rows, total = stack["store"].list_active_page(
        scope_id=stack["scope"].id, subject_id="u1", limit=10, offset=0,
    )
    assert total == 1
    memory = rows[0]
    assert memory.memory_type is MemoryType.EVENT
    assert memory.metadata["summary"] is True
    assert len(memory.metadata["summary_session"]) == 16
    assert memory.metadata["summary_range"] == {"from": 2, "to": 2}
    operations = stack["store"].db.connection.execute(
        "SELECT operation_type FROM memory_operations WHERE memory_id=?",
        (memory.id,),
    ).fetchall()
    assert [row["operation_type"] for row in operations] == ["AUTO_SUMMARY"]


def test_summary_is_candidate_when_direct_write_disabled(extraction_stack):
    stack = extraction_stack
    _grant_all(stack["consent"], stack["scope"])
    stack["extractor"].summary_candidate = ExtractionCandidate(
        MemoryType.EVENT, "用户讨论了咖啡偏好", confidence=0.9,
    )
    service = _service_with(stack, auto_commit_enabled=False)
    job = asyncio.run(service.submit_summary(
        stack["scope"], conversation_id="conv-1",
        messages=("我喜欢黑咖啡",), watermark=1,
    ))
    assert job is not None and job.status is ExtractionJobStatus.SUCCEEDED
    candidates, total = asyncio.run(service.list_candidates(stack["scope"]))
    assert total == 1
    assert candidates[0].metadata["summary"] is True


def test_conflict_detection_pages_past_first_fifty_active_memories(extraction_stack):
    stack = extraction_stack
    _grant_all(stack["consent"], stack["scope"])
    for index in range(55):
        _seed_active(stack["store"], stack["scope"], f"用户喜欢饮品{index}", index)
    _seed_active(stack["store"], stack["scope"], "用户喜欢黑咖啡", 55)
    stack["extractor"].candidates = (
        ExtractionCandidate(MemoryType.PREFERENCE, "用户喜欢绿茶", confidence=0.9),
    )
    _submit(stack, "我喜欢绿茶")
    candidates, total = asyncio.run(
        stack["service"].list_candidates(stack["scope"]),
    )
    assert total == 1
    assert candidates[0].content == "用户喜欢绿茶"


def test_summary_with_high_risk_category_stays_candidate(extraction_stack):
    stack = extraction_stack
    _grant_all(stack["consent"], stack["scope"])
    stack["consent"].grant(
        stack["scope"], ExtractionConsentType.SENSITIVE_CATEGORIES,
        operator_id="u1", metadata={"categories": ["credential"]},
    )
    stack["extractor"].summary_candidate = ExtractionCandidate(
        MemoryType.EVENT, "用户提到密码是 hunter2", confidence=0.99,
    )
    job = asyncio.run(stack["service"].submit_summary(
        stack["scope"], conversation_id="conv-1",
        messages=("我的密码是 hunter2",), watermark=1,
    ))
    assert job is not None and job.status is ExtractionJobStatus.SUCCEEDED
    assert stack["store"].list_active_page(
        scope_id=stack["scope"].id, subject_id="u1", limit=10, offset=0,
    )[1] == 0
    assert asyncio.run(stack["service"].list_candidates(stack["scope"]))[1] == 1


def test_summary_without_content_succeeds_without_memory(extraction_stack):
    stack = extraction_stack
    _grant_all(stack["consent"], stack["scope"])
    stack["extractor"].summary_candidate = None
    job = asyncio.run(stack["service"].submit_summary(
        stack["scope"], conversation_id="conv-1",
        messages=("今天天气不错",), watermark=1,
    ))
    assert job is not None and job.status is ExtractionJobStatus.SUCCEEDED
    assert stack["store"].list_active_page(
        scope_id=stack["scope"].id, subject_id="u1", limit=10, offset=0,
    )[1] == 0
