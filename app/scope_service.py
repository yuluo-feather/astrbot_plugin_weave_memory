"""Stable Scope resolution for Personal and Group identities."""

import json
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from ..core.errors import AuthorizationDenied, NotFound, ValidationError
from ..core.model import AdminDecision, AdminSource, Identity, Scope, ScopeType
from ..core.ports import AdminResolver, Clock, GroupScopeStore, ScopeStore
from .write_coordinator import WriteCoordinator


def stable_scope_id(identity: Identity) -> str:
    key = json.dumps(
        [identity.platform, identity.tenant_id, identity.scope_type.value,
         identity.scope_external_id, identity.subject_type.value, identity.subject_id],
        ensure_ascii=False, separators=(",", ":"),
    )
    return uuid5(NAMESPACE_URL, "weave-memory:scope:" + key).hex


class PersonalScopeService:
    def __init__(self, store: ScopeStore, coordinator: WriteCoordinator,
                 clock: Clock) -> None:
        self._store = store
        self._coordinator = coordinator
        self._clock = clock

    @staticmethod
    def stable_scope_id(identity: Identity) -> str:
        return stable_scope_id(identity)

    def find(self, identity: Identity) -> Scope | None:
        if identity.scope_type is not ScopeType.PERSONAL:
            raise ValidationError("PersonalScopeService accepts PERSONAL identity only")
        try:
            return self._store.get_scope(stable_scope_id(identity))
        except NotFound:
            return None

    def resolve(self, identity: Identity) -> Scope:
        existing = self.find(identity)
        if existing is not None:
            return existing
        scope = Scope(stable_scope_id(identity), identity, self._clock.now())
        self._coordinator.write_metadata(lambda: self._store.create_scope(scope))
        return scope

    def is_enabled(self, scope: Scope) -> bool:
        return self._store.is_scope_enabled(scope.id)


@dataclass(frozen=True, slots=True)
class GroupStatus:
    created: bool
    enabled: bool
    count: int
    admin_source: AdminSource
    admin_available: bool


class GroupScopeService:
    def __init__(self, store: GroupScopeStore, admins: AdminResolver,
                 coordinator: WriteCoordinator, clock: Clock) -> None:
        self._store = store
        self._admins = admins
        self._coordinator = coordinator
        self._clock = clock

    @staticmethod
    def stable_scope_id(identity: Identity) -> str:
        return stable_scope_id(identity)

    def find(self, identity: Identity) -> Scope | None:
        if identity.scope_type is not ScopeType.GROUP:
            raise ValidationError("GroupScopeService accepts GROUP identity only")
        try:
            return self._store.get_scope(stable_scope_id(identity))
        except NotFound:
            return None

    def admin_decision(self, identity: Identity, sender_id: str,
                       event: object | None = None) -> AdminDecision:
        return self._admins.resolve(identity, sender_id, event)

    def ensure_scope(self, identity: Identity, decision: AdminDecision) -> Scope:
        existing = self.find(identity)
        if existing is not None:
            return existing
        if not decision.allowed:
            raise AuthorizationDenied("group memory requires a resolvable administrator")
        scope = Scope(stable_scope_id(identity), identity, self._clock.now())
        self._coordinator.write_metadata(lambda: self._store.create_scope(scope))
        self._coordinator.write_metadata(
            lambda: self._store.set_admin_metadata(
                scope.id, decision.admin_ids, decision.source,
            ),
        )
        return scope

    def is_enabled(self, scope: Scope) -> bool:
        return self._store.is_scope_enabled(scope.id)

    def admin_metadata(self, scope: Scope) -> tuple[tuple[str, ...], AdminSource]:
        return self._store.get_admin_metadata(scope.id)

    def status(self, identity: Identity) -> GroupStatus:
        scope = self.find(identity)
        if scope is None:
            return GroupStatus(False, False, 0, AdminSource.NONE, False)
        _, source = self._store.get_admin_metadata(scope.id)
        return GroupStatus(
            created=True,
            enabled=self._store.is_scope_enabled(scope.id),
            count=self._store.count_active(scope.id),
            admin_source=source,
            admin_available=source is not AdminSource.NONE,
        )

    def require_enabled(self, scope: Scope) -> None:
        if not self._store.is_scope_enabled(scope.id):
            raise AuthorizationDenied("group memory is disabled")
