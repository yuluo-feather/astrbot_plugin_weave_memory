"""V1.1 Group shared write/read use cases."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

from ..core.errors import AuthorizationDenied, Conflict, StorageError, ValidationError
from ..core.model import (
    AdminDecision,
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
from ..core.ports import Clock, IdGenerator, MemoryStore, ScopeStore
from .consent_service import ConsentService, GroupConsentType
from .export import EXPORTED_STATUSES, build_export
from .lifecycle_service import LifecycleService
from .write_coordinator import OperationReceipt, WriteCoordinator, WriteReceipt

_GROUP_TYPES: dict[str, MemoryType] = {
    "topic": MemoryType.FACT,
    "agreement": MemoryType.PREFERENCE,
    "atmosphere": MemoryType.PREFERENCE,
    "experience": MemoryType.EVENT,
}

_GROUP_LABEL_ALIASES: dict[str, str] = {
    "话题": "topic",
    "主题": "topic",
    "事实": "topic",
    "约定": "agreement",
    "共识": "agreement",
    "偏好": "agreement",
    "喜好": "agreement",
    "氛围": "atmosphere",
    "气氛": "atmosphere",
    "经历": "experience",
    "事件": "experience",
    "体验": "experience",
}

_GROUP_LABEL_TAGS: dict[str, tuple[str, ...]] = {
    "topic": ("topic", "话题", "主题", "事实"),
    "agreement": ("agreement", "约定", "共识", "偏好", "喜好"),
    "atmosphere": ("atmosphere", "氛围", "气氛"),
    "experience": ("experience", "经历", "事件", "体验"),
}


class GroupMemoryService:
    def __init__(self, store: MemoryStore, scopes: ScopeStore,
                 coordinator: WriteCoordinator, consent: ConsentService,
                 clock: Clock, ids: IdGenerator, *,
                 max_active: int = 200,
                 lifecycle: LifecycleService | None = None) -> None:
        self._store = store
        self._scopes = scopes
        self._coordinator = coordinator
        self._consent = consent
        self._clock = clock
        self._ids = ids
        self._max_active = max_active
        self._lifecycle = lifecycle

    @staticmethod
    def _require_group(scope: Scope) -> None:
        if scope.identity.scope_type is not ScopeType.GROUP:
            raise ValidationError("GroupMemoryService requires a GROUP scope")

    def _require_readable(self, scope: Scope) -> None:
        self._require_group(scope)
        self._consent.require(scope, GroupConsentType.GROUP_MEMORY)
        if not self._scopes.is_scope_enabled(scope.id):
            raise AuthorizationDenied("group memory is disabled")

    async def save_group(self, scope: Scope, content: str, *,
                         group_label: str, operator_id: str,
                         decision: AdminDecision) -> tuple[MemoryEntity, WriteReceipt]:
        self._require_group(scope)
        if not decision.allowed:
            raise AuthorizationDenied("group write requires an administrator")
        self._consent.require(scope, GroupConsentType.GROUP_MEMORY)
        if not self._scopes.is_scope_enabled(scope.id):
            raise AuthorizationDenied("group memory is disabled")
        raw_label = group_label.strip().lower()
        for prefix in ("我的", "本群", "群"):
            if raw_label.startswith(prefix):
                raw_label = raw_label[len(prefix):]
        label = _GROUP_LABEL_ALIASES.get(raw_label, raw_label)
        memory_type = _GROUP_TYPES.get(label)
        if memory_type is None:
            raise ValidationError("group_label must be topic/agreement/atmosphere/experience")
        normalized = normalize_content(content)
        now = self._clock.now()
        memory_id = self._ids.new_id("memory")
        metadata = {
            "group_label": label,
            "group_id": scope.identity.subject_id,
            "admin_source": decision.source.value,
            "tags": list(_GROUP_LABEL_TAGS.get(label, (label,))),
        }
        memory = MemoryEntity(
            id=memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
            memory_type=memory_type, content=content, normalized_content=normalized,
            status=MemoryStatus.ACTIVE, source_type=SourceType.MANUAL,
            current_revision=1, created_at=now, updated_at=now, metadata=metadata,
        )
        revision = MemoryRevision(
            id=self._ids.new_id("revision"), memory_id=memory_id, revision=1,
            content=content, status=MemoryStatus.ACTIVE, changed_at=now,
            change_reason="group admin save",
            snapshot={
                "scope_id": scope.id, "subject_id": scope.identity.subject_id,
                "memory_type": memory_type.value, "group_label": label,
            },
        )
        source = MemorySource(
            id=self._ids.new_id("source"), memory_id=memory_id,
            source_type=SourceType.MANUAL, operator_type=OperatorType.ADMIN,
            operator_id=operator_id, captured_at=now,
        )

        def guard() -> None:
            self._consent.require(scope, GroupConsentType.GROUP_MEMORY)
            if not self._scopes.is_scope_enabled(scope.id):
                raise AuthorizationDenied("group memory is disabled")

        saved, receipt, _created = await self._coordinator.create_or_get_duplicate(
            memory, revision, source, guard=guard,
        )
        return saved, receipt

    def list_group(self, scope: Scope) -> tuple[MemoryEntity, ...]:
        self._require_readable(scope)
        return self._store.list_active(
            scope_id=scope.id, subject_id=scope.identity.subject_id,
        )

    def write_guard(self, scope: Scope) -> Callable[[], None]:
        """Re-check group consent and enabled state for import writes."""
        def guard() -> None:
            self._require_readable(scope)
        return guard

    def export_group(self, scope: Scope, decision: AdminDecision, *,
                     include_all: bool = False) -> dict:
        if not decision.allowed:
            raise AuthorizationDenied("group export requires an administrator")
        self._require_readable(scope)
        if include_all:
            memories = self._store.list_for_export(
                scope_id=scope.id, subject_id=scope.identity.subject_id,
                statuses=EXPORTED_STATUSES,
            )
        else:
            memories = self._store.list_active(
                scope_id=scope.id, subject_id=scope.identity.subject_id,
            )
        return build_export(
            scope, memories, exported_at=self._clock.now(),
            export_id=self._ids.new_id("export"),
        )

    def needs_cleanup(self, scope: Scope) -> bool:
        return len(self._store.list_active(
            scope_id=scope.id, subject_id=scope.identity.subject_id,
        )) >= self._max_active

    # ---- V2.1 writable console (GROUP scope only, ADR-0031) ----

    def _require_writable(self, scope: Scope, decision: AdminDecision) -> None:
        self._require_group(scope)
        if not decision.allowed:
            raise AuthorizationDenied("group write requires an administrator")
        self._consent.require(scope, GroupConsentType.GROUP_MEMORY)
        if not self._scopes.is_scope_enabled(scope.id):
            raise AuthorizationDenied("group memory is disabled")

    def _write_guard(self, scope: Scope) -> Callable[[], None]:
        def guard() -> None:
            self._consent.require(scope, GroupConsentType.GROUP_MEMORY)
            if not self._scopes.is_scope_enabled(scope.id):
                raise AuthorizationDenied("group memory is disabled")
        return guard

    def _require_lifecycle(self) -> LifecycleService:
        if self._lifecycle is None:
            raise StorageError("group lifecycle service is unavailable")
        return self._lifecycle

    async def create_group(self, scope: Scope, content: str, *,
                           group_label: str, operator_id: str,
                           decision: AdminDecision,
                           ) -> tuple[MemoryEntity, WriteReceipt, str]:
        """Create one group memory and record a WebUI audit row."""
        saved, receipt = await self.save_group(
            scope, content, group_label=group_label,
            operator_id=operator_id, decision=decision,
        )
        audit_id = self._record_audit(
            scope, saved.id, "CREATE_MEMORY", operator_id,
            {"surface": "webui", "group_label": group_label}, self._clock.now(),
        )
        return saved, receipt, audit_id

    async def update_group(self, scope: Scope, memory_id: str, content: str, *,
                           expected_revision: int, operator_id: str,
                           decision: AdminDecision,
                           ) -> tuple[MemoryEntity, WriteReceipt, str]:
        self._require_writable(scope, decision)
        if expected_revision < 1:
            raise ValidationError("expected_revision must be >= 1")
        current = self._store.get(
            memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
        )
        if current.status is not MemoryStatus.ACTIVE:
            raise Conflict("only ACTIVE memory can be edited")
        if current.current_revision != expected_revision:
            raise Conflict("revision conflict")
        normalized = normalize_content(content)
        now = self._clock.now()
        updated = replace(
            current, content=content, normalized_content=normalized,
            current_revision=current.current_revision + 1, updated_at=now,
        )
        revision = MemoryRevision(
            id=self._ids.new_id("revision"), memory_id=memory_id,
            revision=updated.current_revision, content=content,
            status=updated.status, changed_at=now,
            change_reason="group admin edit (webui)",
            snapshot={"scope_id": scope.id, "subject_id": scope.identity.subject_id,
                      "memory_type": updated.memory_type.value,
                      "source_type": updated.source_type.value,
                      "normalized_content": normalized},
        )
        source = MemorySource(
            id=self._ids.new_id("source"), memory_id=memory_id,
            source_type=SourceType.MANUAL, operator_type=OperatorType.ADMIN,
            operator_id=operator_id, captured_at=now,
        )
        receipt = await self._coordinator.update(
            updated, expected_revision=expected_revision,
            revision=revision, source=source, guard=self._write_guard(scope),
        )
        audit_id = self._record_audit(
            scope, memory_id, "UPDATE_MEMORY", operator_id,
            {"surface": "webui", "revision": updated.current_revision}, now,
        )
        return updated, receipt, audit_id

    async def archive_group(self, scope: Scope, memory_id: str, *,
                            operator_id: str, decision: AdminDecision,
                            ) -> tuple[MemoryEntity, str]:
        self._require_writable(scope, decision)
        lifecycle = self._require_lifecycle()
        memory = self._store.get(
            memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
        )
        if memory.status is not MemoryStatus.ACTIVE:
            raise Conflict("only ACTIVE memory can be archived")
        now = self._clock.now()
        operation = MemoryOperation(
            id=self._ids.new_id("operation"), scope_id=scope.id, memory_id=memory_id,
            operation_type="ARCHIVE_MEMORY", operator_type=OperatorType.ADMIN,
            operator_id=operator_id, occurred_at=now,
            metadata={"archive_reason": "USER_ARCHIVE", "surface": "webui"},
        )
        await lifecycle.archive_memory(
            memory, revision_id=self._ids.new_id("revision"), at=now,
            reason="group admin archived memory", operator_type=OperatorType.ADMIN,
            operation=operation,
        )
        return memory, operation.id

    async def restore_group(self, scope: Scope, memory_id: str, *,
                            operator_id: str, decision: AdminDecision,
                            ) -> tuple[MemoryEntity, str]:
        self._require_writable(scope, decision)
        lifecycle = self._require_lifecycle()
        memory = self._store.get(
            memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
        )
        now = self._clock.now()
        operation = MemoryOperation(
            id=self._ids.new_id("operation"), scope_id=scope.id, memory_id=memory_id,
            operation_type="RESTORE_MEMORY", operator_type=OperatorType.ADMIN,
            operator_id=operator_id, occurred_at=now,
            metadata={
                "archive_reason": (
                    memory.archive_reason.value
                    if memory.archive_reason is not None else None
                ),
                "surface": "webui",
            },
        )
        await lifecycle.restore_memory(
            memory, revision_id=self._ids.new_id("revision"), at=now,
            reason="group admin restored memory", operator_type=OperatorType.ADMIN,
            operation=operation,
        )
        return memory, operation.id

    async def purge_group(self, scope: Scope, memory_id: str, *,
                          operator_id: str, decision: AdminDecision,
                          ) -> tuple[str, int]:
        """Physical purge of one ARCHIVED memory; returns (audit_id, purged)."""
        self._require_writable(scope, decision)
        lifecycle = self._require_lifecycle()
        memory = self._store.get(
            memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
        )
        if memory.status is not MemoryStatus.ARCHIVED:
            raise Conflict("only ARCHIVED memory can be purged")
        now = self._clock.now()
        operation_id = self._ids.new_id("operation")
        receipt: OperationReceipt = await lifecycle.purge_memory(
            memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
            operation_id=operation_id, operator_type=OperatorType.ADMIN,
            operator_id=operator_id, at=now,
        )
        return operation_id, receipt.affected_count

    def get_group(self, scope: Scope, memory_id: str) -> MemoryEntity:
        self._require_readable(scope)
        return self._store.get(
            memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
        )

    def list_archived_page(self, scope: Scope, *, limit: int = 20,
                           offset: int = 0,
                           ) -> tuple[tuple[MemoryEntity, ...], int]:
        self._require_readable(scope)
        return self._store.list_archived_page(
            scope_id=scope.id, subject_id=scope.identity.subject_id,
            limit=limit, offset=offset,
        )

    def list_operations(self, scope: Scope, memory_id: str, *,
                        limit: int = 20) -> tuple[dict[str, object], ...]:
        self._require_readable(scope)
        return self._store.list_operations_for_memory(
            memory_id, scope_id=scope.id, limit=limit,
        )

    def _record_audit(self, scope: Scope, memory_id: str | None,
                      operation_type: str, operator_id: str,
                      metadata: dict[str, object],
                      occurred_at: datetime) -> str:
        audit_id = self._ids.new_id("operation")
        self._store.record_operation(
            operation_id=audit_id, scope_id=scope.id, memory_id=memory_id,
            operation_type=operation_type, operator_type=OperatorType.ADMIN,
            operator_id=operator_id, occurred_at=occurred_at, metadata=metadata,
        )
        return audit_id
