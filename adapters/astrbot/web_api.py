"""AstrBot management + WebUI API adapter.

Read-only endpoints keep the V1.6 contract. V2.1 adds a fail-closed write
surface for GROUP scope only (ADR-0031): a Dashboard-login username, an explicit
``api_admin_usernames`` whitelist, a short-lived CSRF token in the JSON body and,
for dangerous operations, a one-time nonce. V2.4 adds the personal console for
the platform member bound to this Dashboard account (ADR-0032).
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
from ...core.model import (
    AdminDecision,
    AdminSource,
    Identity,
    ScopeType,
    SubjectType,
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


def _required_text(value: object, name: str, *, limit: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} is required")
    text = value.strip()
    if len(text) > limit:
        raise ValidationError(f"{name} must be at most {limit} characters")
    return text


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError(f"{name} must be a positive integer")
    return value


class AstrBotManagementApi:
    def __init__(self, service: ManagementService, *,
                 admin_usernames: tuple[str, ...] = (),
                 group_personal_service: Any | None = None,
                 group_memory: Any | None = None,
                 personal_service: Any | None = None,
                 extraction_service: Any | None = None,
                 personal_scopes: Any | None = None,
                 identity_binding: Any | None = None,
                 settings_writer: Any | None = None,
                 security: Any | None = None,
                 settings_provider: Callable[[], Mapping[str, object]] | None = None,
                 webui_enabled: Callable[[], bool] | None = None,
                 request_provider: Callable[[], Any] | None = None) -> None:
        self._service = service
        self._admin_usernames = tuple(admin_usernames)
        self._group_personal_service = group_personal_service
        self._group_memory = group_memory
        self._personal_service = personal_service
        self._extraction_service = extraction_service
        self._personal_scopes = personal_scopes
        self._identity_binding = identity_binding
        self._settings_writer = settings_writer
        self._security = security
        self._settings_provider = settings_provider
        self._webui_enabled = webui_enabled or (lambda: True)
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
        """Fail closed: writes require an explicit admin whitelist."""
        if not self._admin_usernames:
            raise AuthorizationDenied("write access requires api_admin_usernames")
        return self._authorize()

    def _require_webui(self) -> None:
        if not self._webui_enabled():
            raise AuthorizationDenied("webui is disabled")

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

    async def _body(self) -> Mapping[str, object]:
        body = await self._proxy().json(default={})
        if body is None:
            return {}
        if not isinstance(body, Mapping):
            raise ValidationError("request body must be a JSON object")
        return body

    @staticmethod
    def _group_identity(platform: object, tenant_id: object,
                        group_id: object) -> Identity:
        platform_text = _required_text(platform, "platform")
        tenant_text = _required_text(tenant_id, "tenant_id")
        group_text = _required_text(group_id, "group_id")
        return Identity(
            platform=platform_text, tenant_id=tenant_text,
            scope_type=ScopeType.GROUP, scope_external_id=group_text,
            subject_type=SubjectType.GROUP, subject_id=group_text,
        )

    def _group_identity_from_body(self, body: Mapping[str, object]) -> Identity:
        return self._group_identity(
            body.get("platform"), body.get("tenant_id"), body.get("group_id"),
        )

    def _group_scope(self, identity: Identity) -> Any:
        if self._group_memory is None:
            raise StorageError("group memory service is unavailable")
        scope = self._service.group_scope_for(identity)
        return self._group_memory, scope

    def _require_csrf(self, username: str, body: Mapping[str, object]) -> None:
        if self._security is None:
            raise StorageError("webui security is unavailable")
        self._security.require_token(username, body.get("csrf_token"))

    def _consume_nonce(self, username: str, body: Mapping[str, object], *,
                       action: str, target: str) -> None:
        if self._security is None:
            raise StorageError("webui security is unavailable")
        self._security.consume_nonce(
            username, action=action, target=target, nonce=body.get("nonce"),
        )

    def _admin_decision(self, username: str) -> AdminDecision:
        return AdminDecision(
            AdminSource.EXPLICIT_CONFIG, (username,), True, True,
        )

    def _require_binding(self, username: str) -> Identity:
        if self._identity_binding is None:
            raise StorageError("identity binding is unavailable")
        return self._identity_binding.require(username)

    def _personal_scope(self, identity: Identity) -> Any:
        if self._personal_scopes is None:
            raise StorageError("personal scope service is unavailable")
        scope = self._personal_scopes.find(identity)
        if scope is None or not self._personal_scopes.is_enabled(scope):
            raise NotFound("personal memory is not enabled for this account")
        return scope

    async def _run(self, operation: Callable[[], object]) -> object:
        return await self._run_user(lambda _username: operation())

    async def _run_user(self, operation: Callable[[str], object]) -> object:
        try:
            username = self._authorize()
            result = operation(username)
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
        register(f"{prefix}/webui/capabilities", self.h_capabilities, ["GET"],
                 "Weave WebUI capabilities")
        register(f"{prefix}/ui/csrf", self.h_csrf, ["GET"], "Weave CSRF token")
        register(f"{prefix}/ui/nonce", self.h_nonce, ["POST"], "Weave confirmation nonce")
        register(f"{prefix}/overview", self.h_overview, ["GET"], "Weave memory overview")
        register(f"{prefix}/candidates", self.h_candidates, ["GET"], "Weave candidates")
        register(f"{prefix}/memories", self.h_memories, ["GET"], "Weave memories")
        register(f"{prefix}/archived-memories", self.h_memories_archived, ["GET"],
                 "Weave archived memories")
        register(f"{prefix}/memories/<id>", self.h_memory, ["GET"], "Weave memory detail")
        register(f"{prefix}/memories/<id>/history", self.h_memory_history, ["GET"],
                 "Weave memory history")
        register(f"{prefix}/memories/<id>/operations", self.h_memory_operations, ["GET"],
                 "Weave memory audit trail")
        register(f"{prefix}/memories", self.h_create_memory, ["POST"], "Weave create memory")
        register(f"{prefix}/memories/<id>/update", self.h_update_memory, ["POST"],
                 "Weave update memory")
        register(f"{prefix}/memories/<id>/archive", self.h_archive_memory, ["POST"],
                 "Weave archive memory")
        register(f"{prefix}/memories/<id>/restore", self.h_restore_memory, ["POST"],
                 "Weave restore memory")
        register(f"{prefix}/memories/<id>/purge", self.h_purge_memory, ["POST"],
                 "Weave purge memory")
        register(f"{prefix}/groups", self.h_groups, ["GET"], "Weave groups")
        register(f"{prefix}/group-personal/status", self.h_group_personal_status,
                 ["GET"], "Weave group personal status")
        register(f"{prefix}/retrieval/debug", self.h_retrieval_debug, ["POST"],
                 "Weave retrieval debug")
        register(f"{prefix}/settings", self.h_settings, ["GET"], "Weave settings")
        register(f"{prefix}/settings/schema", self.h_settings_schema, ["GET"],
                 "Weave writable settings schema")
        register(f"{prefix}/settings", self.h_update_settings, ["POST"],
                 "Weave update settings")
        register(f"{prefix}/maintenance/status", self.h_maintenance_status, ["GET"],
                 "Weave maintenance status")
        register(f"{prefix}/export", self.h_export, ["GET"], "Weave group export")
        register(f"{prefix}/import", self.h_import, ["POST"], "Weave group import")
        register(f"{prefix}/maintenance/backups", self.h_maintenance_backups, ["GET"],
                 "List Weave backups")
        register(f"{prefix}/maintenance/backup", self.h_maintenance_backup, ["POST"],
                 "Weave backup")
        register(f"{prefix}/maintenance/restore", self.h_maintenance_restore, ["POST"],
                 "Weave restore")
        register(f"{prefix}/maintenance/reindex", self.h_maintenance_reindex, ["POST"],
                 "Weave reindex")
        register(f"{prefix}/webui/binding", self.h_binding_status, ["GET"],
                 "Weave dashboard identity binding")
        register(f"{prefix}/webui/binding", self.h_bind, ["POST"],
                 "Bind dashboard user to platform member")
        register(f"{prefix}/webui/binding/unbind", self.h_unbind, ["POST"],
                 "Remove dashboard identity binding")
        register(f"{prefix}/personal/memories", self.h_personal_memories, ["GET"],
                 "Weave personal memories")
        register(f"{prefix}/personal/memories/<id>", self.h_personal_memory, ["GET"],
                 "Weave personal memory detail")
        register(f"{prefix}/personal/memories/<id>/update", self.h_personal_update,
                 ["POST"], "Weave update personal memory")
        register(f"{prefix}/personal/memories/<id>/archive", self.h_personal_archive,
                 ["POST"], "Weave archive personal memory")
        register(f"{prefix}/personal/memories/<id>/restore", self.h_personal_restore,
                 ["POST"], "Weave restore personal memory")
        register(f"{prefix}/personal/candidates", self.h_personal_candidates, ["GET"],
                 "Weave personal candidates")
        register(f"{prefix}/personal/candidates/<id>/confirm",
                 self.h_personal_candidate_confirm, ["POST"],
                 "Weave confirm personal candidate")
        register(f"{prefix}/personal/candidates/<id>/reject",
                 self.h_personal_candidate_reject, ["POST"],
                 "Weave reject personal candidate")
        return True

    # ---- instance read ----

    async def h_capabilities(self) -> object:
        def operation(username: str) -> object:
            bound = False
            if self._identity_binding is not None:
                bound = bool(self._identity_binding.status(username).get("bound"))
            return {
                "webui": 1,
                "write": bool(self._admin_usernames),
                "admin": username in self._admin_usernames,
                "personal": self._identity_binding is not None,
                "bound": bound,
                "csrf_required": True,
                "nonce_required": ["purge", "restore", "settings"],
            }
        return await self._run_user(operation)

    async def h_csrf(self) -> object:
        def operation(username: str) -> object:
            if self._security is None:
                raise StorageError("webui security is unavailable")
            return self._security.issue_token(username)
        return await self._run_user(operation)

    async def h_nonce(self) -> object:
        async def operation(username: str) -> object:
            self._require_webui()
            if self._security is None:
                raise StorageError("webui security is unavailable")
            body = await self._body()
            self._require_csrf(username, body)
            action = _required_text(body.get("action"), "action")
            target = _required_text(body.get("target"), "target", limit=512)
            return self._security.issue_nonce(username, action=action, target=target)
        return await self._run_admin(operation)

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

    async def h_memories_archived(self) -> object:
        def operation() -> object:
            identity = self._group_identity(*self._group_args())
            group_memory, scope = self._group_scope(identity)
            limit = self._query("limit", "20")
            offset = self._query("cursor", "0")
            try:
                page_size = int(str(limit))
                page_offset = int(str(offset))
            except (TypeError, ValueError) as exc:
                raise ValidationError("limit/cursor must be integers") from exc
            if not 1 <= page_size <= 100 or page_offset < 0:
                raise ValidationError("limit must be 1..100 and cursor non-negative")
            items, total = group_memory.list_archived_page(
                scope, limit=page_size, offset=page_offset,
            )
            next_offset = page_offset + len(items)
            return {
                "items": [self._service.memory_view(memory) for memory in items],
                "total": total,
                "next_cursor": str(next_offset) if next_offset < total else None,
            }
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

    async def h_memory_operations(self, id: str) -> object:
        def operation() -> object:
            identity = self._group_identity(*self._group_args())
            group_memory, scope = self._group_scope(identity)
            return {"items": list(group_memory.list_operations(scope, id))}
        return await self._run(operation)

    async def h_groups(self) -> object:
        return await self._run(self._service.list_groups)

    async def h_group_personal_status(self) -> object:
        def operation() -> object:
            if self._group_personal_service is None:
                raise StorageError("group personal service is unavailable")
            identity = self._group_identity(*self._group_args())
            return {
                "group_enabled": self._group_personal_service.group_enabled(identity),
                "member_opt_in_required": True,
                "member_opt_in_visibility": "not_available_to_dashboard_identity",
            }
        return await self._run(operation)

    async def h_retrieval_debug(self) -> object:
        async def operation() -> object:
            body = await self._body()
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

    async def h_settings_schema(self) -> object:
        def operation() -> object:
            if self._settings_writer is None:
                raise StorageError("settings writer is unavailable")
            return self._settings_writer.describe()
        return await self._run(operation)

    # ---- GROUP writes (V2.1) ----

    async def h_create_memory(self) -> object:
        async def operation(username: str) -> object:
            self._require_webui()
            body = await self._body()
            self._require_csrf(username, body)
            identity = self._group_identity_from_body(body)
            group_memory, scope = self._group_scope(identity)
            created, receipt, audit_id = await group_memory.create_group(
                scope, _required_text(body.get("content"), "content", limit=4000),
                group_label=_required_text(body.get("group_label", "话题"), "group_label"),
                operator_id=username, decision=self._admin_decision(username),
            )
            return {
                "item": self._service.memory_view(created),
                "revision": receipt.revision,
                "audit_id": audit_id,
            }
        return await self._run_admin(operation)

    async def h_update_memory(self, id: str) -> object:
        async def operation(username: str) -> object:
            self._require_webui()
            body = await self._body()
            self._require_csrf(username, body)
            identity = self._group_identity_from_body(body)
            group_memory, scope = self._group_scope(identity)
            updated, receipt, audit_id = await group_memory.update_group(
                scope, id, _required_text(body.get("content"), "content", limit=4000),
                expected_revision=_positive_int(
                    body.get("expected_revision"), "expected_revision",
                ),
                operator_id=username, decision=self._admin_decision(username),
            )
            return {
                "item": self._service.memory_view(updated),
                "revision": receipt.revision,
                "audit_id": audit_id,
            }
        return await self._run_admin(operation)

    async def h_archive_memory(self, id: str) -> object:
        async def operation(username: str) -> object:
            self._require_webui()
            body = await self._body()
            self._require_csrf(username, body)
            identity = self._group_identity_from_body(body)
            group_memory, scope = self._group_scope(identity)
            _memory, audit_id = await group_memory.archive_group(
                scope, id, operator_id=username,
                decision=self._admin_decision(username),
            )
            return {"item_id": id, "status": "ARCHIVED", "audit_id": audit_id}
        return await self._run_admin(operation)

    async def h_restore_memory(self, id: str) -> object:
        async def operation(username: str) -> object:
            self._require_webui()
            body = await self._body()
            self._require_csrf(username, body)
            self._consume_nonce(username, body, action="restore", target=id)
            identity = self._group_identity_from_body(body)
            group_memory, scope = self._group_scope(identity)
            _memory, audit_id = await group_memory.restore_group(
                scope, id, operator_id=username,
                decision=self._admin_decision(username),
            )
            return {"item_id": id, "status": "ACTIVE", "audit_id": audit_id}
        return await self._run_admin(operation)

    async def h_purge_memory(self, id: str) -> object:
        async def operation(username: str) -> object:
            self._require_webui()
            body = await self._body()
            self._require_csrf(username, body)
            if str(body.get("confirm", "")).strip() != f"PURGE {id}":
                raise ValidationError(f"purge requires confirm=\"PURGE {id}\"")
            self._consume_nonce(username, body, action="purge", target=id)
            identity = self._group_identity_from_body(body)
            group_memory, scope = self._group_scope(identity)
            audit_id, purged = await group_memory.purge_group(
                scope, id, operator_id=username,
                decision=self._admin_decision(username),
            )
            return {"item_id": id, "status": "DELETED", "audit_id": audit_id,
                    "purged": purged > 0}
        return await self._run_admin(operation)

    async def h_update_settings(self) -> object:
        async def operation(username: str) -> object:
            self._require_webui()
            if self._settings_writer is None or self._settings_provider is None:
                raise StorageError("settings writer is unavailable")
            body = await self._body()
            self._require_csrf(username, body)
            raw_values = body.get("values")
            if not isinstance(raw_values, Mapping):
                raise ValidationError("values must be an object")
            values = dict(raw_values)
            target = ",".join(sorted(str(key) for key in values))
            return self._settings_writer.update(
                username, values, current=dict(self._settings_provider()),
                require_nonce=lambda expected, _t=target: self._consume_nonce(
                    username, body, action="settings", target=_t,
                ),
            )
        return await self._run_admin(operation)

    # ---- existing admin maintenance ----

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

    async def h_maintenance_backups(self) -> object:
        return await self._run_admin(
            lambda _username: self._service.list_backups(),
        )

    async def h_maintenance_backup(self) -> object:
        async def operation(username: str) -> object:
            body = await self._body()
            self._require_csrf(username, body)
            if not self._flag(body.get("confirm", self._query("confirm"))):
                raise ValidationError("backup requires confirm=true")
            return self._service.create_backup()
        return await self._run_admin(operation)

    async def h_maintenance_restore(self) -> object:
        async def operation(username: str) -> object:
            body = await self._body()
            self._require_csrf(username, body)
            dry_run = self._flag(body.get("dry_run", self._query("dry_run")))
            if not dry_run:
                backup_id = _required_text(
                    body.get("backup_id", self._query("backup_id")), "backup_id",
                )
                if not self._flag(body.get("confirm", self._query("confirm"))):
                    raise ValidationError("restore requires confirm=true")
                self._consume_nonce(username, body, action="restore", target=backup_id)
            return self._service.restore_backup(
                backup_id=body.get("backup_id", self._query("backup_id")),
                mode=body.get("mode", self._query("mode", "merge")),
                dry_run=dry_run,
                confirm=not dry_run,
            )
        return await self._run_admin(operation)

    async def h_maintenance_reindex(self) -> object:
        async def operation(username: str) -> object:
            body = await self._body()
            self._require_csrf(username, body)
            if not self._flag(body.get("confirm", self._query("confirm"))):
                raise ValidationError("reindex requires confirm=true")
            return self._service.reindex()
        return await self._run_admin(operation)

    # ---- V2.4 identity binding ----

    async def h_binding_status(self) -> object:
        def operation(username: str) -> object:
            if self._identity_binding is None:
                raise StorageError("identity binding is unavailable")
            return self._identity_binding.status(username)
        return await self._run_user(operation)

    async def h_bind(self) -> object:
        async def operation(username: str) -> object:
            self._require_webui()
            if self._identity_binding is None:
                raise StorageError("identity binding is unavailable")
            body = await self._body()
            self._require_csrf(username, body)
            return self._identity_binding.bind(
                username, _required_text(body.get("code"), "code", limit=16),
            )
        return await self._run_user(operation)

    async def h_unbind(self) -> object:
        async def operation(username: str) -> object:
            self._require_webui()
            if self._identity_binding is None:
                raise StorageError("identity binding is unavailable")
            body = await self._body()
            self._require_csrf(username, body)
            self._identity_binding.unbind(username)
            return {"bound": False}
        return await self._run_user(operation)

    # ---- V2.4 personal console ----

    async def h_personal_memories(self) -> object:
        def operation(username: str) -> object:
            if self._personal_service is None:
                raise StorageError("personal memory service is unavailable")
            identity = self._require_binding(username)
            scope = self._personal_scope(identity)
            limit = self._query("limit", "20")
            offset = self._query("cursor", "0")
            try:
                page_size = int(str(limit))
                page_offset = int(str(offset))
            except (TypeError, ValueError) as exc:
                raise ValidationError("limit/cursor must be integers") from exc
            if not 1 <= page_size <= 100 or page_offset < 0:
                raise ValidationError("limit must be 1..100 and cursor non-negative")
            items, total = self._personal_service.list_active_page(
                scope, limit=page_size, offset=page_offset,
            )
            next_offset = page_offset + len(items)
            return {
                "items": [self._service.memory_view(memory) for memory in items],
                "total": total,
                "next_cursor": str(next_offset) if next_offset < total else None,
            }
        return await self._run_user(operation)

    async def h_personal_memory(self, id: str) -> object:
        def operation(username: str) -> object:
            if self._personal_service is None:
                raise StorageError("personal memory service is unavailable")
            identity = self._require_binding(username)
            scope = self._personal_scope(identity)
            return self._service.memory_view(
                self._personal_service.get_memory(scope, id),
            )
        return await self._run_user(operation)

    async def h_personal_update(self, id: str) -> object:
        async def operation(username: str) -> object:
            self._require_webui()
            if self._personal_service is None:
                raise StorageError("personal memory service is unavailable")
            body = await self._body()
            self._require_csrf(username, body)
            identity = self._require_binding(username)
            scope = self._personal_scope(identity)
            updated, receipt = await self._personal_service.update_explicit(
                scope, id, _required_text(body.get("content"), "content", limit=4000),
                expected_revision=_positive_int(
                    body.get("expected_revision"), "expected_revision",
                ),
                operator_id=identity.subject_id,
            )
            return {
                "item": self._service.memory_view(updated),
                "revision": receipt.revision,
            }
        return await self._run_user(operation)

    async def h_personal_archive(self, id: str) -> object:
        async def operation(username: str) -> object:
            self._require_webui()
            if self._personal_service is None:
                raise StorageError("personal memory service is unavailable")
            body = await self._body()
            self._require_csrf(username, body)
            identity = self._require_binding(username)
            scope = self._personal_scope(identity)
            receipt = await self._personal_service.archive_memory(
                scope, id, operator_id=identity.subject_id,
            )
            return {"item_id": id, "status": "ARCHIVED", "revision": receipt.revision}
        return await self._run_user(operation)

    async def h_personal_restore(self, id: str) -> object:
        async def operation(username: str) -> object:
            self._require_webui()
            if self._personal_service is None:
                raise StorageError("personal memory service is unavailable")
            body = await self._body()
            self._require_csrf(username, body)
            identity = self._require_binding(username)
            scope = self._personal_scope(identity)
            receipt = await self._personal_service.restore_memory(
                scope, id, operator_id=identity.subject_id,
            )
            return {"item_id": id, "status": "ACTIVE", "revision": receipt.revision}
        return await self._run_user(operation)

    async def h_personal_candidates(self) -> object:
        async def operation(username: str) -> object:
            if self._extraction_service is None:
                raise StorageError("extraction service is unavailable")
            identity = self._require_binding(username)
            scope = self._personal_scope(identity)
            limit = self._query("limit", "20")
            offset = self._query("cursor", "0")
            try:
                page_size = int(str(limit))
                page_offset = int(str(offset))
            except (TypeError, ValueError) as exc:
                raise ValidationError("limit/cursor must be integers") from exc
            items, total = await self._extraction_service.list_candidates(
                scope, limit=max(1, min(page_size, 100)), offset=max(0, page_offset),
            )
            return {
                "items": [self._service.memory_view(memory) for memory in items],
                "total": total,
            }
        return await self._run_user(operation)

    async def h_personal_candidate_confirm(self, id: str) -> object:
        async def operation(username: str) -> object:
            self._require_webui()
            if self._extraction_service is None:
                raise StorageError("extraction service is unavailable")
            body = await self._body()
            self._require_csrf(username, body)
            identity = self._require_binding(username)
            scope = self._personal_scope(identity)
            memory = await self._extraction_service.confirm_candidate(
                scope, id,
                expected_revision=_positive_int(
                    body.get("expected_revision"), "expected_revision",
                ),
                operator_id=identity.subject_id,
            )
            return {"item": self._service.memory_view(memory), "status": "ACTIVE"}
        return await self._run_user(operation)

    async def h_personal_candidate_reject(self, id: str) -> object:
        async def operation(username: str) -> object:
            self._require_webui()
            if self._extraction_service is None:
                raise StorageError("extraction service is unavailable")
            body = await self._body()
            self._require_csrf(username, body)
            identity = self._require_binding(username)
            scope = self._personal_scope(identity)
            memory = await self._extraction_service.reject_candidate(
                scope, id,
                expected_revision=_positive_int(
                    body.get("expected_revision"), "expected_revision",
                ),
                reason=str(body.get("reason", "")),
                operator_id=identity.subject_id,
            )
            return {
                "item_id": id, "status": "REJECTED",
                "body_retained": memory is not None,
            }
        return await self._run_user(operation)
