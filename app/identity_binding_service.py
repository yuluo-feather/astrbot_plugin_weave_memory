"""V2.4 Dashboard username -> platform member binding via one-time codes.

The host only exposes the Dashboard ``username`` to plugin Web APIs; there is no
supported way to map it to a platform sender ID. A bound user proves ownership
by sending ``/织忆 绑定`` in private chat, then entering the short-lived code in
the console. Unbound or ambiguous identities fail closed (ADR-0032).
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from ..core.errors import AuthorizationDenied, ValidationError
from ..core.model import Identity, ScopeType, SubjectType
from ..core.ports import Clock, IdentityBindingStore

_CODE_TTL = timedelta(minutes=10)


class IdentityBindingService:
    def __init__(self, store: IdentityBindingStore, clock: Clock, *,
                 code_ttl: timedelta = _CODE_TTL) -> None:
        self._store = store
        self._clock = clock
        self._code_ttl = code_ttl

    def issue_code(self, identity: Identity) -> dict[str, object]:
        if identity.scope_type is not ScopeType.PERSONAL:
            raise AuthorizationDenied("binding codes are issued in private chat only")
        self._store.purge_expired()
        expires_at = self._clock.now() + self._code_ttl
        for _ in range(5):
            code = f"{secrets.randbelow(1_000_000):06d}"
            try:
                self._store.create_code(
                    code, platform=identity.platform, tenant_id=identity.tenant_id,
                    subject_id=identity.subject_id, expires_at=expires_at,
                )
            except Exception:  # noqa: BLE001 - retry on a code collision
                continue
            return {
                "code": code,
                "expires_at": expires_at.isoformat(),
                "ttl_seconds": int(self._code_ttl.total_seconds()),
            }
        raise ValidationError("could not allocate a binding code; try again")

    def bind(self, username: str, code: str) -> dict[str, object]:
        if not username.strip():
            raise AuthorizationDenied("dashboard login required")
        payload = self._store.consume_code(code.strip())
        if payload is None:
            raise AuthorizationDenied("binding code is invalid, expired or already used")
        binding = self._store.bind(
            username, platform=str(payload["platform"]),
            tenant_id=str(payload["tenant_id"]), subject_id=str(payload["subject_id"]),
        )
        return self._public(binding)

    def resolve(self, username: str) -> Identity | None:
        binding = self._store.find_by_username(username)
        if binding is None:
            return None
        self._store.touch(username)
        return Identity(
            platform=str(binding["platform"]), tenant_id=str(binding["tenant_id"]),
            scope_type=ScopeType.PERSONAL,
            scope_external_id=str(binding["subject_id"]),
            subject_type=SubjectType.USER, subject_id=str(binding["subject_id"]),
        )

    def require(self, username: str) -> Identity:
        identity = self.resolve(username)
        if identity is None:
            raise AuthorizationDenied(
                "dashboard account is not bound to a platform user; "
                "send /织忆 绑定 in private chat first",
            )
        return identity

    def status(self, username: str) -> dict[str, object]:
        binding = self._store.find_by_username(username)
        if binding is None:
            return {"bound": False}
        return {"bound": True, **self._public(binding)}

    def unbind(self, username: str) -> None:
        self._store.unbind(username)

    @staticmethod
    def _public(binding: dict[str, object]) -> dict[str, object]:
        return {
            "platform": binding["platform"],
            "tenant_id": binding["tenant_id"],
            "subject_id": binding["subject_id"],
            "bound_at": binding["bound_at"],
        }
