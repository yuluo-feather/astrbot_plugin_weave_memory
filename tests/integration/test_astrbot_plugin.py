import importlib.util
import shutil
import sys
import types
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

    registered = {}

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
        llm_tool=passthrough,
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
        FakeEvent(message="/织忆 删除 m1"),
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


def test_plugin_agent_tool_saves_agent_source_and_respects_opt_out(monkeypatch, tmp_path):
    import asyncio

    plugin, _ = load_plugin(
        monkeypatch, tmp_path, config={"agent_write_enabled": True},
    )
    saved = asyncio.run(collect(plugin.memory_remember_tool(
        FakeEvent(message="记住我对花生过敏"), "我对花生过敏", "PREFERENCE",
    )))[0]
    assert "已保存记忆" in saved
    row = plugin._database.connection.execute(
        "SELECT m.content, s.source_type, s.operator_type FROM memories m "
        "JOIN memory_sources s ON s.memory_id=m.id",
    ).fetchone()
    assert row["content"] == "我对花生过敏"
    assert row["source_type"] == "AGENT"
    assert row["operator_type"] == "agent"

    asyncio.run(collect(plugin.memory_revoke(FakeEvent(message="/织忆 撤销授权"))))
    blocked = asyncio.run(collect(plugin.memory_remember_tool(
        FakeEvent(message="记住我喜欢跑步"), "我喜欢跑步",
    )))[0]
    assert "未保存" in blocked
    assert plugin._database.connection.execute(
        "SELECT count(*) FROM memories",
    ).fetchone()[0] == 1

    group_blocked = asyncio.run(collect(plugin.memory_remember_tool(
        FakeEvent(group_id="g1", message="记住我喜欢跑步"), "我喜欢跑步",
    )))[0]
    assert "个人记忆只在私聊生效" in group_blocked
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
        FakeEvent(message=f"/织忆 删除 {memory_id}"),
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


def test_plugin_delete_is_irreversible_and_clear_requires_confirmation(monkeypatch, tmp_path):
    import asyncio

    plugin, _ = load_plugin(monkeypatch, tmp_path)
    asyncio.run(collect(plugin.memory_grant(FakeEvent(message="/织忆 授权"))))
    saved = asyncio.run(collect(plugin.memory_save(FakeEvent(message="/织忆 保存 喜欢徒步"))))[0]
    memory_id = saved.split("\n", 1)[0].split("：", 1)[1]

    # Missing confirmation is a no-op.
    prompt = asyncio.run(collect(plugin.memory_clear(FakeEvent(message="/织忆 清空"))))[0]
    assert "确认执行" in prompt
    assert plugin._database.connection.execute("SELECT count(*) FROM memories").fetchone()[0] == 1

    deleted = asyncio.run(collect(plugin.memory_delete(
        FakeEvent(message=f"/织忆 删除 {memory_id}"),
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
    assert "Conflict" in stale_result[0]
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
