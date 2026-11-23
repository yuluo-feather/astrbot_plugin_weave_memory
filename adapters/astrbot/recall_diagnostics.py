"""Privacy-conscious, daily JSONL recall diagnostics with bounded retention."""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)
_EMAIL_RE = re.compile(r"(?i)(?<![\w.+-])[\w.+-]+@[\w.-]+\.[a-z]{2,}(?![\w.-])")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s().-]{6,}\d)(?!\d)")
_LONG_DIGITS_RE = re.compile(r"(?<!\d)\d{15,}(?!\d)")


def _redact_query(query: str) -> str:
    query = _EMAIL_RE.sub("[EMAIL]", query)
    query = _PHONE_RE.sub("[PHONE]", query)
    return _LONG_DIGITS_RE.sub("[ID]", query)


class JsonlRecallDiagnostics:
    """Append one structured record per recall request and prune old daily files."""

    def __init__(
        self,
        directory: Path,
        *,
        retention_days: int = 7,
        include_query: bool = False,
    ) -> None:
        self._directory = directory
        self._retention_days = max(1, min(retention_days, 365))
        self._include_query = include_query
        self._lock = threading.Lock()

    def cleanup(self, *, today: date | None = None) -> None:
        current_day = today or datetime.now(UTC).date()
        cutoff = current_day - timedelta(days=self._retention_days - 1)
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            for path in self._directory.glob("recall-????-??-??.jsonl"):
                try:
                    file_day = date.fromisoformat(path.stem.removeprefix("recall-"))
                except ValueError:
                    continue
                if file_day < cutoff:
                    path.unlink(missing_ok=True)
        except OSError as exc:
            _LOGGER.warning("织忆检索诊断日志清理失败：%s", type(exc).__name__)

    def write(self, record: dict[str, Any]) -> None:
        payload = dict(record)
        if self._include_query and isinstance(payload.get("query"), str):
            payload["query"] = _redact_query(payload["query"])
        else:
            payload.pop("query", None)
        today = datetime.now(UTC).date()
        path = self._directory / f"recall-{today.isoformat()}.jsonl"
        try:
            with self._lock:
                self.cleanup(today=today)
                self._directory.mkdir(parents=True, exist_ok=True)
                line = json.dumps(
                    payload, ensure_ascii=False, separators=(",", ":"),
                ) + "\n"
                with path.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(line)
        except OSError as exc:
            # Diagnostics must never make recall or chat fail.
            _LOGGER.warning("织忆检索诊断日志写入失败：%s", type(exc).__name__)
