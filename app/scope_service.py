"""Stable Personal Scope resolution from a platform identity tuple."""

import json
from uuid import NAMESPACE_URL, uuid5

from ..core.errors import NotFound, ValidationError
from ..core.model import Identity, Scope, ScopeType
from ..core.ports import Clock, ScopeStore
from .write_coordinator import WriteCoordinator


class PersonalScopeService:
    def __init__(self, store: ScopeStore, coordinator: WriteCoordinator,
                 clock: Clock) -> None:
        self._store = store
        self._coordinator = coordinator
        self._clock = clock

    @staticmethod
    def stable_scope_id(identity: Identity) -> str:
        key = json.dumps(
            [identity.platform, identity.tenant_id, identity.scope_type.value,
             identity.scope_external_id, identity.subject_type.value, identity.subject_id],
            ensure_ascii=False, separators=(",", ":"),
        )
        return uuid5(NAMESPACE_URL, "weave-memory:scope:" + key).hex

    def find(self, identity: Identity) -> Scope | None:
        if identity.scope_type is not ScopeType.PERSONAL:
            raise ValidationError("V0 PersonalScopeService accepts PERSONAL identity only")
        try:
            return self._store.get_scope(self.stable_scope_id(identity))
        except NotFound:
            return None

    def resolve(self, identity: Identity) -> Scope:
        existing = self.find(identity)
        if existing is not None:
            return existing
        scope = Scope(self.stable_scope_id(identity), identity, self._clock.now())
        self._coordinator.write_metadata(lambda: self._store.create_scope(scope))
        return scope

    def is_enabled(self, scope: Scope) -> bool:
        return self._store.is_scope_enabled(scope.id)
