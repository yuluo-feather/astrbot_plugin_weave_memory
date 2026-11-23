"""Application-level lifecycle transitions; the only status mutation API."""

from __future__ import annotations

from datetime import datetime

from ..core.errors import ConsentRevocationPending, InvalidTransition, StorageError
from ..core.model import (
    MemoryEntity,
    MemoryOperation,
    MemoryRevision,
    MemoryStatus,
    OperatorType,
)
from ..core.policy.lifecycle import archive_expired_candidate, expire_if_due, transition
from ..core.ports import ScopeStore
from .consent_service import ConsentService, PersonalConsentType
from .write_coordinator import OperationReceipt, WriteCoordinator, WriteReceipt


class LifecycleService:
    def __init__(self, coordinator: WriteCoordinator, scopes: ScopeStore,
                 consent: ConsentService) -> None:
        self._coordinator = coordinator
        self._scopes = scopes
        self._consent = consent

    async def transition_memory(self, memory: MemoryEntity, target: MemoryStatus, *,
                                revision_id: str, at: datetime, reason: str,
                                operator_type: OperatorType,
                                superseded_by: str | None = None,
                                operation: MemoryOperation | None = None) -> WriteReceipt:
        if target is MemoryStatus.DELETED:
            raise InvalidTransition("use delete_memory for irreversible physical deletion")
        updated = transition(
            memory, target, at=at, superseded_by=superseded_by,
            operator_type=operator_type,
        )
        revision = MemoryRevision(
            id=revision_id, memory_id=updated.id, revision=updated.current_revision,
            content=updated.content, status=updated.status, changed_at=at,
            change_reason=reason,
            snapshot={"scope_id": updated.scope_id, "subject_id": updated.subject_id,
                      "memory_type": updated.memory_type.value,
                      "source_type": updated.source_type.value,
                      "normalized_content": updated.normalized_content},
        )
        return await self._coordinator.update_lifecycle(
            updated, expected_revision=memory.current_revision, revision=revision,
            operation=operation,
        )

    async def expire_active(self, memory: MemoryEntity, *, revision_id: str,
                            at: datetime) -> WriteReceipt | None:
        updated = expire_if_due(memory, at=at)
        if updated is memory:
            return None
        return await self.transition_memory(
            memory, updated.status, revision_id=revision_id, at=at,
            reason="semantic validity expired", operator_type=OperatorType.SYSTEM,
        )

    async def archive_candidate(self, memory: MemoryEntity, *,
                                candidate_expires_at: datetime,
                                revision_id: str, at: datetime,
                                operation: MemoryOperation | None = None) -> WriteReceipt | None:
        updated = archive_expired_candidate(
            memory, candidate_expires_at=candidate_expires_at, at=at,
        )
        if updated is memory:
            return None
        return await self.transition_memory(
            memory, updated.status, revision_id=revision_id, at=at,
            reason="candidate workflow TTL expired", operator_type=OperatorType.SYSTEM,
            operation=operation,
        )

    async def reject_candidate(self, memory: MemoryEntity, *, expected_revision: int,
                               revision_id: str, at: datetime, reason: str,
                               operator_type: OperatorType,
                               operation: MemoryOperation,
                               purge_body: bool) -> WriteReceipt:
        updated = transition(memory, MemoryStatus.REJECTED, at=at,
                             operator_type=operator_type)
        revision = MemoryRevision(
            id=revision_id, memory_id=updated.id, revision=updated.current_revision,
            content=updated.content, status=updated.status, changed_at=at,
            change_reason=reason,
            snapshot={"scope_id": updated.scope_id, "subject_id": updated.subject_id,
                      "memory_type": updated.memory_type.value,
                      "source_type": updated.source_type.value},
        )
        return await self._coordinator.reject_candidate(
            updated, expected_revision=expected_revision, revision=revision,
            operation=operation, purge_body=purge_body,
        )

    async def delete_memory(self, memory_id: str, *, scope_id: str,
                            subject_id: str, operation_id: str,
                            operator_type: OperatorType, operator_id: str,
                            at: datetime) -> OperationReceipt:
        return await self._coordinator.delete(
            memory_id, scope_id=scope_id, subject_id=subject_id,
            operation_id=operation_id, operator_type=operator_type,
            operator_id=operator_id, occurred_at=at,
        )

    async def clear_scope(self, scope_id: str, *, operation_id: str,
                          operator_type: OperatorType, operator_id: str,
                          at: datetime) -> OperationReceipt:
        return await self._coordinator.clear_scope(
            scope_id, operation_id=operation_id, operator_type=operator_type,
            operator_id=operator_id, occurred_at=at,
        )

    async def disable_scope(self, scope_id: str, *, operation_id: str,
                            operator_type: OperatorType, operator_id: str,
                            at: datetime) -> OperationReceipt:
        receipt = await self._coordinator.disable_scope(
            scope_id, operation_id=operation_id, operator_type=operator_type,
            operator_id=operator_id, occurred_at=at,
        )
        # Scope disable is the fail-closed boundary. Revoke consent afterward;
        # if this append fails, the scope is already inaccessible and a retry
        # with the same operation ID can complete consent invalidation.
        scope = self._scopes.get_scope(scope_id)
        if self._consent.is_granted(scope, PersonalConsentType.PERSONAL_MEMORY):
            try:
                self._consent.revoke(
                    scope, PersonalConsentType.PERSONAL_MEMORY,
                    operator_id=operator_id, source="scope_disabled",
                )
            except StorageError as exc:
                raise ConsentRevocationPending(
                    "scope is disabled; consent revocation event must be retried",
                ) from exc
        return receipt
