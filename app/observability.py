"""ADR-0024 structured events with a strict field whitelist."""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

logger = logging.getLogger("weave_memory.observability")

_ALLOWED = {
    "event", "component", "stage", "trace_id", "scope_type", "scope_id_hash",
    "channel", "count", "latency_ms", "result", "error_code", "version",
}
_COUNTERS: dict[tuple[str, ...], int] = defaultdict(int)


def emit_event(event: str, **fields: Any) -> None:
    payload = {"event": event}
    for key, value in fields.items():
        if key in _ALLOWED and value is not None:
            payload[key] = value
    logger.info("weave.event %s", payload)


def count_metric(name: str, **labels: str) -> None:
    key = (name, *[f"{k}={v}" for k, v in sorted(labels.items())])
    _COUNTERS[key] += 1


def counter_value(name: str, **labels: str) -> int:
    key = (name, *[f"{k}={v}" for k, v in sorted(labels.items())])
    return _COUNTERS.get(key, 0)
