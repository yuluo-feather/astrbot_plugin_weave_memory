"""Shared parsing of the text after ``/织忆 <subcommand>``."""

from __future__ import annotations

from typing import Any


def tail_after_command(event: Any, command: str) -> str:
    raw = str(getattr(event, "message_str", "") or "").strip().lstrip("/").strip()
    if raw.startswith("织忆"):
        raw = raw[len("织忆"):].strip()
    if raw.startswith(command):
        return raw[len(command):].strip()
    return raw
