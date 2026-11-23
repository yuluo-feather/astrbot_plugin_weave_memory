"""Read one JSON attachment from an AstrBot message (no SDK import, ADR-0017).

Attachments are untrusted input: the file is size-limited, decoded as UTF-8
and only parsed as JSON. Nothing from the file is imported or executed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...core.errors import ValidationError


def _component_type_name(component: Any) -> str:
    value = getattr(component, "type", None)
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    return str(value or "").rsplit(".", 1)[-1]


async def _component_path(component: Any) -> str:
    getter = getattr(component, "get_file", None)
    if callable(getter):
        try:
            path = await getter()
        except Exception as exc:  # noqa: BLE001 - host download failure is user-facing
            raise ValidationError("附件读取失败，请重新发送文件") from exc
        return path if isinstance(path, str) else ""
    path = getattr(component, "file", None)
    return path if isinstance(path, str) else ""


async def read_json_attachment(event: Any, *, max_bytes: int,
                               purpose: str) -> tuple[object, str]:
    message = getattr(getattr(event, "message_obj", None), "message", None)
    if not isinstance(message, (list, tuple)):
        raise ValidationError(f"{purpose}：请在同一条消息里附上 JSON 导出文件。")
    for component in message:
        if _component_type_name(component) != "File":
            continue
        path = await _component_path(component)
        if not path:
            continue
        source = Path(path)
        try:
            if not source.is_file():
                continue
            if source.stat().st_size > max_bytes:
                raise ValidationError(
                    f"{purpose}：文件超过上限（{max_bytes // (1024 * 1024)} MiB）。"
                )
            raw = source.read_bytes()
        except OSError as exc:
            raise ValidationError(f"{purpose}：附件读取失败。") from exc
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValidationError(f"{purpose}：文件必须是 UTF-8 编码的 JSON。") from exc
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"{purpose}：文件不是合法 JSON。") from exc
        return payload, source.name
    raise ValidationError(f"{purpose}：请在同一条消息里附上 JSON 导出文件。")
