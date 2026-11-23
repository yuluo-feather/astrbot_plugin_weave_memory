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
    AdminDecision,
    AdminSource,
    Identity,
    MemoryEntity,
    MemoryStatus,
    MemoryType,
    Scope,
    ScopeType,
    SubjectType,
)
from ..core.ports import BackupPort, ManagementRepository, MemoryStore, SearchIndex
from .consent_service import ConsentService, GroupConsentType
from .group_memory_service import GroupMemoryService
from .group_recall_service import (
    GROUP_CONTEXT_PREFIX,
    GROUP_CONTEXT_SUFFIX,
    GroupRecallService,
)
from .import_service import ImportResult, ImportService, payload_sha256
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
                 imports: ImportService, group_memories: GroupMemoryService,
                 settings: Mapping[str, object], group_max_tokens: int = 192,
                 group_max_memories: int = 2,
                 backup: BackupPort | None = None) -> None:
        self._repository = repository
        self._store = store
        self._index = index
        self._groups = groups
        self._group_recall = group_recall
        self._consent = consent
        self._imports = imports
        self._group_memories = group_memories
        self._settings = dict(settings)
        self._group_max_tokens = group_max_tokens
        self._group_max_memories = group_max_memories
        self._backup = backup

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
            "archived_at": _time(memory.archived_at),
            "purge_after": _time(memory.purge_after),
            "archive_reason": (
                memory.archive_reason.value if memory.archive_reason else None
            ),
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

    @staticmethod
    def _admin_decision(username: str) -> AdminDecision:
        return AdminDecision(
            AdminSource.EXPLICIT_CONFIG, (username,), True, True,
        )

    def export_group_payload(self, *, platform: object, tenant_id: object,
                             group_id: object, username: str,
                             include_all: object = False) -> dict[str, object]:
        scope = self._group_scope(platform, tenant_id, group_id)
        return self._group_memories.export_group(
            scope, self._admin_decision(username),
            include_all=include_all is True,
        )

    @staticmethod
    def _import_summary(result: ImportResult) -> dict[str, object]:
        return {
            "job_id": result.job_id,
            "dry_run": result.dry_run,
            "total": result.total,
            "imported": result.imported,
            "metadata_only": result.metadata_only,
            "skipped": result.skipped,
            "conflicts": result.conflicts,
            "failed": result.failed,
            "resumed_from": result.resumed_from,
            "note": result.note,
            "entries": [{
                "index": entry.index,
                "source_memory_id": entry.source_memory_id,
                "outcome": entry.outcome.value,
                "reason": entry.reason,
                "memory_id": entry.memory_id,
            } for entry in result.entries],
        }

    async def import_group_payload(self, *, platform: object, tenant_id: object,
                                   group_id: object, username: str,
                                   payload: object,
                                   dry_run: object = False) -> dict[str, object]:
        scope = self._group_scope(platform, tenant_id, group_id)
        result = await self._imports.import_payload(
            scope, payload, operator_id=username,
            file_sha256=payload_sha256(payload),
            guard=self._group_memories.write_guard(scope),
            dry_run=dry_run is True,
        )
        return self._import_summary(result)

    def create_backup(self) -> dict[str, object]:
        return self._require_backup().create_backup()

    def list_backups(self) -> dict[str, object]:
        return {"items": self._require_backup().list_backups()}

    def restore_backup(self, *, backup_id: object, mode: object = "merge",
                       dry_run: object = False,
                       confirm: object = False) -> dict[str, object]:
        backup = self._require_backup()
        backup_text = _required_text(backup_id, "backup_id")
        mode_text = str(mode or "merge")
        return backup.restore(
            backup_text, mode=mode_text, dry_run=dry_run is True,
            confirm=confirm is True,
        )

    def reindex(self) -> dict[str, object]:
        try:
            count = self._index.rebuild()
        except IndexUnavailable as exc:
            raise StorageError("could not rebuild the search index") from exc
        return {"rebuilt": True, "documents": count}

    def _require_backup(self) -> BackupPort:
        if self._backup is None:
            raise StorageError("backup service is not available")
        return self._backup
