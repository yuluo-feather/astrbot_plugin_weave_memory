"""AstrBot group command translation for V1.1 Group shared."""

from __future__ import annotations

import logging
from typing import Any

from ...app.export import to_json, to_markdown
from ...app.group_lifecycle_service import GroupLifecycleService
from ...app.group_memory_service import GroupMemoryService
from ...app.group_recall_service import GroupRecallService
from ...app.scope_service import GroupScopeService
from ...core.errors import (
    AuthorizationDenied,
    Conflict,
    GroupAdminUnavailable,
    GroupConsentRevocationPending,
    GroupNotAdmin,
    GroupNotCreated,
    IndexUnavailable,
    InvalidTransition,
    NotFound,
    ValidationError,
)
from ...core.model import AdminSource, Scope
from .identity import AstrBotIdentityAdapter

logger = logging.getLogger(__name__)


class AstrBotGroupCommands:
    def __init__(self, scopes: GroupScopeService, lifecycle: GroupLifecycleService,
                 memories: GroupMemoryService, recall: GroupRecallService, *,
                 retention_days: int = 90,
                 backup_retention_days: int = 30) -> None:
        self._scopes = scopes
        self._lifecycle = lifecycle
        self._memories = memories
        self._recall = recall
        self._retention_days = max(1, retention_days)
        self._backup_retention_days = max(1, backup_retention_days)
        self._identity = AstrBotIdentityAdapter()

    @staticmethod
    def _tail(event: Any, subcommand: str) -> str:
        raw = event.message_str.strip().lstrip("/").strip()
        if raw.startswith("织忆"):
            raw = raw[len("织忆"):].strip()
        if raw.startswith(subcommand):
            return raw[len(subcommand):].strip()
        return raw

    def _identity_and_decision(self, event: Any):
        identity = self._identity.resolve_group_identity(event)
        sender_id = self._identity.resolve_group_operator_id(event)
        decision = self._scopes.admin_decision(identity, sender_id, event)
        if decision.source is AdminSource.NONE:
            logger.warning(
                "织忆群管理员来源丢失：platform=%s tenant=%s group=%s",
                identity.platform, identity.tenant_id, identity.scope_external_id,
            )
        return identity, sender_id, decision

    def _find_scope(self, identity) -> Scope:
        scope = self._scopes.find(identity)
        if scope is None:
            raise GroupNotCreated("当前群尚未创建织忆群记忆，请先由管理员发送 /织忆 群开启。")
        return scope

    @staticmethod
    def _require_admin(decision) -> None:
        if decision.allowed:
            return
        if decision.source is AdminSource.NONE:
            raise GroupAdminUnavailable(
                "群管理员来源不可用：平台未提供角色，且 group_admin_ids 未命中该群。"
                '请在配置中设置 "platform|tenant_id|group_id": ["admin_id"]。'
            )
        raise GroupNotAdmin("需要本群管理员权限。")

    @staticmethod
    def _error_text(action: str, exc: Exception) -> str:
        if isinstance(exc, GroupConsentRevocationPending):
            return "群记忆已关闭，但 group_memory 撤销记录写入失败，请重试 /织忆 群关闭。"
        if isinstance(exc, GroupAdminUnavailable):
            return (
                f"{action}失败：{exc} "
                '请配置 group_admin_ids："platform|tenant_id|group_id": ["admin_id"]。'
            )
        if isinstance(exc, GroupNotAdmin):
            return f"{action}失败：你不是本群管理员。"
        if isinstance(exc, GroupNotCreated):
            return f"{action}失败：当前群尚未创建织忆群记忆，请先由管理员 /织忆 群开启。"
        if isinstance(exc, AuthorizationDenied):
            return f"{action}失败：需要群管理员，或群记忆未开启。"
        if isinstance(exc, NotFound):
            return f"{action}失败：当前群尚未开启织忆群记忆。"
        if isinstance(exc, InvalidTransition):
            return f"{action}失败：请先关闭群记忆再执行该操作。"
        if isinstance(exc, IndexUnavailable):
            return f"{action}失败：检索索引暂不可用，正文数据不会因此丢失。"
        if isinstance(exc, Conflict):
            return f"{action}失败：{exc}。"
        if isinstance(exc, ValidationError):
            return (
                f"{action}失败：第一个参数必须是 "
                "topic/agreement/atmosphere/experience（或中文：话题/约定/偏好/氛围/经历）。"
                "示例：/织忆 群保存 topic 本群周三例会 15:00"
            )
        return f"{action}失败：{type(exc).__name__}。"

    def help(self) -> str:
        return (
            "织忆群记忆命令：\n"
            "/织忆 群帮助 —— 查看群命令\n"
            "/织忆 群开启 —— 管理员开启当前群共享记忆\n"
            "/织忆 群关闭 —— 管理员关闭当前群记忆（可重新开启）\n"
            "/织忆 群清空 确认 —— 管理员清空当前群记忆\n"
            "/织忆 群销毁 确认 —— 管理员彻底移除当前群数据（需先关闭，默认备份）\n"
            "/织忆 群销毁 无备份 确认 —— 危险：不写备份直接销毁，需二次确认\n"
            "/织忆 群保存 <topic|agreement|atmosphere|experience> <内容> —— 管理员写入（中文别名：话题/约定/偏好/氛围/经历）\n"
            "/织忆 群列表 —— 查看当前群记忆（需已开启）\n"
            "/织忆 群查询 <问题> —— 检索当前群记忆（需已开启）\n"
            "/织忆 群状态 —— 查看当前群状态\n"
            "/织忆 群导出 [json|md] —— 群管理员导出当前群共享记忆"
        )

    async def enable(self, event: Any) -> str:
        identity, sender_id, decision = self._identity_and_decision(event)
        self._require_admin(decision)
        scope = self._scopes.ensure_scope(identity, decision)
        await self._lifecycle.enable(scope, decision, operator_id=sender_id)
        return (
            "已开启当前群共享记忆。管理员可使用 /织忆 群保存 写入群级内容。\n"
            "群公告：本群已开启织忆共享记忆；管理员可写入，成员可读；"
            "关闭用 /织忆 群关闭，彻底删除用 /织忆 群销毁 确认。"
        )

    async def disable(self, event: Any) -> str:
        identity, sender_id, decision = self._identity_and_decision(event)
        self._require_admin(decision)
        scope = self._find_scope(identity)
        await self._lifecycle.disable(scope, decision, operator_id=sender_id)
        return "已关闭当前群共享记忆；数据保留，可由管理员重新开启。"

    async def clear(self, event: Any) -> str:
        if self._tail(event, "群清空") != "确认":
            return "清空不可恢复。确认执行请发送：/织忆 群清空 确认"
        identity, sender_id, decision = self._identity_and_decision(event)
        self._require_admin(decision)
        scope = self._find_scope(identity)
        receipt = await self._lifecycle.clear(scope, decision, operator_id=sender_id)
        state = "索引已重建" if receipt.index_state.value == "HEALTHY" else "清空已提交，索引待恢复"
        purge = "" if receipt.purged is not False else "\n物理擦除未完成，将在下次插件启动时自动重试。"
        return f"已清空当前群 {receipt.affected_count} 条记忆。{state}{purge}"

    async def destroy(self, event: Any) -> str:
        payload = self._tail(event, "群销毁").strip()
        with_backup = True
        if payload == "无备份":
            return (
                "危险：无备份销毁不会生成任何快照，被销毁群与其他群都无法从该库恢复。"
                "确认执行请发送：/织忆 群销毁 无备份 确认"
            )
        if payload in {"无备份 确认", "确认 无备份"}:
            with_backup = False
        elif payload != "确认":
            return "群销毁不可恢复。确认执行请发送：/织忆 群销毁 确认"
        identity, sender_id, decision = self._identity_and_decision(event)
        self._require_admin(decision)
        scope = self._find_scope(identity)
        receipt = await self._lifecycle.destroy(
            scope, decision, operator_id=sender_id, with_backup=with_backup,
        )
        purge = "物理擦除完成" if receipt.purged else "物理擦除待下次启动重试"
        if receipt.backup_mode == "SKIPPED":
            backup_text = "未写备份（backup_mode=SKIPPED），请确认这是你要的破坏性操作"
        else:
            backup_text = (
                f"备份已写入插件数据目录（保留 {self._backup_retention_days} 天，"
                "不能用于恢复本群）"
            )
        return (
            f"已销毁当前群数据 {receipt.affected_count} 条；{backup_text}；{purge}。"
        )

    async def save(self, event: Any) -> str:
        payload = self._tail(event, "群保存")
        parts = payload.split(maxsplit=1)
        if len(parts) != 2:
            return (
                "用法：/织忆 群保存 <topic|agreement|atmosphere|experience> <内容>\n"
                "中文别名：话题/约定/偏好/氛围/经历；"
                "例如：/织忆 群保存 topic 本群周三例会 15:00"
            )
        group_label, content = parts
        identity, sender_id, decision = self._identity_and_decision(event)
        self._require_admin(decision)
        scope = self._find_scope(identity)
        memory, receipt = await self._memories.save_group(
            scope, content, group_label=group_label,
            operator_id=sender_id, decision=decision,
        )
        state = "索引已同步" if receipt.index_state.value == "HEALTHY" else "正文已保存，索引待恢复"
        warning = "\n群记忆已达软上限，请管理员清理。" if self._memories.needs_cleanup(scope) else ""
        return f"已保存群记忆：{memory.id}\n{state}{warning}"

    def list(self, event: Any) -> str:
        identity, _, _ = self._identity_and_decision(event)
        scope = self._find_scope(identity)
        memories = self._memories.list_group(scope)
        if not memories:
            return "当前群没有可用的共享记忆。"
        lines = [
            f"{item.id} [{item.memory_type.value}] [revision {item.current_revision}] {item.content}"
            for item in memories[:30]
        ]
        suffix = f"\n另有 {len(memories) - 30} 条未显示" if len(memories) > 30 else ""
        return "当前群共享记忆：\n" + "\n".join(lines) + suffix

    async def query(self, event: Any) -> str:
        query = self._tail(event, "群查询")
        if not query:
            return "用法：/织忆 群查询 <问题>"
        identity, _, _ = self._identity_and_decision(event)
        scope = self._find_scope(identity)
        memories = self._recall.recall_group(query, scope)
        if not memories:
            return "没有找到相关群记忆。"
        await self._recall.record_access(tuple(item.id for item in memories), scope)
        lines = [f"[{item.memory_type.value}] {item.content}（{item.id}）" for item in memories]
        return "检索到的群记忆：\n" + "\n".join(lines)

    def export(self, event: Any) -> str:
        format_name = self._tail(event, "群导出") or "json"
        normalized_format = format_name.strip().lower()
        if normalized_format not in {"json", "md", "markdown"}:
            return "导出失败：格式只支持 json 或 md。"
        identity, _, decision = self._identity_and_decision(event)
        self._require_admin(decision)
        scope = self._find_scope(identity)
        payload = self._memories.export_group(scope, decision)
        if normalized_format == "json":
            return to_json(payload)
        return to_markdown(payload)

    def status(self, event: Any) -> str:
        identity, _, _ = self._identity_and_decision(event)
        status = self._scopes.status(identity)
        if not status.created:
            return "当前群尚未开启织忆群记忆。"
        scope = self._scopes.find(identity)
        state = "已开启" if status.enabled else "已关闭"
        available = "是" if status.admin_available else "否"
        omitted = self._recall.last_omitted_count(scope.id) if scope is not None else 0
        return (
            f"当前群织忆状态：{state}；记忆 {status.count} 条；"
            f"管理员来源 {status.admin_source.value}；管理员可用 {available}。\n"
            f"上次注入截断 {omitted} 条；"
            f"群数据保留期建议 {self._retention_days} 天（不自动删除）；"
            f"销毁备份保留 {self._backup_retention_days} 天。"
        )
