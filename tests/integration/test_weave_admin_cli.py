from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.consents import (
    SQLiteConsentStore,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.database import (
    SQLiteDatabase,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.fts import SQLiteFTSIndex
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.store import (
    SQLiteMemoryStore,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.system import (
    SystemClock,
    UUID4IdGenerator,
)
from data.plugins.astrbot_plugin_weave_memory.app.consent_service import ConsentService
from data.plugins.astrbot_plugin_weave_memory.app.group_lifecycle_service import (
    GroupLifecycleService,
)
from data.plugins.astrbot_plugin_weave_memory.app.group_memory_service import (
    GroupMemoryService,
)
from data.plugins.astrbot_plugin_weave_memory.app.scope_service import stable_scope_id
from data.plugins.astrbot_plugin_weave_memory.app.write_coordinator import (
    WriteCoordinator,
)
from data.plugins.astrbot_plugin_weave_memory.core.model import (
    AdminDecision,
    AdminSource,
    Identity,
    Scope,
    ScopeType,
    SubjectType,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "weave_admin.py"
NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT, capture_output=True, text=True, timeout=30, check=False,
    )


@pytest.fixture
def admin_env(tmp_path):
    data_dir = tmp_path / "plugin_data"
    data_dir.mkdir()
    db = SQLiteDatabase(data_dir / "weave_memory.sqlite3")
    db.migrate()
    store = SQLiteMemoryStore(db)
    index = SQLiteFTSIndex(db)
    clock = SystemClock()
    ids = UUID4IdGenerator()
    coordinator = WriteCoordinator(store, index)
    consent = ConsentService(SQLiteConsentStore(db), coordinator, clock, ids)
    lifecycle = GroupLifecycleService(
        store, coordinator, consent, index, clock, ids,
        data_dir / "group_backups",
    )
    memories = GroupMemoryService(store, store, coordinator, consent, clock, ids)
    identity = Identity("astrbot", "default", ScopeType.GROUP, "g1", SubjectType.GROUP, "g1")
    scope = Scope(stable_scope_id(identity), identity, NOW)
    store.create_scope(scope)
    decision = AdminDecision(AdminSource.EXPLICIT_CONFIG, ("owner",), True, True)
    yield {
        "data_dir": data_dir, "db": db, "store": store, "scope": scope,
        "lifecycle": lifecycle, "memories": memories, "decision": decision,
    }
    db.close()


def test_cli_lists_groups_and_previews_rollback(admin_env):
    asyncio.run(admin_env["lifecycle"].enable(
        admin_env["scope"], admin_env["decision"], operator_id="owner",
    ))
    result = _run("groups", "--data-dir", str(admin_env["data_dir"]))
    assert result.returncode == 0, result.stderr
    groups = json.loads(result.stdout)
    assert groups == [{
        "scope_id": admin_env["scope"].id, "platform": "astrbot",
        "tenant_id": "default", "group_id": "g1", "enabled": True,
    }]

    preview = _run("rollback-preview", "--data-dir", str(admin_env["data_dir"]))
    assert preview.returncode == 0, preview.stderr
    payload = json.loads(preview.stdout)
    assert payload["group_scope_count"] == 1
    assert payload["can_rollback"] is False


def test_cli_group_destroy_requires_confirm_and_supports_no_backup(admin_env):
    asyncio.run(admin_env["lifecycle"].enable(
        admin_env["scope"], admin_env["decision"], operator_id="owner",
    ))
    asyncio.run(admin_env["memories"].save_group(
        admin_env["scope"], "CLI 删除测试", group_label="topic",
        operator_id="owner", decision=admin_env["decision"],
    ))
    asyncio.run(admin_env["lifecycle"].disable(
        admin_env["scope"], admin_env["decision"], operator_id="owner",
    ))
    base = [
        "group-destroy", "--data-dir", str(admin_env["data_dir"]),
        "--platform", "astrbot", "--tenant", "default", "--group", "g1",
        "--admin-id", "owner", "--no-backup",
    ]
    refused = _run(*base)
    assert refused.returncode == 2
    assert json.loads(refused.stdout)["confirmed"] is False

    destroyed = _run(*base, "--confirm")
    assert destroyed.returncode == 0, destroyed.stderr
    receipt = json.loads(destroyed.stdout)
    assert receipt["backup_mode"] == "SKIPPED"
    assert receipt["backup_path"] == ""
    assert admin_env["db"].connection.execute(
        "SELECT 1 FROM memory_scopes WHERE id=?",
        (admin_env["scope"].id,),
    ).fetchone() is None
