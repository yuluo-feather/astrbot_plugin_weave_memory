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
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


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
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.ledger import SQLiteLedger
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.store import (
    SQLiteMemoryStore,
)
from data.plugins.astrbot_plugin_weave_memory.adapters.system import (
    SystemClock,
    UUID4IdGenerator,
)
from data.plugins.astrbot_plugin_weave_memory.app.consent_service import ConsentService
from data.plugins.astrbot_plugin_weave_memory.app.extraction_service import (
    expire_due_candidates,
)
from data.plugins.astrbot_plugin_weave_memory.app.group_lifecycle_service import (
    GroupLifecycleService,
)
from data.plugins.astrbot_plugin_weave_memory.app.ledger_service import LedgerService
from data.plugins.astrbot_plugin_weave_memory.app.lifecycle_service import (
    LifecycleService,
)
from data.plugins.astrbot_plugin_weave_memory.app.scope_service import stable_scope_id
from data.plugins.astrbot_plugin_weave_memory.app.write_coordinator import (
    WriteCoordinator,
)
from data.plugins.astrbot_plugin_weave_memory.core.errors import NotFound
from data.plugins.astrbot_plugin_weave_memory.core.model import (
    AdminDecision,
    AdminSource,
    Identity,
    Scope,
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
    clock = SystemClock()
    ids = UUID4IdGenerator()
    ledger_store = SQLiteLedger(Path(data_dir) / "memory_ledger.sqlite3")
    ledger = LedgerService(ledger_store, clock, ids)
    coordinator = WriteCoordinator(store, index, ledger)
    consent_repository = SQLiteConsentStore(db)
    consent = ConsentService(consent_repository, coordinator, clock, ids)
    lifecycle = LifecycleService(coordinator, store, consent)
    ledger.drain_pending(store, consent_repository)
    group_lifecycle = GroupLifecycleService(
        store, coordinator, consent, index, clock, ids,
        Path(data_dir) / "group_backups", ledger=ledger,
    )
    return store, group_lifecycle, lifecycle


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
    ], ensure_ascii=True, indent=2))
    return 0


def cmd_rollback_preview(args: argparse.Namespace) -> int:
    db = _database(args.data_dir)
    try:
        print(json.dumps(db.rollback_preview(), ensure_ascii=True, indent=2))
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
        }, ensure_ascii=True, indent=2))
        return 2
    db = _database(args.data_dir)
    try:
        store, lifecycle, _ = _lifecycle(db, args.data_dir)
        scope = store.get_scope(scope_id)
        decision = AdminDecision(
            AdminSource.EXPLICIT_CONFIG, (args.admin_id,), True, True,
        )
        receipt = asyncio.run(lifecycle.destroy(
            scope, decision, operator_id=args.admin_id,
            with_backup=not args.no_backup,
        ))
        print(json.dumps(asdict(receipt), ensure_ascii=True, indent=2))
    finally:
        db.close()
    return 0


def cmd_cleanup_backups(args: argparse.Namespace) -> int:
    db = _database(args.data_dir)
    try:
        _, lifecycle, _ = _lifecycle(db, args.data_dir)
        removed = lifecycle.cleanup_destroy_backups(args.days)
        print(json.dumps({"removed": removed}, ensure_ascii=True))
    finally:
        db.close()
    return 0


def _target_identity(args: argparse.Namespace) -> Identity:
    if args.user:
        return Identity(
            args.platform, args.tenant, ScopeType.PERSONAL,
            args.user, SubjectType.USER, args.user,
        )
    return Identity(
        args.platform, args.tenant, ScopeType.GROUP,
        args.group, SubjectType.GROUP, args.group,
    )


def _expire_scope(db: SQLiteDatabase, data_dir: str, args: argparse.Namespace
                  ) -> tuple[SQLiteMemoryStore, Scope, int]:
    store, _, lifecycle = _lifecycle(db, data_dir)
    scope_id = stable_scope_id(_target_identity(args))
    scope = store.get_scope(scope_id)
    archived = asyncio.run(expire_due_candidates(
        store=store, lifecycle=lifecycle, clock=SystemClock(),
        ids=UUID4IdGenerator(), scope=scope, operator_id="weave_admin",
    ))
    return store, scope, archived


def cmd_candidates(args: argparse.Namespace) -> int:
    if not 1 <= args.limit <= 100:
        print(json.dumps({
            "error": "limit must be between 1 and 100",
        }, ensure_ascii=True))
        return 2
    db = _database(args.data_dir)
    try:
        try:
            store, scope, archived = _expire_scope(db, args.data_dir, args)
        except NotFound:
            print(json.dumps({
                "scope_id": stable_scope_id(_target_identity(args)),
                "items": [],
                "total": 0,
                "archived": 0,
            }, ensure_ascii=True, indent=2))
            return 0
        rows, total = store.list_candidates(
            scope_id=scope.id, subject_id=scope.identity.subject_id,
            limit=args.limit, offset=0,
        )
        items = []
        for memory in rows:
            item = {
                "id": memory.id,
                "memory_type": memory.memory_type.value,
                "status": memory.status.value,
                "current_revision": memory.current_revision,
                "created_at": memory.created_at.isoformat(),
                "candidate_expires_at": (
                    memory.candidate_expires_at.isoformat()
                    if memory.candidate_expires_at else None
                ),
            }
            if args.show_content:
                item["content"] = memory.content
            items.append(item)
        print(json.dumps({
            "scope_id": scope.id,
            "items": items,
            "total": total,
            "archived": archived,
        }, ensure_ascii=True, indent=2))
    finally:
        db.close()
    return 0


def cmd_expire_candidates(args: argparse.Namespace) -> int:
    db = _database(args.data_dir)
    try:
        try:
            _, scope, archived = _expire_scope(db, args.data_dir, args)
        except NotFound:
            print(json.dumps({
                "scope_id": stable_scope_id(_target_identity(args)),
                "archived": 0,
            }, ensure_ascii=True, indent=2))
            return 0
        print(json.dumps({
            "scope_id": scope.id,
            "archived": archived,
        }, ensure_ascii=True, indent=2))
    finally:
        db.close()
    return 0


def cmd_maintenance_status(args: argparse.Namespace) -> int:
    db = _database(args.data_dir)
    try:
        store, _, _ = _lifecycle(db, args.data_dir)
        status = store.maintenance_status()
        index = SQLiteFTSIndex(db)
        status["index"] = {
            "healthy": index.is_healthy(),
            "documents": index.document_count(),
        }
        print(json.dumps(status, ensure_ascii=True, indent=2))
    finally:
        db.close()
    return 0


def _add_scope_target(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--tenant", required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--user", help="personal scope subject id")
    target.add_argument("--group", help="group id")


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

    candidates = sub.add_parser(
        "candidates", help="list candidates and lazily archive expired ones",
    )
    _add_scope_target(candidates)
    candidates.add_argument("--limit", type=int, default=50)
    candidates.add_argument("--show-content", action="store_true")
    candidates.set_defaults(func=cmd_candidates)

    expire = sub.add_parser(
        "expire-candidates", help="archive only expired candidates",
    )
    _add_scope_target(expire)
    expire.set_defaults(func=cmd_expire_candidates)

    maintenance = sub.add_parser(
        "maintenance-status", help="show schema, index and purge status",
    )
    maintenance.add_argument("--data-dir", required=True)
    maintenance.set_defaults(func=cmd_maintenance_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
