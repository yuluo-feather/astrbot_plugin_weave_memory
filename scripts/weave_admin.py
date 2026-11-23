#!/usr/bin/env python3
"""Deployer-side group administration CLI for Weave Memory.

Use this when a group has been dissolved or its chat is no longer reachable.
It reuses the same group lifecycle service, backup, consent and audit paths as
the in-chat commands; it does not add a privileged bypass.

Examples:
  python scripts/weave_admin.py groups --data-dir <plugin_data_dir>
  python scripts/weave_admin.py rollback-preview --data-dir <plugin_data_dir>
  python scripts/weave_admin.py group-destroy --data-dir <plugin_data_dir> \\
      --platform astrbot --tenant default --group 123456 --admin-id owner --confirm
"""
# ruff: noqa: E402 - imports below must follow the package namespace bootstrap.
from __future__ import annotations

import argparse
import asyncio
import importlib.machinery
import json
import sys
import types
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "data.plugins.astrbot_plugin_weave_memory"


def install_package_namespace() -> None:
    parent = None
    parts = PACKAGE.split(".")
    for index, part in enumerate(parts):
        name = ".".join(parts[: index + 1])
        module = sys.modules.get(name)
        if module is None:
            module = types.ModuleType(name)
            module.__path__ = [str(ROOT)] if index == len(parts) - 1 else []
            module.__package__ = name
            module.__spec__ = importlib.machinery.ModuleSpec(name, None, is_package=True)
            sys.modules[name] = module
        if parent is not None:
            setattr(parent, part, module)
        parent = module


install_package_namespace()

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
from data.plugins.astrbot_plugin_weave_memory.app.scope_service import stable_scope_id
from data.plugins.astrbot_plugin_weave_memory.app.write_coordinator import (
    WriteCoordinator,
)
from data.plugins.astrbot_plugin_weave_memory.core.model import (
    AdminDecision,
    AdminSource,
    Identity,
    ScopeType,
    SubjectType,
)


def _database(data_dir: str) -> SQLiteDatabase:
    path = Path(data_dir) / "weave_memory.sqlite3"
    if not path.exists():
        raise SystemExit(f"database not found: {path}")
    db = SQLiteDatabase(path)
    db.migrate()
    return db


def _lifecycle(db: SQLiteDatabase, data_dir: str):
    store = SQLiteMemoryStore(db)
    index = SQLiteFTSIndex(db)
    coordinator = WriteCoordinator(store, index)
    clock = SystemClock()
    ids = UUID4IdGenerator()
    consent = ConsentService(SQLiteConsentStore(db), coordinator, clock, ids)
    lifecycle = GroupLifecycleService(
        store, coordinator, consent, index, clock, ids,
        Path(data_dir) / "group_backups",
    )
    return store, lifecycle


def cmd_groups(args: argparse.Namespace) -> int:
    db = _database(args.data_dir)
    try:
        rows = db.connection.execute(
            """SELECT id, platform, tenant_id, scope_external_id, is_enabled
            FROM memory_scopes WHERE scope_type='GROUP'
            ORDER BY platform, tenant_id, scope_external_id""",
        ).fetchall()
    finally:
        db.close()
    print(json.dumps([
        {
            "scope_id": row["id"], "platform": row["platform"],
            "tenant_id": row["tenant_id"], "group_id": row["scope_external_id"],
            "enabled": bool(row["is_enabled"]),
        }
        for row in rows
    ], ensure_ascii=False, indent=2))
    return 0


def cmd_rollback_preview(args: argparse.Namespace) -> int:
    db = _database(args.data_dir)
    try:
        print(json.dumps(db.rollback_preview(), ensure_ascii=False, indent=2))
    finally:
        db.close()
    return 0


def cmd_group_destroy(args: argparse.Namespace) -> int:
    identity = Identity(
        args.platform, args.tenant, ScopeType.GROUP,
        args.group, SubjectType.GROUP, args.group,
    )
    scope_id = stable_scope_id(identity)
    if not args.confirm:
        print(json.dumps({
            "action": "group-destroy",
            "scope_id": scope_id,
            "backup": not args.no_backup,
            "confirmed": False,
            "message": "re-run with --confirm to destroy; this action is irreversible",
        }, ensure_ascii=False, indent=2))
        return 2
    db = _database(args.data_dir)
    try:
        store, lifecycle = _lifecycle(db, args.data_dir)
        scope = store.get_scope(scope_id)
        decision = AdminDecision(
            AdminSource.EXPLICIT_CONFIG, (args.admin_id,), True, True,
        )
        receipt = asyncio.run(lifecycle.destroy(
            scope, decision, operator_id=args.admin_id,
            with_backup=not args.no_backup,
        ))
        print(json.dumps(asdict(receipt), ensure_ascii=False, indent=2))
    finally:
        db.close()
    return 0


def cmd_cleanup_backups(args: argparse.Namespace) -> int:
    db = _database(args.data_dir)
    try:
        _, lifecycle = _lifecycle(db, args.data_dir)
        removed = lifecycle.cleanup_destroy_backups(args.days)
        print(json.dumps({"removed": removed}, ensure_ascii=False))
    finally:
        db.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    groups = sub.add_parser("groups", help="list group scopes")
    groups.add_argument("--data-dir", required=True)
    groups.set_defaults(func=cmd_groups)

    preview = sub.add_parser("rollback-preview", help="preview v5→v4 rollback")
    preview.add_argument("--data-dir", required=True)
    preview.set_defaults(func=cmd_rollback_preview)

    destroy = sub.add_parser("group-destroy", help="destroy one group scope")
    destroy.add_argument("--data-dir", required=True)
    destroy.add_argument("--platform", required=True)
    destroy.add_argument("--tenant", required=True)
    destroy.add_argument("--group", required=True)
    destroy.add_argument("--admin-id", required=True)
    destroy.add_argument("--confirm", action="store_true")
    destroy.add_argument("--no-backup", action="store_true")
    destroy.set_defaults(func=cmd_group_destroy)

    cleanup = sub.add_parser("cleanup-backups", help="remove old destroy backups")
    cleanup.add_argument("--data-dir", required=True)
    cleanup.add_argument("--days", type=int, default=30)
    cleanup.set_defaults(func=cmd_cleanup_backups)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
