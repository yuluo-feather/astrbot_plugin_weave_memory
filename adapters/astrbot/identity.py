"""Duck-typed AstrBot identity adapter; no AstrBot imports leak into core."""

from typing import Any

from ...core.errors import AuthorizationDenied
from ...core.model import Identity, ScopeType, SubjectType


class AstrBotIdentityAdapter:
    def _private_identity_fields(self, event: Any) -> tuple[str, str, str]:
        message_obj = getattr(event, "message_obj", None)
        if message_obj is None:
            raise AuthorizationDenied("message object is unavailable")
        # Both the semantic message type and group ID must indicate a private
        # message. Missing APIs or ambiguous event types fail closed.
        is_private = getattr(event, "is_private_chat", None)
        get_group_id = getattr(event, "get_group_id", None)
        if not callable(is_private) or not callable(get_group_id):
            raise AuthorizationDenied("V0 memory is private-message only")
        try:
            if not is_private():
                raise AuthorizationDenied("V0 memory is private-message only")
            group_id = get_group_id()
            if group_id is None or str(group_id).strip():
                raise AuthorizationDenied("V0 memory is private-message only")
            platform = event.get_platform_name()
            tenant_id = event.get_platform_id()
            subject_id = event.get_sender_id()
        except AttributeError as exc:
            raise AuthorizationDenied("AstrBot identity API is unavailable") from exc
        if platform is None or tenant_id is None or subject_id is None:
            raise AuthorizationDenied("AstrBot returned an incomplete identity")
        platform_value, tenant_value, subject_value = map(str, (platform, tenant_id, subject_id))
        if not platform_value.strip() or not tenant_value.strip() or not subject_value.strip():
            raise AuthorizationDenied("AstrBot returned an empty identity field")
        return platform_value, tenant_value, subject_value

    def resolve_identity(self, event: Any) -> Identity:
        platform, tenant_id, subject_id = self._private_identity_fields(event)
        return Identity(
            platform=platform, tenant_id=tenant_id,
            scope_type=ScopeType.PERSONAL,
            scope_external_id=subject_id,
            subject_type=SubjectType.USER, subject_id=subject_id,
        )

    def _group_identity_fields(self, event: Any) -> tuple[str, str, str, str]:
        message_obj = getattr(event, "message_obj", None)
        if message_obj is None:
            raise AuthorizationDenied("message object is unavailable")
        is_private = getattr(event, "is_private_chat", None)
        get_group_id = getattr(event, "get_group_id", None)
        if not callable(is_private) or not callable(get_group_id):
            raise AuthorizationDenied("group identity API is unavailable")
        try:
            if is_private():
                raise AuthorizationDenied("group memory requires a group chat")
            group_id = get_group_id()
            platform = event.get_platform_name()
            tenant_id = event.get_platform_id()
            subject_id = event.get_sender_id()
        except AttributeError as exc:
            raise AuthorizationDenied("AstrBot identity API is unavailable") from exc
        if group_id is None or not str(group_id).strip():
            raise AuthorizationDenied("group id is unavailable")
        if platform is None or tenant_id is None or subject_id is None:
            raise AuthorizationDenied("AstrBot returned an incomplete identity")
        platform_value, tenant_value, subject_value = map(str, (platform, tenant_id, subject_id))
        group_value = str(group_id)
        if not platform_value.strip() or not tenant_value.strip() or not subject_value.strip():
            raise AuthorizationDenied("AstrBot returned an empty identity field")
        if len(group_value) > 256:
            raise AuthorizationDenied("group id exceeds 256 characters")
        return platform_value, tenant_value, subject_value, group_value

    def resolve_group_identity(self, event: Any) -> Identity:
        platform, tenant_id, _, group_id = self._group_identity_fields(event)
        return Identity(
            platform=platform, tenant_id=tenant_id,
            scope_type=ScopeType.GROUP,
            scope_external_id=group_id,
            subject_type=SubjectType.GROUP, subject_id=group_id,
        )

    def resolve_operator_id(self, event: Any) -> str:
        """Resolve the acting user separately from the memory subject."""
        return self._private_identity_fields(event)[2]

    def resolve_group_operator_id(self, event: Any) -> str:
        return self._group_identity_fields(event)[2]
