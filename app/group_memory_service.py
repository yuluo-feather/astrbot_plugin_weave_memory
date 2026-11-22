"""V1.1 Group shared write/read use cases."""

from __future__ import annotations

from ..core.errors import AuthorizationDenied, ValidationError
from ..core.model import (
    AdminDecision,
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
from ..core.ports import Clock, IdGenerator, MemoryStore, ScopeStore
from .consent_service import ConsentService, GroupConsentType
from .export import build_export
from .write_coordinator import WriteCoordinator, WriteReceipt

_GROUP_TYPES: dict[str, MemoryType] = {
    "topic": MemoryType.FACT,
    "agreement": MemoryType.PREFERENCE,
    "atmosphere": MemoryType.PREFERENCE,
    "experience": MemoryType.EVENT,
}


class GroupMemoryService:
    def __init__(self, store: MemoryStore, scopes: ScopeStore,
                 coordinator: WriteCoordinator, consent: ConsentService,
                 clock: Clock, ids: IdGenerator, *,
                 max_active: int = 200) -> None:
        self._store = store
        self._scopes = scopes
        self._coordinator = coordinator
        self._consent = consent
        self._clock = clock
        self._ids = ids
        self._max_active = max_active

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
        label = group_label.strip().lower()
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

    def export_group(self, scope: Scope, decision: AdminDecision) -> dict:
        if not decision.allowed:
            raise AuthorizationDenied("group export requires an administrator")
        return build_export(scope, self.list_group(scope), exported_at=self._clock.now())

    def needs_cleanup(self, scope: Scope) -> bool:
        return len(self._store.list_active(
            scope_id=scope.id, subject_id=scope.identity.subject_id,
        )) >= self._max_active
