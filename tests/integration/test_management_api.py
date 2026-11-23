from __future__ import annotations

import asyncio

from data.plugins.astrbot_plugin_weave_memory.adapters.astrbot.web_api import (
    AstrBotManagementApi,
)
from data.plugins.astrbot_plugin_weave_memory.core.errors import (
    NotFound,
    StorageError,
    ValidationError,
)


class FakeRequest:
    def __init__(self, *, username=None, query=None, body=None) -> None:
        self.username = username
        self.query = dict(query or {})
        self._body = body if body is not None else {}

    async def json(self, default=None):
        return self._body


class FakeContext:
    def __init__(self) -> None:
        self.routes: list[tuple[str, object, list[str], str]] = []

    def register_web_api(self, route, handler, methods, desc) -> None:
        self.routes.append((route, handler, methods, desc))


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def overview(self):
        self.calls.append(("overview", {}))
        return {"memories": {"total": 0}}

    def list_memories(self, **kwargs):
        self.calls.append(("list_memories", kwargs))
        if kwargs.get("limit") == "bad":
            raise ValidationError("limit must be an integer")
        return {"items": [], "total": 0, "next_cursor": None}

    def list_candidates(self, **kwargs):
        self.calls.append(("list_candidates", kwargs))
        return {"items": [], "total": 0, "next_cursor": None}

    def get_memory(self, **kwargs):
        self.calls.append(("get_memory", kwargs))
        if kwargs["memory_id"] == "missing":
            raise NotFound("memory not found")
        return {"id": kwargs["memory_id"]}

    def memory_history(self, **kwargs):
        self.calls.append(("memory_history", kwargs))
        return {"memory": {}, "revisions": [], "sources": []}

    def list_groups(self):
        self.calls.append(("list_groups", {}))
        return {"items": []}

    def retrieval_debug(self, **kwargs):
        self.calls.append(("retrieval_debug", kwargs))
        if kwargs.get("query") == "boom":
            raise StorageError("disk failed")
        return {"candidates": [], "injected_ids": []}

    def settings(self):
        self.calls.append(("settings", {}))
        return {"candidate_ttl_days": 7}

    def maintenance_status(self):
        self.calls.append(("maintenance_status", {}))
        return {"schema_version": 10}

    def export_group_payload(self, **kwargs):
        self.calls.append(("export_group_payload", kwargs))
        return {"schema_version": "weave-memory-export-v2", "memories": []}

    async def import_group_payload(self, **kwargs):
        self.calls.append(("import_group_payload", kwargs))
        return {"imported": 0, "total": 0}

    def create_backup(self):
        self.calls.append(("create_backup", {}))
        return {"backup_id": "b1"}

    def restore_backup(self, **kwargs):
        self.calls.append(("restore_backup", kwargs))
        return {"backup_id": kwargs.get("backup_id"), "dry_run": kwargs.get("dry_run")}

    def reindex(self):
        self.calls.append(("reindex", {}))
        return {"rebuilt": True, "documents": 1}


def make_api(*, username="admin", query=None, body=None,
             admin_usernames=()):
    request = FakeRequest(username=username, query=query, body=body)
    service = FakeService()
    api = AstrBotManagementApi(
        service, admin_usernames=admin_usernames,
        request_provider=lambda: request,
    )
    return api, service


def test_register_all_management_routes():
    api, _ = make_api()
    context = FakeContext()
    assert api.register(context) is True
    assert len(context.routes) == 14
    routes = {route: methods for route, _, methods, _ in context.routes}
    assert routes["/astrbot_plugin_weave_memory/v1/overview"] == ["GET"]
    assert routes["/astrbot_plugin_weave_memory/v1/retrieval/debug"] == ["POST"]
    assert routes["/astrbot_plugin_weave_memory/v1/export"] == ["GET"]
    assert routes["/astrbot_plugin_weave_memory/v1/import"] == ["POST"]
    assert "/astrbot_plugin_weave_memory/v1/memories/<id>" in routes
    assert "/astrbot_plugin_weave_memory/v1/memories/<id>/history" in routes
    assert api.register(object()) is False


def test_import_export_fail_closed_without_admin_whitelist():
    api, service = make_api(username="admin")
    payload, status = asyncio.run(api.h_export())
    assert status == 403
    assert payload["error"] == "AuthorizationDenied"
    assert service.calls == []

    api, service = make_api(username="someone", admin_usernames=("admin",))
    _, status = asyncio.run(api.h_export())
    assert status == 403
    assert service.calls == []

    api, service = make_api(username=None, admin_usernames=("admin",))
    _, status = asyncio.run(api.h_import())
    assert status == 403
    assert service.calls == []


def test_admin_export_and_import_reach_service():
    query = {"platform": "astrbot", "tenant_id": "t", "group_id": "g", "all": "1"}
    api, service = make_api(
        username="admin", query=query, admin_usernames=("admin",),
    )
    payload = asyncio.run(api.h_export())
    assert payload["status"] == "ok"
    name, kwargs = service.calls[-1]
    assert name == "export_group_payload"
    assert kwargs["include_all"] is True
    assert kwargs["username"] == "admin"
    assert kwargs["group_id"] == "g"

    api, service = make_api(
        username="admin", query=query, admin_usernames=("admin",),
        body={"memories": []},
    )
    result = asyncio.run(api.h_import())
    assert result["status"] == "ok"
    name, kwargs = service.calls[-1]
    assert name == "import_group_payload"
    assert kwargs["payload"] == {"memories": []}
    assert kwargs["username"] == "admin"


