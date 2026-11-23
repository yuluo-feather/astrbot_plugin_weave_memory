"""AstrBot adapter for V1.5 extraction: provider port, capture, and response parsing."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from ...app.extraction_service import ExtractionService
from ...app.sensitive_filter import scan_sensitive
from ...core.errors import AuthorizationDenied
from ...core.model import (
    ExtractionCandidate,
    ExtractionWindow,
    MemoryType,
)
from .identity import AstrBotIdentityAdapter

_SYSTEM_PROMPT = (
    "你是织忆的记忆提取器。只输出 JSON 数组，不要解释或 Markdown。"
    "每个元素格式："
    '{"type":"PREFERENCE|FACT|EVENT","content":"...","confidence":0.0,'
    '"source_message_ids":["..."]}。'
    "只提取用户明确陈述、长期有用的信息；不要提取玩笑、临时信息、凭据或敏感个人信息、"
    "助手陈述或工具输出。没有可提取内容时输出 []。"
)


def build_extraction_prompt(window: ExtractionWindow) -> str:
    joined = "\n".join(window.messages)
    if len(joined) > 4000:
        joined = joined[-4000:]
    return f"对话窗口（仅用户消息）：\n{joined}\n\n请输出候选记忆 JSON 数组。"


def parse_extraction_response(
    text: str, *, allowed_types: tuple[MemoryType, ...],
    sensitive_categories: tuple[str, ...],
) -> tuple[ExtractionCandidate, ...]:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lstrip().startswith("json"):
            raw = raw.lstrip()[4:]
    start = raw.find("[")
    end = raw.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("model response does not contain a JSON array")
    try:
        payload = json.loads(raw[start:end + 1])
    except (TypeError, ValueError) as exc:
        raise ValueError("model response is not valid JSON") from exc
    if isinstance(payload, dict):
        payload = payload.get("candidates", [])
    if not isinstance(payload, list):
        raise ValueError("model response is not a JSON array")

    candidates: list[ExtractionCandidate] = []
    for item in payload[:20]:
        if not isinstance(item, dict):
            continue
        try:
            memory_type = MemoryType(str(item.get("type", "")).upper())
        except ValueError:
            continue
        if memory_type not in allowed_types:
            continue
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        raw_confidence = item.get("confidence")
        confidence: float | None = None
        if raw_confidence is not None:
            try:
                parsed = float(raw_confidence)
            except (TypeError, ValueError):
                parsed = -1.0
            if 0.0 <= parsed <= 1.0:
                confidence = parsed
        raw_ids = item.get("source_message_ids")
        source_ids: tuple[str, ...] = ()
        if isinstance(raw_ids, (list, tuple)):
            source_ids = tuple(
                str(value) for value in raw_ids
                if isinstance(value, (str, int)) and str(value).strip()
            )
        if scan_sensitive(content, sensitive_categories).is_blocked:
            continue
        candidates.append(ExtractionCandidate(
            memory_type=memory_type, content=content,
            confidence=confidence, source_message_ids=source_ids,
        ))
    return tuple(candidates)


class AstrBotLlmExtractor:
    """Duck-typed provider adapter; no AstrBot imports leak into core."""

    def __init__(self, context: Any) -> None:
        self._context = context

    async def extract(
        self, window: ExtractionWindow, *,
        allowed_types: tuple[MemoryType, ...],
        sensitive_categories: tuple[str, ...],
    ) -> tuple[ExtractionCandidate, ...]:
        getter = getattr(self._context, "get_using_provider_async", None)
        if getter is None:
            raise RuntimeError("host provider API is unavailable")
        provider = await getter()
        if provider is None:
            raise RuntimeError("no LLM provider is configured")
        response = await provider.text_chat(
            prompt=build_extraction_prompt(window),
            system_prompt=_SYSTEM_PROMPT,
        )
        text = getattr(response, "completion_text", "") or ""
        return parse_extraction_response(
            text, allowed_types=allowed_types,
            sensitive_categories=sensitive_categories,
        )


class AstrBotExtractionCapture:
    """Private-message capture; group events and commands are ignored."""

    def __init__(
        self, scopes: Any, extraction: ExtractionService, *,
        enabled: bool = True, min_messages: int = 1,
        max_buffer_messages: int = 50,
    ) -> None:
        self._scopes = scopes
        self._extraction = extraction
        self._enabled = enabled
        self._min_messages = max(1, min_messages)
        self._max_buffer = max(self._min_messages, max_buffer_messages)
        self._identity = AstrBotIdentityAdapter()
        self._tasks: set[asyncio.Task[None]] = set()
        self._buffers: dict[str, list[tuple[str, str]]] = {}
        self._watermarks: dict[str, int] = {}

    async def capture(self, event: Any) -> None:
        if not self._enabled:
            return
        try:
            identity = self._identity.resolve_identity(event)
        except AuthorizationDenied:
            return
        scope = self._scopes.find(identity)
        if scope is None:
            return
        text = str(getattr(event, "message_str", "") or "").strip()
        if not text or text.startswith("/") or text.startswith("织忆"):
            return
        message_obj = getattr(event, "message_obj", None)
        message_id = str(getattr(message_obj, "message_id", "") or "").strip()
        conversation_id = str(getattr(event, "unified_msg_origin", "") or "").strip()
        key = conversation_id or scope.id
        # Never retain text captured before the layered consent is complete:
        # otherwise messages sent before opt-in could reach the model after it.
        if not self._extraction.gates(scope).can_store:
            self._buffers.pop(key, None)
            return
        watermark = self._watermarks.get(key, 0) + 1
        self._watermarks[key] = watermark
        buffer = self._buffers.setdefault(key, [])
        buffer.append((message_id, text))
        del buffer[:max(0, len(buffer) - self._max_buffer)]
        if text.startswith("记住") or len(buffer) >= self._min_messages:
            self._schedule(scope, key, watermark)

    def _schedule(self, scope: Any, key: str, watermark: int) -> None:
        messages = tuple(self._buffers.pop(key, ()))
        if not messages:
            return
        task = asyncio.create_task(self._extraction.submit_messages(
            scope, conversation_id=key, messages=messages, watermark=watermark,
        ))
        self._tasks.add(task)
        task.add_done_callback(self._reap)

    def _reap(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass

    async def close(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._buffers.clear()
        self._watermarks.clear()
