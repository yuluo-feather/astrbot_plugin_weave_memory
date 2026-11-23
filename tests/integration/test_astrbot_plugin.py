import importlib.util
import shutil
import sys
import types
from functools import partial
from pathlib import Path

import pytest


class FakeTextPart:
    def __init__(self, text):
        self.text = text
        self.is_temp = False

    def mark_as_temp(self):
        self.is_temp = True
        return self


class FakeEvent:
    def __init__(self, *, user="u1", platform_id="instance-1", group_id="",
                 message="", group_owner=None, group_admins=None):
        group = None
        if group_id:
            group = types.SimpleNamespace(
                group_id=group_id,
                group_owner=group_owner,
                group_admins=group_admins,
            )
        self.message_obj = types.SimpleNamespace(group_id=group_id, group=group)
        self.message_str = message
        self._user = user
        self._platform_id = platform_id
        self.sent = []
        self._extras = {}

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def is_private_chat(self):
        return not bool(self.message_obj.group_id)

    def get_group_id(self):
        return self.message_obj.group_id

    def is_admin(self):
        group = self.message_obj.group
        return bool(group and group.group_owner == self._user)

    def get_platform_name(self):
        return "test-platform"

    def get_platform_id(self):
        return self._platform_id

    def get_sender_id(self):
        return self._user

    def plain_result(self, text):
        return text

    async def send(self, result):
        self.sent.append(result)


