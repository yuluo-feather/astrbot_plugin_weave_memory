"""AstrBot management Web API adapter (V1.6 read-only, V1.7 group import/export).

The host only forwards the dashboard username; there is no platform identity or
role. Read endpoints are therefore limited to non-personal scopes. The V1.7
import/export endpoints are the only writers and are fail-closed on an explicit
``api_admin_usernames`` whitelist (ADR-0017).
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import Any

from ...app.management_service import ManagementService
from ...core.errors import (
    AuthorizationDenied,
    Conflict,
    DomainError,
    IndexUnavailable,
    NotFound,
    ServiceClosed,
    StorageError,
    ValidationError,
)

PLUGIN_NAME = "astrbot_plugin_weave_memory"

_ERROR_STATUS: tuple[tuple[type[DomainError], int], ...] = (
    (ValidationError, 400),
    (AuthorizationDenied, 403),
    (NotFound, 404),
    (Conflict, 409),
    (IndexUnavailable, 503),
    (StorageError, 503),
    (ServiceClosed, 503),
)


def _error_response(exc: DomainError) -> tuple[dict[str, object], int]:
    status = 500
    for error_type, mapped in _ERROR_STATUS:
        if isinstance(exc, error_type):
            status = mapped
            break
    message = str(exc)
    if isinstance(exc, (IndexUnavailable, StorageError, ServiceClosed)):
        message = "service unavailable"
    elif isinstance(exc, AuthorizationDenied):
        message = "forbidden"
    elif isinstance(exc, NotFound):
        message = "not found"
    return {
        "status": "error",
        "error": type(exc).__name__,
        "message": message,
    }, status


class AstrBotManagementApi:
    def __init__(self, service: ManagementService, *,
                 admin_usernames: tuple[str, ...] = (),
                 request_provider: Callable[[], Any] | None = None) -> None:
        self._service = service
        self._admin_usernames = tuple(admin_usernames)
        self._request_provider = request_provider

    def _proxy(self) -> Any:
        if self._request_provider is not None:
            return self._request_provider()
        from astrbot.api.web import request

        return request

    def _authorize(self) -> str:
        username = getattr(self._proxy(), "username", None)
        if not isinstance(username, str) or not username.strip():
            raise AuthorizationDenied("dashboard login required")
        if self._admin_usernames and username not in self._admin_usernames:
            raise AuthorizationDenied("dashboard user is not in api_admin_usernames")
        return username

    def _authorize_admin(self) -> str:
        """Fail closed: import/export require an explicit admin whitelist."""
        if not self._admin_usernames:
            raise AuthorizationDenied("import/export requires api_admin_usernames")
        return self._authorize()

    @staticmethod
    def _flag(value: object) -> bool:
        if isinstance(value, bool):
            return value
        return isinstance(value, str) and value.strip().lower() in {
            "1", "true", "yes", "all",
        }

    def _query(self, name: str, default: object = None) -> object:
        return self._proxy().query.get(name, default)

    def _group_args(self) -> tuple[object, object, object]:
        scope = self._query("scope", "GROUP")
        if scope == "PERSONAL":
            raise AuthorizationDenied("personal scope is not available through the management API")
        if scope not in (None, "", "GROUP"):
            raise ValidationError("scope must be GROUP")
        return self._query("platform"), self._query("tenant_id"), self._query("group_id")

    async def _run(self, operation: Callable[[], object]) -> object:
        try:
            self._authorize()
            result = operation()
            if inspect.isawaitable(result):
                result = await result
        except DomainError as exc:
            return _error_response(exc)
        return {"status": "ok", "data": result}

    async def _run_admin(self, operation: Callable[[str], object]) -> object:
        try:
            username = self._authorize_admin()
            result = operation(username)
            if inspect.isawaitable(result):
                result = await result
        except DomainError as exc:
            return _error_response(exc)
        return {"status": "ok", "data": result}

    def register(self, context: object) -> bool:
        register = getattr(context, "register_web_api", None)
        if not callable(register):
            return False
        prefix = f"/{PLUGIN_NAME}/v1"
        register(f"{prefix}/overview", self.h_overview, ["GET"], "Weave memory overview")
        register(f"{prefix}/candidates", self.h_candidates, ["GET"], "Weave candidates")
        register(f"{prefix}/memories", self.h_memories, ["GET"], "Weave memories")
        register(f"{prefix}/memories/<id>", self.h_memory, ["GET"], "Weave memory detail")
        register(f"{prefix}/memories/<id>/history", self.h_memory_history, ["GET"],
                 "Weave memory history")
        register(f"{prefix}/groups", self.h_groups, ["GET"], "Weave groups")
        register(f"{prefix}/retrieval/debug", self.h_retrieval_debug, ["POST"],
                 "Weave retrieval debug")
        register(f"{prefix}/settings", self.h_settings, ["GET"], "Weave settings")
        register(f"{prefix}/maintenance/status", self.h_maintenance_status, ["GET"],
                 "Weave maintenance status")
        register(f"{prefix}/export", self.h_export, ["GET"], "Weave group export")
        register(f"{prefix}/import", self.h_import, ["POST"], "Weave group import")
        register(f"{prefix}/maintenance/backup", self.h_maintenance_backup, ["POST"],
                 "Weave backup")
        register(f"{prefix}/maintenance/restore", self.h_maintenance_restore, ["POST"],
                 "Weave restore")
        register(f"{prefix}/maintenance/reindex", self.h_maintenance_reindex, ["POST"],
                 "Weave reindex")
        return True

    async def h_overview(self) -> object:
        return await self._run(self._service.overview)

    async def h_candidates(self) -> object:
        def operation() -> object:
            platform, tenant_id, group_id = self._group_args()
            return self._service.list_candidates(
                platform=platform, tenant_id=tenant_id, group_id=group_id,
                limit=self._query("limit"), cursor=self._query("cursor"),
            )
        return await self._run(operation)

    async def h_memories(self) -> object:
        def operation() -> object:
            platform, tenant_id, group_id = self._group_args()
            return self._service.list_memories(
                platform=platform, tenant_id=tenant_id, group_id=group_id,
                limit=self._query("limit"), cursor=self._query("cursor"),
                memory_type=self._query("memory_type"), status=self._query("status"),
            )
        return await self._run(operation)

    async def h_memory(self, id: str) -> object:
        def operation() -> object:
            platform, tenant_id, group_id = self._group_args()
            return self._service.get_memory(
                platform=platform, tenant_id=tenant_id, group_id=group_id,
                memory_id=id,
            )
        return await self._run(operation)

    async def h_memory_history(self, id: str) -> object:
        def operation() -> object:
            platform, tenant_id, group_id = self._group_args()
            return self._service.memory_history(
                platform=platform, tenant_id=tenant_id, group_id=group_id,
                memory_id=id,
            )
        return await self._run(operation)

    async def h_groups(self) -> object:
        return await self._run(self._service.list_groups)

    async def h_retrieval_debug(self) -> object:
        async def operation() -> object:
            body = await self._proxy().json(default={})
            if not isinstance(body, Mapping):
                raise ValidationError("request body must be a JSON object")
            scope = body.get("scope", "GROUP")
            if scope == "PERSONAL":
                raise AuthorizationDenied(
                    "personal scope is not available through the management API",
                )
            if scope not in (None, "", "GROUP"):
                raise ValidationError("scope must be GROUP")
            return self._service.retrieval_debug(
                platform=body.get("platform"), tenant_id=body.get("tenant_id"),
                group_id=body.get("group_id"), query=body.get("query"),
                limit=body.get("limit"),
            )
        return await self._run(operation)

    async def h_settings(self) -> object:
        return await self._run(self._service.settings)

    async def h_maintenance_status(self) -> object:
        return await self._run(self._service.maintenance_status)

    async def h_export(self) -> object:
        def operation(username: str) -> object:
            platform, tenant_id, group_id = self._group_args()
            return self._service.export_group_payload(
                platform=platform, tenant_id=tenant_id, group_id=group_id,
                username=username, include_all=self._flag(self._query("all")),
            )
        return await self._run_admin(operation)

    async def h_import(self) -> object:
        async def operation(username: str) -> object:
            platform, tenant_id, group_id = self._group_args()
            body = await self._proxy().json(default=None)
            if body is None:
                raise ValidationError("request body must be a JSON export")
            return await self._service.import_group_payload(
                platform=platform, tenant_id=tenant_id, group_id=group_id,
                username=username, payload=body,
                dry_run=self._flag(self._query("dry_run")),
            )
        return await self._run_admin(operation)

    async def _body(self) -> Mapping[str, object]:
        body = await self._proxy().json(default={})
        if body is None:
            return {}
        if not isinstance(body, Mapping):
            raise ValidationError("request body must be a JSON object")
        return body

    async def h_maintenance_backup(self) -> object:
        async def operation(_username: str) -> object:
            body = await self._body()
            if not self._flag(body.get("confirm", self._query("confirm"))):
                raise ValidationError("backup requires confirm=true")
            return self._service.create_backup()
        return await self._run_admin(operation)

    async def h_maintenance_restore(self) -> object:
        async def operation(_username: str) -> object:
            body = await self._body()
            dry_run = self._flag(body.get("dry_run", self._query("dry_run")))
            if not dry_run and not self._flag(
                body.get("confirm", self._query("confirm")),
            ):
                raise ValidationError("restore requires confirm=true")
            return self._service.restore_backup(
                backup_id=body.get("backup_id", self._query("backup_id")),
                mode=body.get("mode", self._query("mode", "merge")),
                dry_run=dry_run,
                confirm=not dry_run,
            )
        return await self._run_admin(operation)

    async def h_maintenance_reindex(self) -> object:
        async def operation(_username: str) -> object:
            body = await self._body()
            if not self._flag(body.get("confirm", self._query("confirm"))):
                raise ValidationError("reindex requires confirm=true")
            async_reindex = getattr(self._service, "reindex_async", None)
            if callable(async_reindex):
                return await async_reindex()
            return self._service.reindex()
        return await self._run_admin(operation)
