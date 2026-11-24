from __future__ import annotations

import asyncio
from datetime import timedelta
from hashlib import sha256
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.message import TextPart

from .adapters.astrbot.backup_commands import AstrBotBackupCommands
from .adapters.astrbot.commands import AstrBotMemoryCommands
from .adapters.astrbot.embedding import AstrBotEmbeddingProvider
from .adapters.astrbot.extraction import (
    AstrBotExtractionCapture,
    AstrBotLlmExtractor,
)
from .adapters.astrbot.extraction_commands import AstrBotExtractionCommands
from .adapters.astrbot.group_admin import AstrBotGroupAdminResolver
from .adapters.astrbot.group_commands import AstrBotGroupCommands
from .adapters.astrbot.group_injection import AstrBotGroupMemoryInjector
from .adapters.astrbot.group_lifecycle_events import parse_group_lifecycle_event
from .adapters.astrbot.identity import AstrBotIdentityAdapter
from .adapters.astrbot.injection import AstrBotMemoryInjector
from .adapters.astrbot.recall_diagnostics import JsonlRecallDiagnostics
from .adapters.astrbot.rerank import AstrBotRerankProvider
from .adapters.astrbot.retrieval_trace import RetrievalTraceRecorder
from .adapters.astrbot.tool_call_binding import AstrBotToolCallBinding
from .adapters.astrbot.web_api import AstrBotManagementApi
from .adapters.sqlite.backup import BackupService
from .adapters.sqlite.consents import SQLiteConsentStore
from .adapters.sqlite.database import SQLiteDatabase
from .adapters.sqlite.extraction_jobs import SQLiteExtractionJobStore
from .adapters.sqlite.fts import SQLiteFTSIndex
from .adapters.sqlite.import_jobs import SQLiteImportJobStore
from .adapters.sqlite.index_jobs import (
    SQLiteIndexJobStore,
    SQLiteIndexStateStore,
    SQLiteVectorOutbox,
)
from .adapters.sqlite.ledger import SQLiteLedger
from .adapters.sqlite.provider_audit import SQLiteProviderCallAudit
from .adapters.sqlite.store import SQLiteMemoryStore
from .adapters.sqlite.vector import SQLiteVectorIndex
from .adapters.system import SystemClock, UUID4IdGenerator
from .app.consent_service import ConsentService
from .app.embedding_cache import EmbeddingLRUCache
from .app.extraction_service import ExtractionService
from .app.group_lifecycle_service import GroupLifecycleService
from .app.group_memory_service import GroupMemoryService
from .app.group_recall_service import GroupContextComposer, GroupRecallService
from .app.hybrid_recall_service import HybridGroupRecallService, HybridRecallService
from .app.import_service import ImportService
from .app.index_job_service import IndexJobService
from .app.index_rebuild_service import IndexRebuildService
from .app.index_worker import IndexWorker
from .app.ledger_service import LedgerService
from .app.lifecycle_service import LifecycleService
from .app.management_service import ManagementService
from .app.memory_service import MemoryApplicationService
from .app.provider_config import ProviderConfig
from .app.provider_policy import ProviderPolicy
from .app.recall_service import MemoryContextComposer, RecallService
from .app.retention_service import RetentionService
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
from .core.model import MemoryEntity
from .core.normalization import NORMALIZATION_VERSION

_COMMAND_ERRORS = (
    AuthorizationDenied, Conflict, IndexUnavailable, InvalidTransition, NotFound,
    ServiceClosed, StorageError, ValidationError,
)

_TERMINATE_TIMEOUT_SECONDS = 5.0

_LLM_TOOL = getattr(filter, "llm_tool", None)


def _llm_tool(name=None, **kwargs):
    """Register an LLM tool when the host supports it; keep commands usable otherwise."""
    if _LLM_TOOL is None:
        def passthrough(fn):
            return fn
        return passthrough
    return _LLM_TOOL(name=name, **kwargs)


def _hook(name: str):
    """Host hook decorator with a no-op fallback for hosts without agent tools."""
    decorator = getattr(filter, name, None)
    if decorator is None:
        def no_op(*args, **kwargs):
            def passthrough(fn):
                return fn
            return passthrough
        return no_op
    return decorator


class WeaveMemoryPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self._config = config or {}
        self._ready = False
        self._database: SQLiteDatabase | None = None
        self._ledger_store: SQLiteLedger | None = None
        self._coordinator: WriteCoordinator | None = None
        self._commands: AstrBotMemoryCommands | None = None
        self._group_commands: AstrBotGroupCommands | None = None
        self._extraction_commands: AstrBotExtractionCommands | None = None
        self._backup_commands: AstrBotBackupCommands | None = None
        self._extraction_capture: AstrBotExtractionCapture | None = None
        self._retention: RetentionService | None = None
        self._scheduled_backup_task: asyncio.Task[None] | None = None
        self._index_worker: IndexWorker | None = None
        self._provider_policy: ProviderPolicy | None = None
        self._injector: AstrBotMemoryInjector | None = None
        self._group_injector: AstrBotGroupMemoryInjector | None = None
        self._group_scopes: GroupScopeService | None = None
        self._group_lifecycle: GroupLifecycleService | None = None
        self._tool_call_binding = AstrBotToolCallBinding(
            plugin_module=type(self).__module__,
            plugin_name="astrbot_plugin_weave_memory",
            plugin_instance=self,
        )

        try:
            data_dir = Path(StarTools.get_data_dir())
            data_dir.mkdir(parents=True, exist_ok=True)
            self._database = SQLiteDatabase(data_dir / "weave_memory.sqlite3")
            backup_path = self._database.migrate()
            if backup_path is not None:
                logger.info(f"织忆数据库迁移前备份：{backup_path}")
            clock = SystemClock()
            ids = UUID4IdGenerator()
            provider_config = ProviderConfig.from_mapping(
                self._config if hasattr(self._config, "get") else {},
            )
            index = SQLiteFTSIndex(
                self._database,
                relevance_ratio=provider_config.fts_relevance_ratio,
            )
            provider: AstrBotEmbeddingProvider | None = None
            provider_policy: ProviderPolicy | None = None
            provider_audit: SQLiteProviderCallAudit | None = None
            if provider_config.embedding_provider_id:
                provider_audit = SQLiteProviderCallAudit(self._database)
                try:
                    provider = AstrBotEmbeddingProvider(
                        self.context, provider_config.embedding_provider_id,
                        provider_config.embedding_model,
                        batch_size=provider_config.embedding_batch_size,
                    )
                except Exception as exc:  # noqa: BLE001 - provider is optional
                    logger.warning(
                        "织忆 embedding provider 不可用：%s", type(exc).__name__,
                    )
                    provider = None
                if provider is not None:
                    provider_policy = ProviderPolicy(
                        provider, provider_config, provider_audit,
                        EmbeddingLRUCache(
                            max_mb=provider_config.embedding_cache_mb,
                            ttl_seconds=provider_config.embedding_cache_ttl_minutes * 60,
                        ),
                        clock,
                    )
            vector_outbox = (
                SQLiteVectorOutbox()
                if provider_config.vector_index_enabled else None
            )
            store = SQLiteMemoryStore(
                self._database, vector_outbox=vector_outbox,
            )
            consent_repository = SQLiteConsentStore(self._database)
            ledger_enabled = self._config_bool("ledger_first_enabled", True)
            ledger_store = SQLiteLedger(data_dir / "memory_ledger.sqlite3")
            self._ledger_store = ledger_store
            ledger = LedgerService(
                ledger_store, clock, ids, enabled=ledger_enabled,
                rotate_events=self._config_non_negative_int(
                    "ledger_rotate_events", 0,
                ),
            )
            coordinator = WriteCoordinator(store, index, ledger)
            vector_index: SQLiteVectorIndex | None = None
            index_jobs: SQLiteIndexJobStore | None = None
            index_worker: IndexWorker | None = None
            index_rebuild: IndexRebuildService | None = None
            if provider is not None:
                config_hash = sha256(
                    (
                        f"{provider.provider_id}|{provider.model}|{provider.dimension}|"
                        f"{NORMALIZATION_VERSION}|"
                        f"{provider_config.embedding_max_text_chars}"
                    ).encode("utf-8"),
                ).hexdigest()[:16]
                vector_index = SQLiteVectorIndex(
                    self._database, provider_id=provider.provider_id,
                    model=provider.model, dim=provider.dimension,
                    config_hash=config_hash,
                    states=SQLiteIndexStateStore(self._database),
                    snapshot_cache_mb=provider_config.vector_snapshot_cache_mb,
                )
                index_jobs = SQLiteIndexJobStore(self._database)
                index_service = IndexJobService(
                    store, index_jobs, vector_index, provider_policy,
                    coordinator, clock,
                    max_attempts=provider_config.index_job_max_attempts,
                    enabled=provider_config.vector_index_enabled,
                )
                index_rebuild = IndexRebuildService(
                    store, vector_index, provider_policy, coordinator, clock,
                )
                index_worker = IndexWorker(
                    index_service, index_jobs, index_rebuild, clock,
                    interval_seconds=provider_config.index_worker_interval_seconds,
                    enabled=provider_config.vector_index_enabled,
                )
            self._index_worker = index_worker
            self._provider_policy = provider_policy
            scopes = PersonalScopeService(store, coordinator, clock)
            consent = ConsentService(consent_repository, coordinator, clock, ids)
            archive_retention_days = self._config_int("archive_retention_days", 30)
            lifecycle = LifecycleService(
                coordinator, store, consent,
                archive_retention_days=archive_retention_days,
            )
            memories = MemoryApplicationService(store, coordinator, consent, lifecycle, clock, ids)
            imports = ImportService(
                store, SQLiteImportJobStore(self._database), coordinator,
                lifecycle, clock, ids,
            )
            backup = BackupService(
                database=self._database, store=store, index=index,
                consents=consent_repository, coordinator=coordinator,
                ledger=ledger, clock=clock, ids=ids, data_dir=data_dir,
                config=self._config if hasattr(self._config, "items") else {},
                enabled=ledger_enabled,
                keep_last=self._config_int("backup_keep_last", 5),
                retention_days=self._config_int("backup_retention_days", 30),
            )
            # Restore journal must be resolved before any read or write is served.
            backup.recover_if_needed()
            backup_interval_hours = self._config_int("backup_interval_hours", 0)
            if backup_interval_hours > 0 and backup.enabled:
                try:
                    self._scheduled_backup_task = (
                        asyncio.get_running_loop().create_task(
                            self._scheduled_backup_loop(
                                backup, backup_interval_hours * 3600,
                            ),
                        )
                    )
                except RuntimeError:
                    logger.warning(
                        "织忆定时备份未启动：当前没有运行中的事件循环。",
                    )
            applied, failed = ledger.drain_pending(store, consent_repository)
            if applied:
                logger.info(f"织忆账本重放：{applied} 个 pending 事件已提交。")
            if failed:
                logger.warning(
                    f"织忆账本仍有 {len(failed)} 个 pending 事件未完成，"
                    f"ids={list(failed[:10])}，将在备份/下次启动重试。",
                )
            StartupRecoveryService(self._database, index).run()
            try:
                self._database.secure_purge_if_pending()
            except StorageError as exc:
                logger.warning(f"织忆启动时的物理擦除未完成：{type(exc).__name__}")
            retention = RetentionService(
                store, lifecycle, clock, ids,
                batch_size=self._config_int("purge_batch_size", 100),
                interval_seconds=self._config_int("purge_interval_seconds", 3600),
            )
            self._retention = retention
            try:
                retention.start()
            except RuntimeError:
                logger.warning("织忆到期清理未启动：当前没有运行中的事件循环。")
            prune_access = getattr(store, "prune_access_events", None)
            if callable(prune_access):
                try:
                    prune_access(before=clock.now() - timedelta(days=90))
                except Exception as exc:  # noqa: BLE001 - audit is best-effort
                    logger.warning(
                        "织忆访问审计清理失败：%s", type(exc).__name__,
                    )
            if provider_audit is not None:
                try:
                    provider_audit.prune_before(
                        clock.now() - timedelta(
                            days=provider_config.provider_call_audit_retention_days,
                        ),
                    )
                except StorageError as exc:
                    logger.warning(
                        "织忆 Provider 审计清理失败：%s", type(exc).__name__,
                    )
            if index_worker is not None:
                try:
                    index_worker.start()
                except RuntimeError:
                    logger.warning("织忆向量索引 worker 未启动：没有运行中的事件循环。")

            diagnostics_enabled = self._config_bool("recall_diagnostics_enabled", True)
            diagnostics = None
            if diagnostics_enabled:
                diagnostics = JsonlRecallDiagnostics(
                    data_dir / "recall_diagnostics",
                    retention_days=self._config_int("recall_diagnostics_retention_days", 7),
                    include_query=self._config_bool("recall_diagnostics_include_query", False),
                )
                diagnostics.cleanup()
            recall = RecallService(
                store, index, consent, clock, coordinator, store,
                diagnostics.write if diagnostics is not None else None,
                include_query_in_diagnostics=self._config_bool(
                    "recall_diagnostics_include_query", False,
                ),
            )
            trace_recorder = RetrievalTraceRecorder(
                diagnostics.write if diagnostics is not None else None,
            )
            hybrid: HybridRecallService | None = None
            if provider_config.hybrid_retrieval_enabled:
                rerank_provider = None
                if provider_config.rerank_enabled and provider_config.rerank_provider_id:
                    try:
                        rerank_provider = AstrBotRerankProvider(
                            self.context, provider_config.rerank_provider_id,
                        )
                    except Exception as exc:  # noqa: BLE001 - optional provider
                        logger.warning(
                            "织忆 rerank provider 不可用：%s", type(exc).__name__,
                        )
                hybrid = HybridRecallService(
                    store, index, consent, clock, store, coordinator,
                    vector=vector_index, provider=provider_policy,
                    rerank=rerank_provider,
                    vector_candidate_limit=provider_config.vector_candidate_limit,
                    hybrid_candidate_limit=provider_config.hybrid_candidate_limit,
                    classifier_mode=provider_config.query_classifier_mode,
                    rerank_enabled=provider_config.rerank_enabled,
                    rerank_timeout_seconds=provider_config.rerank_timeout_seconds,
                    rerank_max_docs=provider_config.rerank_max_docs,
                    rerank_max_text_chars=provider_config.rerank_max_text_chars,
                    rerank_cooldown_seconds=(
                        provider_config.provider_failure_cooldown_seconds
                    ),
                    tag_candidate_limit=provider_config.tag_candidate_limit,
                    time_candidate_limit=provider_config.time_candidate_limit,
                    relevance_threshold=provider_config.vector_relevance_threshold,
                    fts_relevance_ratio=provider_config.fts_relevance_ratio,
                    fusion_method="rrf",
                    recorder=trace_recorder,
                    include_query_text=self._config_bool(
                        "recall_diagnostics_include_query", False,
                    ),
                )
            composer = MemoryContextComposer(
                hybrid or recall,
                max_tokens=self._config_int("personal_max_tokens", 384),
                max_memories=self._config_non_negative_int(
                    "personal_max_memories", 4,
                ),
                candidate_limit=self._config_int("personal_candidate_limit", 12),
            )
            group_admins = AstrBotGroupAdminResolver(
                self._explicit_group_admins(self._config),
            )
            group_scopes = GroupScopeService(store, group_admins, coordinator, clock)
            group_lifecycle = GroupLifecycleService(
                store, coordinator, consent, index, clock, ids,
                data_dir / "group_backups", ledger=ledger,
            )
            self._group_scopes = group_scopes
            self._group_lifecycle = group_lifecycle
            destroy_backup_retention = self._config_int(
                "group_destroy_backup_retention_days", 30,
            )
            removed_backups = group_lifecycle.cleanup_destroy_backups(
                destroy_backup_retention,
            )
            if removed_backups:
                logger.info(f"织忆清理超期群销毁备份：{removed_backups} 份")
            group_memories = GroupMemoryService(
                store, store, coordinator, consent, clock, ids,
                max_active=self._config_int("group_max_active", 200),
            )
            group_recall = GroupRecallService(
                store, index, consent, clock, coordinator, store,
                diagnostics.write if diagnostics is not None else None,
                include_query_in_diagnostics=self._config_bool(
                    "recall_diagnostics_include_query", False,
                ),
            )
            group_composer = GroupContextComposer(
                group_recall,
                max_tokens=self._config_int("group_max_tokens", 192),
                max_memories=self._config_non_negative_int("group_max_memories", 2),
                candidate_limit=self._config_int("group_candidate_limit", 8),
            )
            if hybrid is not None:
                hybrid_group = HybridGroupRecallService(
                    store, index, consent, clock, store, coordinator,
                    vector=vector_index, provider=provider_policy,
                    rerank=None,
                    vector_candidate_limit=provider_config.vector_candidate_limit,
                    hybrid_candidate_limit=provider_config.hybrid_candidate_limit,
                    classifier_mode=provider_config.query_classifier_mode,
                    tag_candidate_limit=provider_config.tag_candidate_limit,
                    time_candidate_limit=provider_config.time_candidate_limit,
                    relevance_threshold=provider_config.vector_relevance_threshold,
                    fts_relevance_ratio=provider_config.fts_relevance_ratio,
                    recorder=trace_recorder,
                    include_query_text=self._config_bool(
                        "recall_diagnostics_include_query", False,
                    ),
                )
                group_composer = GroupContextComposer(
                    hybrid_group,
                    max_tokens=self._config_int("group_max_tokens", 192),
                    max_memories=self._config_non_negative_int(
                        "group_max_memories", 2,
                    ),
                    candidate_limit=self._config_int("group_candidate_limit", 8),
                )

            extraction_enabled = self._config_bool("extraction_enabled", False)
            extraction = ExtractionService(
                store=store,
                jobs=SQLiteExtractionJobStore(self._database),
                coordinator=coordinator,
                consent=consent,
                scopes=store,
                lifecycle=lifecycle,
                extractor=AstrBotLlmExtractor(self.context),
                clock=clock,
                ids=ids,
                global_enabled=extraction_enabled,
                max_retries=self._config_int("extraction_max_retries", 3),
                timeout_seconds=self._config_int("extraction_timeout_seconds", 30),
                queue_size=self._config_int("extraction_queue_size", 100),
                candidate_ttl_days=self._config_int("candidate_ttl_days", 7),
                rejected_keep_body=self._config_bool("rejected_keep_body", False),
            )
            if extraction_enabled:
                recovered = extraction.recover_interrupted()
                if recovered:
                    logger.warning(f"织忆提取任务恢复：{recovered} 个中断任务已标记失败。")

            self._coordinator = coordinator
            auto_enroll = self._config_bool("auto_enroll", True)
            self._commands = AstrBotMemoryCommands(
                scopes, consent, memories, recall, lifecycle, clock, ids, imports,
                auto_enroll=auto_enroll,
            )
            self._group_commands = AstrBotGroupCommands(
                group_scopes, group_lifecycle, group_memories, group_recall, imports,
                retention_days=self._config_int("group_retention_days", 90),
                backup_retention_days=destroy_backup_retention,
            )
            self._extraction_commands = AstrBotExtractionCommands(
                scopes, consent, extraction,
            )
            self._backup_commands = AstrBotBackupCommands(backup)
            self._extraction_capture = AstrBotExtractionCapture(
                scopes, extraction, enabled=extraction_enabled,
                min_messages=self._config_int("extraction_min_messages", 5),
                notifier=self._notify_extraction_candidates,
            )
            self._injector = AstrBotMemoryInjector(
                scopes, consent, composer, auto_enroll=auto_enroll,
            )
            self._group_injector = AstrBotGroupMemoryInjector(
                group_scopes, consent, group_composer,
            )
            api_admin_usernames = self._config_list("api_admin_usernames")
            management = ManagementService(
                repository=store, store=store, index=index, groups=group_scopes,
                group_recall=group_recall, consent=consent,
                imports=imports, group_memories=group_memories,
                backup=backup,
                vector_rebuild=index_rebuild,
                vector_enabled=provider_config.vector_index_enabled,
                settings={
                    "auto_enroll": auto_enroll,
                    "agent_write_enabled": self._config_bool("agent_write_enabled", True),
                    "extraction_enabled": extraction_enabled,
                    "extraction_min_messages": self._config_int(
                        "extraction_min_messages", 5,
                    ),
                    "extraction_max_retries": self._config_int("extraction_max_retries", 3),
                    "extraction_timeout_seconds": self._config_int(
                        "extraction_timeout_seconds", 30,
                    ),
                    "extraction_queue_size": self._config_int("extraction_queue_size", 100),
                    "candidate_ttl_days": self._config_int("candidate_ttl_days", 7),
                    "archive_retention_days": archive_retention_days,
                    "purge_batch_size": self._config_int("purge_batch_size", 100),
                    "purge_interval_seconds": self._config_int(
                        "purge_interval_seconds", 3600,
                    ),
                    "hybrid_retrieval_enabled": provider_config.hybrid_retrieval_enabled,
                    "vector_index_enabled": provider_config.vector_index_enabled,
                    "rerank_enabled": provider_config.rerank_enabled,
                    "query_classifier_mode": provider_config.query_classifier_mode,
                    "tag_candidate_limit": provider_config.tag_candidate_limit,
                    "time_candidate_limit": provider_config.time_candidate_limit,
                    "vector_relevance_threshold": (
                        provider_config.vector_relevance_threshold
                    ),
                    "fts_relevance_ratio": provider_config.fts_relevance_ratio,
                    "vector_snapshot_cache_mb": (
                        provider_config.vector_snapshot_cache_mb
                    ),
                    "embedding_provider_id": provider_config.embedding_provider_id,
                    "embedding_model": provider_config.embedding_model,
                    "external_provider_opt_in": provider_config.external_provider_opt_in,
                    "provider_status": (
                        provider_policy.status() if provider_policy is not None
                        else {"available": False}
                    ),
                    "vector_index_status": (
                        {
                            "target": vector_index.status().target.value,
                            "status": vector_index.status().status.value,
                            "dim": vector_index.status().dim,
                            "cache": vector_index.snapshot_cache_stats(),
                        }
                        if vector_index is not None and vector_index.status() is not None
                        else None
                    ),
                    "index_jobs": (
                        index_jobs.count_by_status() if index_jobs is not None else {}
                    ),
                    "rejected_keep_body": self._config_bool("rejected_keep_body", False),
                    "personal_max_tokens": self._config_int("personal_max_tokens", 384),
                    "personal_max_memories": self._config_non_negative_int(
                        "personal_max_memories", 4,
                    ),
                    "personal_candidate_limit": self._config_int(
                        "personal_candidate_limit", 12,
                    ),
                    "group_max_tokens": self._config_int("group_max_tokens", 192),
                    "group_max_memories": self._config_non_negative_int(
                        "group_max_memories", 2,
                    ),
                    "group_candidate_limit": self._config_int("group_candidate_limit", 8),
                    "group_max_active": self._config_int("group_max_active", 200),
                    "group_retention_days": self._config_int("group_retention_days", 90),
                    "group_destroy_backup_retention_days": destroy_backup_retention,
                    "recall_diagnostics_enabled": diagnostics_enabled,
                    "recall_diagnostics_retention_days": self._config_int(
                        "recall_diagnostics_retention_days", 7,
                    ),
                    "recall_diagnostics_include_query": self._config_bool(
                        "recall_diagnostics_include_query", False,
                    ),
                    "api_admin_whitelist_enabled": bool(api_admin_usernames),
                    "ledger_first_enabled": ledger_enabled,
                    "ledger_rotate_events": self._config_non_negative_int(
                        "ledger_rotate_events", 0,
                    ),
                    "backup_keep_last": self._config_int("backup_keep_last", 5),
                    "backup_retention_days": self._config_int(
                        "backup_retention_days", 30,
                    ),
                },
                group_max_tokens=self._config_int("group_max_tokens", 192),
                group_max_memories=self._config_non_negative_int(
                    "group_max_memories", 2,
                ),
            )
            management_api = AstrBotManagementApi(
                management, admin_usernames=api_admin_usernames,
            )
            try:
                registered = management_api.register(self.context)
            except Exception as exc:  # noqa: BLE001 - API must not block plugin startup
                logger.warning(f"织忆管理 API 注册失败：{type(exc).__name__}")
            else:
                if registered:
                    logger.info("织忆管理 API 已注册。")
            self._ready = True
        except (OSError, RuntimeError, ValueError, StorageError, IndexUnavailable) as exc:
            logger.error(f"织忆初始化失败，记忆功能暂不可用：{type(exc).__name__}")
            if self._database is not None:
                self._database.close()
                self._database = None
            if self._ledger_store is not None:
                self._ledger_store.close()
                self._ledger_store = None

    def _command_service(self) -> AstrBotMemoryCommands:
        if not self._ready or self._commands is None:
            raise ServiceClosed("memory engine is not ready")
        return self._commands

    def _group_command_service(self) -> AstrBotGroupCommands:
        if not self._ready or self._group_commands is None:
            raise ServiceClosed("memory engine is not ready")
        return self._group_commands

    def _extraction_command_service(self) -> AstrBotExtractionCommands:
        if not self._ready or self._extraction_commands is None:
            raise ServiceClosed("memory engine is not ready")
        return self._extraction_commands

    def _backup_command_service(self) -> AstrBotBackupCommands:
        if not self._ready or self._backup_commands is None:
            raise ServiceClosed("memory engine is not ready")
        return self._backup_commands

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

    def _config_non_negative_int(self, key: str, default: int) -> int:
        if not hasattr(self._config, "get"):
            return default
        try:
            value = int(self._config.get(key, default))  # type: ignore[union-attr]
        except (TypeError, ValueError):
            return default
        return value if value >= 0 else default

    def _config_list(self, key: str) -> tuple[str, ...]:
        if not hasattr(self._config, "get"):
            return ()
        raw = self._config.get(key, [])  # type: ignore[union-attr]
        if not isinstance(raw, list):
            return ()
        return tuple(
            item.strip() for item in raw
            if isinstance(item, str) and item.strip()
        )

    @staticmethod
    def _error_text(action: str, exc: Exception) -> str:
        if isinstance(exc, ConsentRevocationPending):
            return "个人记忆空间已关闭，数据已停止读写；但授权撤销记录写入失败，请重试 /织忆 关闭 确认。"
        if isinstance(exc, ServiceClosed):
            return "织忆当前未就绪，请检查插件日志。"
        if isinstance(exc, InvalidTransition):
            return f"{action}失败：当前状态不允许该操作。"
        if isinstance(exc, AuthorizationDenied):
            return f"{action}失败：尚未授权或个人记忆空间已关闭。"
        if isinstance(exc, NotFound):
            return f"{action}失败：没有找到对应的个人记忆或作用域。"
        if isinstance(exc, IndexUnavailable):
            return f"{action}失败：检索索引暂不可用，正文数据不会因此丢失。"
        if isinstance(exc, (Conflict, ValidationError)):
            return f"{action}失败：{exc}。"
        return f"{action}失败：{type(exc).__name__}。"

    @staticmethod
    async def _notify_extraction_candidates(
        event: AstrMessageEvent, candidates: tuple[MemoryEntity, ...],
    ) -> None:
        if not candidates:
            return
        lines = [
            f"{memory.id} · {memory.memory_type.value} · {memory.content[:80]}"
            for memory in candidates
        ]
        text = (
            "织忆提取到候选记忆：\n" + "\n".join(lines)
            + "\n用 /织忆 候选 列表 查看；"
            "/织忆 候选 确认 <ID> 或 /织忆 候选 拒绝 <ID> [原因]。"
        )
        try:
            await event.send(event.plain_result(text))
        except Exception as exc:  # Notification is best-effort.
            logger.warning(f"织忆候选通知发送失败：{type(exc).__name__}")

    @filter.command_group("织忆", alias={"/织忆"})
    def memory(self):
        """个人记忆管理命令组。"""

    @memory.command("帮助")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def memory_help(self, event: AstrMessageEvent):
        if not self._ready or self._commands is None:
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

    @memory.command("检索调试")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def memory_retrieval_debug(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(
                await self._command_service().retrieval_debug(event),
            )
        except _COMMAND_ERRORS as exc:
            yield event.plain_result(self._error_text("检索调试", exc))

    @memory.command("状态")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def memory_status(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(self._command_service().status(event))
        except _COMMAND_ERRORS as exc:
            yield event.plain_result(self._error_text("状态", exc))

    @memory.command("统计")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def memory_stats(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(self._command_service().stats(event))
        except _COMMAND_ERRORS as exc:
            yield event.plain_result(self._error_text("统计", exc))

    @memory.command("操作记录")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def memory_operations(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(self._command_service().operations(event))
        except _COMMAND_ERRORS as exc:
            yield event.plain_result(self._error_text("操作记录", exc))

    @memory.command("备份")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def memory_backup(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(self._backup_command_service().backup(event))
        except _COMMAND_ERRORS as exc:
            yield event.plain_result(self._error_text("备份", exc))

    @memory.command("备份恢复")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def memory_backup_restore(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(self._backup_command_service().restore(event))
        except _COMMAND_ERRORS as exc:
            yield event.plain_result(self._error_text("备份恢复", exc))

    @memory.command("恢复")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def memory_restore(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(await self._command_service().restore_memory(event))
        except _COMMAND_ERRORS as exc:
            yield event.plain_result(self._error_text("恢复", exc))

    @memory.command("归档")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def memory_archive(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(await self._command_service().archive(event))
        except _COMMAND_ERRORS as exc:
            yield event.plain_result(self._error_text("归档", exc))

    @memory.command("彻底删除")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def memory_purge(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(await self._command_service().purge(event))
        except _COMMAND_ERRORS as exc:
            yield event.plain_result(self._error_text("彻底删除", exc))

    @memory.command("导出")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def memory_export(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(self._command_service().export_command(event))
        except _COMMAND_ERRORS as exc:
            yield event.plain_result(self._error_text("导出", exc))

    @memory.command("导入")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def memory_import(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(await self._command_service().import_command(event))
        except _COMMAND_ERRORS as exc:
            yield event.plain_result(self._error_text("导入", exc))

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

    @memory.command("提取")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def extraction_control(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(
                await self._extraction_command_service().handle_extract(event)
            )
        except _COMMAND_ERRORS as exc:
            yield event.plain_result(self._error_text("提取", exc))

    @memory.command("候选")
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def extraction_candidates(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(
                await self._extraction_command_service().handle_candidate(event)
            )
        except _COMMAND_ERRORS as exc:
            yield event.plain_result(self._error_text("候选", exc))

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def extraction_capture_event(self, event: AstrMessageEvent):
        if self._extraction_capture is not None:
            await self._extraction_capture.capture(event)

    @_hook("after_message_sent")()
    async def extraction_flush_event(self, event: AstrMessageEvent):
        if self._extraction_capture is not None:
            await self._extraction_capture.flush(event)

    @_hook("on_using_llm_tool")()
    async def weave_bind_tool_call(self, event: AstrMessageEvent, tool, tool_args):
        """Verify that the invoked handler is registered by this plugin."""
        self._tool_call_binding.bind_tool_start(event, tool, tool_args)

    @_hook("on_llm_tool_respond")()
    async def weave_clear_tool_call(self, event: AstrMessageEvent, tool, tool_args, tool_result):
        self._tool_call_binding.clear_active(event)

    def _consume_tool_binding(self, event: AstrMessageEvent, tool_name: str) -> bool:
        return self._tool_call_binding.consume(event, expected_name=tool_name)

    @_llm_tool(name="weave_memory_remember")
    async def memory_remember_tool(
        self, event: AstrMessageEvent, content: str = "", memory_type: str = "FACT",
        requested_persistence: bool = True, reason: str = "",
    ):
        """织忆个人记忆写入入口。

        只在用户明确要求记住某件长期事实时调用；只有本工具返回“已保存记忆”后才能对用户
        声称已保存，未调用或失败时必须如实说明未保存。一次只保存一条简洁事实，不保存
        完整聊天原文；不得保存密码、密钥等敏感信息。

        Args:
            content(string): 要保存的事实。
            memory_type(string): PREFERENCE、FACT 或 EVENT，默认 FACT。
            requested_persistence(boolean): 本次是否请求长期持久化，默认 true。
            reason(string): 可选的写入理由，仅用于审计。
        """
        if not self._config_bool("agent_write_enabled", True):
            yield "未保存：织忆的模型记忆写入已关闭。"
            return
        if not self._consume_tool_binding(event, "weave_memory_remember"):
            yield "未保存：无法确认织忆插件的工具调用身份。"
            return
        try:
            yield await self._command_service().remember(
                event, content, memory_type,
                requested_persistence=requested_persistence, reason=reason,
            )
        except _COMMAND_ERRORS as exc:
            yield self._error_text("保存", exc)
        except Exception as exc:
            logger.warning(f"织忆 weave_memory_remember 失败：{type(exc).__name__}")
            yield "未保存：记忆服务暂不可用。"

    @_llm_tool(name="weave_memory_search")
    async def memory_search_tool(self, event: AstrMessageEvent, query: str = "", limit: int = 8):
        """在当前用户的 Personal 记忆中检索相关内容；不允许指定作用域。

        Args:
            query(string): 用户当前明确需要查询的内容。
            limit(int): 返回数量，1 到 20，默认 8。
        """
        if not self._consume_tool_binding(event, "weave_memory_search"):
            yield "检索失败：无法确认织忆插件的工具调用身份。"
            return
        try:
            yield await self._command_service().agent_search(event, query, limit=limit)
        except _COMMAND_ERRORS as exc:
            yield self._error_text("检索", exc)
        except Exception as exc:
            logger.warning(f"织忆 weave_memory_search 失败：{type(exc).__name__}")
            yield "检索失败：记忆服务暂不可用。"

    @_llm_tool(name="weave_memory_get")
    async def memory_get_tool(self, event: AstrMessageEvent, memory_id: str = ""):
        """按 ID 读取当前用户 Personal 记忆；跨用户或无授权时拒绝。

        Args:
            memory_id(string): 记忆 ID。
        """
        if not self._consume_tool_binding(event, "weave_memory_get"):
            yield "读取失败：无法确认织忆插件的工具调用身份。"
            return
        try:
            yield await self._command_service().agent_get(event, memory_id)
        except _COMMAND_ERRORS as exc:
            yield self._error_text("读取", exc)
        except Exception as exc:
            logger.warning(f"织忆 weave_memory_get 失败：{type(exc).__name__}")
            yield "读取失败：记忆服务暂不可用。"

    @_llm_tool(name="weave_memory_update")
    async def memory_update_tool(
        self, event: AstrMessageEvent, memory_id: str = "", expected_revision: int = 0,
        content: str = "", reason: str = "",
    ):
        """Agent Personal 记忆更新入口。

        只在用户明确要求修改某条记忆时调用；必须携带用户确认过的当前 revision，避免覆盖
        并发修改；只改正文，不改类型。只有本工具返回“已更新记忆”后才能对用户声称已修改。

        Args:
            memory_id(string): 要修改的记忆 ID。
            expected_revision(int): 用户确认的当前 revision，用于防止覆盖。
            content(string): 修改后的完整记忆内容。
            reason(string): 可选的修改理由，仅用于审计。
        """
        if not self._config_bool("agent_write_enabled", True):
            yield "未更新：织忆的模型记忆写入已关闭。"
            return
        if not self._consume_tool_binding(event, "weave_memory_update"):
            yield "未更新：无法确认织忆插件的工具调用身份。"
            return
        try:
            yield await self._command_service().agent_update(
                event, memory_id, expected_revision, content, reason=reason,
            )
        except _COMMAND_ERRORS as exc:
            yield self._error_text("更新", exc)
        except Exception as exc:
            logger.warning(f"织忆 weave_memory_update 失败：{type(exc).__name__}")
            yield "未更新：记忆服务暂不可用。"

    @_llm_tool(name="weave_memory_list")
    async def memory_list_tool(
        self, event: AstrMessageEvent, offset: int = 0, limit: int = 20,
        memory_type: str = "",
    ):
        """分页列出当前用户的 Personal 记忆，可按类型过滤。

        Args:
            offset(int): 从 0 开始的偏移量。
            limit(int): 每页数量，1 到 100，默认 20。
            memory_type(string): 可选 PREFERENCE、FACT 或 EVENT；空字符串表示全部。
        """
        if not self._consume_tool_binding(event, "weave_memory_list"):
            yield "读取失败：无法确认织忆插件的工具调用身份。"
            return
        try:
            yield await self._command_service().agent_list(
                event, offset=offset, limit=limit, memory_type=memory_type,
            )
        except _COMMAND_ERRORS as exc:
            yield self._error_text("读取", exc)
        except Exception as exc:
            logger.warning(f"织忆 weave_memory_list 失败：{type(exc).__name__}")
            yield "读取失败：记忆服务暂不可用。"

    @_llm_tool(name="weave_memory_history")
    async def memory_history_tool(self, event: AstrMessageEvent, memory_id: str = ""):
        """查看当前用户指定记忆的版本与来源历史；不跨作用域读取。

        Args:
            memory_id(string): 记忆 ID。
        """
        if not self._consume_tool_binding(event, "weave_memory_history"):
            yield "读取失败：无法确认织忆插件的工具调用身份。"
            return
        try:
            yield await self._command_service().agent_history(event, memory_id)
        except _COMMAND_ERRORS as exc:
            yield self._error_text("读取历史", exc)
        except Exception as exc:
            logger.warning(f"织忆 weave_memory_history 失败：{type(exc).__name__}")
            yield "读取失败：记忆服务暂不可用。"

    @memory.command("群帮助")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def memory_group_help(self, event: AstrMessageEvent):
        if not self._ready or self._group_commands is None:
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

    @memory.command("群导出")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def memory_group_export(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(self._group_command_service().export(event))
        except _COMMAND_ERRORS as exc:
            yield event.plain_result(AstrBotGroupCommands._error_text("群导出", exc))

    @memory.command("群导入")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def memory_group_import(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(
                await self._group_command_service().import_command(event),
            )
        except _COMMAND_ERRORS as exc:
            yield event.plain_result(AstrBotGroupCommands._error_text("群导入", exc))

    @filter.on_llm_request()
    async def inject_personal_memory(self, event: AstrMessageEvent, req: ProviderRequest):
        """Append only temporary context; never mutate persistent chat history."""
        if not self._ready or self._injector is None:
            return
        try:
            # Hosts without is_private_chat fall through to the group injector,
            # which fails closed for personal scopes: no memory is injected.
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

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_lifecycle_notice(self, event: AstrMessageEvent):
        """Disable group memory when the bot is removed or the group dissolves."""
        if (
            not self._ready
            or self._group_scopes is None
            or self._group_lifecycle is None
        ):
            return
        message_obj = getattr(event, "message_obj", None)
        raw_message = getattr(message_obj, "raw_message", None)
        self_id = None
        get_self_id = getattr(event, "get_self_id", None)
        if callable(get_self_id):
            try:
                self_id = get_self_id()
            except Exception:  # noqa: BLE001 - host API variance
                self_id = None
        if self_id is None:
            self_id = getattr(message_obj, "self_id", None)
        if parse_group_lifecycle_event(raw_message, self_id) is None:
            return
        try:
            identity = AstrBotIdentityAdapter().resolve_group_identity(event)
        except AuthorizationDenied:
            return
        scope = self._group_scopes.find(identity)
        if scope is None:
            return
        try:
            await self._group_lifecycle.disable_system(
                scope, operator_id="weave_group_lifecycle",
            )
            logger.info("织忆群记忆已因退群/解散自动关闭：%s", scope.id)
        except Exception as exc:  # noqa: BLE001 - lifecycle must not break host
            logger.warning(
                "织忆群生命周期自动关闭失败：%s", type(exc).__name__,
            )
        stop_event = getattr(event, "stop_event", None)
        if callable(stop_event):
            stop_event()

    async def _scheduled_backup_loop(self, backup: BackupService,
                                     interval_seconds: int) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                result = await asyncio.to_thread(backup.create_backup)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - background maintenance
                logger.warning(
                    "织忆定时备份失败：%s", type(exc).__name__,
                )
            else:
                logger.info("织忆定时备份完成：%s", result.get("backup_id"))

    async def terminate(self):
        if self._scheduled_backup_task is not None:
            self._scheduled_backup_task.cancel()
            try:
                await self._scheduled_backup_task
            except asyncio.CancelledError:
                pass
            self._scheduled_backup_task = None
        if self._index_worker is not None:
            try:
                await asyncio.wait_for(
                    self._index_worker.stop(), timeout=_TERMINATE_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning("织忆热重载收尾超时：向量索引 worker 未在限定时间内停止。")
            self._index_worker = None
        if self._retention is not None:
            try:
                await asyncio.wait_for(
                    self._retention.stop(), timeout=_TERMINATE_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning("织忆热重载收尾超时：到期清理任务未在限定时间内停止。")
            self._retention = None
        if self._extraction_capture is not None:
            try:
                await asyncio.wait_for(
                    self._extraction_capture.close(), timeout=_TERMINATE_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning("织忆热重载收尾超时：提取任务未在限定时间内取消。")
            self._extraction_capture = None
        if self._coordinator is not None:
            try:
                await asyncio.wait_for(
                    self._coordinator.close(), timeout=_TERMINATE_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning("织忆热重载收尾超时：写协调器未在限定时间内关闭，继续释放连接。")
        if self._database is not None:
            try:
                self._database.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                logger.warning(f"织忆关闭数据库失败：{type(exc).__name__}")
            self._database = None
        if self._ledger_store is not None:
            try:
                self._ledger_store.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                logger.warning(f"织忆关闭账本失败：{type(exc).__name__}")
            self._ledger_store = None
        self._ready = False
