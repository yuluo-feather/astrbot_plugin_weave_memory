"""Instance-level backup/restore command translation (V1.8)."""

from __future__ import annotations

import json
from typing import Any

from ...core.errors import AuthorizationDenied, ValidationError
from ...core.ports import RESTORE_MODES, BackupPort
from .command_text import tail_after_command
from .identity import AstrBotIdentityAdapter


class AstrBotBackupCommands:
    def __init__(self, service: BackupPort, *,
                 admin_ids: tuple[str, ...] = ()) -> None:
        self._service = service
        self._admin_ids = {
            str(item).strip() for item in admin_ids if str(item).strip()
        }
        self._identity = AstrBotIdentityAdapter()

    def _authorize(self, event: Any) -> None:
        is_admin = getattr(event, "is_admin", None)
        if callable(is_admin):
            try:
                if is_admin():
                    return
            except Exception:  # noqa: BLE001 - host API variance
                pass
        operator = ""
        try:
            operator = self._identity.resolve_operator_id(event)
        except Exception:  # noqa: BLE001 - identity may be incomplete
            operator = ""
        if operator and operator in self._admin_ids:
            return
        raise AuthorizationDenied(
            "备份/恢复仅限管理员：请由宿主管理员执行，或把用户 ID 加入 command_admin_ids",
        )

    def help(self) -> str:
        return (
            "织忆备份与恢复（实例级，仅私聊）：\n"
            "/织忆 备份 —— 立即创建主库快照（自动先重放未完成 ledger 事件）\n"
            "/织忆 备份 列表 —— 列出可用备份\n"
            "/织忆 备份恢复 <备份ID> —— dry-run，只输出将复活/不会复活清单\n"
            "/织忆 备份恢复 <备份ID> 确认 —— 以 merge 模式恢复\n"
            "/织忆 备份恢复 <备份ID> <merge|revert|disaster-rebuild> 确认 —— 显式模式恢复\n"
            "/织忆 备份恢复 快照 —— 列出恢复前快照\n"
            "/织忆 备份恢复 快照 <快照名> 确认 —— 回滚到该恢复前快照\n"
            "备份不含 memory_ledger.sqlite3；旧备份不会复活已删除/清空/撤销授权的内容。"
        )

    def backup(self, event: Any) -> str:
        self._authorize(event)
        tail = tail_after_command(event, "备份")
        if tail in {"列表", "list"}:
            return json.dumps(
                {"backups": self._service.list_backups()},
                ensure_ascii=False, indent=2,
            )
        if tail:
            return "用法：/织忆 备份 [列表]"
        manifest = self._service.create_backup()
        return json.dumps(manifest, ensure_ascii=False, indent=2)

    def restore(self, event: Any) -> str:
        self._authorize(event)
        tail = tail_after_command(event, "备份恢复")
        if not tail:
            return "用法：/织忆 备份恢复 <备份ID> [merge|revert|disaster-rebuild] 确认"
        parts = tail.split()
        if parts[0] in {"快照", "snapshot"}:
            if len(parts) == 1:
                return json.dumps(
                    {"snapshots": self._service.list_restore_snapshots()},
                    ensure_ascii=False, indent=2,
                )
            if len(parts) == 2:
                return "用法：/织忆 备份恢复 快照 <快照名> 确认"
            confirm = parts[2] in {"确认", "confirm"}
            if len(parts) != 3 or not confirm:
                raise ValidationError("快照回滚必须显式附带「确认」")
            result = self._service.rollback_to_snapshot(
                parts[1], confirm=True,
            )
            return json.dumps(result, ensure_ascii=False, indent=2)
        backup_id = parts[0]
        mode = "merge"
        confirm = False
        for token in parts[1:]:
            lowered = token.lower()
            if lowered == "确认":
                confirm = True
            elif lowered in RESTORE_MODES:
                mode = lowered
            else:
                raise ValidationError("恢复模式只能是 merge、revert 或 disaster-rebuild")
        result = self._service.restore(
            backup_id, mode=mode, confirm=confirm, dry_run=not confirm,
        )
        return json.dumps(result, ensure_ascii=False, indent=2)
