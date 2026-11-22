"""AstrBot group administrator resolution with config fallback."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ...core.errors import ValidationError
from ...core.model import AdminDecision, AdminSource, Identity, ScopeType


class AstrBotGroupAdminResolver:
    def __init__(self, explicit_admin_ids: Mapping[
        tuple[str, str, str], Sequence[str]
    ] | None = None) -> None:
        self._explicit = {
            tuple(key): tuple(str(item) for item in value)
            for key, value in (explicit_admin_ids or {}).items()
        }

    def resolve(self, identity: Identity, sender_id: str,
                event: object | None = None) -> AdminDecision:
        if identity.scope_type is not ScopeType.GROUP:
            raise ValidationError("group admin resolver requires a GROUP identity")
        message_obj = getattr(event, "message_obj", None) if event is not None else None
        group = getattr(message_obj, "group", None) if message_obj is not None else None
        admin_ids: set[str] = set()
        owner = getattr(group, "group_owner", None) if group is not None else None
        if owner:
            admin_ids.add(str(owner))
        admins = getattr(group, "group_admins", None) if group is not None else None
        if isinstance(admins, Sequence) and not isinstance(admins, (str, bytes)):
            admin_ids.update(str(item) for item in admins if str(item).strip())
        if admin_ids:
            ordered = tuple(sorted(admin_ids))
            return AdminDecision(
                AdminSource.PLATFORM_ROLE, ordered, sender_id in admin_ids, True,
            )
        is_admin = getattr(event, "is_admin", None) if event is not None else None
        if callable(is_admin) and is_admin():
            return AdminDecision(
                AdminSource.PLATFORM_ROLE, (sender_id,), True, True,
            )
        key = (identity.platform, identity.tenant_id, identity.scope_external_id)
        configured = self._explicit.get(key, ())
        if configured:
            return AdminDecision(
                AdminSource.EXPLICIT_CONFIG, configured, sender_id in configured, True,
            )
        return AdminDecision(AdminSource.NONE, (), False, False)
