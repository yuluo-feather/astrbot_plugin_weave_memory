import pytest
from data.plugins.astrbot_plugin_weave_memory.adapters.astrbot.backup_commands import (
    AstrBotBackupCommands,
)
from data.plugins.astrbot_plugin_weave_memory.core.errors import (
    AuthorizationDenied,
    NotFound,
)


class FakeService:
    def __init__(self, *, missing: bool = False) -> None:
        self._missing = missing

    def create_backup(self):
        return {}

    def list_backups(self):
        return []

    def list_restore_snapshots(self):
        return []

    def restore(self, *args, **kwargs):
        if self._missing:
            raise NotFound("backup not found")
        return {}

    def rollback_to_snapshot(self, *args, **kwargs):
        if self._missing:
            raise NotFound("restore snapshot not found")
        return {}


class FakeEvent:
    def __init__(self, sender: str, *, admin: bool = False,
                 message: str = "") -> None:
        self._sender = sender
        self._admin = admin
        self.message_str = message
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


def test_non_admin_gets_the_admin_reason_instead_of_a_consent_notice():
    commands = AstrBotBackupCommands(FakeService())
    text = commands.backup(FakeEvent("u1", message="/织忆 备份"))
    assert "仅限管理员" in text
    assert "尚未授权" not in text


def test_missing_restore_snapshot_has_its_own_message():
    commands = AstrBotBackupCommands(FakeService(missing=True))
    text = commands.restore(FakeEvent(
        "u1", admin=True, message="/织忆 备份恢复 快照 no-such 确认",
    ))
    assert "没有找到该恢复前快照" in text


def test_missing_backup_has_its_own_message():
    commands = AstrBotBackupCommands(FakeService(missing=True))
    text = commands.restore(FakeEvent(
        "u1", admin=True, message="/织忆 备份恢复 no-such 确认",
    ))
    assert "没有找到对应的备份" in text
