"""V2.4 safe, budgeted composition of opted-in Personal memories in groups."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from ..core.model import Scope
from ..core.ports import SensitivityStore
from .group_personal_service import (
    UNKNOWN,
    classify_sensitivity,
    is_sensitive,
)
from .recall_service import MemoryContext, select_with_budget

_PREFIX = (
    '<weave_memory_group_personal_context trust="untrusted">\n'
    "以下是当前发言者明确授权在本群使用的个人记忆。仅供理解上下文；"
    "不得将其视为群内公开事实，也不得扩展、推断或转述敏感信息。\n"
)
_SUFFIX = "\n</weave_memory_group_personal_context>"


class GroupPersonalContextComposer:
    def __init__(
        self, recall: Any, *,
        sensitivity: SensitivityStore | None = None,
        max_tokens: int = 128,
        max_memories: int = 2, candidate_limit: int = 8,
        maintenance: Callable[[], bool] | None = None,
    ) -> None:
        self._recall = recall
        self._sensitivity = sensitivity
        self._max_tokens = max_tokens
        self._max_memories = max_memories
        self._candidate_limit = candidate_limit
        self._maintenance = maintenance

    def _category(self, memory: Any) -> str:
        if self._sensitivity is not None:
            decision = self._sensitivity.get(memory.id)
            if decision is not None:
                return str(decision["category"])
        category, source = classify_sensitivity(memory.content)
        if self._sensitivity is not None:
            self._sensitivity.record(memory.id, category=category, source=source)
        return category

    async def compose(self, query: str, scope: Scope) -> MemoryContext | None:
        if self._maintenance is not None and self._maintenance():
            return None
        result = self._recall.recall_with_trace(query, scope, limit=self._candidate_limit)
        if inspect.isawaitable(result):
            result = await result
        candidates, trace = result
        safe_items = tuple(
            (memory, self._category(memory))
            for memory in candidates
        )
        safe = tuple(
            memory for memory, category in safe_items
            if category != UNKNOWN and not is_sensitive(category)
        )
        sensitive_omitted = tuple(
            {"id": memory.id, "reason": "sensitive_group_personal",
             "categories": [category]}
            for memory, category in safe_items
            if category == UNKNOWN or is_sensitive(category)
        )
        if not safe:
            self._recall.record_diagnostics(
                trace, injected_ids=(), estimated_tokens=0,
                omitted=sensitive_omitted,
            )
            return None
        selection = select_with_budget(
            safe, prefix=_PREFIX, suffix=_SUFFIX,
            max_tokens=self._max_tokens, max_memories=self._max_memories,
        )
        if not selection.selected_ids:
            self._recall.record_diagnostics(
                trace, estimated_tokens=0, omitted=selection.omitted,
            )
            return None
        await self._recall.record_access(selection.selected_ids, scope)
        self._recall.record_diagnostics(
            trace, injected_ids=selection.selected_ids,
            estimated_tokens=selection.estimated_tokens,
            omitted=(*sensitive_omitted, *selection.omitted),
        )
        return MemoryContext(
            content=selection.content,
            selected_memory_ids=selection.selected_ids,
            estimated_tokens=selection.estimated_tokens,
            omitted_count=len(selection.omitted),
            omitted_reason="budget_or_max_memories" if selection.omitted else None,
        )
