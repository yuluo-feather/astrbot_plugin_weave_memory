import pytest
from data.plugins.astrbot_plugin_weave_memory.adapters.astrbot.backup_commands import (
    AstrBotBackupCommands,
)
from data.plugins.astrbot_plugin_weave_memory.core.errors import AuthorizationDenied


class FakeService:
    def create_backup(self):
        return {}

    def list_backups(self):
        return []

    def restore(self, *args, **kwargs):
        return {}


class FakeEvent:
    def __init__(self, sender: str, *, admin: bool = False) -> None:
        self._sender = sender
        self._admin = admin
        self.message_obj = object()

    def is_admin(self) -> bool:
        return self._admin

    def is_private_chat(self) -> bool:
        return True

    def get_group_id(self) -> str:
        return ""

    def get_platform_name(self) -> str:
        return "test"

    def get_platform_id(self) -> str:
        return "instance"

    def get_sender_id(self) -> str:
        return self._sender


def test_non_admin_private_user_is_rejected():
    commands = AstrBotBackupCommands(FakeService())
    with pytest.raises(AuthorizationDenied):
        commands._authorize(FakeEvent("u1"))


def test_host_admin_is_allowed():
    commands = AstrBotBackupCommands(FakeService())
    commands._authorize(FakeEvent("u1", admin=True))


def test_configured_admin_id_is_allowed():
    commands = AstrBotBackupCommands(FakeService(), admin_ids=("u2",))
    commands._authorize(FakeEvent("u2"))
