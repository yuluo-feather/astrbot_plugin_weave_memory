"""Deterministic time-intent parsing for temporal queries (stdlib only)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True, slots=True)
class TimeIntent:
    start: datetime
    end: datetime
    label: str


def _day_bounds(now: datetime, offset_days: int) -> tuple[datetime, datetime]:
    day = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    start = day + timedelta(days=offset_days)
    return start, start + timedelta(days=1)


def parse_time_intent(query: str, *, now: datetime) -> TimeIntent | None:
    text = (query or "").strip().lower()
    if not text:
        return None
    if "昨天" in text or "yesterday" in text:
        start, end = _day_bounds(now, -1)
        return TimeIntent(start, end, "yesterday")
    if "今天" in text or "today" in text:
        start, end = _day_bounds(now, 0)
        return TimeIntent(start, end, "today")
    match = re.search(r"最近\s*(\d{1,2})\s*天", text)
    if match:
        days = max(1, min(90, int(match.group(1))))
        return TimeIntent(now - timedelta(days=days), now, f"last_{days}_days")
    match = re.search(r"last\s*(\d{1,2})\s*days", text)
    if match:
        days = max(1, min(90, int(match.group(1))))
        return TimeIntent(now - timedelta(days=days), now, f"last_{days}_days")
    if "上周" in text or "last week" in text:
        return TimeIntent(now - timedelta(days=14), now - timedelta(days=7), "last_week")
    if "最近" in text or "recent" in text:
        return TimeIntent(now - timedelta(days=30), now, "recent_30_days")
    if "这周" in text or "this week" in text:
        return TimeIntent(now - timedelta(days=7), now, "this_week")
    if "上个月" in text or "last month" in text:
        return TimeIntent(now - timedelta(days=60), now - timedelta(days=30), "last_month")
    return None
