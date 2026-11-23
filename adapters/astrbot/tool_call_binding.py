"""Bind AstrBot tool execution to this plugin's registered handler instance."""

from functools import partial
from typing import Any

_ACTIVE_KEY = "_weave_active_tool_binding"

_TOOL_HANDLERS = {
    "weave_memory_remember": "memory_remember_tool",
    "weave_memory_search": "memory_search_tool",
    "weave_memory_get": "memory_get_tool",
    "weave_memory_update": "memory_update_tool",
    "weave_memory_list": "memory_list_tool",
    "weave_memory_history": "memory_history_tool",
}


class AstrBotToolCallBinding:
    """Verify handler ownership using AstrBot's loaded FunctionTool contract."""

    def __init__(
        self, *, plugin_module: str, plugin_name: str, plugin_instance: object,
    ) -> None:
        self._plugin_module = plugin_module
        self._plugin_name = plugin_name
        self._plugin_instance = plugin_instance

    def bind_tool_start(self, event: Any, tool: Any, tool_args: Any) -> None:
        event.set_extra(_ACTIVE_KEY, None)
        name = getattr(tool, "name", None)
        if not isinstance(name, str) or name not in _TOOL_HANDLERS:
            return
        if not isinstance(tool_args, dict):
            return

        enabled_plugins = getattr(event, "plugins_name", None)
        if enabled_plugins is not None:
            if not isinstance(enabled_plugins, (list, tuple, set)):
                return
            if self._plugin_name not in enabled_plugins:
                return

        handler = getattr(tool, "handler", None)
        handler_module_path = getattr(tool, "handler_module_path", None)
        if handler_module_path != self._plugin_module:
            return
        if not isinstance(handler, partial) or not handler.args:
            return
        raw_handler = handler.func
        if (
            getattr(raw_handler, "__module__", None) != self._plugin_module
            or getattr(raw_handler, "__name__", None) != _TOOL_HANDLERS[name]
            or handler.args[0] is not self._plugin_instance
        ):
            return
        event.set_extra(_ACTIVE_KEY, name)

    @staticmethod
    def consume(event: Any, *, expected_name: str) -> bool:
        active_name = event.get_extra(_ACTIVE_KEY)
        event.set_extra(_ACTIVE_KEY, None)
        return active_name == expected_name

    @staticmethod
    def clear_active(event: Any) -> None:
        event.set_extra(_ACTIVE_KEY, None)
