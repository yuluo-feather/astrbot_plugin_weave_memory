import types

import pytest
from data.plugins.astrbot_plugin_weave_memory.adapters.astrbot.identity import (
    AstrBotIdentityAdapter,
)
from data.plugins.astrbot_plugin_weave_memory.core.errors import AuthorizationDenied
from data.plugins.astrbot_plugin_weave_memory.core.model import ScopeType, SubjectType


class Event:
    def __init__(self, *, group_id="", platform_id="instance-1", sender_id="user-1"):
        self.message_obj = types.SimpleNamespace(group_id=group_id)
        self._platform_id = platform_id
        self._sender_id = sender_id

    def is_private_chat(self):
        return not bool(self.message_obj.group_id)

    def get_group_id(self):
        return self.message_obj.group_id

    def get_platform_name(self):
        return "telegram"

    def get_platform_id(self):
        return self._platform_id

    def get_sender_id(self):
        return self._sender_id


def test_identity_uses_platform_instance_and_sender_not_session_origin():
    adapter = AstrBotIdentityAdapter()
    identity = adapter.resolve_identity(Event())
    assert identity.platform == "telegram"
    assert identity.tenant_id == "instance-1"
    assert identity.scope_type is ScopeType.PERSONAL
    assert identity.scope_external_id == "user-1"
    assert identity.subject_type is SubjectType.USER
    assert identity.subject_id == "user-1"
    other = adapter.resolve_identity(Event(platform_id="instance-2"))
    assert other.tenant_id != identity.tenant_id


def test_group_event_is_rejected():
    with pytest.raises(AuthorizationDenied):
        AstrBotIdentityAdapter().resolve_identity(Event(group_id="group-1"))


def test_missing_group_field_fails_closed():
    event = Event()
    event.message_obj = types.SimpleNamespace()
    with pytest.raises(AuthorizationDenied):
        AstrBotIdentityAdapter().resolve_identity(event)


def test_missing_identity_method_fails_closed():
    event = types.SimpleNamespace(message_obj=types.SimpleNamespace(group_id=""))
    with pytest.raises(AuthorizationDenied):
        AstrBotIdentityAdapter().resolve_identity(event)
