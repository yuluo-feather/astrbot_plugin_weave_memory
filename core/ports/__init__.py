"""Platform- and infrastructure-independent ports owned by the core."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ..model import (
    AdminDecision,
    AdminSource,
    ExtractionCandidate,
    ExtractionJob,
    ExtractionJobStatus,
    ExtractionWindow,
    Identity,
    ImportJob,
    MemoryConsent,
    MemoryEntity,
    MemoryOperation,
    MemoryRevision,
    MemorySource,
    MemoryStatus,
    MemoryType,
    OperatorType,
    Scope,
)
from ..policy.dedup import DedupKey


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class SearchCandidate:
    memory_id: str
    score: float
    status: MemoryStatus
    valid_from: datetime | None
    valid_until: datetime | None
    scope_enabled: bool


class IdGenerator(Protocol):
    def new_id(self, entity_kind: str) -> str: ...


class MemoryStore(Protocol):
    def create(self, memory: MemoryEntity, revision: MemoryRevision,
               source: MemorySource) -> None: ...

    def get(self, memory_id: str, *, scope_id: str, subject_id: str) -> MemoryEntity: ...

    def find_active_duplicate(self, key: DedupKey) -> MemoryEntity | None: ...

    def find_candidate_duplicate(self, key: DedupKey) -> MemoryEntity | None: ...

    def list_candidates(
        self, *, scope_id: str, subject_id: str, limit: int, offset: int,
    ) -> tuple[tuple[MemoryEntity, ...], int]: ...

    def update(self, memory: MemoryEntity, *, expected_revision: int,
               revision: MemoryRevision, source: MemorySource | None = None,
               operation: MemoryOperation | None = None) -> None: ...

    def list_expired_candidates(
        self, *, scope_id: str, subject_id: str, at: datetime, limit: int = 100,
    ) -> tuple[MemoryEntity, ...]: ...

    def reject_candidate(self, memory: MemoryEntity, *, expected_revision: int,
                         revision: MemoryRevision, operation: MemoryOperation,
                         purge_body: bool) -> None: ...

    def list_active(self, *, scope_id: str, subject_id: str) -> tuple[MemoryEntity, ...]: ...

    def list_for_export(
        self, *, scope_id: str, subject_id: str,
        statuses: tuple[MemoryStatus, ...],
    ) -> tuple[MemoryEntity, ...]: ...

    def find_import_source(self, evidence_ref: str) -> bool: ...

    def record_operation(
        self, *, operation_id: str, scope_id: str, memory_id: str | None,
        operation_type: str, operator_type: OperatorType, operator_id: str,
        occurred_at: datetime, metadata: Mapping[str, object],
    ) -> None: ...

    def list_active_page(
        self, *, scope_id: str, subject_id: str, limit: int, offset: int,
        memory_type: MemoryType | None = None,
    ) -> tuple[tuple[MemoryEntity, ...], int]: ...

    def list_revisions(
        self, memory_id: str, *, scope_id: str, subject_id: str,
    ) -> tuple[MemoryRevision, ...]: ...

    def list_sources(
        self, memory_id: str, *, scope_id: str, subject_id: str,
    ) -> tuple[MemorySource, ...]: ...

    def statistics(self, *, scope_id: str, subject_id: str) -> dict[str, object]: ...

    def list_operations(
        self, *, scope_id: str, subject_id: str, limit: int, offset: int,
    ) -> tuple[tuple[dict[str, object], ...], int]: ...

    def record_access(self, memory_ids: tuple[str, ...], *, scope_id: str,
                      subject_id: str, at: datetime) -> None: ...

    def delete(self, memory_id: str, *, scope_id: str, subject_id: str,
               operation_id: str, operator_type: OperatorType, operator_id: str,
               occurred_at: datetime) -> None: ...

    def clear_scope(self, scope_id: str, *, operation_id: str,
                    operator_type: OperatorType, operator_id: str,
                    occurred_at: datetime) -> tuple[str, ...]: ...

    def disable_scope(self, scope_id: str, *, operation_id: str,
                      operator_type: OperatorType, operator_id: str,
                      occurred_at: datetime) -> bool: ...

    def purge_deleted_payload(self) -> None: ...


class SearchIndex(Protocol):
    def is_healthy(self) -> bool: ...
    def document_count(
        self, *, scope_id: str | None = None, subject_id: str | None = None,
    ) -> int: ...
    def integrity_check(self) -> None: ...
    def sync_memory(self, memory_id: str) -> None: ...
    def rebuild(self) -> int: ...
    def recreate_and_rebuild(self) -> int: ...
    def search(self, query: str, *, scope_id: str, subject_id: str,
               limit: int = 20, at: datetime | None = None,
               scope_type: str = "PERSONAL") -> tuple[str, ...]: ...
    def search_diagnostics(
        self, query: str, *, scope_id: str, subject_id: str, limit: int = 100,
        scope_type: str = "PERSONAL",
    ) -> tuple[SearchCandidate, ...]: ...


class PlatformAdapter(Protocol):
    """The only boundary allowed to accept host event objects."""
    def resolve_identity(self, event: object) -> Identity: ...


class ManagementRepository(Protocol):
    """Read-only aggregate queries for the V1.6 management API (ADR-0016)."""

    def overview_counts(self) -> dict[str, object]: ...

    def recent_operations(self, *, limit: int = 5) -> tuple[dict[str, object], ...]: ...

    def list_group_scopes(self) -> tuple[dict[str, object], ...]: ...

    def maintenance_status(self) -> dict[str, object]: ...


class LlmExtractor(Protocol):
    """V1.5 structured extraction port (ADR-0015).

    Implementations live in adapters; core never calls a provider directly.
    """

    async def extract(
        self, window: ExtractionWindow, *,
        allowed_types: tuple[MemoryType, ...],
        sensitive_categories: tuple[str, ...],
    ) -> tuple[ExtractionCandidate, ...]: ...


class ExtractionJobStore(Protocol):
    """Durable, idempotent extraction job state (schema v6)."""

    def create_or_get(self, job: ExtractionJob) -> tuple[ExtractionJob, bool]: ...

    def get(self, job_id: str) -> ExtractionJob: ...

    def update(self, job: ExtractionJob, *, expected_status: ExtractionJobStatus) -> None: ...

    def list_by_status(
        self, *, statuses: tuple[ExtractionJobStatus, ...], limit: int = 100,
    ) -> tuple[ExtractionJob, ...]: ...

    def count_by_status(self, *, scope_id: str, subject_id: str) -> dict[str, int]: ...


class ImportJobStore(Protocol):
    """Durable import job state (schema v8, ADR-0017)."""

    def find_latest(self, *, scope_id: str, file_sha256: str) -> ImportJob | None: ...

    def create(self, job: ImportJob) -> ImportJob: ...

    def update(self, job: ImportJob) -> None: ...

    def get(self, job_id: str) -> ImportJob: ...


class SchemaVersionProvider(Protocol):
    CURRENT_SCHEMA_VERSION: int
    def schema_version(self) -> int: ...


class ScopeStore(Protocol):
    def create_scope(self, scope: Scope) -> None: ...
    def get_scope(self, scope_id: str) -> Scope: ...
    def is_scope_enabled(self, scope_id: str) -> bool: ...


class AdminResolver(Protocol):
    def resolve(self, identity: Identity, sender_id: str,
                event: object | None = None) -> AdminDecision: ...


class GroupScopeStore(ScopeStore, Protocol):
    def get_admin_metadata(self, scope_id: str) -> tuple[tuple[str, ...], AdminSource]: ...
    def set_admin_metadata(self, scope_id: str, admin_ids: tuple[str, ...],
                           source: AdminSource) -> None: ...
    def enable_scope(self, scope_id: str, *, operation_id: str,
                     operator_type: OperatorType, operator_id: str,
                     occurred_at: datetime) -> bool: ...
    def disable_group_scope(self, scope_id: str, *, operation_id: str,
                            operator_type: OperatorType, operator_id: str,
                            occurred_at: datetime) -> bool: ...
    def backup_group_scope(self, scope_id: str, destination: str) -> str: ...
    def delete_group_scope(self, scope_id: str, *, operation_id: str,
                           operator_type: OperatorType, operator_id: str,
                           occurred_at: datetime) -> int: ...
    def count_active(self, scope_id: str) -> int: ...


class ConsentRepository(Protocol):
    def record(self, consent: MemoryConsent,
               *, allow_disabled_grant: bool = False) -> None: ...
    def current(self, *, subject_id: str, scope_id: str,
                consent_type: str) -> MemoryConsent | None: ...
    def is_granted(self, *, subject_id: str, scope_id: str,
                   consent_type: str) -> bool: ...
    def history(self, *, subject_id: str, scope_id: str,
                consent_type: str) -> tuple[MemoryConsent, ...]: ...
