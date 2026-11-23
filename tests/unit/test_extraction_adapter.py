from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from data.plugins.astrbot_plugin_weave_memory.adapters.astrbot.extraction import (
    AstrBotExtractionCapture,
    parse_extraction_response,
)
from data.plugins.astrbot_plugin_weave_memory.core.model import (
    MemoryType,
)

ALLOWED = (MemoryType.PREFERENCE, MemoryType.FACT, MemoryType.EVENT)

class FakeMessage:
    def __init__(self, message_id: str = "msg-1") -> None:
        self.message_id = message_id


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


class FakeScopes:
    def __init__(self, scope) -> None:
        self._scope = scope
        self.calls = 0

    def find(self, identity):
        self.calls += 1
        return self._scope


class FakeExtraction:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def gates(self, scope):
        return SimpleNamespace(can_store=True)

    async def submit_messages(self, scope, *, conversation_id, messages, watermark=0):
        self.calls.append((scope, conversation_id, messages, watermark))
        return None


def test_parse_extraction_response_filters_types_and_sensitive_content():
    payload = """```json
    [
      {"type": "PREFERENCE", "content": "用户喜欢黑咖啡", "confidence": 0.8},
      {"type": "PLAN", "content": "下周去北京"},
      {"type": "FACT", "content": "用户密码是 hunter2"},
      {"type": "EVENT", "content": "用户昨天完成了迁移", "confidence": 3}
    ]
    ```"""
    candidates = parse_extraction_response(
        payload, allowed_types=ALLOWED, sensitive_categories=(),
    )
    assert [candidate.content for candidate in candidates] == [
        "用户喜欢黑咖啡", "用户昨天完成了迁移",
    ]
    assert candidates[0].confidence == pytest.approx(0.8)
    assert candidates[1].confidence is None


def test_parse_extraction_response_accepts_candidates_object_and_drops_invalid():
    payload = '{"candidates": [{"type": "fact", "content": "用户住在杭州"}]}'
    candidates = parse_extraction_response(
        payload, allowed_types=ALLOWED, sensitive_categories=(),
    )
    assert len(candidates) == 1
    assert candidates[0].memory_type is MemoryType.FACT


def test_parse_extraction_response_rejects_non_array():
    with pytest.raises(ValueError, match="JSON array"):
        parse_extraction_response("not json", allowed_types=ALLOWED, sensitive_categories=())


def test_capture_ignores_group_commands_and_disabled(monkeypatch):
    scope = SimpleNamespace(id="scope")
    extraction = FakeExtraction()
    scopes = FakeScopes(scope)

    async def run(event, *, enabled=True):
        capture = AstrBotExtractionCapture(scopes, extraction, enabled=enabled)
        await capture.capture(event)
        await asyncio.sleep(0.05)
        await capture.close()

    asyncio.run(run(FakeEvent("我喜欢黑咖啡", private=False, group_id="g1")))
    asyncio.run(run(FakeEvent("/织忆 提取 状态")))
    asyncio.run(run(FakeEvent("我喜欢黑咖啡"), enabled=False))
    assert extraction.calls == []

    asyncio.run(run(FakeEvent("我喜欢黑咖啡")))
    assert len(extraction.calls) == 1
    _, conversation_id, messages, _watermark = extraction.calls[0]
    assert conversation_id == "astrbot:FriendMessage:session-1"
    assert messages == (("msg-1", "我喜欢黑咖啡"),)


def test_flush_submits_buffer_on_turn_end_and_clears_gate_failure():
    scope = SimpleNamespace(id="scope")
    extraction = FakeExtraction()
    scopes = FakeScopes(scope)

    async def run():
        capture = AstrBotExtractionCapture(
            scopes, extraction, min_messages=5,
        )
        await capture.capture(FakeEvent("第一句"))
        assert extraction.calls == []
        await capture.flush(FakeEvent(""))
        await asyncio.sleep(0.05)
        assert len(extraction.calls) == 1
        _, _, messages, watermark = extraction.calls[0]
        assert messages == (("msg-1", "第一句"),)
        assert watermark == 1

        extraction.can_store = False
        extraction.gates = lambda scope: SimpleNamespace(can_store=extraction.can_store)
        await capture.capture(FakeEvent("第二句"))
        await capture.flush(FakeEvent(""))
        await asyncio.sleep(0.05)
        await capture.close()

    asyncio.run(run())
    assert len(extraction.calls) == 1


def test_capture_discards_messages_buffered_before_opt_in():
    scope = SimpleNamespace(id="scope")
    extraction = FakeExtraction()
    extraction.can_store = False
    extraction.gates = lambda scope: SimpleNamespace(can_store=extraction.can_store)
    scopes = FakeScopes(scope)

    async def run(capture, texts):
        for index, text in enumerate(texts, start=1):
            event = FakeEvent(text)
            event.message_obj.message_id = f"msg-{index}"
            await capture.capture(event)
        await asyncio.sleep(0.05)

    async def scenario():
        capture = AstrBotExtractionCapture(scopes, extraction, min_messages=3)
        await run(capture, ("opt-in 前的第一句", "opt-in 前的第二句"))
        extraction.can_store = True
        await run(capture, ("记住 现在这句",))
        await capture.close()

    asyncio.run(scenario())
    assert len(extraction.calls) == 1
    _, _, messages, _ = extraction.calls[0]
    assert messages == (("msg-1", "记住 现在这句"),)


def test_capture_batches_until_min_messages_or_remember():
    scope = SimpleNamespace(id="scope")
    extraction = FakeExtraction()
    scopes = FakeScopes(scope)

    async def run(*texts):
        capture = AstrBotExtractionCapture(
            scopes, extraction, min_messages=3,
        )
        for index, text in enumerate(texts, start=1):
            event = FakeEvent(text)
            event.message_obj.message_id = f"msg-{index}"
            await capture.capture(event)
        await asyncio.sleep(0.05)
        await capture.close()

    asyncio.run(run("第一句", "第二句"))
    assert extraction.calls == []
    asyncio.run(run("第一句", "第二句", "记住 这个"))
    assert len(extraction.calls) == 1
    _, _, messages, watermark = extraction.calls[0]
    assert messages == (
        ("msg-1", "第一句"), ("msg-2", "第二句"), ("msg-3", "记住 这个"),
    )
    assert watermark == 3
