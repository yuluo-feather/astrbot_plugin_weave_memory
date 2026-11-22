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
    def __init__(self, *, user="u1", platform_id="instance-1", group_id="", message=""):
        self.message_obj = types.SimpleNamespace(group_id=group_id)
        self.message_str = message
        self._user = user
        self._platform_id = platform_id

    def is_private_chat(self):
        return not bool(self.message_obj.group_id)

    def get_group_id(self):
        return self.message_obj.group_id

    def get_platform_name(self):
        return "test-platform"

    def get_platform_id(self):
        return self._platform_id

    def get_sender_id(self):
        return self._user

    def plain_result(self, text):
        return text


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

    def command_group(name, **kwargs):
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
        EventMessageType=types.SimpleNamespace(PRIVATE_MESSAGE="private"),
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


def load_plugin(monkeypatch, tmp_path):
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
    return module.WeaveMemoryPlugin(object()), star_module


async def collect(generator):
    return [item async for item in generator]


def test_plugin_grant_save_and_private_only_temporary_injection(monkeypatch, tmp_path):
    import asyncio

    plugin, _ = load_plugin(monkeypatch, tmp_path)
    user_event = FakeEvent(message="/记忆 授权")
    grant_result = asyncio.run(collect(plugin.memory_grant(user_event)))
    assert "已授权" in grant_result[0]

    save_event = FakeEvent(message="/记忆 保存 我喜欢黑咖啡")
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
        FakeEvent(message="/记忆 查询 黑咖啡"),
    )))
    assert "检索到的个人记忆" in query_result[0]

    group_request = types.SimpleNamespace(extra_user_content_parts=[])
    asyncio.run(plugin.inject_personal_memory(
        FakeEvent(group_id="group-1", message="你记得什么？"), group_request,
    ))
    assert group_request.extra_user_content_parts == []
    asyncio.run(plugin.terminate())


def test_plugin_consent_revocation_stops_injection_and_data_survives(monkeypatch, tmp_path):
    import asyncio

    plugin, _ = load_plugin(monkeypatch, tmp_path)
    event = FakeEvent(message="/记忆 授权")
    asyncio.run(collect(plugin.memory_grant(event)))
    asyncio.run(collect(plugin.memory_save(FakeEvent(message="/记忆 保存 我住在东京"))))
    asyncio.run(collect(plugin.memory_revoke(FakeEvent(message="/记忆 撤销授权"))))

    request = types.SimpleNamespace(extra_user_content_parts=[])
    asyncio.run(plugin.inject_personal_memory(FakeEvent(message="我住在哪里？"), request))
    assert request.extra_user_content_parts == []

    # Regranting reveals the retained memory again; revoke is not deletion.
    asyncio.run(collect(plugin.memory_grant(FakeEvent(message="/记忆 授权"))))
    request2 = types.SimpleNamespace(extra_user_content_parts=[])
    asyncio.run(plugin.inject_personal_memory(FakeEvent(message="我住在哪里？"), request2))
    assert request2.extra_user_content_parts
    assert "东京" in request2.extra_user_content_parts[0].text
    asyncio.run(plugin.terminate())


def test_plugin_persistence_survives_reload(monkeypatch, tmp_path):
    import asyncio

    plugin1, _ = load_plugin(monkeypatch, tmp_path)
    asyncio.run(collect(plugin1.memory_grant(FakeEvent(message="/记忆 授权"))))
    asyncio.run(collect(plugin1.memory_save(FakeEvent(message="/记忆 保存 用户喜欢徒步"))))
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
    asyncio.run(collect(plugin.memory_grant(FakeEvent(message="/记忆 授权"))))
    saved = asyncio.run(collect(plugin.memory_save(FakeEvent(message="/记忆 保存 喜欢徒步"))))[0]
    memory_id = saved.split("\n", 1)[0].split("：", 1)[1]

    # Missing confirmation is a no-op.
    prompt = asyncio.run(collect(plugin.memory_clear(FakeEvent(message="/记忆 清空"))))[0]
    assert "确认执行" in prompt
    assert plugin._database.connection.execute("SELECT count(*) FROM memories").fetchone()[0] == 1

    deleted = asyncio.run(collect(plugin.memory_delete(
        FakeEvent(message=f"/记忆 删除 {memory_id}"),
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
    asyncio.run(collect(plugin.memory_grant(FakeEvent(message="/记忆 授权"))))
    asyncio.run(collect(plugin.memory_save(FakeEvent(message="/记忆 保存 喜欢游泳"))))
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
    asyncio.run(collect(plugin.memory_grant(FakeEvent(message="/记忆 授权"))))
    save_result = asyncio.run(collect(plugin.memory_save(
        FakeEvent(message="/记忆 保存 我喜欢无糖茶"),
    )))
    memory_id = save_result[0].split("：", 1)[1].splitlines()[0]
    list_result = asyncio.run(collect(plugin.memory_list(FakeEvent(message="/记忆 列表"))))
    assert "revision 1" in list_result[0]

    update_result = asyncio.run(collect(plugin.memory_update(FakeEvent(
        message=f"/记忆 更新 {memory_id} 1 我更喜欢无糖绿茶",
    ))))
    assert "revision 2" in update_result[0]
    updated_list = asyncio.run(collect(plugin.memory_list(FakeEvent(message="/记忆 列表"))))
    assert "revision 2" in updated_list[0]

    stale_result = asyncio.run(collect(plugin.memory_update(FakeEvent(
        message=f"/记忆 更新 {memory_id} 1 我喜欢热绿茶",
    ))))
    assert "Conflict" in stale_result[0]
    asyncio.run(plugin.terminate())


def test_plugin_scope_close_revokes_consent_and_stops_injection(monkeypatch, tmp_path):
    import asyncio

    plugin, _ = load_plugin(monkeypatch, tmp_path)
    user_event = FakeEvent(message="/记忆 授权")
    asyncio.run(collect(plugin.memory_grant(user_event)))
    asyncio.run(collect(plugin.memory_save(
        FakeEvent(message="/记忆 保存 我喜欢热可可"),
    )))
    close_result = asyncio.run(collect(plugin.memory_disable(
        FakeEvent(message="/记忆 关闭 确认"),
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
    asyncio.run(collect(plugin.memory_grant(FakeEvent(message="/记忆 授权"))))
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
        FakeEvent(message="/记忆 关闭 确认"),
    )))
    assert "个人记忆空间已关闭" in failed[0]
    assert "请重试" in failed[0]

    retried = asyncio.run(collect(plugin.memory_disable(
        FakeEvent(message="/记忆 关闭 确认"),
    )))
    assert "永久关闭" in retried[0]
    asyncio.run(plugin.terminate())
