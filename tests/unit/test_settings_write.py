import pytest
from data.plugins.astrbot_plugin_weave_memory.app.settings_service import (
    SettingsWriteService,
)
from data.plugins.astrbot_plugin_weave_memory.core.errors import (
    AuthorizationDenied,
    ValidationError,
)

SCHEMA = {
    "group_max_tokens": {"type": "int", "default": 192},
    "personal_max_memories": {"type": "int", "default": 4},
    "api_admin_usernames": {"type": "list", "default": []},
    "webui_enabled": {"type": "bool", "default": True},
    "embedding_model": {"type": "string", "default": ""},
    "provider_api_key": {"type": "string", "default": ""},
}


def make_service():
    saved: list[dict] = []
    service = SettingsWriteService(SCHEMA, writer=lambda values: saved.append(values))
    return service, saved


def test_unknown_key_is_rejected():
    service, saved = make_service()
    with pytest.raises(ValidationError):
        service.update("admin", {"nope": 1}, current={}, require_nonce=lambda _k: None)
    assert saved == []


def test_secret_key_is_rejected():
    service, saved = make_service()
    with pytest.raises(AuthorizationDenied):
        service.update(
            "admin", {"provider_api_key": "x"}, current={},
            require_nonce=lambda _k: None,
        )
    assert saved == []


def test_type_validation():
    service, _saved = make_service()
    for bad in ({"group_max_tokens": "12"}, {"webui_enabled": "yes"},
                {"api_admin_usernames": "admin"}):
        with pytest.raises(ValidationError):
            service.update("admin", bad, current={}, require_nonce=lambda _k: None)


def test_important_key_requires_nonce_and_reports_reload():
    service, saved = make_service()
    requested: list[str] = []

    result = service.update(
        "admin", {"group_max_tokens": 256, "api_admin_usernames": ["root"]},
        current={"webui_enabled": True},
        require_nonce=requested.append,
    )
    assert requested == ["api_admin_usernames"]
    assert result["saved"] == ["api_admin_usernames", "group_max_tokens"]
    assert result["reload_required"] == ["api_admin_usernames", "group_max_tokens"]
    assert saved[-1]["group_max_tokens"] == 256
    assert saved[-1]["webui_enabled"] is True


def test_hot_apply_key_does_not_require_reload():
    service, _saved = make_service()
    result = service.update(
        "admin", {"webui_enabled": False}, current={},
        require_nonce=lambda _k: None,
    )
    assert result["reload_required"] == []
    assert result["important"] == ["webui_enabled"]


def test_empty_update_is_rejected():
    service, _saved = make_service()
    with pytest.raises(ValidationError):
        service.update("admin", {}, current={}, require_nonce=lambda _k: None)
