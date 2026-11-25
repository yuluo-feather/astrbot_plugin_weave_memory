from datetime import UTC, datetime, timedelta

import pytest
from data.plugins.astrbot_plugin_weave_memory.app.webui_security import WebUiSecurity
from data.plugins.astrbot_plugin_weave_memory.core.errors import AuthorizationDenied


class FakeClock:
    def __init__(self) -> None:
        self.now_value = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.now_value

    def advance(self, **kwargs) -> None:
        self.now_value += timedelta(**kwargs)


def test_csrf_token_is_bound_to_username():
    clock = FakeClock()
    security = WebUiSecurity(clock)
    token = security.issue_token("admin")["csrf_token"]

    security.require_token("admin", token)
    with pytest.raises(AuthorizationDenied):
        security.require_token("other", token)
    with pytest.raises(AuthorizationDenied):
        security.require_token("admin", "not-a-token")
    with pytest.raises(AuthorizationDenied):
        security.require_token("admin", None)


def test_csrf_token_expires():
    clock = FakeClock()
    security = WebUiSecurity(clock)
    token = security.issue_token("admin")["csrf_token"]

    clock.advance(minutes=6)
    with pytest.raises(AuthorizationDenied):
        security.require_token("admin", token)


def test_nonce_is_one_time_and_target_bound():
    clock = FakeClock()
    security = WebUiSecurity(clock)
    nonce = security.issue_nonce("admin", action="purge", target="m-1")["nonce"]

    security.consume_nonce("admin", action="purge", target="m-1", nonce=nonce)
    with pytest.raises(AuthorizationDenied):
        security.consume_nonce("admin", action="purge", target="m-1", nonce=nonce)


def test_nonce_rejects_wrong_action_or_target():
    clock = FakeClock()
    security = WebUiSecurity(clock)
    nonce = security.issue_nonce("admin", action="restore", target="m-1")["nonce"]

    with pytest.raises(AuthorizationDenied):
        security.consume_nonce("admin", action="purge", target="m-1", nonce=nonce)
    with pytest.raises(AuthorizationDenied):
        security.consume_nonce("admin", action="restore", target="m-2", nonce=nonce)
    # A mismatched attempt must not burn the nonce for its real target.
    security.consume_nonce("admin", action="restore", target="m-1", nonce=nonce)


def test_nonce_expires_after_two_minutes():
    clock = FakeClock()
    security = WebUiSecurity(clock)
    nonce = security.issue_nonce("admin", action="settings", target="a,b")["nonce"]

    clock.advance(minutes=3)
    with pytest.raises(AuthorizationDenied):
        security.consume_nonce("admin", action="settings", target="a,b", nonce=nonce)