def install_astrbot_stubs(monkeypatch, data_dir):
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    core_module = types.ModuleType("astrbot.core")
    core_agent_module = types.ModuleType("astrbot.core.agent")
    core_agent_message_module = types.ModuleType("astrbot.core.agent.message")
    core_agent_message_module.TextPart = FakeTextPart
    core_module.agent = core_agent_module
    core_agent_module.message = core_agent_message_module
    event_module = types.ModuleType("astrbot.api.event")
    star_module = types.ModuleType("astrbot.api.star")
    provider_module = types.ModuleType("astrbot.api.provider")

    def passthrough(*args, **kwargs):
        return lambda fn: fn

    registered = {"tools": {}}

    def llm_tool(name=None, **kwargs):
        def decorate(fn):
            registered["tools"][name or fn.__name__] = fn
            return fn
        return decorate

    def command_group(name, **kwargs):
        registered["name"] = name
        registered["alias"] = kwargs.get("alias")

        def decorate(fn):
            def command(subcommand):
                return passthrough()
            fn.command = command
            return fn
        return decorate

    filter_obj = types.SimpleNamespace(
        command=passthrough,
        command_group=command_group,
        event_message_type=passthrough,
        on_llm_request=passthrough,
        on_llm_response=passthrough,
        on_using_llm_tool=passthrough,
        on_llm_tool_respond=passthrough,
        llm_tool=llm_tool,
        EventMessageType=types.SimpleNamespace(
            PRIVATE_MESSAGE="private", GROUP_MESSAGE="group",
        ),
        registered=registered,
    )
    event_module.filter = filter_obj
    event_module.AstrMessageEvent = FakeEvent

    class Star:
        def __init__(self, context):
            self.context = context

    class StarTools:
        @staticmethod
        def get_data_dir():
            Path(data_dir).mkdir(parents=True, exist_ok=True)
            return Path(data_dir)

    star_module.Star = Star
    star_module.Context = object
    star_module.StarTools = StarTools
    provider_module.ProviderRequest = object
    api.logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )
    api.event = event_module
    api.star = star_module
    api.provider = provider_module
    astrbot.api = api

    modules = {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.core": core_module,
        "astrbot.core.agent": core_agent_module,
        "astrbot.core.agent.message": core_agent_message_module,
        "astrbot.api.event": event_module,
        "astrbot.api.star": star_module,
        "astrbot.api.provider": provider_module,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return star_module


def load_plugin(monkeypatch, tmp_path, config=None):
    star_module = install_astrbot_stubs(monkeypatch, tmp_path)
    root = Path(__file__).parents[2]
    source_main = root / "main.py"
    package_dir = tmp_path / "plugin_package"
    package_dir.mkdir(exist_ok=True)
    shutil.copy2(source_main, package_dir / "main.py")
    for package in ("core", "app", "adapters"):
        if not (package_dir / package).exists():
            shutil.copytree(
                root / package, package_dir / package,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )
    package_name = f"weave_plugin_test_{id(tmp_path)}"
    package = types.ModuleType(package_name)
    package.__path__ = [str(package_dir)]
    monkeypatch.setitem(sys.modules, package_name, package)
    module_name = f"{package_name}.main"
    spec = importlib.util.spec_from_file_location(module_name, package_dir / "main.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module.WeaveMemoryPlugin(object(), config), star_module


async def collect(generator):
    return [item async for item in generator]


def bind_plugin_tool(plugin, event, *, name, handler_name, args):
    import asyncio
    handler = getattr(type(plugin), handler_name)
    tool = types.SimpleNamespace(
        name=name,
        handler=partial(handler, plugin),
        handler_module_path=plugin.__class__.__module__,
        parameters={"properties": {key: {} for key in args}},
    )
    asyncio.run(plugin.weave_bind_tool_call(event, tool, dict(args)))


def test_plugin_registers_only_zhuyi_prefix_and_help(monkeypatch, tmp_path):
    import asyncio
    import sys

    plugin, _ = load_plugin(monkeypatch, tmp_path)
    filter_obj = sys.modules["astrbot.api.event"].filter
    assert filter_obj.registered["name"] == "织忆"
    assert filter_obj.registered["alias"] == {"/织忆"}
    help_text = plugin._commands.help()
    assert "默认已开启" in help_text
    assert "/织忆 保存" in help_text
    assert "/记忆" not in help_text

    asyncio.run(collect(plugin.memory_grant(FakeEvent(message="/织忆 授权"))))
    saved = asyncio.run(collect(plugin.memory_save(
        FakeEvent(message="/织忆 保存 前缀回归"),
    )))[0]
    assert "已保存记忆" in saved
    listed = asyncio.run(collect(plugin.memory_list(
        FakeEvent(message="/织忆 列表"),
    )))[0]
    assert "前缀回归" in listed
    asyncio.run(plugin.terminate())


def test_plugin_group_admin_config_parsing(monkeypatch, tmp_path):
    plugin, _ = load_plugin(monkeypatch, tmp_path)
    mapping = plugin._explicit_group_admins({
        "group_admin_ids": {"p|t|g": ["u1", "u2"]},
    })
    assert mapping == {("p", "t", "g"): ("u1", "u2")}
    mapping2 = plugin._explicit_group_admins({
        "group_admin_ids": [{
            "platform": "p", "tenant_id": "t", "group_id": "g",
            "admin_ids": ["u3"],
        }],
    })
    assert mapping2 == {("p", "t", "g"): ("u3",)}
    assert plugin._explicit_group_admins({"group_admin_ids": "bad"}) == {}


def test_plugin_group_shared_enable_save_inject_close_clear_destroy(monkeypatch, tmp_path):
    import asyncio

    plugin, _ = load_plugin(monkeypatch, tmp_path)
    admin = dict(user="admin-1", group_id="g1", group_owner="admin-1")
    member = dict(user="member-1", group_id="g1", group_owner="admin-1")

    help_text = asyncio.run(collect(plugin.memory_group_help(
        FakeEvent(message="/织忆 群帮助", **member),
    )))[0]
    assert "群保存" in help_text
    assert "群导出" in help_text
    before = asyncio.run(collect(plugin.memory_group_status(
        FakeEvent(message="/织忆 群状态", **member),
    )))[0]
    assert "尚未开启" in before

    opened = asyncio.run(collect(plugin.memory_group_enable(
        FakeEvent(message="/织忆 群开启", **admin),
    )))[0]
    assert "已开启" in opened

    saved = asyncio.run(collect(plugin.memory_group_save(
        FakeEvent(message="/织忆 群保存 agreement 周五复盘", **admin),
    )))[0]
    assert "已保存群记忆" in saved
    listed = asyncio.run(collect(plugin.memory_group_list(
        FakeEvent(message="/织忆 群列表", **member),
    )))[0]
    assert "周五复盘" in listed

    exported = asyncio.run(collect(plugin.memory_group_export(
        FakeEvent(message="/织忆 群导出 json", **admin),
    )))[0]
    assert "周五复盘" in exported
    export_denied = asyncio.run(collect(plugin.memory_group_export(
        FakeEvent(message="/织忆 群导出 json", **member),
    )))[0]
    assert "失败" in export_denied

    request = types.SimpleNamespace(extra_user_content_parts=[])
    asyncio.run(plugin.inject_personal_memory(
        FakeEvent(message="周五复盘", **member), request,
    ))
    assert request.extra_user_content_parts
    assert "周五复盘" in request.extra_user_content_parts[0].text
    assert request.extra_user_content_parts[0].is_temp is True

    denied = asyncio.run(collect(plugin.memory_group_save(
        FakeEvent(message="/织忆 群保存 topic 成员写入", **member),
    )))[0]
    assert "失败" in denied
    status = asyncio.run(collect(plugin.memory_group_status(
        FakeEvent(message="/织忆 群状态", **member),
    )))[0]
    assert "已开启" in status
    assert "管理员可用 是" in status

    asyncio.run(collect(plugin.memory_grant(FakeEvent(user="u1", message="/织忆 授权"))))
    asyncio.run(collect(plugin.memory_save(
        FakeEvent(user="u1", message="/织忆 保存 个人秘密"),
    )))
    group_request = types.SimpleNamespace(extra_user_content_parts=[])
    asyncio.run(plugin.inject_personal_memory(
        FakeEvent(message="个人秘密", **member), group_request,
    ))
    assert group_request.extra_user_content_parts == []
    private_request = types.SimpleNamespace(extra_user_content_parts=[])
    asyncio.run(plugin.inject_personal_memory(
        FakeEvent(user="u1", message="周五复盘"), private_request,
    ))
    assert private_request.extra_user_content_parts == []
    personal_request = types.SimpleNamespace(extra_user_content_parts=[])
    asyncio.run(plugin.inject_personal_memory(
        FakeEvent(user="u1", message="个人秘密"), personal_request,
    ))
    assert personal_request.extra_user_content_parts
    assert "个人秘密" in personal_request.extra_user_content_parts[0].text

    closed = asyncio.run(collect(plugin.memory_group_disable(
        FakeEvent(message="/织忆 群关闭", **admin),
    )))[0]
    assert "已关闭" in closed
    request2 = types.SimpleNamespace(extra_user_content_parts=[])
    asyncio.run(plugin.inject_personal_memory(
        FakeEvent(message="周五复盘", **member), request2,
    ))
    assert request2.extra_user_content_parts == []

    cleared = asyncio.run(collect(plugin.memory_group_clear(
        FakeEvent(message="/织忆 群清空 确认", **admin),
    )))[0]
    assert "已清空当前群" in cleared
    destroyed = asyncio.run(collect(plugin.memory_group_destroy(
        FakeEvent(message="/织忆 群销毁 确认", **admin),
    )))[0]
    assert "已销毁当前群数据" in destroyed
    asyncio.run(plugin.terminate())


def test_plugin_grant_save_and_private_only_temporary_injection(monkeypatch, tmp_path):
    import asyncio

    plugin, _ = load_plugin(monkeypatch, tmp_path)
    user_event = FakeEvent(message="/织忆 授权")
    grant_result = asyncio.run(collect(plugin.memory_grant(user_event)))
    assert "已授权" in grant_result[0]

    save_event = FakeEvent(message="/织忆 保存 我喜欢黑咖啡")
    save_result = asyncio.run(collect(plugin.memory_save(save_event)))
    assert "已保存记忆" in save_result[0]

    request = types.SimpleNamespace(extra_user_content_parts=[])
    asyncio.run(plugin.inject_personal_memory(
        FakeEvent(message="你还记得我喜欢喝什么咖啡吗？"), request,
    ))
    assert len(request.extra_user_content_parts) == 1
    assert request.extra_user_content_parts[0].is_temp is True
    assert "喜欢黑咖啡" in request.extra_user_content_parts[0].text

    query_result = asyncio.run(collect(plugin.memory_query(
        FakeEvent(message="/织忆 查询 黑咖啡"),
    )))
    assert "检索到的个人记忆" in query_result[0]

    group_request = types.SimpleNamespace(extra_user_content_parts=[])
    asyncio.run(plugin.inject_personal_memory(
        FakeEvent(group_id="group-1", message="你记得什么？"), group_request,
    ))
    assert group_request.extra_user_content_parts == []
    asyncio.run(plugin.terminate())


def test_plugin_isolates_users_and_unauthorized_injection(monkeypatch, tmp_path):
    import asyncio

    plugin, _ = load_plugin(monkeypatch, tmp_path)
    asyncio.run(collect(plugin.memory_grant(FakeEvent(user="u1", message="/织忆 授权"))))
    asyncio.run(collect(plugin.memory_save(
        FakeEvent(user="u1", message="/织忆 保存 用户A喜欢黑咖啡"),
    )))

    other_list = asyncio.run(collect(plugin.memory_list(
        FakeEvent(user="u2", message="/织忆 列表"),
    )))[0]
    assert "用户A" not in other_list
    request = types.SimpleNamespace(extra_user_content_parts=[])
    asyncio.run(plugin.inject_personal_memory(
        FakeEvent(user="u2", message="我喜欢什么？"), request,
    ))
    assert request.extra_user_content_parts == []

    own_list = asyncio.run(collect(plugin.memory_list(
        FakeEvent(user="u1", message="/织忆 列表"),
    )))[0]
    assert "用户A喜欢黑咖啡" in own_list
    asyncio.run(plugin.terminate())


def test_plugin_reports_pending_secure_purge(monkeypatch, tmp_path):
    import asyncio

    from data.plugins.astrbot_plugin_weave_memory.app.write_coordinator import (
        IndexState,
        OperationReceipt,
    )

    plugin, _ = load_plugin(monkeypatch, tmp_path)
    asyncio.run(collect(plugin.memory_grant(FakeEvent(message="/织忆 授权"))))

    async def fake_delete(*args, **kwargs):
        return OperationReceipt("op-delete", 1, IndexState.HEALTHY, purged=False)

    plugin._commands._lifecycle.delete_memory = fake_delete
    deleted = asyncio.run(collect(plugin.memory_delete(
        FakeEvent(message="/织忆 删除 m1 确认"),
    )))[0]
    assert "记忆已删除" in deleted
    assert "物理擦除未完成" in deleted
    assert "将在下次插件启动时自动重试" in deleted

    async def fake_clear(*args, **kwargs):
        return OperationReceipt("op-clear", 2, IndexState.HEALTHY, purged=False)

    plugin._commands._lifecycle.clear_scope = fake_clear
    cleared = asyncio.run(collect(plugin.memory_clear(
        FakeEvent(message="/织忆 清空 确认"),
    )))[0]
    assert "已清空 2 条记忆" in cleared
    assert "物理擦除未完成" in cleared
    assert "将在下次插件启动时自动重试" in cleared
    asyncio.run(plugin.terminate())


def test_plugin_consent_revocation_stops_injection_and_data_survives(monkeypatch, tmp_path):
    import asyncio

    plugin, _ = load_plugin(monkeypatch, tmp_path)
    event = FakeEvent(message="/织忆 授权")
    asyncio.run(collect(plugin.memory_grant(event)))
    asyncio.run(collect(plugin.memory_save(FakeEvent(message="/织忆 保存 我住在东京"))))
    asyncio.run(collect(plugin.memory_revoke(FakeEvent(message="/织忆 撤销授权"))))

    request = types.SimpleNamespace(extra_user_content_parts=[])
    asyncio.run(plugin.inject_personal_memory(FakeEvent(message="我住在哪里？"), request))
    assert request.extra_user_content_parts == []

    # Regranting reveals the retained memory again; revoke is not deletion.
    asyncio.run(collect(plugin.memory_grant(FakeEvent(message="/织忆 授权"))))
    request2 = types.SimpleNamespace(extra_user_content_parts=[])
    asyncio.run(plugin.inject_personal_memory(FakeEvent(message="我住在哪里？"), request2))
    assert request2.extra_user_content_parts
    assert "东京" in request2.extra_user_content_parts[0].text
    asyncio.run(plugin.terminate())


def test_plugin_auto_enroll_grants_once_and_notifies(monkeypatch, tmp_path):
    import asyncio

    plugin, _ = load_plugin(monkeypatch, tmp_path)
    first = FakeEvent(message="我喜欢黑咖啡")
    request = types.SimpleNamespace(extra_user_content_parts=[])
    asyncio.run(plugin.inject_personal_memory(first, request))

    assert len(first.sent) == 1
    assert "默认开启" in first.sent[0]
    assert request.extra_user_content_parts == []
    rows = plugin._database.connection.execute(
        "SELECT state, source FROM memory_consents ORDER BY rowid",
    ).fetchall()
    assert [(row["state"], row["source"]) for row in rows] == [("GRANTED", "auto_enroll")]

    second = FakeEvent(message="我喜欢黑咖啡")
    request2 = types.SimpleNamespace(extra_user_content_parts=[])
    asyncio.run(plugin.inject_personal_memory(second, request2))
    assert second.sent == []
    assert plugin._database.connection.execute(
        "SELECT count(*) FROM memory_consents",
    ).fetchone()[0] == 1
    asyncio.run(plugin.terminate())


def test_v13_agent_tool_names_follow_plan_prefix_and_scope(monkeypatch, tmp_path):
    import asyncio

    plugin, _ = load_plugin(monkeypatch, tmp_path)
    filter_obj = sys.modules["astrbot.api.event"].filter
    assert set(filter_obj.registered["tools"]) == {
        "weave_memory_remember",
        "weave_memory_search",
        "weave_memory_get",
        "weave_memory_update",
        "weave_memory_list",
        "weave_memory_history",
    }
    assert "memory_save" not in filter_obj.registered["tools"]
    asyncio.run(plugin.terminate())


def test_plugin_command_auto_enroll_notifies_user(monkeypatch, tmp_path):
    import asyncio

    plugin, _ = load_plugin(monkeypatch, tmp_path)
    result = asyncio.run(collect(plugin.memory_save(
        FakeEvent(message="/织忆 保存 我喜欢黑咖啡"),
    )))[0]
    assert "已保存记忆" in result
    assert "默认开启" in result
    asyncio.run(plugin.terminate())


def test_plugin_agent_tool_write_fails_closed_without_host_call_id(monkeypatch, tmp_path):
    import asyncio

    plugin, _ = load_plugin(monkeypatch, tmp_path)
    event = FakeEvent(message="记住我喜欢黑咖啡")
    bind_plugin_tool(
        plugin, event, name="weave_memory_remember",
        handler_name="memory_remember_tool",
        args={"content": "我喜欢黑咖啡", "memory_type": "PREFERENCE"},
    )
    result = asyncio.run(collect(plugin.memory_remember_tool(
        event, "我喜欢黑咖啡", "PREFERENCE",
    )))[0]
    assert "tool_call_id" in result
    assert "已保存记忆" not in result
    assert event.sent == []
    assert plugin._database.connection.execute(
        "SELECT count(*) FROM memories",
    ).fetchone()[0] == 0
    asyncio.run(plugin.terminate())

def test_plugin_writes_privacy_safe_recall_diagnostics_jsonl(monkeypatch, tmp_path):
    import asyncio
    import json

    plugin, _ = load_plugin(monkeypatch, tmp_path)
    asyncio.run(collect(plugin.memory_save(FakeEvent(
        message="/织忆 保存 我喜欢黑咖啡",
    ))))
    request = types.SimpleNamespace(extra_user_content_parts=[])
    asyncio.run(plugin.inject_personal_memory(
        FakeEvent(message="我喜欢什么咖啡？"), request,
    ))
    files = list((tmp_path / "recall_diagnostics").glob("recall-*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["trace_id"]
    assert record["query_hash"]
    assert record["scope"]["type"] == "PERSONAL"
    assert record["final_injected_ids"]
    assert record["estimated_tokens"] > 0
    assert "query" not in record
    assert "我喜欢什么咖啡" not in lines[0]
    assert "我喜欢黑咖啡" not in lines[0]
    asyncio.run(plugin.terminate())


def test_agent_search_diagnostics_record_ids_returned_to_tool(monkeypatch, tmp_path):
    import asyncio
    import json

    plugin, _ = load_plugin(monkeypatch, tmp_path)
    asyncio.run(collect(plugin.memory_save(FakeEvent(
        message="/织忆 保存 我喜欢黑咖啡",
    ))))
    event = FakeEvent(message="我喜欢什么咖啡？")
    bind_plugin_tool(
        plugin, event, name="weave_memory_search",
        handler_name="memory_search_tool",
        args={"query": "黑咖啡", "limit": 8},
    )
    result = asyncio.run(collect(plugin.memory_search_tool(
        event, "黑咖啡", 8,
    )))[0]
    returned = json.loads(result)
    assert returned

    files = list((tmp_path / "recall_diagnostics").glob("recall-*.jsonl"))
    assert len(files) == 1
    record = json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])
    assert record["final_injected_ids"] == [item["id"] for item in returned]
    assert record["estimated_tokens"] > 0
    asyncio.run(plugin.terminate())


def test_plugin_terminate_times_out_when_coordinator_hangs(monkeypatch, tmp_path):
    import asyncio
    import time

    plugin, _ = load_plugin(monkeypatch, tmp_path)
    module = sys.modules[plugin.__class__.__module__]
    monkeypatch.setattr(module, "_TERMINATE_TIMEOUT_SECONDS", 0.01)

    async def hang():
        await asyncio.sleep(10)

    monkeypatch.setattr(plugin._coordinator, "close", hang)
    started = time.monotonic()
    asyncio.run(plugin.terminate())
    assert time.monotonic() - started < 1
    assert plugin._database is None


def test_plugin_auto_enroll_disabled_requires_explicit_grant(monkeypatch, tmp_path):
    import asyncio

    plugin, _ = load_plugin(monkeypatch, tmp_path, config={"auto_enroll": False})
    event = FakeEvent(message="我喜欢黑咖啡")
    request = types.SimpleNamespace(extra_user_content_parts=[])
    asyncio.run(plugin.inject_personal_memory(event, request))

    assert event.sent == []
    assert request.extra_user_content_parts == []
    assert plugin._database.connection.execute(
        "SELECT count(*) FROM memory_consents",
    ).fetchone()[0] == 0

    asyncio.run(collect(plugin.memory_grant(FakeEvent(message="/织忆 授权"))))
    asyncio.run(collect(plugin.memory_save(FakeEvent(message="/织忆 保存 我喜欢黑咖啡"))))
    request2 = types.SimpleNamespace(extra_user_content_parts=[])
    asyncio.run(plugin.inject_personal_memory(FakeEvent(message="我喜欢什么咖啡"), request2))
    assert request2.extra_user_content_parts
    asyncio.run(plugin.terminate())


def test_plugin_agent_update_tool_fails_closed_on_astrbot_4294(monkeypatch, tmp_path):
    import asyncio

    plugin, _ = load_plugin(monkeypatch, tmp_path)
    for tool_name, handler_name, args in (
        ("weave_memory_update", "memory_update_tool", {
            "memory_id": "memory-not-used", "expected_revision": 1,
            "content": "新内容",
        }),
    ):
        event = FakeEvent(message="请明确保存或修改我的记忆")
        bind_plugin_tool(plugin, event, name=tool_name, handler_name=handler_name, args=args)
        result = asyncio.run(collect(getattr(plugin, handler_name)(event, **args)))[0]
        assert "tool_call_id" in result
    assert plugin._database.connection.execute(
        "SELECT count(*) FROM memories",
    ).fetchone()[0] == 0
    asyncio.run(plugin.terminate())

def test_plugin_agent_write_enabled_by_default_but_host_id_is_unavailable(monkeypatch, tmp_path):
    import asyncio

    plugin, _ = load_plugin(monkeypatch, tmp_path)
    event = FakeEvent(message="记住我对花生过敏")
    bind_plugin_tool(
        plugin, event, name="weave_memory_remember",
        handler_name="memory_remember_tool",
        args={"content": "我对花生过敏", "memory_type": "PREFERENCE"},
    )
    result = asyncio.run(collect(plugin.memory_remember_tool(
        event, "我对花生过敏", "PREFERENCE",
    )))[0]
    assert "tool_call_id" in result
    assert plugin._database.connection.execute(
        "SELECT count(*) FROM memories",
    ).fetchone()[0] == 0
    asyncio.run(plugin.terminate())

def test_v13_agent_read_tools_are_personal_scoped_and_revisioned(monkeypatch, tmp_path):
    import asyncio
    import json

    plugin, _ = load_plugin(monkeypatch, tmp_path)
    saved = asyncio.run(collect(plugin.memory_save(
        FakeEvent(message="/织忆 保存 喜欢黑咖啡"),
    )))[0]
    assert "已保存记忆" in saved
    memory_id = plugin._database.connection.execute("SELECT id FROM memories").fetchone()[0]

    def invoke(tool_name, handler_name, args):
        event = FakeEvent(message="读取我的个人记忆")
        bind_plugin_tool(plugin, event, name=tool_name, handler_name=handler_name, args=args)
        return asyncio.run(collect(getattr(plugin, handler_name)(event, **args)))[0]

    got = json.loads(invoke("weave_memory_get", "memory_get_tool", {"memory_id": memory_id}))
    assert got["content"] == "喜欢黑咖啡"
    assert got["revision"] == 1
    history = json.loads(invoke("weave_memory_history", "memory_history_tool", {"memory_id": memory_id}))
    assert history["revisions"][0]["revision"] == 1
    assert history["sources"][0]["source_type"] == "EXPLICIT"
    page = json.loads(invoke("weave_memory_list", "memory_list_tool", {"offset": 0, "limit": 10}))
    assert page["total"] == 1
    assert page["items"][0]["id"] == memory_id
    search = json.loads(invoke("weave_memory_search", "memory_search_tool", {"query": "黑咖啡", "limit": 8}))
    assert search[0]["id"] == memory_id

    foreign_event = FakeEvent(user="u2", message="读取他人的记忆")
    bind_plugin_tool(
        plugin, foreign_event, name="weave_memory_get", handler_name="memory_get_tool",
        args={"memory_id": memory_id},
    )
    foreign_result = asyncio.run(collect(plugin.memory_get_tool(
        foreign_event, memory_id,
    )))[0]
    assert "没有找到对应的个人记忆" in foreign_result

    group_event = FakeEvent(group_id="g1", message="读取群聊中的个人记忆")
    bind_plugin_tool(
        plugin, group_event, name="weave_memory_get", handler_name="memory_get_tool",
        args={"memory_id": memory_id},
    )
    group_result = asyncio.run(collect(plugin.memory_get_tool(
        group_event, memory_id,
    )))[0]
    assert "尚未授权" in group_result

    blocked = invoke("weave_memory_update", "memory_update_tool", {
        "memory_id": memory_id, "expected_revision": 1,
        "content": "喜欢无糖黑咖啡",
    })
    assert "tool_call_id" in blocked
    update = asyncio.run(collect(plugin.memory_update(FakeEvent(
        message=f"/织忆 更新 {memory_id} 1 喜欢无糖黑咖啡",
    ))))[0]
    assert "revision 2" in update
    stale = asyncio.run(collect(plugin.memory_update(FakeEvent(
        message=f"/织忆 更新 {memory_id} 1 旧 revision 不得覆盖",
    ))))[0]
    assert "revision conflict" in stale
    asyncio.run(plugin.terminate())


def test_v13_agent_read_tools_reject_foreign_and_group_scopes(monkeypatch, tmp_path):
    import asyncio

    plugin, _ = load_plugin(monkeypatch, tmp_path)
    saved = asyncio.run(collect(plugin.memory_save(FakeEvent(
        message="/织忆 保存 仅属于原用户的秘密记忆正文",
    ))))[0]
    assert "已保存记忆" in saved
    memory_id = plugin._database.connection.execute(
        "SELECT id FROM memories",
    ).fetchone()[0]

    read_tools = (
        ("weave_memory_search", "memory_search_tool", {"query": "秘密记忆", "limit": 8}),
        ("weave_memory_get", "memory_get_tool", {"memory_id": memory_id}),
        ("weave_memory_list", "memory_list_tool", {"offset": 0, "limit": 20}),
        ("weave_memory_history", "memory_history_tool", {"memory_id": memory_id}),
    )
    for event in (
        FakeEvent(user="u2", message="读取其他用户的记忆"),
        FakeEvent(group_id="g1", message="在群聊中读取个人记忆"),
    ):
        for tool_name, handler_name, args in read_tools:
            bind_plugin_tool(
                plugin, event, name=tool_name, handler_name=handler_name, args=args,
            )
            result = asyncio.run(collect(
                getattr(plugin, handler_name)(event, **args)
            ))[0]
            assert "仅属于原用户的秘密记忆正文" not in result
            assert memory_id not in result

    asyncio.run(plugin.terminate())


def test_v13_personal_management_commands_status_stats_operations_export(monkeypatch, tmp_path):
    import asyncio
    import json

    plugin, _ = load_plugin(monkeypatch, tmp_path)
    saved = asyncio.run(collect(plugin.memory_save(FakeEvent(
        message="/织忆 保存 仅属于用户的测试正文",
    ))))[0]
    memory_id = saved.split("\n", 1)[0].split("：", 1)[1]
    asyncio.run(collect(plugin.memory_save(FakeEvent(
        user="u2", message="/织忆 保存 其他用户的记忆",
    ))))

    status = json.loads(asyncio.run(collect(plugin.memory_status(FakeEvent(
        message="/织忆 状态",
    ))))[0])
    assert status["scope"] == "PERSONAL"
    assert status["consent"] == "GRANTED"
    assert status["memory"]["total"] == 1
    assert "healthy" in status["index"]
    assert status["index"]["documents"] == 1

    stats = json.loads(asyncio.run(collect(plugin.memory_stats(FakeEvent(
        message="/织忆 统计",
    ))))[0])
    assert stats["by_type"]["FACT"] == 1

    deleted = asyncio.run(collect(plugin.memory_delete(FakeEvent(
        message=f"/织忆 删除 {memory_id} 确认",
    ))))[0]
    assert "已删除" in deleted
    operations = json.loads(asyncio.run(collect(plugin.memory_operations(FakeEvent(
        message="/织忆 操作记录",
    ))))[0])
    assert operations["total"] == 1
    assert operations["items"][0]["operation_type"] == "DELETE_MEMORY"
    assert "仅属于用户的测试正文" not in json.dumps(operations, ensure_ascii=False)

    asyncio.run(collect(plugin.memory_save(FakeEvent(
        message="/织忆 保存 导出测试正文",
    ))))
    exported_json = asyncio.run(collect(plugin.memory_export(FakeEvent(
        message="/织忆 导出 json",
    ))))[0]
    export_payload = json.loads(exported_json)
    assert export_payload["format"] == "weave-memory-export-v1"
    assert export_payload["memories"][0]["content"] == "导出测试正文"
    exported_md = asyncio.run(collect(plugin.memory_export(FakeEvent(
        message="/织忆 导出 md",
    ))))[0]
    assert "导出测试正文" in exported_md
    asyncio.run(plugin.terminate())

def test_plugin_agent_write_can_be_disabled(monkeypatch, tmp_path):
    import asyncio

    plugin, _ = load_plugin(monkeypatch, tmp_path, config={"agent_write_enabled": False})
    result = asyncio.run(collect(plugin.memory_remember_tool(
        FakeEvent(message="记住我喜欢跑步"), "我喜欢跑步",
    )))[0]
    assert "已关闭" in result
    assert plugin._database.connection.execute(
        "SELECT count(*) FROM memories",
    ).fetchone()[0] == 0
    asyncio.run(plugin.terminate())


def test_plugin_commands_auto_enroll_without_explicit_grant(monkeypatch, tmp_path):
    import asyncio

    plugin, _ = load_plugin(monkeypatch, tmp_path)
    saved = asyncio.run(collect(plugin.memory_save(
        FakeEvent(message="/织忆 保存 我喜欢黑咖啡"),
    )))[0]
    assert "已保存记忆" in saved
    rows = plugin._database.connection.execute(
        "SELECT state, source FROM memory_consents ORDER BY rowid",
    ).fetchall()
    assert [(row["state"], row["source"]) for row in rows] == [("GRANTED", "auto_enroll")]

    listed = asyncio.run(collect(plugin.memory_list(
        FakeEvent(message="/织忆 列表"),
    )))[0]
    assert "我喜欢黑咖啡" in listed
    asyncio.run(plugin.terminate())


def test_plugin_close_works_after_revoke(monkeypatch, tmp_path):
    import asyncio

    plugin, _ = load_plugin(monkeypatch, tmp_path)
    asyncio.run(collect(plugin.memory_grant(FakeEvent(message="/织忆 授权"))))
    asyncio.run(collect(plugin.memory_save(FakeEvent(message="/织忆 保存 我住在东京"))))
    asyncio.run(collect(plugin.memory_revoke(FakeEvent(message="/织忆 撤销授权"))))
    closed = asyncio.run(collect(plugin.memory_disable(
        FakeEvent(message="/织忆 关闭 确认"),
    )))[0]
    assert "已永久关闭" in closed

    request = types.SimpleNamespace(extra_user_content_parts=[])
    asyncio.run(plugin.inject_personal_memory(FakeEvent(message="我住在哪里？"), request))
    assert request.extra_user_content_parts == []
    asyncio.run(plugin.terminate())


def test_plugin_agent_tool_failure_does_not_escape(monkeypatch, tmp_path):
    import asyncio

    plugin, _ = load_plugin(
        monkeypatch, tmp_path, config={"agent_write_enabled": True},
    )

    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    plugin._commands.remember = boom
    result = asyncio.run(collect(plugin.memory_remember_tool(
        FakeEvent(message="记住我喜欢跑步"), "我喜欢跑步",
    )))[0]
    assert "未保存" in result
    asyncio.run(plugin.terminate())


def test_plugin_delete_and_clear_invalidate_injection(monkeypatch, tmp_path):
    import asyncio

    plugin, _ = load_plugin(monkeypatch, tmp_path)
    saved = asyncio.run(collect(plugin.memory_save(
        FakeEvent(message="/织忆 保存 删除前记忆"),
    )))[0]
    memory_id = saved.split("\n", 1)[0].split("：", 1)[1]
    asyncio.run(collect(plugin.memory_delete(
        FakeEvent(message=f"/织忆 删除 {memory_id} 确认"),
    )))
    request = types.SimpleNamespace(extra_user_content_parts=[])
    asyncio.run(plugin.inject_personal_memory(
        FakeEvent(message="删除前记忆"), request,
    ))
    assert request.extra_user_content_parts == []

    asyncio.run(collect(plugin.memory_save(
        FakeEvent(message="/织忆 保存 清空前记忆"),
    )))
    asyncio.run(collect(plugin.memory_clear(
        FakeEvent(message="/织忆 清空 确认"),
    )))
    request2 = types.SimpleNamespace(extra_user_content_parts=[])
    asyncio.run(plugin.inject_personal_memory(
        FakeEvent(message="清空前记忆"), request2,
    ))
    assert request2.extra_user_content_parts == []
    asyncio.run(plugin.terminate())


def test_plugin_persistence_survives_reload(monkeypatch, tmp_path):
    import asyncio

    plugin1, _ = load_plugin(monkeypatch, tmp_path)
    asyncio.run(collect(plugin1.memory_grant(FakeEvent(message="/织忆 授权"))))
    asyncio.run(collect(plugin1.memory_save(FakeEvent(message="/织忆 保存 用户喜欢徒步"))))
    asyncio.run(plugin1.terminate())

    plugin2, _ = load_plugin(monkeypatch, tmp_path)
    request = types.SimpleNamespace(extra_user_content_parts=[])
    asyncio.run(plugin2.inject_personal_memory(FakeEvent(message="用户喜欢什么？"), request))
    assert request.extra_user_content_parts
    assert "徒步" in request.extra_user_content_parts[0].text
    asyncio.run(plugin2.terminate())


def test_plugin_delete_and_clear_require_confirmation(monkeypatch, tmp_path):
    import asyncio

    plugin, _ = load_plugin(monkeypatch, tmp_path)
    asyncio.run(collect(plugin.memory_grant(FakeEvent(message="/织忆 授权"))))
    saved = asyncio.run(collect(plugin.memory_save(FakeEvent(message="/织忆 保存 喜欢徒步"))))[0]
    memory_id = saved.split("\n", 1)[0].split("：", 1)[1]

    # Missing confirmation is a no-op.
    prompt = asyncio.run(collect(plugin.memory_clear(FakeEvent(message="/织忆 清空"))))[0]
    assert "确认执行" in prompt
    assert plugin._database.connection.execute("SELECT count(*) FROM memories").fetchone()[0] == 1

    delete_prompt = asyncio.run(collect(plugin.memory_delete(FakeEvent(
        message=f"/织忆 删除 {memory_id}",
    ))))[0]
    assert "确认执行" in delete_prompt
    assert plugin._database.connection.execute("SELECT count(*) FROM memories").fetchone()[0] == 1

    deleted = asyncio.run(collect(plugin.memory_delete(
        FakeEvent(message=f"/织忆 删除 {memory_id} 确认"),
    )))[0]
    assert "已删除" in deleted
    assert plugin._database.connection.execute("SELECT count(*) FROM memories").fetchone()[0] == 0
    assert plugin._database.connection.execute(
        "SELECT count(*) FROM memory_operations WHERE operation_type='DELETE_MEMORY'",
    ).fetchone()[0] == 1
    asyncio.run(plugin.terminate())


def test_plugin_injection_failure_does_not_escape_to_chat(monkeypatch, tmp_path):
    import asyncio

    plugin, _ = load_plugin(monkeypatch, tmp_path)
    asyncio.run(collect(plugin.memory_grant(FakeEvent(message="/织忆 授权"))))
    asyncio.run(collect(plugin.memory_save(FakeEvent(message="/织忆 保存 喜欢游泳"))))
    plugin._database.connection.execute("DROP TABLE memory_fts")
    request = types.SimpleNamespace(extra_user_content_parts=[])
    # Hook catches the index failure and leaves the host request unchanged.
    asyncio.run(plugin.inject_personal_memory(FakeEvent(message="我喜欢什么运动？"), request))
    assert request.extra_user_content_parts == []
    asyncio.run(plugin.terminate())


def test_plugin_hook_isolates_unexpected_errors_but_preserves_cancellation(monkeypatch, tmp_path):
    import asyncio

    plugin, _ = load_plugin(monkeypatch, tmp_path)

    async def unexpected_error(_event):
        raise RuntimeError("simulated host API failure")

    plugin._injector.context_for = unexpected_error
    request = types.SimpleNamespace(extra_user_content_parts=[])
    # Unexpected integration-boundary errors are isolated from the host chat.
    asyncio.run(plugin.inject_personal_memory(FakeEvent(message="hello"), request))
    assert request.extra_user_content_parts == []

    async def cancelled(_event):
        raise asyncio.CancelledError

    plugin._injector.context_for = cancelled
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(plugin.inject_personal_memory(FakeEvent(message="hello"), request))
    asyncio.run(plugin.terminate())


def test_plugin_explicit_update_requires_revision_and_updates_memory(monkeypatch, tmp_path):
    import asyncio

    plugin, _ = load_plugin(monkeypatch, tmp_path)
    asyncio.run(collect(plugin.memory_grant(FakeEvent(message="/织忆 授权"))))
    save_result = asyncio.run(collect(plugin.memory_save(
        FakeEvent(message="/织忆 保存 我喜欢无糖茶"),
    )))
    memory_id = save_result[0].split("：", 1)[1].splitlines()[0]
    list_result = asyncio.run(collect(plugin.memory_list(FakeEvent(message="/织忆 列表"))))
    assert "revision 1" in list_result[0]

    update_result = asyncio.run(collect(plugin.memory_update(FakeEvent(
        message=f"/织忆 更新 {memory_id} 1 我更喜欢无糖绿茶",
    ))))
    assert "revision 2" in update_result[0]
    updated_list = asyncio.run(collect(plugin.memory_list(FakeEvent(message="/织忆 列表"))))
    assert "revision 2" in updated_list[0]

    stale_result = asyncio.run(collect(plugin.memory_update(FakeEvent(
        message=f"/织忆 更新 {memory_id} 1 我喜欢热绿茶",
    ))))
    assert "revision conflict" in stale_result[0]
    asyncio.run(plugin.terminate())


def test_plugin_scope_close_revokes_consent_and_stops_injection(monkeypatch, tmp_path):
    import asyncio

    plugin, _ = load_plugin(monkeypatch, tmp_path)
    user_event = FakeEvent(message="/织忆 授权")
    asyncio.run(collect(plugin.memory_grant(user_event)))
    asyncio.run(collect(plugin.memory_save(
        FakeEvent(message="/织忆 保存 我喜欢热可可"),
    )))
    close_result = asyncio.run(collect(plugin.memory_disable(
        FakeEvent(message="/织忆 关闭 确认"),
    )))
    assert "永久关闭" in close_result[0]
    request = types.SimpleNamespace(extra_user_content_parts=[])
    asyncio.run(plugin.inject_personal_memory(
        FakeEvent(message="你记得我喜欢喝什么吗？"), request,
    ))
    assert request.extra_user_content_parts == []
    asyncio.run(plugin.terminate())


def test_plugin_reports_closed_scope_when_consent_revoke_needs_retry(monkeypatch, tmp_path):
    import asyncio

    plugin, _ = load_plugin(monkeypatch, tmp_path)
    asyncio.run(collect(plugin.memory_grant(FakeEvent(message="/织忆 授权"))))
    repository = plugin._commands._consent._repository
    original_record = repository.record
    fail_once = True

    def fail_first_revoke(consent_event):
        nonlocal fail_once
        if consent_event.state.value == "REVOKED" and fail_once:
            fail_once = False
            import importlib
            package_name = plugin._commands._consent.__class__.__module__.split(".", 1)[0]
            errors = importlib.import_module(f"{package_name}.core.errors")
            raise errors.StorageError("simulated consent write failure")
        original_record(consent_event)

    monkeypatch.setattr(repository, "record", fail_first_revoke)
    failed = asyncio.run(collect(plugin.memory_disable(
        FakeEvent(message="/织忆 关闭 确认"),
    )))
    assert "个人记忆空间已关闭" in failed[0]
    assert "请重试" in failed[0]

    retried = asyncio.run(collect(plugin.memory_disable(
        FakeEvent(message="/织忆 关闭 确认"),
    )))
    assert "永久关闭" in retried[0]
    asyncio.run(plugin.terminate())
