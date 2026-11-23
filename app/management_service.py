"""V1.6 read-only management views (ADR-0016).

The service is transport-independent: the AstrBot adapter only resolves the
dashboard username and request parameters, then calls these methods.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from ..core.errors import (
    AuthorizationDenied,
    IndexUnavailable,
    NotFound,
    StorageError,
    ValidationError,
)
from ..core.model import (
    Identity,
    MemoryEntity,
    MemoryStatus,
    MemoryType,
    Scope,
    ScopeType,
    SubjectType,
)
from ..core.ports import ManagementRepository, MemoryStore, SearchIndex
from .consent_service import ConsentService, GroupConsentType
from .group_recall_service import (
    GROUP_CONTEXT_PREFIX,
    GROUP_CONTEXT_SUFFIX,
    GroupRecallService,
)
from .recall_service import select_with_budget
from .scope_service import GroupScopeService


def _integer(value: object, *, name: str, default: int,
             minimum: int, maximum: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ValidationError(f"{name} must be an integer")
    if isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError as exc:
            raise ValidationError(f"{name} must be an integer") from exc
    elif isinstance(value, int):
        parsed = value
    else:
        raise ValidationError(f"{name} must be an integer")
    if not minimum <= parsed <= maximum:
        raise ValidationError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _required_text(value: object, name: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise ValidationError(f"{name} is required")
    return text


def _time(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class ManagementService:
    def __init__(self, *, repository: ManagementRepository, store: MemoryStore,
                 index: SearchIndex, groups: GroupScopeService,
                 group_recall: GroupRecallService, consent: ConsentService,
                 settings: Mapping[str, object], group_max_tokens: int = 192,
                 group_max_memories: int = 2) -> None:
        self._repository = repository
        self._store = store
        self._index = index
        self._groups = groups
        self._group_recall = group_recall
        self._consent = consent
        self._settings = dict(settings)
        self._group_max_tokens = group_max_tokens
        self._group_max_memories = group_max_memories

    def _index_status(self) -> dict[str, object]:
        try:
            return {
                "healthy": self._index.is_healthy(),
                "documents": self._index.document_count(),
            }
        except (IndexUnavailable, StorageError):
            return {"healthy": False, "documents": 0}

    def _group_scope(self, platform: object, tenant_id: object,
                     group_id: object) -> Scope:
        platform_text = _required_text(platform, "platform")
        tenant_text = _required_text(tenant_id, "tenant_id")
        group_text = _required_text(group_id, "group_id")
        if len(group_text) > 256:
            raise ValidationError("group_id must be at most 256 characters")
        identity = Identity(
            platform_text, tenant_text, ScopeType.GROUP, group_text,
            SubjectType.GROUP, group_text,
        )
        scope = self._groups.find(identity)
        if scope is None:
            raise NotFound("group memory scope not found")
        self._consent.require(scope, GroupConsentType.GROUP_MEMORY)
        if not self._groups.is_enabled(scope):
            raise AuthorizationDenied("group memory is disabled")
        return scope

    @staticmethod
    def _page(limit: object, cursor: object) -> tuple[int, int]:
        page_size = _integer(limit, name="limit", default=20, minimum=1, maximum=100)
        offset = _integer(cursor, name="cursor", default=0, minimum=0, maximum=1_000_000)
        return page_size, offset

    @staticmethod
    def _memory_view(memory: MemoryEntity) -> dict[str, object]:
        return {
            "id": memory.id,
            "memory_type": memory.memory_type.value,
            "status": memory.status.value,
            "source_type": memory.source_type.value,
            "content": memory.content,
            "current_revision": memory.current_revision,
            "created_at": _time(memory.created_at),
            "updated_at": _time(memory.updated_at),
            "valid_from": _time(memory.valid_from),
            "valid_until": _time(memory.valid_until),
            "candidate_expires_at": _time(memory.candidate_expires_at),
            "last_accessed_at": _time(memory.last_accessed_at),
            "metadata": dict(memory.metadata),
        }

    def overview(self) -> dict[str, object]:
        counts = self._repository.overview_counts()
        return {
            **counts,
            "index": self._index_status(),
            "recent_operations": list(self._repository.recent_operations(limit=5)),
        }

    def list_candidates(self, *, platform: object, tenant_id: object,
                        group_id: object, limit: object = None,
                        cursor: object = None) -> dict[str, object]:
        scope = self._group_scope(platform, tenant_id, group_id)
        page_size, offset = self._page(limit, cursor)
        rows, total = self._store.list_candidates(
            scope_id=scope.id, subject_id=scope.identity.subject_id,
            limit=page_size, offset=offset,
        )
        next_offset = offset + len(rows)
        return {
            "items": [self._memory_view(memory) for memory in rows],
            "total": total,
            "next_cursor": str(next_offset) if next_offset < total else None,
        }

    def list_memories(self, *, platform: object, tenant_id: object,
                      group_id: object, limit: object = None,
                      cursor: object = None, memory_type: object = None,
                      status: object = None) -> dict[str, object]:
        if status not in (None, "", "ACTIVE"):
            raise AuthorizationDenied("only ACTIVE group memories are exposed")
        scope = self._group_scope(platform, tenant_id, group_id)
        page_size, offset = self._page(limit, cursor)
        parsed_type: MemoryType | None = None
        if memory_type not in (None, ""):
            try:
                parsed_type = MemoryType(str(memory_type))
            except ValueError as exc:
                raise ValidationError("unknown memory_type") from exc
        rows, total = self._store.list_active_page(
            scope_id=scope.id, subject_id=scope.identity.subject_id,
            limit=page_size, offset=offset, memory_type=parsed_type,
        )
        next_offset = offset + len(rows)
        return {
            "items": [self._memory_view(memory) for memory in rows],
            "total": total,
            "next_cursor": str(next_offset) if next_offset < total else None,
        }

    def get_memory(self, *, platform: object, tenant_id: object,
                   group_id: object, memory_id: object) -> dict[str, object]:
        scope = self._group_scope(platform, tenant_id, group_id)
        memory = self._store.get(
            _required_text(memory_id, "memory_id"),
            scope_id=scope.id, subject_id=scope.identity.subject_id,
        )
        if memory.status is not MemoryStatus.ACTIVE:
            raise NotFound("memory not found")
        return self._memory_view(memory)

    def memory_history(self, *, platform: object, tenant_id: object,
                       group_id: object, memory_id: object) -> dict[str, object]:
        scope = self._group_scope(platform, tenant_id, group_id)
        memory_id_text = _required_text(memory_id, "memory_id")
        memory = self._store.get(
            memory_id_text, scope_id=scope.id, subject_id=scope.identity.subject_id,
        )
        revisions = self._store.list_revisions(
            memory_id_text, scope_id=scope.id, subject_id=scope.identity.subject_id,
        )
        sources = self._store.list_sources(
            memory_id_text, scope_id=scope.id, subject_id=scope.identity.subject_id,
        )
        return {
            "memory": self._memory_view(memory),
            "revisions": [{
                "id": revision.id,
                "revision": revision.revision,
                "status": revision.status.value,
                "content": revision.content,
                "changed_at": _time(revision.changed_at),
                "change_reason": revision.change_reason,
            } for revision in revisions],
            "sources": [{
                "id": source.id,
                "source_type": source.source_type.value,
                "operator_type": source.operator_type.value,
                "operator_id": source.operator_id,
                "captured_at": _time(source.captured_at),
                "evidence_ref": source.evidence_ref,
                "metadata": dict(source.metadata),
            } for source in sources],
        }

    def list_groups(self) -> dict[str, object]:
        return {"items": list(self._repository.list_group_scopes())}

    def retrieval_debug(self, *, platform: object, tenant_id: object,
                        group_id: object, query: object,
                        limit: object = None) -> dict[str, object]:
        scope = self._group_scope(platform, tenant_id, group_id)
        query_text = _required_text(query, "query")
        if len(query_text) > 256:
            raise ValidationError("query must be at most 256 characters")
        limit_value = _integer(limit, name="limit", default=8, minimum=1, maximum=50)
        memories, trace = self._group_recall.recall_group_with_trace(
            query_text, scope, limit=limit_value,
        )
        selection = select_with_budget(
            memories, prefix=GROUP_CONTEXT_PREFIX, suffix=GROUP_CONTEXT_SUFFIX,
            max_tokens=self._group_max_tokens,
            max_memories=self._group_max_memories,
        )
        return {
            "query_hash": trace.query_hash,
            "candidates": [dict(candidate) for candidate in trace.candidates],
            "injected_ids": list(selection.selected_ids),
            "estimated_tokens": selection.estimated_tokens,
            "omitted": list(selection.omitted),
            "omitted_count": len(selection.omitted),
        }

    def settings(self) -> dict[str, object]:
        return dict(self._settings)

    def maintenance_status(self) -> dict[str, object]:
        return {
            **self._repository.maintenance_status(),
            "index": self._index_status(),
        }
