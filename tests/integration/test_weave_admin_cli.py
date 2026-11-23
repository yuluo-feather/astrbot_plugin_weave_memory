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


def _seed_expired_personal_candidate(admin_env) -> str:
    identity = Identity(
        "astrbot", "default", ScopeType.PERSONAL, "u1", SubjectType.USER, "u1",
    )
    scope_id = stable_scope_id(identity)
    stamp = "2020-01-01T00:00:00.000000Z"
    memory_id = "candidate-cli-expired"
    with admin_env["db"].transaction() as conn:
        conn.execute(
            """INSERT INTO memory_scopes
            (id, platform, tenant_id, scope_type, scope_external_id, subject_type,
             subject_id, created_at, is_enabled, disabled_at, admin_ids_json,
             admin_source)
            VALUES (?, 'astrbot', 'default', 'PERSONAL', 'u1', 'USER', 'u1', ?,
                    1, NULL, '[]', 'NONE')""",
            (scope_id, stamp),
        )
        conn.execute(
            """INSERT INTO memories
            (id, scope_id, subject_id, memory_type, content, normalized_content,
             normalization_version, status, source_type, current_revision,
             valid_from, valid_until, candidate_expires_at, superseded_by,
             created_at, updated_at, last_accessed_at, metadata_json)
            VALUES (?, ?, 'u1', 'PREFERENCE', '过期候选', '过期候选', 1,
                    'CANDIDATE', 'INFERRED', 1, NULL, NULL,
                    '2020-01-02T00:00:00.000000Z', NULL, ?, ?, NULL, '{}')""",
            (memory_id, scope_id, stamp, stamp),
        )
    return memory_id


def test_cli_lists_and_expires_candidates(admin_env):
    memory_id = _seed_expired_personal_candidate(admin_env)
    result = _run(
        "candidates", "--data-dir", str(admin_env["data_dir"]),
        "--platform", "astrbot", "--tenant", "default", "--user", "u1",
        "--show-content",
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["total"] == 0
    assert payload["items"] == []
    assert payload["archived"] == 1
    row = admin_env["db"].connection.execute(
        "SELECT status, content, candidate_expires_at FROM memories WHERE id=?",
        (memory_id,),
    ).fetchone()
    assert row["status"] == "ARCHIVED"
    assert row["content"] == "过期候选"
    assert row["candidate_expires_at"] is None
    assert admin_env["db"].connection.execute(
        "SELECT count(*) FROM memory_operations WHERE operation_type='CANDIDATE_EXPIRE'",
    ).fetchone()[0] == 1


def test_cli_expire_candidates_handles_missing_scope(admin_env):
    result = _run(
        "expire-candidates", "--data-dir", str(admin_env["data_dir"]),
        "--platform", "astrbot", "--tenant", "default", "--user", "missing",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["archived"] == 0


def test_cli_maintenance_status_reports_schema_and_index(admin_env):
    result = _run("maintenance-status", "--data-dir", str(admin_env["data_dir"]))
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 8
    assert payload["index"]["healthy"] is True
    assert payload["secure_purge_pending"] is False
