from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from data.plugins.astrbot_plugin_weave_memory.adapters.astrbot.extraction_commands import (
    AstrBotExtractionCommands,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.astrbot.identity import (
    AstrBotIdentityAdapter,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.consents import (
    SQLiteConsentStore,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.database import (
    SQLiteDatabase,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.extraction_jobs import (
    SQLiteExtractionJobStore,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.fts import SQLiteFTSIndex
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.store import (
    SQLiteMemoryStore,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.system import UUID4IdGenerator
from data.plugins.astrbot_plugin_weave_memory.app.consent_service import (
    ConsentService,
    ExtractionConsentType,
    PersonalConsentType,
)
from data.plugins.astrbot_plugin_weave_memory.app.extraction_service import (
    ExtractionService,
)
from data.plugins.astrbot_plugin_weave_memory.app.lifecycle_service import (
    LifecycleService,
)
from data.plugins.astrbot_plugin_weave_memory.app.scope_service import (
    PersonalScopeService,
)
from data.plugins.astrbot_plugin_weave_memory.app.write_coordinator import (
    WriteCoordinator,
)
from data.plugins.astrbot_plugin_weave_memory.core.errors import (
    AuthorizationDenied,
    NotFound,
)
from data.plugins.astrbot_plugin_weave_memory.core.model import (
    ExtractionCandidate,
    MemoryStatus,
    MemoryType,
)

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


class FixedClock:
    def now(self) -> datetime:
        return NOW


class FakeExtractor:
    def __init__(self, candidates=()) -> None:
        self.candidates = tuple(candidates)

    async def extract(self, window, *, allowed_types, sensitive_categories):
        return self.candidates


class FakeMessage:
    message_id = "msg-1"


class FakeEvent:
    def __init__(self, text: str, *, private: bool = True, group_id: str = "") -> None:
        self.message_str = text
        self.message_obj = FakeMessage()
        self.unified_msg_origin = "astrbot:FriendMessage:session-1"
        self._private = private
        self._group_id = group_id

    def is_private_chat(self) -> bool:
        return self._private

    def get_group_id(self):
        return self._group_id

    def get_platform_name(self) -> str:
        return "astrbot"

    def get_platform_id(self) -> str:
        return "default"

    def get_sender_id(self) -> str:
        return "u1"


@pytest.fixture
def command_stack(tmp_path):
    db = SQLiteDatabase(tmp_path / "commands.db")
    db.migrate()
    store = SQLiteMemoryStore(db)
    index = SQLiteFTSIndex(db)
    clock = FixedClock()
    ids = UUID4IdGenerator()
    coordinator = WriteCoordinator(store, index)
    consent = ConsentService(SQLiteConsentStore(db), coordinator, clock, ids)
    lifecycle = LifecycleService(coordinator, store, consent)
    jobs = SQLiteExtractionJobStore(db)
    scopes = PersonalScopeService(store, coordinator, clock)
    event = FakeEvent("/织忆 提取 状态")
    scope = scopes.resolve(AstrBotIdentityAdapter().resolve_identity(event))
    consent.grant(scope, PersonalConsentType.PERSONAL_MEMORY, operator_id="u1")
    extractor = FakeExtractor()
    service = ExtractionService(
        store=store, jobs=jobs, coordinator=coordinator, consent=consent,
        scopes=store, lifecycle=lifecycle, extractor=extractor,
        clock=clock, ids=ids, global_enabled=True,
    )
    commands = AstrBotExtractionCommands(scopes, consent, service)
    yield {
        "db": db, "store": store, "consent": consent, "scope": scope,
        "service": service, "commands": commands, "extractor": extractor,
    }
    db.close()


def test_enable_all_and_status_show_effective_gates(command_stack):
    stack = command_stack
    event = FakeEvent("/织忆 提取 开启 全部")
    text = asyncio.run(stack["commands"].handle_extract(event))
    assert "已开启：捕获、自动提取、外部模型、保存候选" in text
    for consent_type in (
        ExtractionConsentType.CAPTURE,
        ExtractionConsentType.AUTO_EXTRACT,
        ExtractionConsentType.EXTERNAL_MODEL,
        ExtractionConsentType.STORE_CANDIDATE,
    ):
        assert stack["consent"].is_granted(stack["scope"], consent_type) is True

    status = asyncio.run(stack["commands"].handle_extract(FakeEvent("/织忆 提取 状态")))
    assert "当前有效：捕获=开；提取=开；保存候选=开" in status
    assert "缺失条件" not in status


def test_disabling_external_model_keeps_auto_extract_consent(command_stack):
    stack = command_stack
    asyncio.run(stack["commands"].handle_extract(FakeEvent("/织忆 提取 开启 全部")))
    text = asyncio.run(stack["commands"].handle_extract(FakeEvent("/织忆 提取 关闭 external_model")))
    assert "外部模型" in text
    assert stack["consent"].is_granted(stack["scope"], ExtractionConsentType.AUTO_EXTRACT) is True
    status = asyncio.run(stack["commands"].handle_extract(FakeEvent("/织忆 提取 状态")))
    assert "当前有效：捕获=开；提取=关；保存候选=关" in status


def test_sensitive_categories_command_sets_and_clears_whitelist(command_stack):
    stack = command_stack
    asyncio.run(stack["commands"].handle_extract(FakeEvent("/织忆 提取 开启 全部")))
    text = asyncio.run(stack["commands"].handle_extract(FakeEvent("/织忆 提取 敏感 credential")))
    assert "credential" in text
    assert stack["consent"].sensitive_categories(stack["scope"]) == ("credential",)
    text = asyncio.run(stack["commands"].handle_extract(FakeEvent("/织忆 提取 敏感 清空")))
    assert "已清空" in text
    assert stack["consent"].sensitive_categories(stack["scope"]) == ()


def test_candidate_list_confirm_and_reject(command_stack):
    stack = command_stack
    asyncio.run(stack["commands"].handle_extract(FakeEvent("/织忆 提取 开启 全部")))
    stack["extractor"].candidates = (
        ExtractionCandidate(MemoryType.PREFERENCE, "用户喜欢黑咖啡"),
    )
    asyncio.run(stack["service"].submit(
        stack["scope"], conversation_id="conv", message_id="msg", content="我喜欢黑咖啡",
    ))
    listing = asyncio.run(stack["commands"].handle_candidate(FakeEvent("/织忆 候选 列表")))
    assert "待确认候选：1 条" in listing
    candidate_id = listing.splitlines()[1].split(" · ")[0]

    confirmed = asyncio.run(stack["commands"].handle_candidate(
        FakeEvent(f"/织忆 候选 确认 {candidate_id}"),
    ))
    assert "已确认候选" in confirmed
    memory = stack["store"].get(
        candidate_id, scope_id=stack["scope"].id, subject_id="u1",
    )
    assert memory.status is MemoryStatus.ACTIVE

    stack["extractor"].candidates = (
        ExtractionCandidate(MemoryType.FACT, "用户住在杭州"),
    )
    asyncio.run(stack["service"].submit(
        stack["scope"], conversation_id="conv-2", message_id="msg-2", content="我住在杭州",
    ))
    listing = asyncio.run(stack["commands"].handle_candidate(FakeEvent("/织忆 候选 列表")))
    candidate_id = listing.splitlines()[1].split(" · ")[0]
    rejected = asyncio.run(stack["commands"].handle_candidate(
        FakeEvent(f"/织忆 候选 拒绝 {candidate_id} 不准确"),
    ))
    assert "已拒绝候选" in rejected
    with pytest.raises(NotFound):
        stack["store"].get(
            candidate_id, scope_id=stack["scope"].id, subject_id="u1",
        )


def test_group_command_is_rejected(command_stack):
    stack = command_stack
    event = FakeEvent("/织忆 提取 状态", private=False, group_id="g1")
    with pytest.raises(AuthorizationDenied):
        asyncio.run(stack["commands"].handle_extract(event))
