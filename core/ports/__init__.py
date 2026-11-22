"""Platform- and infrastructure-independent ports owned by the core."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from ..model import (
    Identity,
    MemoryConsent,
    MemoryEntity,
    MemoryRevision,
    MemorySource,
    OperatorType,
    Scope,
)
from ..policy.dedup import DedupKey


class Clock(Protocol):
    def now(self) -> datetime: ...


class IdGenerator(Protocol):
    def new_id(self, entity_kind: str) -> str: ...


class MemoryStore(Protocol):
    def create(self, memory: MemoryEntity, revision: MemoryRevision,
               source: MemorySource) -> None: ...

    def get(self, memory_id: str, *, scope_id: str, subject_id: str) -> MemoryEntity: ...

    def find_active_duplicate(self, key: DedupKey) -> MemoryEntity | None: ...

    def update(self, memory: MemoryEntity, *, expected_revision: int,
               revision: MemoryRevision, source: MemorySource | None = None) -> None: ...

    def list_active(self, *, scope_id: str, subject_id: str) -> tuple[MemoryEntity, ...]: ...

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
    def document_count(self) -> int: ...
    def integrity_check(self) -> None: ...
    def sync_memory(self, memory_id: str) -> None: ...
    def rebuild(self) -> int: ...
    def recreate_and_rebuild(self) -> int: ...
    def search(self, query: str, *, scope_id: str, subject_id: str,
               limit: int = 20, at: datetime | None = None) -> tuple[str, ...]: ...


class PlatformAdapter(Protocol):
    """The only boundary allowed to accept host event objects."""
    def resolve_identity(self, event: object) -> Identity: ...


class ManagementService(Protocol):
    """Reserved V1.6/V2 boundary for the JSON management API.

    V1.0 defines no methods and provides no implementation; signatures freeze
    when V1.6 starts.
    """


class LlmExtractor(Protocol):
    """Reserved V1.5 boundary for structured extraction.

    V1.0 defines no methods and calls no model; ExtractionService is the V1.5
    application service built on this port.
    """


class SchemaVersionProvider(Protocol):
    CURRENT_SCHEMA_VERSION: int
    def schema_version(self) -> int: ...


class ScopeStore(Protocol):
    def create_scope(self, scope: Scope) -> None: ...
    def get_scope(self, scope_id: str) -> Scope: ...
    def is_scope_enabled(self, scope_id: str) -> bool: ...


class ConsentRepository(Protocol):
    def record(self, consent: MemoryConsent) -> None: ...
    def current(self, *, subject_id: str, scope_id: str,
                consent_type: str) -> MemoryConsent | None: ...
    def is_granted(self, *, subject_id: str, scope_id: str,
                   consent_type: str) -> bool: ...
    def history(self, *, subject_id: str, scope_id: str,
                consent_type: str) -> tuple[MemoryConsent, ...]: ...
