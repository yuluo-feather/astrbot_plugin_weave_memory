"""AstrBot command translation; no AstrBot SDK imports or domain rules."""

import json
from typing import Any

from ...app.consent_service import ConsentService, PersonalConsentType
from ...app.export import to_json, to_markdown
from ...app.lifecycle_service import LifecycleService
from ...app.memory_service import MemoryApplicationService
from ...app.recall_service import RecallService, estimate_tokens
from ...app.scope_service import PersonalScopeService
from ...core.errors import AuthorizationDenied, NotFound, ValidationError
from ...core.model import MemoryType, OperatorType
from ...core.ports import Clock, IdGenerator
from .identity import AstrBotIdentityAdapter
from .notices import AUTO_ENROLL_NOTICE

_AUTO_ENROLL_FLAG = "_weave_auto_enroll_notice"


class AstrBotMemoryCommands:
    def __init__(self, scopes: PersonalScopeService, consent: ConsentService,
                 memories: MemoryApplicationService, recall: RecallService,
                 lifecycle: LifecycleService, clock: Clock,
                 ids: IdGenerator, *, auto_enroll: bool = True) -> None:
        self._scopes = scopes
        self._consent = consent
        self._memories = memories
        self._recall = recall
        self._lifecycle = lifecycle
        self._clock = clock
        self._ids = ids
        self._auto_enroll = auto_enroll
        self._identity = AstrBotIdentityAdapter()

    def _scope(self, event: Any, *, create: bool = False, enroll: bool = True):
        identity = self._identity.resolve_identity(event)
        scope = self._scopes.resolve(identity) if create else self._scopes.find(identity)
        if scope is None:
            if not (create or (enroll and self._auto_enroll)):
                raise NotFound("尚未建立个人记忆空间，请先使用 /织忆 授权")
            scope = self._scopes.resolve(identity)
        if enroll and self._auto_enroll and not self._consent.is_granted(
            scope, PersonalConsentType.PERSONAL_MEMORY,
        ):
            if not self._scopes.is_enabled(scope):
                raise AuthorizationDenied("personal memory scope is disabled")
            if not self._consent.ensure_auto_enrolled(
                scope, PersonalConsentType.PERSONAL_MEMORY,
                operator_id=identity.subject_id,
            ):
                raise AuthorizationDenied("personal memory consent is revoked")
            set_extra = getattr(event, "set_extra", None)
            if callable(set_extra):
                set_extra(_AUTO_ENROLL_FLAG, True)
        return scope

    @staticmethod
    def _notice_pending(event: Any) -> bool:
        get_extra = getattr(event, "get_extra", None)
        return bool(callable(get_extra) and get_extra(_AUTO_ENROLL_FLAG, False))

    @staticmethod
    def _clear_notice(event: Any) -> None:
        set_extra = getattr(event, "set_extra", None)
        if callable(set_extra):
            set_extra(_AUTO_ENROLL_FLAG, False)

    def _take_notice(self, event: Any) -> str:
        if not self._notice_pending(event):
            return ""
        self._clear_notice(event)
        return "\n\n" + AUTO_ENROLL_NOTICE

    async def _send_pending_notice(self, event: Any) -> None:
        if not self._notice_pending(event):
            return
        self._clear_notice(event)
        try:
            await event.send(event.plain_result(AUTO_ENROLL_NOTICE))
        except Exception:
            # A notice failure must not break the tool call.
            pass

    @staticmethod
    def _tail(event: Any, subcommand: str) -> str:
        raw = event.message_str.strip().lstrip("/").strip()
        if raw.startswith("织忆"):
            raw = raw[len("织忆"):].strip()
        if raw.startswith(subcommand):
            return raw[len(subcommand):].strip()
        return raw

    def help(self) -> str:
        return (
            "织忆命令：\n"
            "默认已开启；首次私聊会自动登记并发送一次告知（auto_enroll=false 时需 /织忆 授权）。\n"
            "/织忆 授权 —— 显式开启或恢复个人记忆\n"
            "/织忆 撤销授权 —— 停止保存、检索和注入（不删除已有数据，不会被自动登记复活）\n"
            "/织忆 保存 <内容> —— 显式保存一条事实\n"
            "/织忆 更新 <记忆ID> <当前revision> <新内容> —— 显式修订指定记忆\n"
            "/织忆 列表 —— 查看已保存记忆\n"
            "/织忆 查询 <问题> —— 主动检索记忆\n"
            "/织忆 状态 —— 查看个人空间、授权和索引状态\n"
            "/织忆 统计 —— 查看记忆数量分布\n"
            "/织忆 操作记录 —— 查看不含正文的操作审计\n"
            "/织忆 导出 [json|md] —— 导出当前个人空间的 ACTIVE 记忆\n"
            "/织忆 删除 <记忆ID> 确认 —— 二次确认后不可恢复地删除一条记忆\n"
            "/织忆 清空 确认 —— 不可恢复地清空个人记忆\n"
            "/织忆 关闭 确认 —— 永久关闭个人记忆空间（当前版本不支持重新开启，数据保留）\n"
            "模型工具写入仅应在用户明确要求时发生；AstrBot 4.29.4 未提供写入所需的 tool_call_id，当前请使用保存/更新命令。"
        )

    def grant(self, event: Any) -> str:
        scope = self._scope(event, create=True, enroll=False)
        operator_id = self._identity.resolve_operator_id(event)
        self._consent.grant(scope, PersonalConsentType.PERSONAL_MEMORY,
                            operator_id=operator_id)
        return "已授权个人记忆。你可以使用 /织忆 保存 <内容> 保存记忆；自动注入也已开启。"

    def revoke(self, event: Any) -> str:
        scope = self._scope(event, create=True, enroll=False)
        operator_id = self._identity.resolve_operator_id(event)
        self._consent.revoke(scope, PersonalConsentType.PERSONAL_MEMORY,
                             operator_id=operator_id)
        return "已撤销个人记忆授权。后续保存、检索和自动注入均停止；已有数据不会自动删除。"

    async def save(self, event: Any) -> str:
        content = self._tail(event, "保存")
        if not content:
            return "用法：/织忆 保存 <你希望记住的内容>"
        scope = self._scope(event)
        operator_id = self._identity.resolve_operator_id(event)
        memory, receipt = await self._memories.save_explicit(
            scope, content, operator_id=operator_id,
            memory_type=MemoryType.FACT,
        )
        state = "索引已同步" if receipt.index_state.value == "HEALTHY" else "正文已保存，索引待恢复"
        return f"已保存记忆：{memory.id}\n{state}" + self._take_notice(event)

    async def remember(self, event: Any, content: str,
                       memory_type: str = "FACT", *, tool_call_id: str) -> str:
        """Agent tool entry: save only after an explicit user request."""
        content = (content or "").strip()
        if not content:
            return "未保存：内容为空。"
        try:
            parsed_type = MemoryType((memory_type or "FACT").upper())
        except ValueError:
            return "未保存：memory_type 只能是 PREFERENCE、FACT 或 EVENT。"
        scope = self._scope(event, create=True, enroll=False)
        if not self._scopes.is_enabled(scope):
            return "未保存：个人记忆空间已关闭。"
        operator_id = self._identity.resolve_operator_id(event)
        if not self._consent.is_granted(scope, PersonalConsentType.PERSONAL_MEMORY):
            if not self._auto_enroll:
                return "未保存：尚未授权，请先发送 /织忆 授权。"
            if not self._consent.ensure_auto_enrolled(
                scope, PersonalConsentType.PERSONAL_MEMORY,
                operator_id=operator_id,
            ):
                return "未保存：个人记忆已撤销，发送 /织忆 授权 可重新开启。"
            set_extra = getattr(event, "set_extra", None)
            if callable(set_extra):
                set_extra(_AUTO_ENROLL_FLAG, True)
        memory, receipt = await self._memories.save_agent(
            scope, content, operator_id=operator_id, tool_call_id=tool_call_id,
            memory_type=parsed_type,
        )
        state = "索引已同步" if receipt.index_state.value == "HEALTHY" else "正文已保存，索引待恢复"
        notice = ""
        if self._notice_pending(event):
            self._clear_notice(event)
            try:
                await event.send(event.plain_result(AUTO_ENROLL_NOTICE))
            except Exception:
                notice = "\n\n" + AUTO_ENROLL_NOTICE
        return f"已保存记忆：{memory.id}\n{state}" + notice

    async def agent_search(self, event: Any, query: str, *, limit: int = 8) -> str:
        query = query.strip()
        if not query:
            return "检索失败：query 不能为空。"
        if type(limit) is not int or limit < 1 or limit > 20:
            return "检索失败：limit 必须在 1～20 之间。"
        scope = self._scope(event)
        await self._send_pending_notice(event)
        memories, trace = self._recall.recall_with_trace(query, scope, limit=limit)
        if not memories:
            self._recall.record_diagnostics(trace)
            return "没有找到相关记忆。"
        # Tool results are returned to the Agent, so record the IDs actually
        # exposed after the scoped access check rather than logging an empty
        # final-result set before the read is committed.
        await self._recall.record_access(tuple(item.id for item in memories), scope)
        result = json.dumps(
            [
                {
                    "id": item.id,
                    "memory_type": item.memory_type.value,
                    "content": item.content,
                    "revision": item.current_revision,
                }
                for item in memories
            ],
            ensure_ascii=False,
        )
        self._recall.record_diagnostics(
            trace, injected_ids=tuple(item.id for item in memories),
            estimated_tokens=estimate_tokens(result),
        )
        return result

    async def agent_get(self, event: Any, memory_id: str) -> str:
        scope = self._scope(event)
        await self._send_pending_notice(event)
        memory = self._memories.get_memory(scope, memory_id)
        await self._recall.record_access((memory.id,), scope)
        return json.dumps(
            {
                "id": memory.id,
                "memory_type": memory.memory_type.value,
                "content": memory.content,
                "status": memory.status.value,
                "source_type": memory.source_type.value,
                "revision": memory.current_revision,
                "created_at": memory.created_at.isoformat(),
                "updated_at": memory.updated_at.isoformat(),
            },
            ensure_ascii=False,
        )

    async def agent_save(
        self, event: Any, content: str, memory_type: str, *, tool_call_id: str,
    ) -> str:
        content = content.strip()
        if not content:
            return "未保存：content 不能为空。"
        try:
            parsed_type = MemoryType(memory_type.upper())
        except ValueError:
            return "未保存：memory_type 只能是 PREFERENCE、FACT 或 EVENT。"
        scope = self._scope(event)
        operator_id = self._identity.resolve_operator_id(event)
        memory, receipt = await self._memories.save_agent(
            scope, content, operator_id=operator_id,
            tool_call_id=tool_call_id, memory_type=parsed_type,
        )
        state = "索引已同步" if receipt.index_state.value == "HEALTHY" else "正文已保存，索引待恢复"
        return f"已保存记忆：{memory.id}（revision {memory.current_revision}）\n{state}"

    async def agent_update(
        self, event: Any, memory_id: str, expected_revision: int, content: str,
        *, tool_call_id: str,
    ) -> str:
        if type(expected_revision) is not int or expected_revision < 1:
            return "未更新：expected_revision 必须是正整数。"
        if not content.strip():
            return "未更新：content 不能为空。"
        scope = self._scope(event)
        operator_id = self._identity.resolve_operator_id(event)
        memory, receipt = await self._memories.update_agent(
            scope, memory_id, content, expected_revision=expected_revision,
            operator_id=operator_id, tool_call_id=tool_call_id,
        )
        state = "索引已同步" if receipt.index_state.value == "HEALTHY" else "正文已更新，索引待恢复"
        return f"已更新记忆：{memory.id}（revision {memory.current_revision}）\n{state}"

    async def agent_list(
        self, event: Any, *, offset: int = 0, limit: int = 20,
        memory_type: str = "",
    ) -> str:
        parsed_type = None
        if memory_type:
            try:
                parsed_type = MemoryType(memory_type.upper())
            except ValueError:
                return "读取失败：memory_type 只能是 PREFERENCE、FACT 或 EVENT。"
        scope = self._scope(event)
        await self._send_pending_notice(event)
        memories, total = self._memories.list_active_page(
            scope, limit=limit, offset=offset, memory_type=parsed_type,
        )
        return json.dumps(
            {
                "total": total,
                "offset": offset,
                "limit": limit,
                "items": [
                    {
                        "id": item.id,
                        "memory_type": item.memory_type.value,
                        "content": item.content,
                        "revision": item.current_revision,
                        "source_type": item.source_type.value,
                    }
                    for item in memories
                ],
            },
            ensure_ascii=False,
        )

    async def agent_history(self, event: Any, memory_id: str) -> str:
        scope = self._scope(event)
        await self._send_pending_notice(event)
        revisions, sources = self._memories.history(scope, memory_id)
        return json.dumps(
            {
                "memory_id": memory_id,
                "revisions": [
                    {
                        "revision": item.revision,
                        "content": item.content,
                        "status": item.status.value,
                        "changed_at": item.changed_at.isoformat(),
                        "change_reason": item.change_reason,
                    }
                    for item in revisions
                ],
                "sources": [
                    {
                        "source_type": item.source_type.value,
                        "operator_type": item.operator_type.value,
                        "operator_id": item.operator_id,
                        "captured_at": item.captured_at.isoformat(),
                        "evidence_ref": item.evidence_ref,
                    }
                    for item in sources
                ],
            },
            ensure_ascii=False,
        )

    def status(self, event: Any) -> str:
        scope = self._scope(event)
        stats = self._memories.statistics(scope)
        index = self._recall.index_status(scope)
        result = {
            "scope": "PERSONAL",
            "enabled": self._scopes.is_enabled(scope),
            "consent": "GRANTED" if self._consent.is_granted(
                scope, PersonalConsentType.PERSONAL_MEMORY,
            ) else "NOT_GRANTED",
            "memory": stats,
            "index": index,
        }
        return json.dumps(result, ensure_ascii=False) + self._take_notice(event)

    def stats(self, event: Any) -> str:
        scope = self._scope(event)
        stats = dict(self._memories.statistics(scope))
        recent, _ = self._memories.operations(scope, limit=5, offset=0)
        stats["scope_type"] = "PERSONAL"
        stats["recent_operations"] = list(recent)
        return json.dumps(stats, ensure_ascii=False) + self._take_notice(event)

    def operations(self, event: Any, *, limit: int = 20, offset: int = 0) -> str:
        scope = self._scope(event)
        operations, total = self._memories.operations(
            scope, limit=limit, offset=offset,
        )
        return json.dumps(
            {"total": total, "offset": offset, "limit": limit, "items": operations},
            ensure_ascii=False,
        ) + self._take_notice(event)

    def export(self, event: Any, format_name: str = "json") -> str:
        scope = self._scope(event)
        payload = self._memories.export_personal(scope)
        normalized_format = format_name.strip().lower()
        if normalized_format == "json":
            return to_json(payload) + self._take_notice(event)
        if normalized_format in {"md", "markdown"}:
            return to_markdown(payload) + self._take_notice(event)
        return "导出失败：格式只支持 json 或 md。"

    def export_command(self, event: Any) -> str:
        return self.export(event, self._tail(event, "导出") or "json")

    async def update(self, event: Any) -> str:
        payload = self._tail(event, "更新")
        parts = payload.split(maxsplit=2)
        if len(parts) != 3:
            return "用法：/织忆 更新 <记忆ID> <当前revision> <新内容>"
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
        return f"记忆已更新：{memory.id}（revision {memory.current_revision}）\n{state}" + self._take_notice(event)

    def list(self, event: Any) -> str:
        scope = self._scope(event)
        memories = self._memories.list_active(scope)
        if not memories:
            return "当前没有可用的个人记忆。" + self._take_notice(event)
        lines = [f"{item.id} [{item.memory_type.value}] [revision {item.current_revision}] {item.content}" for item in memories[:30]]
        suffix = f"\n另有 {len(memories) - 30} 条未显示" if len(memories) > 30 else ""
        return "个人记忆：\n" + "\n".join(lines) + suffix + self._take_notice(event)

    async def query(self, event: Any) -> str:
        query = self._tail(event, "查询")
        if not query:
            return "用法：/织忆 查询 <问题>"
        scope = self._scope(event)
        memories, trace = self._recall.recall_with_trace(query, scope)
        if not memories:
            self._recall.record_diagnostics(trace)
            return "没有找到相关记忆。" + self._take_notice(event)
        await self._recall.record_access(tuple(item.id for item in memories), scope)
        lines = [f"[{item.memory_type.value}] {item.content}（{item.id}）" for item in memories]
        result = "检索到的个人记忆：\n" + "\n".join(lines) + self._take_notice(event)
        self._recall.record_diagnostics(
            trace, injected_ids=tuple(item.id for item in memories),
            estimated_tokens=estimate_tokens(result),
        )
        return result

    async def delete(self, event: Any) -> str:
        tail = self._tail(event, "删除")
        parts = tail.split(maxsplit=1)
        if not parts:
            return "用法：/织忆 删除 <记忆ID>"
        memory_id = parts[0]
        if len(parts) != 2 or parts[1] != "确认":
            return f"删除记忆不可恢复。确认执行请发送：/织忆 删除 {memory_id} 确认"
        scope = self._scope(event, enroll=False)
        operator_id = self._identity.resolve_operator_id(event)
        receipt = await self._lifecycle.delete_memory(
            memory_id, scope_id=scope.id, subject_id=scope.identity.subject_id,
            operation_id=self._ids.new_id("operation"),
            operator_type=OperatorType.USER, operator_id=operator_id,
            at=self._clock.now(),
        )
        state = "索引已同步" if receipt.index_state.value == "HEALTHY" else "删除已提交，索引待恢复"
        purge = "" if receipt.purged is not False else "\n物理擦除未完成，将在下次插件启动时自动重试。"
        return f"记忆已删除。{state}{purge}"

    async def clear(self, event: Any) -> str:
        if self._tail(event, "清空") != "确认":
            return "清空不可恢复。确认执行请发送：/织忆 清空 确认"
        scope = self._scope(event, enroll=False)
        operator_id = self._identity.resolve_operator_id(event)
        receipt = await self._lifecycle.clear_scope(
            scope.id, operation_id=self._ids.new_id("operation"),
            operator_type=OperatorType.USER, operator_id=operator_id,
            at=self._clock.now(),
        )
        state = "索引已重建" if receipt.index_state.value == "HEALTHY" else "清空已提交，索引待恢复"
        purge = "" if receipt.purged is not False else "\n物理擦除未完成，将在下次插件启动时自动重试。"
        return f"已清空 {receipt.affected_count} 条记忆。{state}{purge}"

    async def disable(self, event: Any) -> str:
        if self._tail(event, "关闭") != "确认":
            return "关闭会永久停止读写和注入（V0 不支持重新开启），但保留数据。确认执行请发送：/织忆 关闭 确认"
        scope = self._scope(event, enroll=False)
        operator_id = self._identity.resolve_operator_id(event)
        await self._lifecycle.disable_scope(
            scope.id, operation_id=self._ids.new_id("operation"),
            operator_type=OperatorType.USER, operator_id=operator_id,
            at=self._clock.now(),
        )
        return "个人记忆空间已永久关闭（V0 不支持重新开启）。数据仍保留，但普通读写和自动注入已停止。"
