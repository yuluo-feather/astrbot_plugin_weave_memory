"""Scope-scoped export serialization (V1.7, ADR-0017).

The payload carries the stable idempotency fields the import side requires:
``export_id`` and ``source_platform`` at file level, ``source_memory_id`` and
``source_revision`` per entry. Non-ACTIVE entries export metadata only.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ..core.model import MemoryEntity, MemoryStatus, Scope

EXPORT_FORMAT = "weave-memory-export-v2"
EXPORTED_STATUSES: tuple[MemoryStatus, ...] = (
    MemoryStatus.ACTIVE,
    MemoryStatus.ARCHIVED,
    MemoryStatus.EXPIRED,
    MemoryStatus.SUPERSEDED,
)


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


def _memory_payload(memory: MemoryEntity, *, exported_at: datetime) -> dict[str, Any]:
    active = memory.status is MemoryStatus.ACTIVE
    return {
        "source_memory_id": memory.id,
        "source_revision": memory.current_revision,
        "memory_type": memory.memory_type.value,
        "status": memory.status.value,
        "content": memory.content if active else None,
        "body_omitted": not active,
        "source_type": memory.source_type.value,
        "created_at": memory.created_at.isoformat(),
        "updated_at": memory.updated_at.isoformat(),
        "valid_from": memory.valid_from.isoformat() if memory.valid_from else None,
        "valid_until": memory.valid_until.isoformat() if memory.valid_until else None,
        "metadata": dict(memory.metadata),
        "exported_at": exported_at.isoformat(),
        "schema_version": EXPORT_FORMAT,
    }


def build_export(scope: Scope, memories: tuple[MemoryEntity, ...], *,
                 exported_at: datetime, export_id: str) -> dict[str, Any]:
    return {
        "schema_version": EXPORT_FORMAT,
        "export_id": export_id,
        "source_platform": scope.identity.platform,
        "exported_at": exported_at.isoformat(),
        "scope": _scope_payload(scope),
        "memories": [_memory_payload(memory, exported_at=exported_at) for memory in memories],
    }


def to_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def to_markdown(payload: dict[str, Any]) -> str:
    scope = payload["scope"]
    memories = payload["memories"]
    lines = [
        f"# 织忆导出 · {scope['scope_type']} {scope['scope_external_id']}",
        "",
        f"导出时间：{payload['exported_at']}",
        f"记忆数：{len(memories)}",
        "",
    ]
    for memory in memories:
        if memory.get("body_omitted") or memory.get("content") is None:
            lines.append(
                f"- [{memory['memory_type']}] <{memory['status']}，不导出正文> "
                f"（revision {memory['source_revision']}，来源 {memory['source_type']}）"
            )
            continue
        lines.append(
            f"- [{memory['memory_type']}] {memory['content']} "
            f"（revision {memory['source_revision']}，来源 {memory['source_type']}）"
        )
    return "\n".join(lines) + "\n"
