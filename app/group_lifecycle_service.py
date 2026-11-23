"""V1.1 Group lifecycle use cases: enable/disable/clear/destroy."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from ..core.errors import (
    AuthorizationDenied,
    GroupConsentRevocationPending,
    IndexUnavailable,
    InvalidTransition,
    NotFound,
    StorageError,
    ValidationError,
)
from ..core.model import AdminDecision, OperatorType, Scope, ScopeType
from ..core.ports import Clock, GroupScopeStore, IdGenerator, SearchIndex
from .consent_service import ConsentService, GroupConsentType
from .write_coordinator import OperationReceipt, WriteCoordinator


@dataclass(frozen=True, slots=True)
class GroupLifecycleReceipt:
    operation_id: str
    changed: bool


@dataclass(frozen=True, slots=True)
class GroupDestroyReceipt:
    operation_id: str
    scope_id: str
    backup_path: str
    backup_sha256: str
    affected_count: int
    purged: bool


class GroupLifecycleService:
    def __init__(self, store: GroupScopeStore, coordinator: WriteCoordinator,
                 consent: ConsentService, index: SearchIndex,
                 clock: Clock, ids: IdGenerator, backup_dir: str | Path) -> None:
        self._store = store
        self._coordinator = coordinator
        self._consent = consent
        self._index = index
        self._clock = clock
        self._ids = ids
        self._backup_dir = Path(backup_dir)

    @staticmethod
    def _require_group(scope: Scope) -> None:
        if scope.identity.scope_type is not ScopeType.GROUP:
            raise ValidationError("GroupLifecycleService requires a GROUP scope")

    @staticmethod
    def _require_admin(decision: AdminDecision) -> None:
        if not decision.allowed:
            raise AuthorizationDenied("group lifecycle requires an administrator")

    async def enable(self, scope: Scope, decision: AdminDecision, *,
                     operator_id: str,
                     operation_id: str | None = None) -> GroupLifecycleReceipt:
        self._require_group(scope)
        self._require_admin(decision)
        op = operation_id or self._ids.new_id("operation")
        self._coordinator.write_metadata(
            lambda: self._store.set_admin_metadata(
                scope.id, decision.admin_ids, decision.source,
            ),
        )
        changed = self._coordinator.write_metadata(
            lambda: self._store.enable_scope(
                scope.id, operation_id=op, operator_type=OperatorType.ADMIN,
                operator_id=operator_id, occurred_at=self._clock.now(),
            ),
        )
        # Consent may only be granted to an enabled scope; this runs after the
        # enable write. No await sits between the two, so readers cannot observe
        # an enabled group without consent on the owning event loop.
        self._consent.grant(
            scope, GroupConsentType.GROUP_MEMORY, operator_id=operator_id,
        )
        return GroupLifecycleReceipt(op, changed)

    async def disable(self, scope: Scope, decision: AdminDecision, *,
                      operator_id: str,
                      operation_id: str | None = None) -> GroupLifecycleReceipt:
        self._require_group(scope)
        self._require_admin(decision)
        op = operation_id or self._ids.new_id("operation")
        changed = self._coordinator.write_metadata(
            lambda: self._store.disable_group_scope(
                scope.id, operation_id=op, operator_type=OperatorType.ADMIN,
                operator_id=operator_id, occurred_at=self._clock.now(),
            ),
        )
        if self._consent.is_granted(scope, GroupConsentType.GROUP_MEMORY):
            try:
                self._consent.revoke(
                    scope, GroupConsentType.GROUP_MEMORY,
                    operator_id=operator_id, source="group_scope_disabled",
                )
            except StorageError as exc:
                raise GroupConsentRevocationPending(
                    "group scope is disabled; group_memory revocation must be retried",
                ) from exc
        return GroupLifecycleReceipt(op, changed)

    async def clear(self, scope: Scope, decision: AdminDecision, *,
                    operator_id: str,
                    operation_id: str | None = None) -> OperationReceipt:
        self._require_group(scope)
        self._require_admin(decision)
        return await self._coordinator.clear_scope(
            scope.id, operation_id=operation_id or self._ids.new_id("operation"),
            operator_type=OperatorType.ADMIN, operator_id=operator_id,
            occurred_at=self._clock.now(),
        )

    async def destroy(self, scope: Scope, decision: AdminDecision, *,
                      operator_id: str,
                      operation_id: str | None = None) -> GroupDestroyReceipt:
        self._require_group(scope)
        self._require_admin(decision)
        op = operation_id or self._ids.new_id("operation")
        try:
            self._store.get_scope(scope.id)
        except NotFound:
            return GroupDestroyReceipt(op, scope.id, "", "", 0, False)
        if self._store.is_scope_enabled(scope.id):
            raise InvalidTransition("disable group scope before destroy")
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        existing = sorted(self._backup_dir.glob(f"group-{scope.id}-{op}-*.sqlite3"))
        if existing:
            backup_path = existing[0]
            digest = _sha256_file(backup_path)
        else:
            stamp = self._clock.now().strftime("%Y%m%dT%H%M%S%f")
            backup_path = self._backup_dir / f"group-{scope.id}-{op}-{stamp}.sqlite3"
            digest = self._store.backup_group_scope(scope.id, str(backup_path))
        row_counts = self._store.group_row_counts(scope.id)

        def destroy_operation() -> tuple[int, bool]:
            removed = self._store.delete_group_scope(
                scope.id, operation_id=op, operator_type=OperatorType.ADMIN,
                operator_id=operator_id, occurred_at=self._clock.now(),
            )
            try:
                self._index.rebuild()
            except IndexUnavailable:
                return removed, False
            try:
                self._store.purge_deleted_payload()
            except StorageError:
                return removed, False
            return removed, True

        affected, purged = await self._coordinator.run_serialized(destroy_operation)
        manifest = backup_path.with_suffix(".json")
        temp = manifest.with_suffix(manifest.suffix + ".tmp")
        payload = json.dumps({
            "operation_id": op,
            "scope_id": scope.id,
            "backup_path": str(backup_path),
            "backup_sha256": digest,
            "affected_count": affected,
            "purged": purged,
            "row_counts": row_counts,
            "destroyed_at": self._clock.now().isoformat(),
        }, ensure_ascii=False, indent=2)
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(manifest)
        return GroupDestroyReceipt(
            op, scope.id, str(backup_path), digest, affected, purged,
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
