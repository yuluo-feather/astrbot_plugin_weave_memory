from __future__ import annotations

from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.message import TextPart

from .adapters.astrbot.commands import AstrBotMemoryCommands
from .adapters.astrbot.group_admin import AstrBotGroupAdminResolver
from .adapters.astrbot.group_commands import AstrBotGroupCommands
from .adapters.astrbot.group_injection import AstrBotGroupMemoryInjector
from .adapters.astrbot.injection import AstrBotMemoryInjector
from .adapters.sqlite.consents import SQLiteConsentStore
from .adapters.sqlite.database import SQLiteDatabase
from .adapters.sqlite.fts import SQLiteFTSIndex
from .adapters.sqlite.store import SQLiteMemoryStore
from .adapters.system import SystemClock, UUID4IdGenerator
from .app.consent_service import ConsentService
from .app.group_lifecycle_service import GroupLifecycleService
from .app.group_memory_service import GroupMemoryService
from .app.group_recall_service import GroupContextComposer, GroupRecallService
from .app.lifecycle_service import LifecycleService
from .app.memory_service import MemoryApplicationService
from .app.recall_service import MemoryContextComposer, RecallService
from .app.scope_service import GroupScopeService, PersonalScopeService
from .app.startup_recovery import StartupRecoveryService
from .app.write_coordinator import WriteCoordinator
from .core.errors import (
    AuthorizationDenied,
    Conflict,
    ConsentRevocationPending,
    IndexUnavailable,
    InvalidTransition,
    NotFound,
    ServiceClosed,
    StorageError,
    ValidationError,
)

_COMMAND_ERRORS = (
    AuthorizationDenied, Conflict, IndexUnavailable, InvalidTransition, NotFound,
    ServiceClosed, StorageError, ValidationError,
)


class WeaveMemoryPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self._config = config or {}
        self._ready = False
        self._database: SQLiteDatabase | None = None
        self._coordinator: WriteCoordinator | None = None
        self._commands: AstrBotMemoryCommands | None = None
        self._group_commands: AstrBotGroupCommands | None = None
        self._injector: AstrBotMemoryInjector | None = None
        self._group_injector: AstrBotGroupMemoryInjector | None = None

        try:
            data_dir = Path(StarTools.get_data_dir())
            data_dir.mkdir(parents=True, exist_ok=True)
            self._database = SQLiteDatabase(data_dir / "weave_memory.sqlite3")
            backup_path = self._database.migrate()
            if backup_path is not None:
                logger.info(f"织忆数据库迁移前备份：{backup_path}")
            store = SQLiteMemoryStore(self._database)
            index = SQLiteFTSIndex(self._database)
            StartupRecoveryService(self._database, index).run()
            try:
                self._database.secure_purge_if_pending()
            except StorageError as exc:
                logger.warning(f"织忆启动时的物理擦除未完成：{type(exc).__name__}")

            clock = SystemClock()
            ids = UUID4IdGenerator()
            consent_repository = SQLiteConsentStore(self._database)
            coordinator = WriteCoordinator(store, index)
            scopes = PersonalScopeService(store, coordinator, clock)
            consent = ConsentService(consent_repository, coordinator, clock, ids)
            lifecycle = LifecycleService(coordinator, store, consent)
            memories = MemoryApplicationService(store, coordinator, consent, lifecycle, clock, ids)
            recall = RecallService(store, index, consent, clock, coordinator, store)
            composer = MemoryContextComposer(recall)
            group_admins = AstrBotGroupAdminResolver(
                self._explicit_group_admins(self._config),
            )
            group_scopes = GroupScopeService(store, group_admins, coordinator, clock)
            group_lifecycle = GroupLifecycleService(
                store, coordinator, consent, index, clock, ids,
                data_dir / "group_backups",
            )
            group_memories = GroupMemoryService(
                store, store, coordinator, consent, clock, ids,
                max_active=self._config_int("group_max_active", 200),
            )
            group_recall = GroupRecallService(
                store, index, consent, clock, coordinator, store,
            )
            group_composer = GroupContextComposer(
                group_recall,
                max_tokens=self._config_int("group_max_tokens", 192),
                max_memories=self._config_int("group_max_memories", 2),
                candidate_limit=self._config_int("group_candidate_limit", 8),
            )

            self._coordinator = coordinator
            auto_enroll = self._config_bool("auto_enroll", True)
            self._commands = AstrBotMemoryCommands(
                scopes, consent, memories, recall, lifecycle, clock, ids,
                auto_enroll=auto_enroll,
            )
            self._group_commands = AstrBotGroupCommands(
                group_scopes, group_lifecycle, group_memories, group_recall,
            )
            self._injector = AstrBotMemoryInjector(
                scopes, consent, composer, auto_enroll=auto_enroll,
            )
            self._group_injector = AstrBotGroupMemoryInjector(
                group_scopes, consent, group_composer,
            )
            self._ready = True
        except (OSError, RuntimeError, ValueError, StorageError, IndexUnavailable) as exc:
            logger.error(f"织忆初始化失败，记忆功能暂不可用：{type(exc).__name__}")
            if self._database is not None:
                self._database.close()
                self._database = None

    def _command_service(self) -> AstrBotMemoryCommands:
        if not self._ready or self._commands is None:
            raise ServiceClosed("memory engine is not ready")
        return self._commands

    def _group_command_service(self) -> AstrBotGroupCommands:
        if not self._ready or self._group_commands is None:
            raise ServiceClosed("memory engine is not ready")
        return self._group_commands

    @staticmethod
    def _explicit_group_admins(config: object) -> dict[tuple[str, str, str], tuple[str, ...]]:
        if not hasattr(config, "get"):
            return {}
        raw = config.get("group_admin_ids", [])  # type: ignore[union-attr]
        result: dict[tuple[str, str, str], tuple[str, ...]] = {}
        if isinstance(raw, dict):
            for key, admin_ids in raw.items():
                if not isinstance(key, str) or not isinstance(admin_ids, list):
                    continue
                parts = key.split("|")
                if len(parts) != 3:
                    continue
                platform, tenant_id, group_id = parts
                if not platform.strip() or not tenant_id.strip() or not group_id.strip():
                    continue
                ids = tuple(str(value) for value in admin_ids
                            if isinstance(value, str) and value.strip())
                if len(ids) != len(admin_ids):
                    continue
                result[(platform, tenant_id, group_id)] = ids
            return result
        if not isinstance(raw, list):
            return {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            platform_value = item.get("platform")
            tenant_value = item.get("tenant_id")
            group_value = item.get("group_id")
            admin_ids = item.get("admin_ids")
            if not isinstance(platform_value, str) or not isinstance(tenant_value, str):
                continue
            if not isinstance(group_value, str) or not isinstance(admin_ids, list):
                continue
            if not platform_value.strip() or not tenant_value.strip() or not group_value.strip():
                continue
            ids = tuple(str(value) for value in admin_ids
                        if isinstance(value, str) and value.strip())
            if len(ids) != len(admin_ids):
                continue
            result[(platform_value, tenant_value, group_value)] = ids
        return result

    def _config_int(self, key: str, default: int) -> int:
        if not hasattr(self._config, "get"):
            return default
        try:
            value = int(self._config.get(key, default))  # type: ignore[union-attr]
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    def _config_bool(self, key: str, default: bool) -> bool:
        if not hasattr(self._config, "get"):
            return default
        value = self._config.get(key, default)  # type: ignore[union-attr]
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return default

    @staticmethod
    def _error_text(action: str, exc: Exception) -> str:
        if isinstance(exc, ConsentRevocationPending):
            return "个人记忆空间已关闭，数据已停止读写；但授权撤销记录写入失败，请重试 /织忆 关闭 确认。"
        if isinstance(exc, AuthorizationDenied):
            return f"{action}失败：尚未授权或个人记忆空间已关闭。"
        if isinstance(exc, NotFound):
            return f"{action}失败：没有找到对应的个人记忆或作用域。"
        if isinstance(exc, IndexUnavailable):
            return f"{action}失败：检索索引暂不可用，正文数据不会因此丢失。"
        return f"{action}失败：{type(exc).__name__}。"

    @filter.command_group("织忆", alias={"/织忆"})
    def memory(self):
        """个人记忆管理命令组。"""

    @memory.command("帮助")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def memory_help(self, event: AstrMessageEvent):
        if self._commands is None:
            yield event.plain_result("织忆当前未就绪，请检查插件日志和 SQLite FTS5 支持。")
            return
        yield event.plain_result(self._commands.help())

    @memory.command("授权")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def memory_grant(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(self._command_service().grant(event))
        except _COMMAND_ERRORS as exc:
            yield event.plain_result(self._error_text("授权", exc))

    @memory.command("撤销授权")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def memory_revoke(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(self._command_service().revoke(event))
        except _COMMAND_ERRORS as exc:
            yield event.plain_result(self._error_text("撤销授权", exc))

    @memory.command("保存")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def memory_save(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(await self._command_service().save(event))
        except _COMMAND_ERRORS as exc:
            yield event.plain_result(self._error_text("保存", exc))

    @memory.command("更新")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def memory_update(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(await self._command_service().update(event))
        except _COMMAND_ERRORS as exc:
            yield event.plain_result(self._error_text("更新", exc))

    @memory.command("列表")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def memory_list(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(self._command_service().list(event))
        except _COMMAND_ERRORS as exc:
            yield event.plain_result(self._error_text("读取", exc))

    @memory.command("查询")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def memory_query(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(await self._command_service().query(event))
        except _COMMAND_ERRORS as exc:
            yield event.plain_result(self._error_text("检索", exc))

    @memory.command("删除")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def memory_delete(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(await self._command_service().delete(event))
        except _COMMAND_ERRORS as exc:
            yield event.plain_result(self._error_text("删除", exc))

    @memory.command("清空")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def memory_clear(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(await self._command_service().clear(event))
        except _COMMAND_ERRORS as exc:
            yield event.plain_result(self._error_text("清空", exc))

    @memory.command("关闭")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def memory_disable(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(await self._command_service().disable(event))
        except _COMMAND_ERRORS as exc:
            yield event.plain_result(self._error_text("关闭", exc))

    @filter.llm_tool(name="weave_memory_remember")
    async def memory_remember_tool(self, event: AstrMessageEvent,
                                   content: str = "", memory_type: str = "FACT") -> str:
        """织忆个人记忆：保存用户明确要求记住的长期偏好、背景、目标或事实。

        仅在用户**明确表达长期记忆意图**时调用，例如「记住我喜欢黑咖啡」「以后都用中文」
        「我对花生过敏」。一次闲聊、临时情绪、当前任务细节、密码/密钥/身份证/银行卡等敏感
        信息不得调用。只保存一条简洁事实，不保存完整聊天原文。群聊不记录个人记忆。

        Args:
            content(string): 要保存的事实，尽量保留用户原话中的主体与偏好。
            memory_type(string): PREFERENCE、FACT 或 EVENT，默认 FACT。
        """
        if not self._config_bool("agent_write_enabled", True):
            return "未保存：织忆的模型记忆写入已关闭。"
        try:
            return await self._command_service().remember(event, content, memory_type)
        except AuthorizationDenied:
            return "未保存：个人记忆只在私聊生效。"
        except _COMMAND_ERRORS as exc:
            return f"未保存：{type(exc).__name__}。"

    @memory.command("群帮助")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def memory_group_help(self, event: AstrMessageEvent):
        if self._group_commands is None:
            yield event.plain_result("织忆当前未就绪，请检查插件日志和 SQLite FTS5 支持。")
            return
        yield event.plain_result(self._group_commands.help())

    @memory.command("群开启")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def memory_group_enable(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(await self._group_command_service().enable(event))
        except _COMMAND_ERRORS as exc:
            yield event.plain_result(AstrBotGroupCommands._error_text("群开启", exc))

    @memory.command("群关闭")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def memory_group_disable(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(await self._group_command_service().disable(event))
        except _COMMAND_ERRORS as exc:
            yield event.plain_result(AstrBotGroupCommands._error_text("群关闭", exc))

    @memory.command("群清空")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def memory_group_clear(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(await self._group_command_service().clear(event))
        except _COMMAND_ERRORS as exc:
            yield event.plain_result(AstrBotGroupCommands._error_text("群清空", exc))

    @memory.command("群销毁")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def memory_group_destroy(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(await self._group_command_service().destroy(event))
        except _COMMAND_ERRORS as exc:
            yield event.plain_result(AstrBotGroupCommands._error_text("群销毁", exc))

    @memory.command("群保存")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def memory_group_save(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(await self._group_command_service().save(event))
        except _COMMAND_ERRORS as exc:
            yield event.plain_result(AstrBotGroupCommands._error_text("群保存", exc))

    @memory.command("群列表")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def memory_group_list(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(self._group_command_service().list(event))
        except _COMMAND_ERRORS as exc:
            yield event.plain_result(AstrBotGroupCommands._error_text("群列表", exc))

    @memory.command("群查询")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def memory_group_query(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(await self._group_command_service().query(event))
        except _COMMAND_ERRORS as exc:
            yield event.plain_result(AstrBotGroupCommands._error_text("群查询", exc))

    @memory.command("群状态")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def memory_group_status(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(self._group_command_service().status(event))
        except _COMMAND_ERRORS as exc:
            yield event.plain_result(AstrBotGroupCommands._error_text("群状态", exc))

    @filter.on_llm_request()
    async def inject_personal_memory(self, event: AstrMessageEvent, req: ProviderRequest):
        """Append only temporary context; never mutate persistent chat history."""
        if not self._ready or self._injector is None:
            return
        try:
            is_private = getattr(event, "is_private_chat", None)
            if callable(is_private) and is_private():
                context = await self._injector.context_for(event)
            elif self._group_injector is not None:
                context = await self._group_injector.context_for(event)
            else:
                context = None
            if context:
                req.extra_user_content_parts.append(TextPart(text=context).mark_as_temp())
        except Exception as exc:  # noqa: BLE001 - injection must never break host chat
            # Any memory-layer or host-API failure is isolated from the chat request.
            logger.warning(f"织忆本轮未注入记忆：{type(exc).__name__}")

    async def terminate(self):
        if self._coordinator is not None:
            await self._coordinator.close()
        if self._database is not None:
            self._database.close()
            self._database = None
        self._ready = False
