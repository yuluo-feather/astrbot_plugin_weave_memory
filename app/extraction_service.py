"""V1.5 personal extraction pipeline (ADR-0015)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256

from ..core.errors import (
    AuthorizationDenied,
    Conflict,
    InvalidTransition,
    NotFound,
    ValidationError,
)
from ..core.model import (
    ExtractionCandidate,
    ExtractionJob,
    ExtractionJobStatus,
    ExtractionWindow,
    MemoryEntity,
    MemoryOperation,
    MemoryRevision,
    MemorySource,
    MemoryStatus,
    MemoryType,
    OperatorType,
    Scope,
    ScopeType,
    SourceType,
)
from ..core.normalization import normalize_content
from ..core.policy.auto_commit import (
    AutoCommitDecision,
    should_auto_commit,
    similarity,
)
from ..core.policy.dedup import DedupKey
from ..core.ports import (
    Clock,
    ExtractionJobStore,
    IdGenerator,
    LlmExtractor,
    MemoryStore,
    ScopeStore,
)
from .consent_service import ConsentService, PersonalConsentType
from .extraction_gates import ExtractionGates, resolve_gates
from .lifecycle_service import LifecycleService
from .sensitive_filter import detect_sensitive_categories, scan_sensitive
from .write_coordinator import WriteCoordinator

_ALLOWED_TYPES = (MemoryType.PREFERENCE, MemoryType.FACT, MemoryType.EVENT)
_DIRECT_IMPORTANCE = 0.7
_CANDIDATE_IMPORTANCE = 0.5
_CONFIRMED_IMPORTANCE = 0.8
_SUMMARY_IMPORTANCE = 0.6
CandidateNotifier = Callable[[tuple[MemoryEntity, ...]], Awaitable[None]]


async def expire_due_candidates(
    *, store: MemoryStore, lifecycle: LifecycleService, clock: Clock,
    ids: IdGenerator, scope: Scope, limit: int = 100, max_rounds: int = 10,
    operator_id: str = "extraction_service",
) -> int:
    """Archive candidates whose workflow TTL elapsed, writing CANDIDATE_EXPIRE."""
    now = clock.now()
    archived = 0
    for _ in range(max_rounds):
        expired = store.list_expired_candidates(
            scope_id=scope.id, subject_id=scope.identity.subject_id,
            at=now, limit=limit,
        )
        if not expired:
            break
        progressed = 0
        for memory in expired:
            expires_at = memory.candidate_expires_at
            if expires_at is None:
                continue
            operation = MemoryOperation(
                id=ids.new_id("operation"), scope_id=scope.id,
                memory_id=memory.id, operation_type="CANDIDATE_EXPIRE",
                operator_type=OperatorType.SYSTEM,
                operator_id=operator_id, occurred_at=now,
                metadata={
                    "from_status": MemoryStatus.CANDIDATE.value,
                    "to_status": MemoryStatus.ARCHIVED.value,
                    "memory_type": memory.memory_type.value,
                    "source_type": memory.source_type.value,
                    "candidate_expires_at": expires_at.isoformat(),
                    "result": "SUCCESS",
                },
            )
            try:
                receipt = await lifecycle.archive_candidate(
                    memory, candidate_expires_at=expires_at,
                    revision_id=ids.new_id("revision"), at=now,
                    operation=operation,
                )
            except (Conflict, InvalidTransition, NotFound):
                continue
            if receipt is not None:
                progressed += 1
                archived += 1
        if progressed == 0:
            break
    return archived


class ExtractionService:
    def __init__(
        self, *, store: MemoryStore, jobs: ExtractionJobStore,
        coordinator: WriteCoordinator, consent: ConsentService,
        scopes: ScopeStore, lifecycle: LifecycleService,
        extractor: LlmExtractor, clock: Clock, ids: IdGenerator,
        global_enabled: bool = False, max_retries: int = 3,
        timeout_seconds: float = 30.0, queue_size: int = 100,
        candidate_ttl_days: int = 7, rejected_keep_body: bool = False,
        auto_commit_enabled: bool = True,
        auto_commit_confidence: float = 0.8,
        consolidation_similarity: float = 0.9,
    ) -> None:
        self._store = store
        self._jobs = jobs
        self._coordinator = coordinator
        self._consent = consent
        self._scopes = scopes
        self._lifecycle = lifecycle
        self._extractor = extractor
        self._clock = clock
        self._ids = ids
        self._global_enabled = global_enabled
        self._max_retries = max(1, max_retries)
        self._timeout = max(1.0, timeout_seconds)
        self._queue_size = max(1, queue_size)
        self._candidate_ttl_days = max(1, candidate_ttl_days)
        self._rejected_keep_body = rejected_keep_body
        self._auto_commit_enabled = auto_commit_enabled
        self._auto_commit_confidence = min(1.0, max(0.0, auto_commit_confidence))
        self._consolidation_similarity = min(
            1.0, max(0.0, consolidation_similarity),
        )

    def gates(self, scope: Scope) -> ExtractionGates:
        return resolve_gates(
            scope, self._consent, self._scopes, global_enabled=self._global_enabled,
        )

    def _require_store_gate(self, scope: Scope) -> None:
        if not self.gates(scope).can_store:
            raise AuthorizationDenied("extraction consent no longer allows storing candidates")

    async def submit(
        self, scope: Scope, *, conversation_id: str, message_id: str,
        content: str, watermark: int = 0,
        on_candidates: CandidateNotifier | None = None,
    ) -> ExtractionJob | None:
        """Capture one private message and run the bounded extraction pipeline."""
        return await self.submit_messages(
            scope, conversation_id=conversation_id,
            messages=((message_id, content),), watermark=watermark,
            on_candidates=on_candidates,
        )

    async def submit_messages(
        self, scope: Scope, *, conversation_id: str,
        messages: tuple[tuple[str, str], ...], watermark: int = 0,
        on_candidates: CandidateNotifier | None = None,
    ) -> ExtractionJob | None:
        """Capture a message batch; sensitive messages are dropped before the model."""
        if scope.identity.scope_type is not ScopeType.PERSONAL:
            return None
        gates = self.gates(scope)
        if not gates.can_store:
            return None
        allowed: list[str] = []
        message_ids: list[str] = []
        detected: set[str] = set()
        for message_id, content in messages:
            normalized = normalize_content(content)
            if not normalized:
                continue
            scan = scan_sensitive(content, gates.sensitive_categories)
            detected.update(scan.detected)
            if scan.is_blocked:
                continue
            allowed.append(content)
            message_ids.append(message_id)
        if not allowed:
            return None

        now = self._clock.now()
        content_hash = sha256(
            "\n".join(normalize_content(message) for message in allowed).encode("utf-8")
        ).hexdigest()
        job = ExtractionJob(
            id=self._ids.new_id("extraction_job"),
            platform=scope.identity.platform, tenant_id=scope.identity.tenant_id,
            scope_id=scope.id, subject_id=scope.identity.subject_id,
            conversation_id=conversation_id, watermark=watermark,
            content_hash=content_hash,
            status=ExtractionJobStatus.PENDING, attempts=0,
            created_at=now, updated_at=now,
            metadata={
                "message_ids": message_ids,
                "message_count": len(message_ids),
                "sensitive_categories": sorted(detected),
            },
        )
        if self._pending_count(scope) >= self._queue_size:
            full = replace(job, status=ExtractionJobStatus.FAILED,
                           last_error="queue_full")
            stored, _created = self._jobs.create_or_get(full)
            return stored
        stored, created = self._jobs.create_or_get(job)
        if not created and stored.status is not ExtractionJobStatus.PENDING:
            return stored
        window = ExtractionWindow(
            scope_id=scope.id, subject_id=scope.identity.subject_id,
            conversation_id=conversation_id, watermark=watermark,
            content_hash=job.content_hash, messages=tuple(allowed),
        )
        return await self._run(scope, stored, window, gates, on_candidates)

    def _pending_count(self, scope: Scope) -> int:
        counts = self._jobs.count_by_status(
            scope_id=scope.id, subject_id=scope.identity.subject_id,
        )
        return counts.get(ExtractionJobStatus.PENDING.value, 0)

    async def submit_summary(
        self, scope: Scope, *, conversation_id: str,
        messages: tuple[str, ...], watermark: int = 0,
        on_candidates: CandidateNotifier | None = None,
    ) -> ExtractionJob | None:
        """Capture a session window and ask the model for one summary memory."""
        if scope.identity.scope_type is not ScopeType.PERSONAL:
            return None
        gates = self.gates(scope)
        if not gates.can_store:
            return None
        allowed: list[str] = []
        for content in messages:
            normalized = normalize_content(content)
            if not normalized:
                continue
            if scan_sensitive(content, gates.sensitive_categories).is_blocked:
                continue
            allowed.append(content)
        if not allowed:
            return None
        now = self._clock.now()
        content_hash = sha256(
            "\n".join(normalize_content(message) for message in allowed).encode("utf-8")
        ).hexdigest()
        job = ExtractionJob(
            id=self._ids.new_id("extraction_job"),
            platform=scope.identity.platform, tenant_id=scope.identity.tenant_id,
            scope_id=scope.id, subject_id=scope.identity.subject_id,
            conversation_id=conversation_id, watermark=watermark,
            content_hash=content_hash,
            status=ExtractionJobStatus.PENDING, attempts=0,
            created_at=now, updated_at=now,
            metadata={"summary": True, "message_count": len(allowed)},
        )
        if self._pending_count(scope) >= self._queue_size:
            full = replace(job, status=ExtractionJobStatus.FAILED,
                           last_error="queue_full")
            stored, _created = self._jobs.create_or_get(full)
            return stored
        stored, created = self._jobs.create_or_get(job)
        if not created and stored.status is not ExtractionJobStatus.PENDING:
            return stored
        window = ExtractionWindow(
            scope_id=scope.id, subject_id=scope.identity.subject_id,
            conversation_id=conversation_id, watermark=watermark,
            content_hash=job.content_hash, messages=tuple(allowed),
        )
        return await self._run_summary(scope, stored, window, gates, on_candidates)

    async def _run_summary(
        self, scope: Scope, job: ExtractionJob, window: ExtractionWindow,
        gates: ExtractionGates, on_candidates: CandidateNotifier | None,
    ) -> ExtractionJob:
        running = replace(
            job, status=ExtractionJobStatus.RUNNING, attempts=job.attempts + 1,
            updated_at=self._clock.now(),
        )
        try:
            self._jobs.update(running, expected_status=ExtractionJobStatus.PENDING)
        except Conflict:
            return self._jobs.get(job.id)
        summarize = getattr(self._extractor, "summarize", None)
        if summarize is None:
            return self._finish(
                running, ExtractionJobStatus.FAILED,
                last_error="summary_not_supported",
            )
        current = running
        while True:
            latest = self._jobs.get(job.id)
            if latest.status is ExtractionJobStatus.CANCELLED:
                return latest
            if not self.gates(scope).can_store:
                return self._finish(
                    current, ExtractionJobStatus.FAILED,
                    last_error="consent_revoked",
                )
            try:
                async with asyncio.timeout(self._timeout):
                    candidate = await summarize(
                        window,
                        sensitive_categories=gates.sensitive_categories,
                    )
                latest = self._jobs.get(job.id)
                if latest.status is ExtractionJobStatus.CANCELLED:
                    return latest
                if candidate is None:
                    return self._finish(current, ExtractionJobStatus.SUCCEEDED)
                created_any, skipped_any, created = await self._materialize_summary(
                    scope, job.id, candidate, window,
                )
                break
            except (AuthorizationDenied, ValidationError):
                return self._finish(
                    current, ExtractionJobStatus.FAILED,
                    last_error="summary_rejected",
                )
            except Exception:  # Boundary: summary failure must not block chat.
                if current.attempts >= self._max_retries:
                    return self._finish(
                        current, ExtractionJobStatus.FAILED,
                        last_error="summary_failed",
                    )
                current = replace(
                    current, attempts=current.attempts + 1,
                    updated_at=self._clock.now(),
                )
                try:
                    self._jobs.update(
                        current, expected_status=ExtractionJobStatus.RUNNING,
                    )
                except Conflict:
                    return self._jobs.get(job.id)
        if created and on_candidates is not None:
            try:
                await on_candidates(created)
            except Exception:  # Notification failure must not fail the job.
                pass
        if created_any:
            return self._finish(current, ExtractionJobStatus.SUCCEEDED)
        if skipped_any:
            return self._finish(current, ExtractionJobStatus.SKIPPED_DUPLICATE_ACTIVE)
        return self._finish(current, ExtractionJobStatus.SUCCEEDED)

    async def _materialize_summary(
        self, scope: Scope, job_id: str, candidate: ExtractionCandidate,
        window: ExtractionWindow,
    ) -> tuple[bool, bool, tuple[MemoryEntity, ...]]:
        normalized = normalize_content(candidate.content)
        if not normalized:
            return False, False, ()
        key = DedupKey(scope.id, scope.identity.subject_id, candidate.memory_type.value, normalized)
        now = self._clock.now()
        if self._store.find_active_duplicate(key) is not None:
            return False, True, ()
        if self._store.find_candidate_duplicate(key) is not None:
            return False, True, ()
        active_memories = self._active_same_type(scope, candidate.memory_type, now)
        target = self._consolidation_target(candidate, active_memories)
        if target is not None:
            await self._merge_duplicate(scope, candidate, job_id, now, target)
            return False, True, ()
        summary_metadata = {
            "summary": True,
            "summary_session": sha256(
                window.conversation_id.encode("utf-8"),
            ).hexdigest()[:16],
            "summary_range": {"from": window.watermark, "to": window.watermark},
            "importance": _SUMMARY_IMPORTANCE,
        }
        commit = (
            self._auto_commit_enabled
            and candidate.confidence is not None
            and candidate.confidence >= self._auto_commit_confidence
        )
        try:
            if commit:
                await self._create_active(
                    scope, candidate, job_id, now,
                    AutoCommitDecision(True, "window_summary", "summary"),
                    extra_metadata=summary_metadata,
                    operation_type="AUTO_SUMMARY",
                )
                return True, False, ()
            memory = await self._create_candidate(
                scope, candidate, job_id, now, extra_metadata=summary_metadata,
            )
        except Conflict:
            return False, True, ()
        return True, False, (memory,)

    async def _run(
        self, scope: Scope, job: ExtractionJob, window: ExtractionWindow,
        gates: ExtractionGates, on_candidates: CandidateNotifier | None,
    ) -> ExtractionJob:
        running = replace(
            job, status=ExtractionJobStatus.RUNNING, attempts=job.attempts + 1,
            updated_at=self._clock.now(),
        )
        try:
            self._jobs.update(running, expected_status=ExtractionJobStatus.PENDING)
        except Conflict:
            return self._jobs.get(job.id)

        current = running
        while True:
            latest = self._jobs.get(job.id)
            if latest.status is ExtractionJobStatus.CANCELLED:
                return latest
            if not self.gates(scope).can_store:
                return self._finish(
                    current, ExtractionJobStatus.FAILED,
                    last_error="consent_revoked",
                )
            try:
                async with asyncio.timeout(self._timeout):
                    candidates = await self._extractor.extract(
                        window, allowed_types=_ALLOWED_TYPES,
                        sensitive_categories=gates.sensitive_categories,
                    )
                latest = self._jobs.get(job.id)
                if latest.status is ExtractionJobStatus.CANCELLED:
                    return latest
                created_any, skipped_any, created, committed = await self._materialize(
                    scope, job.id, candidates, window.messages,
                )
                break
            except (AuthorizationDenied, ValidationError) as exc:
                return self._finish(
                    current, ExtractionJobStatus.FAILED,
                    last_error=type(exc).__name__,
                )
            except Exception as exc:  # Boundary: extractor/task failure must not block chat.
                if current.attempts >= self._max_retries:
                    return self._finish(
                        current, ExtractionJobStatus.FAILED,
                        last_error=type(exc).__name__,
                    )
                current = replace(
                    current, attempts=current.attempts + 1,
                    updated_at=self._clock.now(),
                )
                try:
                    self._jobs.update(
                        current, expected_status=ExtractionJobStatus.RUNNING,
                    )
                except Conflict:
                    return self._jobs.get(job.id)
        if created and on_candidates is not None:
            try:
                await on_candidates(created)
            except Exception:  # Notification failure must not fail the job.
                pass
        if created_any or committed:
            return self._finish(current, ExtractionJobStatus.SUCCEEDED)
        if skipped_any:
            return self._finish(current, ExtractionJobStatus.SKIPPED_DUPLICATE_ACTIVE)
        return self._finish(current, ExtractionJobStatus.SUCCEEDED)

    def _finish(
        self, job: ExtractionJob, status: ExtractionJobStatus, *,
        last_error: str | None = None,
    ) -> ExtractionJob:
        finished = replace(
            job, status=status, last_error=last_error, updated_at=self._clock.now(),
        )
        try:
            self._jobs.update(finished, expected_status=ExtractionJobStatus.RUNNING)
        except Conflict:
            return self._jobs.get(job.id)
        return finished

    async def _materialize(
        self, scope: Scope, job_id: str, candidates: tuple[ExtractionCandidate, ...],
        messages: tuple[str, ...],
    ) -> tuple[bool, bool, tuple[MemoryEntity, ...], int]:
        created_any = False
        skipped_any = False
        committed = 0
        created: list[MemoryEntity] = []
        now = self._clock.now()
        for candidate in candidates:
            if candidate.memory_type not in _ALLOWED_TYPES:
                continue
            normalized = normalize_content(candidate.content)
            if not normalized:
                continue
            key = DedupKey(
                scope.id, scope.identity.subject_id, candidate.memory_type.value,
                normalized,
            )
            if self._store.find_active_duplicate(key) is not None:
                skipped_any = True
                continue
            if self._store.find_candidate_duplicate(key) is not None:
                skipped_any = True
                continue
            active_memories = self._active_same_type(scope, candidate.memory_type, now)
            target = self._consolidation_target(candidate, active_memories)
            if target is not None:
                await self._merge_duplicate(scope, candidate, job_id, now, target)
                skipped_any = True
                continue
            decision = self._auto_commit_decision(
                candidate, messages, active_memories,
            )
            try:
                if decision.commit:
                    await self._create_active(scope, candidate, job_id, now, decision)
                    committed += 1
                    continue
                memory = await self._create_candidate(scope, candidate, job_id, now)
            except Conflict:
                skipped_any = True
                continue
            created.append(memory)
            created_any = True
        return created_any, skipped_any, tuple(created), committed

    def _active_same_type(
        self, scope: Scope, memory_type: MemoryType, now: datetime,
    ) -> tuple[MemoryEntity, ...]:
        rows, _total = self._store.list_active_page(
            scope_id=scope.id, subject_id=scope.identity.subject_id,
            limit=50, offset=0, memory_type=memory_type, at=now,
        )
        return rows

    def _consolidation_target(
        self, candidate: ExtractionCandidate,
        active_memories: tuple[MemoryEntity, ...],
    ) -> MemoryEntity | None:
        if self._consolidation_similarity <= 0:
            return None
        for active in active_memories:
            if active.memory_type is not candidate.memory_type:
                continue
            if similarity(candidate.content, active.content) >= self._consolidation_similarity:
                return active
        return None

    def _auto_commit_decision(
        self, candidate: ExtractionCandidate, messages: tuple[str, ...],
        active_memories: tuple[MemoryEntity, ...],
    ) -> AutoCommitDecision:
        if not self._auto_commit_enabled:
            return AutoCommitDecision(False, "auto_commit_disabled", "mode is candidate-only")
        return should_auto_commit(
            candidate, messages, active_memories,
            confidence_threshold=self._auto_commit_confidence,
            consolidation_similarity=self._consolidation_similarity,
            sensitive_detected=detect_sensitive_categories(candidate.content),
        )

    async def _merge_duplicate(
        self, scope: Scope, candidate: ExtractionCandidate, job_id: str,
        now: datetime, target: MemoryEntity,
    ) -> MemoryEntity:
        """Fold a near-duplicate arrival into the existing ACTIVE memory."""
        score = similarity(candidate.content, target.content)
        sources = list(target.metadata.get("merged_sources", []))
        marker = f"extraction_job:{job_id}"
        if marker not in sources:
            sources.append(marker)
        updated = replace(
            target,
            current_revision=target.current_revision + 1,
            updated_at=now,
            metadata={
                **dict(target.metadata),
                "merged_sources": sources,
                "last_merged_at": now.isoformat(),
            },
        )
        revision = MemoryRevision(
            id=self._ids.new_id("revision"), memory_id=target.id,
            revision=updated.current_revision, content=target.content,
            status=target.status, changed_at=now,
            change_reason="near duplicate merge",
            snapshot={
                "scope_id": scope.id, "subject_id": scope.identity.subject_id,
                "memory_type": target.memory_type.value,
                "source_type": target.source_type.value,
                "merged_similarity": round(score, 4),
            },
        )
        # A source row must match the survivor's source_type, so the incoming
        # evidence is kept in metadata/audit instead of a second source row.
        await self._coordinator.update(
            updated, expected_revision=target.current_revision,
            revision=revision, guard=lambda: self._require_store_gate(scope),
        )
        self._store.record_operation(
            operation_id=self._ids.new_id("operation"), scope_id=scope.id,
            memory_id=target.id, operation_type="MEMORY_MERGE",
            operator_type=OperatorType.SYSTEM, operator_id="extraction_service",
            occurred_at=now,
            metadata={
                "source_job_id": job_id,
                "similarity": round(score, 4),
                "result": "SUCCESS",
            },
        )
        return updated

    async def _create_candidate(
        self, scope: Scope, candidate: ExtractionCandidate, job_id: str,
        now: datetime, *, extra_metadata: Mapping[str, object] | None = None,
    ) -> MemoryEntity:
        memory_id = self._ids.new_id("memory")
        normalized = normalize_content(candidate.content)
        metadata: dict[str, object] = {
            "extraction_job_id": job_id,
            "confidence": candidate.confidence,
            "auto": True,
            "direct_write": False,
            "importance": _CANDIDATE_IMPORTANCE,
        }
        if extra_metadata:
            metadata.update(extra_metadata)
        memory = MemoryEntity(
            id=memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
            memory_type=candidate.memory_type, content=candidate.content,
            normalized_content=normalized, status=MemoryStatus.CANDIDATE,
            source_type=SourceType.INFERRED, current_revision=1,
            created_at=now, updated_at=now,
            candidate_expires_at=now + timedelta(days=self._candidate_ttl_days),
            metadata=metadata,
        )
        revision = MemoryRevision(
            id=self._ids.new_id("revision"), memory_id=memory_id, revision=1,
            content=candidate.content, status=MemoryStatus.CANDIDATE,
            changed_at=now, change_reason="background extraction candidate",
            snapshot={
                "scope_id": scope.id, "subject_id": scope.identity.subject_id,
                "memory_type": candidate.memory_type.value,
                "source_type": SourceType.INFERRED.value,
            },
        )
        source = MemorySource(
            id=self._ids.new_id("source"), memory_id=memory_id,
            source_type=SourceType.INFERRED, operator_type=OperatorType.SYSTEM,
            operator_id="extraction_service", captured_at=now,
            evidence_ref=f"extraction_job:{job_id}",
            metadata={
                "source_message_ids": list(candidate.source_message_ids),
                "confidence": candidate.confidence,
            },
        )
        await self._coordinator.create(
            memory, revision, source,
            guard=lambda: self._require_store_gate(scope),
        )
        return memory

    async def _create_active(
        self, scope: Scope, candidate: ExtractionCandidate, job_id: str,
        now: datetime, decision: AutoCommitDecision, *,
        extra_metadata: Mapping[str, object] | None = None,
        operation_type: str = "AUTO_COMMIT",
    ) -> MemoryEntity:
        memory_id = self._ids.new_id("memory")
        normalized = normalize_content(candidate.content)
        metadata: dict[str, object] = {
            "extraction_job_id": job_id,
            "confidence": candidate.confidence,
            "auto": True,
            "direct_write": True,
            "commit_rule": decision.rule,
            "importance": _DIRECT_IMPORTANCE,
        }
        if extra_metadata:
            metadata.update(extra_metadata)
        memory = MemoryEntity(
            id=memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
            memory_type=candidate.memory_type, content=candidate.content,
            normalized_content=normalized, status=MemoryStatus.ACTIVE,
            source_type=SourceType.INFERRED, current_revision=1,
            created_at=now, updated_at=now, metadata=metadata,
        )
        revision = MemoryRevision(
            id=self._ids.new_id("revision"), memory_id=memory_id, revision=1,
            content=candidate.content, status=MemoryStatus.ACTIVE,
            changed_at=now,
            change_reason=(
                f"auto commit: {decision.rule}"
                if operation_type == "AUTO_COMMIT" else "auto summary"
            ),
            snapshot={
                "scope_id": scope.id, "subject_id": scope.identity.subject_id,
                "memory_type": candidate.memory_type.value,
                "source_type": SourceType.INFERRED.value,
                "commit_rule": decision.rule,
            },
        )
        source = MemorySource(
            id=self._ids.new_id("source"), memory_id=memory_id,
            source_type=SourceType.INFERRED, operator_type=OperatorType.SYSTEM,
            operator_id="extraction_service", captured_at=now,
            evidence_ref=f"extraction_job:{job_id}",
            metadata={
                "source_message_ids": list(candidate.source_message_ids),
                "confidence": candidate.confidence,
                "commit_rule": decision.rule,
            },
        )
        await self._coordinator.create(
            memory, revision, source,
            guard=lambda: self._require_store_gate(scope),
        )
        self._store.record_operation(
            operation_id=self._ids.new_id("operation"), scope_id=scope.id,
            memory_id=memory_id, operation_type=operation_type,
            operator_type=OperatorType.SYSTEM, operator_id="extraction_service",
            occurred_at=now,
            metadata={
                "commit_rule": decision.rule,
                "confidence": candidate.confidence,
                "importance": _DIRECT_IMPORTANCE,
                "result": "SUCCESS",
            },
        )
        return memory

    async def _expire_due_candidates(self, scope: Scope) -> int:
        """Archive candidates whose workflow TTL has elapsed, writing audit records."""
        return await expire_due_candidates(
            store=self._store, lifecycle=self._lifecycle, clock=self._clock,
            ids=self._ids, scope=scope,
        )

    async def list_candidates(
        self, scope: Scope, *, limit: int = 20, offset: int = 0,
    ) -> tuple[tuple[MemoryEntity, ...], int]:
        self._consent.require(scope, PersonalConsentType.PERSONAL_MEMORY)
        await self._expire_due_candidates(scope)
        return self._store.list_candidates(
            scope_id=scope.id, subject_id=scope.identity.subject_id,
            limit=limit, offset=offset,
        )

    def get_candidate(self, scope: Scope, memory_id: str) -> MemoryEntity:
        self._consent.require(scope, PersonalConsentType.PERSONAL_MEMORY)
        memory = self._store.get(
            memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
        )
        if memory.status is not MemoryStatus.CANDIDATE:
            raise NotFound("candidate not found")
        return memory

    @staticmethod
    def _candidate_operation_metadata(
        memory: MemoryEntity, target: MemoryStatus, *,
        reason: str = "", body_retained: bool = True,
    ) -> dict[str, object]:
        metadata: dict[str, object] = {
            "from_status": MemoryStatus.CANDIDATE.value,
            "to_status": target.value,
            "memory_type": memory.memory_type.value,
            "source_type": memory.source_type.value,
            "result": "SUCCESS",
        }
        if reason:
            metadata["reason"] = reason
        if target is MemoryStatus.REJECTED:
            metadata["body_retained"] = body_retained
        return metadata

    async def confirm_candidate(
        self, scope: Scope, memory_id: str, *, expected_revision: int,
        operator_id: str | None = None,
    ) -> MemoryEntity:
        self._consent.require(scope, PersonalConsentType.PERSONAL_MEMORY)
        await self._expire_due_candidates(scope)
        memory = self._store.get(
            memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
        )
        if memory.status is not MemoryStatus.CANDIDATE:
            raise Conflict("only CANDIDATE memories can be confirmed")
        if memory.current_revision != expected_revision:
            raise Conflict("revision conflict")
        memory = replace(
            memory,
            metadata={
                **dict(memory.metadata),
                "importance": _CONFIRMED_IMPORTANCE,
                "auto": False,
                "user_confirmed": True,
            },
        )
        now = self._clock.now()
        operation = MemoryOperation(
            id=self._ids.new_id("operation"), scope_id=scope.id,
            memory_id=memory.id, operation_type="CANDIDATE_CONFIRM",
            operator_type=OperatorType.USER,
            operator_id=operator_id or scope.identity.subject_id, occurred_at=now,
            metadata=self._candidate_operation_metadata(memory, MemoryStatus.ACTIVE),
        )
        await self._lifecycle.transition_memory(
            memory, MemoryStatus.ACTIVE,
            revision_id=self._ids.new_id("revision"), at=now,
            reason="user confirmed extraction candidate",
            operator_type=OperatorType.USER, operation=operation,
        )
        return self._store.get(
            memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
        )

    async def reject_candidate(
        self, scope: Scope, memory_id: str, *, expected_revision: int,
        reason: str = "", operator_id: str | None = None,
    ) -> MemoryEntity | None:
        self._consent.require(scope, PersonalConsentType.PERSONAL_MEMORY)
        await self._expire_due_candidates(scope)
        memory = self._store.get(
            memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
        )
        if memory.status is not MemoryStatus.CANDIDATE:
            raise Conflict("only CANDIDATE memories can be rejected")
        if memory.current_revision != expected_revision:
            raise Conflict("revision conflict")
        now = self._clock.now()
        trimmed_reason = reason.strip()[:200]
        purge_body = not self._rejected_keep_body
        change_reason = "user rejected extraction candidate"
        if trimmed_reason:
            change_reason = f"{change_reason}: {trimmed_reason}"
        operation = MemoryOperation(
            id=self._ids.new_id("operation"), scope_id=scope.id,
            memory_id=memory.id, operation_type="CANDIDATE_REJECT",
            operator_type=OperatorType.USER,
            operator_id=operator_id or scope.identity.subject_id, occurred_at=now,
            metadata=self._candidate_operation_metadata(
                memory, MemoryStatus.REJECTED, reason=trimmed_reason,
                body_retained=not purge_body,
            ),
        )
        await self._lifecycle.reject_candidate(
            memory, expected_revision=expected_revision,
            revision_id=self._ids.new_id("revision"), at=now,
            reason=change_reason, operator_type=OperatorType.USER,
            operation=operation, purge_body=purge_body,
        )
        if purge_body:
            return None
        return self._store.get(
            memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
        )

    def cancel_job(self, scope: Scope, job_id: str, *,
                   operator_id: str | None = None) -> ExtractionJob:
        """Cancel a queued/running job so the extractor is not called again."""
        self._consent.require(scope, PersonalConsentType.PERSONAL_MEMORY)
        job = self._jobs.get(job_id)
        if job.scope_id != scope.id or job.subject_id != scope.identity.subject_id:
            raise NotFound("extraction job not found")
        if job.status not in (
            ExtractionJobStatus.PENDING, ExtractionJobStatus.RUNNING,
        ):
            raise Conflict("only PENDING or RUNNING extraction jobs can be cancelled")
        cancelled = replace(
            job, status=ExtractionJobStatus.CANCELLED,
            last_error="cancelled_by_user", updated_at=self._clock.now(),
            metadata={
                **dict(job.metadata),
                "cancelled_by": operator_id or scope.identity.subject_id,
            },
        )
        try:
            self._jobs.update(cancelled, expected_status=job.status)
        except Conflict as exc:
            raise Conflict("extraction job status changed concurrently") from exc
        return cancelled

    def job_summary(self, scope: Scope) -> dict[str, int]:
        self._consent.require(scope, PersonalConsentType.PERSONAL_MEMORY)
        return self._jobs.count_by_status(
            scope_id=scope.id, subject_id=scope.identity.subject_id,
        )

    def recover_interrupted(self) -> int:
        """Mark jobs interrupted by restart as FAILED; windows are not persisted.

        PENDING rows are included: the message window is not stored, so they can
        never be resumed and would otherwise occupy the queue forever.
        """
        now = self._clock.now()
        recovered = 0
        for status in (ExtractionJobStatus.RUNNING, ExtractionJobStatus.PENDING):
            for job in self._jobs.list_by_status(statuses=(status,), limit=1000):
                failed = replace(
                    job, status=ExtractionJobStatus.FAILED,
                    last_error="interrupted_by_restart", updated_at=now,
                )
                try:
                    self._jobs.update(failed, expected_status=status)
                except Conflict:
                    continue
                recovered += 1
        return recovered
