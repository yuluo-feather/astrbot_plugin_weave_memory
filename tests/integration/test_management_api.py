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
        return {"schema_version": 7}


def make_api(*, username="admin", query=None, body=None,
             admin_usernames=()):
    request = FakeRequest(username=username, query=query, body=body)
    service = FakeService()
    api = AstrBotManagementApi(
        service, admin_usernames=admin_usernames,
        request_provider=lambda: request,
    )
    return api, service


def test_register_all_read_only_routes():
    api, _ = make_api()
    context = FakeContext()
    assert api.register(context) is True
    assert len(context.routes) == 9
    routes = {route: methods for route, _, methods, _ in context.routes}
    assert routes["/astrbot_plugin_weave_memory/v1/overview"] == ["GET"]
    assert routes["/astrbot_plugin_weave_memory/v1/retrieval/debug"] == ["POST"]
    assert "/astrbot_plugin_weave_memory/v1/memories/<id>" in routes
    assert "/astrbot_plugin_weave_memory/v1/memories/<id>/history" in routes
    assert api.register(object()) is False


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
    assert asyncio.run(api.h_maintenance_status())["data"] == {"schema_version": 7}
    methods = {name for name, _ in service.calls}
    assert methods == {"settings", "maintenance_status"}
