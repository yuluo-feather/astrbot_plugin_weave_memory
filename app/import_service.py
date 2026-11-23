"""V1.7 import pipeline (ADR-0017).

Pure application layer: JSON validation, scope enforcement, idempotency,
lifecycle hand-off and job bookkeeping. The transport adapters only resolve
the caller's scope, administrator decision and raw file bytes.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any

from ..core.errors import (
    AuthorizationDenied,
    Conflict,
    NotFound,
    StorageError,
    ValidationError,
)
from ..core.model import (
    ImportJob,
    ImportJobStatus,
    MemoryEntity,
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
from ..core.policy.dedup import DedupKey
from ..core.ports import Clock, IdGenerator, ImportJobStore, MemoryStore
from .export import EXPORT_FORMAT
from .lifecycle_service import LifecycleService
from .write_coordinator import ImportWriteOutcome, WriteCoordinator

IMPORT_MAX_FILE_BYTES = 5 * 1024 * 1024
IMPORT_MAX_ENTRIES = 5000
_IMPORTABLE_STATUSES = {
    MemoryStatus.ARCHIVED, MemoryStatus.EXPIRED, MemoryStatus.SUPERSEDED,
    MemoryStatus.REJECTED, MemoryStatus.DELETED,
}


class ImportOutcome(StrEnum):
    IMPORTED = "IMPORTED"
    METADATA_ONLY = "METADATA_ONLY"
    SKIPPED_DUPLICATE_IMPORT = "SKIPPED_DUPLICATE_IMPORT"
    SKIPPED_DUPLICATE_ACTIVE = "SKIPPED_DUPLICATE_ACTIVE"
    CONFLICT = "CONFLICT"
    FAILED = "FAILED"
    WOULD_IMPORT = "WOULD_IMPORT"
    WOULD_METADATA = "WOULD_METADATA"
    WOULD_SKIP = "WOULD_SKIP"
    WOULD_CONFLICT = "WOULD_CONFLICT"


@dataclass(frozen=True, slots=True)
class ImportEntryResult:
    index: int
    source_memory_id: str
    outcome: ImportOutcome
    reason: str = ""
    memory_id: str | None = None


@dataclass(frozen=True, slots=True)
class ImportResult:
    job_id: str | None
    dry_run: bool
    total: int
    imported: int = 0
    metadata_only: int = 0
    skipped: int = 0
    conflicts: int = 0
    failed: int = 0
    entries: tuple[ImportEntryResult, ...] = ()
    note: str = ""
    resumed_from: int = 0


@dataclass(frozen=True, slots=True)
class ExportEnvelope:
    schema_version: str
    export_id: str
    source_platform: str
    exported_at: datetime
    scope_type: ScopeType
    entries: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class ParsedEntry:
    source_memory_id: str
    source_revision: int
    memory_type: MemoryType
    status: MemoryStatus
    content: str | None
    created_at: datetime
    updated_at: datetime
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


def _parse_time(value: object, name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} is required")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{name} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError(f"{name} must be timezone-aware")
    return parsed


def _optional_time(value: object, name: str) -> datetime | None:
    if value is None:
        return None
    return _parse_time(value, name)


def parse_export_payload(payload: object) -> ExportEnvelope:
    if not isinstance(payload, Mapping):
        raise ValidationError("export payload must be a JSON object")
    schema_version = payload.get("schema_version")
    if schema_version != EXPORT_FORMAT:
        raise ValidationError(f"export schema_version must be {EXPORT_FORMAT}")
    export_id = payload.get("export_id")
    if not isinstance(export_id, str) or not export_id.strip():
        raise ValidationError("export_id is required")
    source_platform = payload.get("source_platform")
    if not isinstance(source_platform, str) or not source_platform.strip():
        raise ValidationError("source_platform is required")
    exported_at = _parse_time(payload.get("exported_at"), "exported_at")
    scope = payload.get("scope")
    if not isinstance(scope, Mapping):
        raise ValidationError("scope must be an object")
    try:
        scope_type = ScopeType(scope.get("scope_type"))
    except ValueError as exc:
        raise ValidationError("scope.scope_type must be PERSONAL or GROUP") from exc
    if scope_type not in (ScopeType.PERSONAL, ScopeType.GROUP):
        raise ValidationError("scope.scope_type must be PERSONAL or GROUP")
    memories = payload.get("memories")
    if not isinstance(memories, list):
        raise ValidationError("memories must be a list")
    if len(memories) > IMPORT_MAX_ENTRIES:
        raise ValidationError(f"an export may contain at most {IMPORT_MAX_ENTRIES} memories")
    if any(not isinstance(entry, Mapping) for entry in memories):
        raise ValidationError("every memory entry must be an object")
    return ExportEnvelope(
        schema_version=EXPORT_FORMAT,
        export_id=export_id.strip(),
        source_platform=source_platform.strip(),
        exported_at=exported_at,
        scope_type=scope_type,
        entries=tuple(memories),
    )


def parse_entry(raw: Mapping[str, Any]) -> ParsedEntry:
    source_memory_id = raw.get("source_memory_id")
    if not isinstance(source_memory_id, str) or not source_memory_id.strip():
        raise ValidationError("source_memory_id is required")
    if len(source_memory_id) > 512:
        raise ValidationError("source_memory_id must be at most 512 characters")
    source_revision = raw.get("source_revision")
    if type(source_revision) is not int or source_revision < 1:
        raise ValidationError("source_revision must be a positive integer")
    try:
        memory_type = MemoryType(raw.get("memory_type"))
    except ValueError as exc:
        raise ValidationError("memory_type must be PREFERENCE, FACT or EVENT") from exc
    try:
        status = MemoryStatus(raw.get("status"))
    except ValueError as exc:
        raise ValidationError("status is not a known memory status") from exc
    if status is MemoryStatus.CANDIDATE:
        raise ValidationError("CANDIDATE entries cannot be imported")
    active = status is MemoryStatus.ACTIVE
    content = raw.get("content")
    if active:
        if not isinstance(content, str) or not content.strip():
            raise ValidationError("ACTIVE entries require non-empty content")
    else:
        content = None
    created_at = _parse_time(raw.get("created_at"), "created_at")
    updated_at = _parse_time(raw.get("updated_at"), "updated_at")
    if updated_at < created_at:
        raise ValidationError("updated_at must not be earlier than created_at")
    valid_from = _optional_time(raw.get("valid_from"), "valid_from")
    valid_until = _optional_time(raw.get("valid_until"), "valid_until")
    if valid_from is not None and valid_until is not None and valid_until <= valid_from:
        raise ValidationError("valid_until must be later than valid_from")
    metadata = raw.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise ValidationError("metadata must be an object")
    try:
        json.dumps(dict(metadata), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValidationError("metadata must contain JSON-compatible values") from exc
    return ParsedEntry(
        source_memory_id=source_memory_id.strip(),
        source_revision=source_revision,
        memory_type=memory_type,
        status=status,
        content=content,
        created_at=created_at,
        updated_at=updated_at,
        valid_from=valid_from,
        valid_until=valid_until,
        metadata=dict(metadata),
    )


def build_evidence_ref(*, source_platform: str, export_id: str,
                       source_memory_id: str, source_revision: int) -> str:
    return json.dumps(
        {
            "source_platform": source_platform,
            "export_id": export_id,
            "source_memory_id": source_memory_id,
            "source_revision": source_revision,
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def payload_sha256(payload: object) -> str:
    """Stable file fingerprint computed from canonical JSON, not raw bytes.

    Both the message-file and Web API entry points parse the document first,
    so the same content fingerprints identically through either transport.
    """
    try:
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError("export payload must be JSON-serializable") from exc
    return sha256(canonical.encode("utf-8")).hexdigest()


class ImportService:
    def __init__(self, store: MemoryStore, jobs: ImportJobStore,
                 coordinator: WriteCoordinator, lifecycle: LifecycleService,
                 clock: Clock, ids: IdGenerator) -> None:
        self._store = store
        self._jobs = jobs
        self._coordinator = coordinator
        self._lifecycle = lifecycle
        self._clock = clock
        self._ids = ids

    @staticmethod
    def _enforce_scope(envelope: ExportEnvelope, scope: Scope) -> None:
        if envelope.scope_type is not scope.identity.scope_type:
            raise ValidationError("export scope type does not match the target scope")
        if scope.identity.scope_type not in (ScopeType.PERSONAL, ScopeType.GROUP):
            raise ValidationError("import supports only Personal and Group scopes")

    def _guard_scope(self, scope: Scope, guard: Callable[[], None],
                     envelope: ExportEnvelope) -> None:
        self._enforce_scope(envelope, scope)
        guard()

    def _preview(self, scope: Scope, envelope: ExportEnvelope,
                 parsed: ParsedEntry) -> ImportOutcome:
        evidence_ref = build_evidence_ref(
            source_platform=envelope.source_platform, export_id=envelope.export_id,
            source_memory_id=parsed.source_memory_id,
            source_revision=parsed.source_revision,
        )
        if self._store.find_import_source(evidence_ref):
            return ImportOutcome.WOULD_SKIP
        if parsed.status is not MemoryStatus.ACTIVE:
            return ImportOutcome.WOULD_METADATA
        normalized = normalize_content(parsed.content or "")
        key = DedupKey(
            scope.id, scope.identity.subject_id, parsed.memory_type.value, normalized,
        )
        if self._store.find_active_duplicate(key) is not None:
            return ImportOutcome.WOULD_SKIP
        if self._store.find_candidate_duplicate(key) is not None:
            return ImportOutcome.WOULD_CONFLICT
        return ImportOutcome.WOULD_IMPORT

    def _new_memory(self, scope: Scope, parsed: ParsedEntry, *,
                    source_platform: str, export_id: str,
                    operator_id: str) -> tuple[MemoryEntity, MemoryRevision, MemorySource]:
        content = parsed.content or ""
        memory_id = self._ids.new_id("memory")
        metadata = dict(parsed.metadata)
        metadata["import"] = {
            "source_platform": source_platform,
            "export_id": export_id,
            "source_memory_id": parsed.source_memory_id,
            "source_revision": parsed.source_revision,
        }
        memory = MemoryEntity(
            id=memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
            memory_type=parsed.memory_type, content=content,
            normalized_content=normalize_content(content),
            status=MemoryStatus.ACTIVE, source_type=SourceType.IMPORTED,
            current_revision=1, created_at=parsed.created_at,
            updated_at=parsed.updated_at, valid_from=parsed.valid_from,
            valid_until=parsed.valid_until, metadata=metadata,
        )
        revision = MemoryRevision(
            id=self._ids.new_id("revision"), memory_id=memory_id, revision=1,
            content=content, status=MemoryStatus.ACTIVE, changed_at=self._clock.now(),
            change_reason="imported",
            snapshot={
                "scope_id": scope.id, "subject_id": scope.identity.subject_id,
                "memory_type": parsed.memory_type.value,
                "source_type": SourceType.IMPORTED.value,
                "normalized_content": memory.normalized_content,
            },
        )
        source = MemorySource(
            id=self._ids.new_id("source"), memory_id=memory_id,
            source_type=SourceType.IMPORTED, operator_type=OperatorType.USER,
            operator_id=operator_id, captured_at=self._clock.now(),
            evidence_ref=build_evidence_ref(
                source_platform=source_platform, export_id=export_id,
                source_memory_id=parsed.source_memory_id,
                source_revision=parsed.source_revision,
            ),
            metadata={"origin": "import", "export_id": export_id},
        )
        return memory, revision, source

    async def _import_metadata_only(self, scope: Scope, parsed: ParsedEntry, *,
                                    envelope: ExportEnvelope, operator_id: str,
                                    job_id: str) -> None:
        def write() -> None:
            self._store.record_operation(
                operation_id=self._ids.new_id("operation"), scope_id=scope.id,
                memory_id=None, operation_type="IMPORT_METADATA",
                operator_type=OperatorType.USER, operator_id=operator_id,
                occurred_at=self._clock.now(),
                metadata={
                    "result": "SUCCESS",
                    "source_platform": envelope.source_platform,
                    "export_id": envelope.export_id,
                    "source_memory_id": parsed.source_memory_id,
                    "source_revision": parsed.source_revision,
                    "source_status": parsed.status.value,
                    "dataset_version": envelope.schema_version,
                    "job_id": job_id,
                },
            )

        await self._coordinator.run_serialized(write)

    async def import_payload(self, scope: Scope, payload: object, *,
                             operator_id: str, file_sha256: str,
                             guard: Callable[[], None],
                             dry_run: bool = False) -> ImportResult:
        envelope = parse_export_payload(payload)
        self._guard_scope(scope, guard, envelope)
        if dry_run:
            return self._dry_run(scope, envelope)

        job, resumed_from, note = self._begin_job(scope, envelope, file_sha256)
        entries: list[ImportEntryResult] = []
        imported = metadata_only = skipped = conflicts = failed = 0
        created_ids = list(job.created_memory_ids)
        try:
            for index in range(resumed_from, len(envelope.entries)):
                raw = envelope.entries[index]
                source_memory_id = str(raw.get("source_memory_id") or "")
                try:
                    parsed = parse_entry(raw)
                except ValidationError as exc:
                    failed += 1
                    entries.append(ImportEntryResult(
                        index, source_memory_id, ImportOutcome.FAILED, str(exc),
                    ))
                else:
                    result = await self._import_entry(
                        scope, envelope, parsed, index=index, operator_id=operator_id,
                        guard=guard, job_id=job.id,
                    )
                    if result.outcome is ImportOutcome.IMPORTED:
                        imported += 1
                        assert result.memory_id is not None
                        created_ids.append(result.memory_id)
                    elif result.outcome is ImportOutcome.METADATA_ONLY:
                        metadata_only += 1
                    elif result.outcome is ImportOutcome.CONFLICT:
                        conflicts += 1
                    else:
                        skipped += 1
                    entries.append(result)
                job = ImportJob(
                    id=job.id, scope_id=job.scope_id, subject_id=job.subject_id,
                    file_sha256=job.file_sha256, dataset_version=job.dataset_version,
                    status=ImportJobStatus.RUNNING, cursor=index + 1,
                    created_memory_ids=tuple(created_ids),
                    created_at=job.created_at, updated_at=self._clock.now(),
                )
                self._jobs.update(job)
            job = ImportJob(
                id=job.id, scope_id=job.scope_id, subject_id=job.subject_id,
                file_sha256=job.file_sha256, dataset_version=job.dataset_version,
                status=ImportJobStatus.DONE, cursor=len(envelope.entries),
                created_memory_ids=tuple(created_ids),
                created_at=job.created_at, updated_at=self._clock.now(),
            )
            self._jobs.update(job)
        except (StorageError, AuthorizationDenied, NotFound, Conflict, ValidationError) as exc:
            self._jobs.update(ImportJob(
                id=job.id, scope_id=job.scope_id, subject_id=job.subject_id,
                file_sha256=job.file_sha256, dataset_version=job.dataset_version,
                status=ImportJobStatus.FAILED, cursor=job.cursor,
                created_memory_ids=tuple(created_ids),
                created_at=job.created_at, updated_at=self._clock.now(),
                last_error=f"{type(exc).__name__}: {exc}"[:500],
            ))
            raise
        return ImportResult(
            job_id=job.id, dry_run=False, total=len(envelope.entries),
            imported=imported, metadata_only=metadata_only, skipped=skipped,
            conflicts=conflicts, failed=failed, entries=tuple(entries),
            note=note, resumed_from=resumed_from,
        )

    def _begin_job(self, scope: Scope, envelope: ExportEnvelope,
                   file_sha256: str) -> tuple[ImportJob, int, str]:
        existing = self._jobs.find_latest(scope_id=scope.id, file_sha256=file_sha256)
        now = self._clock.now()
        if existing is not None:
            if existing.status is ImportJobStatus.DONE:
                return (
                    existing, len(envelope.entries),
                    f"该文件已完成导入（任务 {existing.id}）；重复导入按幂等键跳过。",
                )
            if existing.status in (ImportJobStatus.PENDING, ImportJobStatus.RUNNING):
                resumed = ImportJob(
                    id=existing.id, scope_id=existing.scope_id,
                    subject_id=existing.subject_id, file_sha256=existing.file_sha256,
                    dataset_version=existing.dataset_version,
                    status=ImportJobStatus.RUNNING, cursor=existing.cursor,
                    created_memory_ids=existing.created_memory_ids,
                    created_at=existing.created_at, updated_at=now,
                )
                self._jobs.update(resumed)
                return resumed, existing.cursor, f"已从第 {existing.cursor + 1} 条续跑。"
        job = ImportJob(
            id=self._ids.new_id("import"), scope_id=scope.id,
            subject_id=scope.identity.subject_id, file_sha256=file_sha256,
            dataset_version=envelope.schema_version,
            status=ImportJobStatus.RUNNING, cursor=0, created_memory_ids=(),
            created_at=now, updated_at=now,
        )
        self._jobs.create(job)
        return job, 0, ""

    async def _import_entry(self, scope: Scope, envelope: ExportEnvelope,
                            parsed: ParsedEntry, *, index: int, operator_id: str,
                            guard: Callable[[], None],
                            job_id: str) -> ImportEntryResult:
        if parsed.status is not MemoryStatus.ACTIVE:
            if parsed.status not in _IMPORTABLE_STATUSES:
                return ImportEntryResult(
                    index, parsed.source_memory_id, ImportOutcome.FAILED,
                    "status cannot be imported",
                )
            await self._import_metadata_only(
                scope, parsed, envelope=envelope, operator_id=operator_id,
                job_id=job_id,
            )
            return ImportEntryResult(
                index, parsed.source_memory_id, ImportOutcome.METADATA_ONLY,
                f"status={parsed.status.value}",
            )
        memory, revision, source = self._new_memory(
            scope, parsed, source_platform=envelope.source_platform,
            export_id=envelope.export_id, operator_id=operator_id,
        )
        try:
            written = await self._coordinator.create_import(
                memory, revision, source, guard=guard,
            )
        except Conflict as exc:
            return ImportEntryResult(
                index, parsed.source_memory_id, ImportOutcome.CONFLICT, str(exc),
            )
        if written.outcome is ImportWriteOutcome.DUPLICATE_IMPORT:
            return ImportEntryResult(
                index, parsed.source_memory_id,
                ImportOutcome.SKIPPED_DUPLICATE_IMPORT, "idempotency key already imported",
            )
        if written.outcome is ImportWriteOutcome.DUPLICATE_ACTIVE:
            existing_id = written.memory.id if written.memory is not None else ""
            return ImportEntryResult(
                index, parsed.source_memory_id,
                ImportOutcome.SKIPPED_DUPLICATE_ACTIVE,
                f"active duplicate {existing_id}", existing_id or None,
            )
        return ImportEntryResult(
            index, parsed.source_memory_id, ImportOutcome.IMPORTED, "",
            written.memory.id if written.memory is not None else None,
        )

    def _dry_run(self, scope: Scope, envelope: ExportEnvelope) -> ImportResult:
        entries: list[ImportEntryResult] = []
        imported = metadata_only = skipped = conflicts = failed = 0
        for index, raw in enumerate(envelope.entries):
            source_memory_id = str(raw.get("source_memory_id") or "")
            try:
                parsed = parse_entry(raw)
            except ValidationError as exc:
                failed += 1
                entries.append(ImportEntryResult(
                    index, source_memory_id, ImportOutcome.FAILED, str(exc),
                ))
                continue
            outcome = self._preview(scope, envelope, parsed)
            if outcome is ImportOutcome.WOULD_IMPORT:
                imported += 1
            elif outcome is ImportOutcome.WOULD_METADATA:
                metadata_only += 1
            elif outcome is ImportOutcome.WOULD_CONFLICT:
                conflicts += 1
            else:
                skipped += 1
            entries.append(ImportEntryResult(
                index, parsed.source_memory_id, outcome,
            ))
        return ImportResult(
            job_id=None, dry_run=True, total=len(envelope.entries),
            imported=imported, metadata_only=metadata_only, skipped=skipped,
            conflicts=conflicts, failed=failed, entries=tuple(entries),
        )

    async def rollback(self, scope: Scope, job_id: str, *,
                       operator_id: str) -> ImportResult:
        job = self._jobs.get(job_id)
        if job.scope_id != scope.id or job.subject_id != scope.identity.subject_id:
            raise AuthorizationDenied("import job does not belong to this scope")
        if job.status is ImportJobStatus.RUNNING:
            raise Conflict("import job is still running")
        deleted = 0
        missing = 0
        for memory_id in job.created_memory_ids:
            try:
                await self._lifecycle.delete_memory(
                    memory_id, scope_id=scope.id,
                    subject_id=scope.identity.subject_id,
                    operation_id=self._ids.new_id("operation"),
                    operator_type=OperatorType.USER, operator_id=operator_id,
                    at=self._clock.now(),
                )
            except NotFound:
                missing += 1
            else:
                deleted += 1
        rolled_back = ImportJob(
            id=job.id, scope_id=job.scope_id, subject_id=job.subject_id,
            file_sha256=job.file_sha256, dataset_version=job.dataset_version,
            status=ImportJobStatus.FAILED, cursor=job.cursor,
            created_memory_ids=job.created_memory_ids,
            created_at=job.created_at, updated_at=self._clock.now(),
            last_error="rolled back by operator",
        )
        self._jobs.update(rolled_back)
        return ImportResult(
            job_id=job.id, dry_run=False, total=len(job.created_memory_ids),
            imported=deleted, failed=missing,
            note="导入回滚只删除本次新建记忆；任务已置 FAILED，不可续跑。",
        )
