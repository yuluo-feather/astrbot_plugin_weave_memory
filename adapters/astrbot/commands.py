"""AstrBot command translation; no AstrBot SDK imports or domain rules."""

from typing import Any

from ...app.consent_service import ConsentService, PersonalConsentType
from ...app.lifecycle_service import LifecycleService
from ...app.memory_service import MemoryApplicationService
from ...app.recall_service import RecallService
from ...app.scope_service import PersonalScopeService
from ...core.errors import NotFound, ValidationError
from ...core.model import MemoryType, OperatorType
from ...core.ports import Clock, IdGenerator
from .identity import AstrBotIdentityAdapter


class AstrBotMemoryCommands:
    def __init__(self, scopes: PersonalScopeService, consent: ConsentService,
                 memories: MemoryApplicationService, recall: RecallService,
                 lifecycle: LifecycleService, clock: Clock,
                 ids: IdGenerator) -> None:
        self._scopes = scopes
        self._consent = consent
        self._memories = memories
        self._recall = recall
        self._lifecycle = lifecycle
        self._clock = clock
        self._ids = ids
        self._identity = AstrBotIdentityAdapter()

    def _scope(self, event: Any, *, create: bool = False):
        identity = self._identity.resolve_identity(event)
        scope = self._scopes.resolve(identity) if create else self._scopes.find(identity)
        if scope is None:
            raise NotFound("尚未建立个人记忆空间，请先使用 /记忆 授权")
        return scope

    @staticmethod
    def _tail(event: Any, subcommand: str) -> str:
        raw = event.message_str.strip().lstrip("/").strip()
        if raw.startswith("记忆"):
            raw = raw[len("记忆"):].strip()
        if raw.startswith(subcommand):
            return raw[len(subcommand):].strip()
        return raw

    def help(self) -> str:
        return (
            "织忆命令：\n"
            "/记忆 授权 —— 开启个人记忆存储与自动注入\n"
            "/记忆 撤销授权 —— 停止存储、检索和注入（不删除已有数据）\n"
            "/记忆 保存 <内容> —— 显式保存一条事实\n"
            "/记忆 更新 <记忆ID> <当前revision> <新内容> —— 显式修订指定记忆\n"
            "/记忆 列表 —— 查看已保存记忆\n"
            "/记忆 查询 <问题> —— 主动检索记忆\n"
            "/记忆 删除 <记忆ID> —— 不可恢复地删除一条记忆\n"
            "/记忆 清空 确认 —— 不可恢复地清空个人记忆\n"
            "/记忆 关闭 确认 —— 永久关闭个人记忆空间（当前版本不支持重新开启，数据保留）"
        )

    def grant(self, event: Any) -> str:
        scope = self._scope(event, create=True)
        operator_id = self._identity.resolve_operator_id(event)
        self._consent.grant(scope, PersonalConsentType.PERSONAL_MEMORY,
                            operator_id=operator_id)
        return "已授权个人记忆。你可以使用 /记忆 保存 <内容> 保存记忆；自动注入也已开启。"

    def revoke(self, event: Any) -> str:
        scope = self._scope(event, create=True)
        operator_id = self._identity.resolve_operator_id(event)
        self._consent.revoke(scope, PersonalConsentType.PERSONAL_MEMORY,
                             operator_id=operator_id)
        return "已撤销个人记忆授权。后续保存、检索和自动注入均停止；已有数据不会自动删除。"

    async def save(self, event: Any) -> str:
        content = self._tail(event, "保存")
        if not content:
            return "用法：/记忆 保存 <你希望记住的内容>"
        scope = self._scope(event)
        operator_id = self._identity.resolve_operator_id(event)
        memory, receipt = await self._memories.save_explicit(
            scope, content, operator_id=operator_id,
            memory_type=MemoryType.FACT,
        )
        state = "索引已同步" if receipt.index_state.value == "HEALTHY" else "正文已保存，索引待恢复"
        return f"已保存记忆：{memory.id}\n{state}"

    async def update(self, event: Any) -> str:
        payload = self._tail(event, "更新")
        parts = payload.split(maxsplit=2)
        if len(parts) != 3:
            return "用法：/记忆 更新 <记忆ID> <当前revision> <新内容>"
        memory_id, revision_text, content = parts
        try:
            expected_revision = int(revision_text)
        except ValueError as exc:
            raise ValidationError("revision 必须是正整数") from exc
        if expected_revision < 1:
            raise ValidationError("revision 必须是正整数")
        scope = self._scope(event)
        operator_id = self._identity.resolve_operator_id(event)
        memory, receipt = await self._memories.update_explicit(
            scope, memory_id, content, expected_revision=expected_revision,
            operator_id=operator_id,
        )
        state = "索引已同步" if receipt.index_state.value == "HEALTHY" else "正文已更新，索引待恢复"
        return f"记忆已更新：{memory.id}（revision {memory.current_revision}）\n{state}"

    def list(self, event: Any) -> str:
        scope = self._scope(event)
        memories = self._memories.list_active(scope)
        if not memories:
            return "当前没有可用的个人记忆。"
        lines = [f"{item.id} [{item.memory_type.value}] [revision {item.current_revision}] {item.content}" for item in memories[:30]]
        suffix = f"\n另有 {len(memories) - 30} 条未显示" if len(memories) > 30 else ""
        return "个人记忆：\n" + "\n".join(lines) + suffix

    async def query(self, event: Any) -> str:
        query = self._tail(event, "查询")
        if not query:
            return "用法：/记忆 查询 <问题>"
        scope = self._scope(event)
        memories = self._recall.recall(query, scope)
        if not memories:
            return "没有找到相关记忆。"
        await self._recall.record_access(tuple(item.id for item in memories), scope)
        lines = [f"[{item.memory_type.value}] {item.content}（{item.id}）" for item in memories]
        return "检索到的个人记忆：\n" + "\n".join(lines)

    async def delete(self, event: Any) -> str:
        memory_id = self._tail(event, "删除")
        if not memory_id:
            return "用法：/记忆 删除 <记忆ID>"
        scope = self._scope(event)
        operator_id = self._identity.resolve_operator_id(event)
        receipt = await self._lifecycle.delete_memory(
            memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
            operation_id=self._ids.new_id("operation"),
            operator_type=OperatorType.USER, operator_id=operator_id,
            at=self._clock.now(),
        )
        state = "索引已同步" if receipt.index_state.value == "HEALTHY" else "删除已提交，索引待恢复"
        return f"记忆已删除。{state}"

    async def clear(self, event: Any) -> str:
        if self._tail(event, "清空") != "确认":
            return "清空不可恢复。确认执行请发送：/记忆 清空 确认"
        scope = self._scope(event)
        operator_id = self._identity.resolve_operator_id(event)
        receipt = await self._lifecycle.clear_scope(
            scope.id, operation_id=self._ids.new_id("operation"),
            operator_type=OperatorType.USER, operator_id=operator_id,
            at=self._clock.now(),
        )
        state = "索引已重建" if receipt.index_state.value == "HEALTHY" else "清空已提交，索引待恢复"
        return f"已清空 {receipt.affected_count} 条记忆。{state}"

    async def disable(self, event: Any) -> str:
        if self._tail(event, "关闭") != "确认":
            return "关闭会永久停止读写和注入（V0 不支持重新开启），但保留数据。确认执行请发送：/记忆 关闭 确认"
        scope = self._scope(event)
        operator_id = self._identity.resolve_operator_id(event)
        await self._lifecycle.disable_scope(
            scope.id, operation_id=self._ids.new_id("operation"),
            operator_type=OperatorType.USER, operator_id=operator_id,
            at=self._clock.now(),
        )
        return "个人记忆空间已永久关闭（V0 不支持重新开启）。数据仍保留，但普通读写和自动注入已停止。"
