import importlib.machinery
import sys
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest

_PLUGIN_PKG = "data.plugins.astrbot_plugin_weave_memory"
_PLUGIN_DIR = str(Path(__file__).resolve().parents[1])


def _install_plugin_package() -> None:
    parts = _PLUGIN_PKG.split(".")
    parent = None
    for i, part in enumerate(parts):
        name = ".".join(parts[:i + 1])
        mod = sys.modules.get(name)
        if mod is None:
            mod = types.ModuleType(name)
            mod.__path__ = [_PLUGIN_DIR] if i == len(parts) - 1 else []
            mod.__package__ = name
            mod.__spec__ = importlib.machinery.ModuleSpec(name, None, is_package=True)
            sys.modules[name] = mod
        if parent is not None:
            setattr(parent, part, mod)
        parent = mod


_install_plugin_package()

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)

@pytest.fixture
def memory_factory():
    from data.plugins.astrbot_plugin_weave_memory.core.model import (
        MemoryEntity,
        MemoryStatus,
        MemoryType,
        SourceType,
    )
    from data.plugins.astrbot_plugin_weave_memory.core.normalization import (
        normalize_content,
    )

    def make(*, id="m-1", scope_id="scope-a", subject_id="user-a", content="喜欢黑咖啡。",
             status=MemoryStatus.ACTIVE, revision=1, superseded_by=None):
        return MemoryEntity(
            id=id, scope_id=scope_id, subject_id=subject_id,
            memory_type=MemoryType.PREFERENCE, content=content,
            normalized_content=normalize_content(content), status=status,
            source_type=SourceType.EXPLICIT, current_revision=revision,
            created_at=NOW, updated_at=NOW, superseded_by=superseded_by,
        )
    return make