def test_missing_username_is_forbidden():
    api, service = make_api(username=None)
    payload, status = asyncio.run(api.h_overview())
    assert status == 403
    assert payload["error"] == "AuthorizationDenied"
    assert service.calls == []


def test_admin_whitelist_denies_other_users():
    api, service = make_api(username="someone", admin_usernames=("admin",))
    payload, status = asyncio.run(api.h_overview())
    assert status == 403
    assert service.calls == []

    api, service = make_api(username="admin", admin_usernames=("admin",))
    payload = asyncio.run(api.h_overview())
    assert payload["status"] == "ok"
    assert service.calls == [("overview", {})]


def test_group_query_params_and_personal_scope_rejection():
    api, service = make_api(query={
        "platform": "astrbot", "tenant_id": "t1", "group_id": "g1",
    })
    result = asyncio.run(api.h_memories())
    assert result["status"] == "ok"
    assert service.calls[-1] == ("list_memories", {
        "platform": "astrbot", "tenant_id": "t1", "group_id": "g1",
        "limit": None, "cursor": None, "memory_type": None, "status": None,
    })

    api, service = make_api(query={"scope": "PERSONAL", "group_id": "g1"})
    payload, status = asyncio.run(api.h_memories())
    assert status == 403
    assert service.calls == []


def test_path_params_and_error_mapping():
    api, service = make_api(query={
        "platform": "astrbot", "tenant_id": "t1", "group_id": "g1",
    })
    assert asyncio.run(api.h_memory(id="m1"))["data"] == {"id": "m1"}
    payload, status = asyncio.run(api.h_memory(id="missing"))
    assert status == 404
    assert payload["message"] == "not found"

    api, _ = make_api(query={
        "platform": "astrbot", "tenant_id": "t1", "group_id": "g1",
        "limit": "bad",
    })
    payload, status = asyncio.run(api.h_memories())
    assert status == 400
    assert payload["error"] == "ValidationError"


def test_retrieval_debug_body_and_personal_rejection():
    api, service = make_api(body={
        "platform": "astrbot", "tenant_id": "t1", "group_id": "g1",
        "query": "复盘", "limit": 5,
    })
    result = asyncio.run(api.h_retrieval_debug())
    assert result["status"] == "ok"
    assert service.calls[-1] == ("retrieval_debug", {
        "platform": "astrbot", "tenant_id": "t1", "group_id": "g1",
        "query": "复盘", "limit": 5,
    })

    api, service = make_api(body={"scope": "PERSONAL", "query": "x"})
    payload, status = asyncio.run(api.h_retrieval_debug())
    assert status == 403
    assert service.calls == []

    api, _ = make_api(body={
        "platform": "astrbot", "tenant_id": "t1", "group_id": "g1",
        "query": "boom",
    })
    payload, status = asyncio.run(api.h_retrieval_debug())
    assert status == 503
    assert payload["message"] == "service unavailable"


def test_settings_and_maintenance_are_read_only():
    api, service = make_api()
    assert asyncio.run(api.h_settings())["data"] == {"candidate_ttl_days": 7}
    assert asyncio.run(api.h_maintenance_status())["data"] == {"schema_version": 10}
    methods = {name for name, _ in service.calls}
    assert methods == {"settings", "maintenance_status"}


def test_maintenance_backup_requires_admin_and_confirm():
    api, service = make_api(username="admin", body={"confirm": True})
    payload, status = asyncio.run(api.h_maintenance_backup())
    assert status == 403
    assert service.calls == []

    api, service = make_api(
        username="admin", admin_usernames=("admin",), body={},
    )
    payload, status = asyncio.run(api.h_maintenance_backup())
    assert status == 400
    assert payload["error"] == "ValidationError"

    api, service = make_api(
        username="admin", admin_usernames=("admin",), body={"confirm": True},
    )
    assert asyncio.run(api.h_maintenance_backup())["status"] == "ok"
    assert service.calls == [("create_backup", {})]


def test_maintenance_restore_dry_run_and_confirm():
    api, service = make_api(
        username="admin", admin_usernames=("admin",),
        body={"backup_id": "b1", "dry_run": True},
    )
    result = asyncio.run(api.h_maintenance_restore())
    assert result["status"] == "ok"
    assert service.calls[-1] == ("restore_backup", {
        "backup_id": "b1", "mode": "merge", "dry_run": True, "confirm": False,
    })

    api, service = make_api(
        username="admin", admin_usernames=("admin",),
        body={"backup_id": "b1"},
    )
    payload, status = asyncio.run(api.h_maintenance_restore())
    assert status == 400

    api, service = make_api(
        username="admin", admin_usernames=("admin",),
        body={"backup_id": "b1", "mode": "revert", "confirm": True},
    )
    assert asyncio.run(api.h_maintenance_restore())["status"] == "ok"
    assert service.calls[-1][1]["mode"] == "revert"
    assert service.calls[-1][1]["confirm"] is True


def test_maintenance_reindex_requires_confirm():
    api, service = make_api(username="admin", admin_usernames=("admin",))
    payload, status = asyncio.run(api.h_maintenance_reindex())
    assert status == 400
    assert service.calls == []

    api, service = make_api(
        username="admin", admin_usernames=("admin",), body={"confirm": True},
    )
    assert asyncio.run(api.h_maintenance_reindex())["data"] == {
        "rebuilt": True, "documents": 1,
    }
