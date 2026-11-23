"""Structural contract: SQLite store exposes the frozen GroupScopeStore methods."""

from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.store import (
    SQLiteMemoryStore,
)
from data.plugins.astrbot_plugin_weave_memory.core.ports import GroupScopeStore


def _protocol_methods(protocol) -> set[str]:
    names: set[str] = set()
    for base in protocol.__mro__:
        for name, value in vars(base).items():
            if callable(value) and not name.startswith("_"):
                names.add(name)
    return names


def test_sqlite_store_exposes_group_scope_protocol_methods():
    missing = sorted(
        name
        for name in _protocol_methods(GroupScopeStore)
        if not callable(getattr(SQLiteMemoryStore, name, None))
    )
    assert missing == []
