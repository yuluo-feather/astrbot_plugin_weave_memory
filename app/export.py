"""Scope-scoped export serialization for V1.2.

The command UX for export lands in V1.3; V1.2 exposes a service-level payload
and JSON/Markdown serializers so the scope and consent boundary is testable.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ..core.model import MemoryEntity, Scope

EXPORT_FORMAT = "weave-memory-export-v1"


def _scope_payload(scope: Scope) -> dict[str, Any]:
    identity = scope.identity
    return {
        "platform": identity.platform,
        "tenant_id": identity.tenant_id,
        "scope_type": identity.scope_type.value,
        "scope_external_id": identity.scope_external_id,
        "subject_type": identity.subject_type.value,
        "subject_id": identity.subject_id,
    }


def _memory_payload(memory: MemoryEntity) -> dict[str, Any]:
    return {
        "id": memory.id,
        "content": memory.content,
        "status": memory.status.value,
        "memory_type": memory.memory_type.value,
        "source_type": memory.source_type.value,
        "current_revision": memory.current_revision,
        "scope_id": memory.scope_id,
        "subject_id": memory.subject_id,
        "created_at": memory.created_at.isoformat(),
        "updated_at": memory.updated_at.isoformat(),
        "valid_from": memory.valid_from.isoformat() if memory.valid_from else None,
        "valid_until": memory.valid_until.isoformat() if memory.valid_until else None,
        "metadata": dict(memory.metadata),
    }


def build_export(scope: Scope, memories: tuple[MemoryEntity, ...], *,
                 exported_at: datetime) -> dict[str, Any]:
    return {
        "format": EXPORT_FORMAT,
        "exported_at": exported_at.isoformat(),
        "scope": _scope_payload(scope),
        "memories": [_memory_payload(memory) for memory in memories],
    }


def to_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def to_markdown(payload: dict[str, Any]) -> str:
    scope = payload["scope"]
    lines = [
        f"# 织忆导出 · {scope['scope_type']} {scope['scope_external_id']}",
        "",
        f"导出时间：{payload['exported_at']}",
        f"记忆数：{len(payload['memories'])}",
        "",
    ]
    for memory in payload["memories"]:
        lines.append(
            f"- [{memory['memory_type']}] {memory['content']} "
            f"（revision {memory['current_revision']}，来源 {memory['source_type']}）"
        )
    return "\n".join(lines) + "\n"
