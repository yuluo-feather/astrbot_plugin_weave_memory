"""Bounded in-memory RetrievalTrace ring buffer with optional JSONL sink."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from threading import RLock
from typing import Any


class RetrievalTraceRecorder:
    def __init__(self, writer: Callable[[dict[str, Any]], None] | None = None,
                 *, max_records: int = 200) -> None:
        self._writer = writer
        self._records: deque[dict[str, Any]] = deque(maxlen=max(1, max_records))
        self._lock = RLock()

    def write(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._records.append(record)
        if self._writer is not None:
            try:
                self._writer(record)
            except Exception:  # noqa: BLE001 - diagnostics must not break chat
                pass

    def recent(self, limit: int = 20) -> tuple[dict[str, Any], ...]:
        with self._lock:
            if limit <= 0:
                return ()
            return tuple(list(self._records)[-limit:])
