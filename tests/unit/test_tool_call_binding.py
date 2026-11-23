from functools import partial
from types import SimpleNamespace

from data.plugins.astrbot_plugin_weave_memory.adapters.astrbot.tool_call_binding import (
    AstrBotToolCallBinding,
)


class FakeEvent:
    def __init__(self, plugins_name=None):
        self.plugins_name = plugins_name
        self.extras = {}

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)


def make_handler(name):
    async def handler(*args, **kwargs):
        return None
    handler.__module__ = "plugin.main"
    handler.__name__ = name
    return handler


def make_tool(name, handler_name, instance):
    handler = make_handler(handler_name)
    return SimpleNamespace(
        name=name,
        handler=partial(handler, instance),
        handler_module_path="plugin.main",
        parameters={"properties": {"query": {}}},
    )


def test_binding_accepts_only_plugin_owned_handler():
    plugin = object()
    binding = AstrBotToolCallBinding(
        plugin_module="plugin.main", plugin_name="weave", plugin_instance=plugin,
    )
    event = FakeEvent()
    binding.bind_tool_start(
        event, make_tool("weave_memory_search", "memory_search_tool", plugin), {"query": "咖啡"},
    )
    assert binding.consume(event, expected_name="weave_memory_search") is True
    assert binding.consume(event, expected_name="weave_memory_search") is False


def test_binding_fails_closed_for_foreign_plugin_or_handler():
    plugin = object()
    binding = AstrBotToolCallBinding(
        plugin_module="plugin.main", plugin_name="weave", plugin_instance=plugin,
    )
    foreign_persona_event = FakeEvent(plugins_name=["other-plugin"])
    binding.bind_tool_start(
        foreign_persona_event,
        make_tool("weave_memory_search", "memory_search_tool", plugin),
        {"query": "咖啡"},
    )
    assert binding.consume(foreign_persona_event, expected_name="weave_memory_search") is False

    foreign_handler_event = FakeEvent()
    binding.bind_tool_start(
        foreign_handler_event,
        make_tool("weave_memory_search", "foreign_handler", plugin),
        {"query": "咖啡"},
    )
    assert binding.consume(foreign_handler_event, expected_name="weave_memory_search") is False


def test_binding_clears_previous_invocation_before_checking_next_tool():
    plugin = object()
    binding = AstrBotToolCallBinding(
        plugin_module="plugin.main", plugin_name="weave", plugin_instance=plugin,
    )
    event = FakeEvent()
    binding.bind_tool_start(
        event, make_tool("weave_memory_search", "memory_search_tool", plugin), {"query": "咖啡"},
    )
    binding.bind_tool_start(
        event, make_tool("weave_memory_get", "foreign_handler", plugin), {"query": "咖啡"},
    )
    assert binding.consume(event, expected_name="weave_memory_search") is False


def test_binding_rejects_partial_bound_to_another_plugin_instance():
    plugin = object()
    other_plugin = object()
    binding = AstrBotToolCallBinding(
        plugin_module="plugin.main", plugin_name="weave", plugin_instance=plugin,
    )
    event = FakeEvent()
    binding.bind_tool_start(
        event, make_tool("weave_memory_search", "memory_search_tool", other_plugin),
        {"query": "咖啡"},
    )
    assert binding.consume(event, expected_name="weave_memory_search") is False
