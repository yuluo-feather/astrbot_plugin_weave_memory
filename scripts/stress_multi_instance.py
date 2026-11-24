#!/usr/bin/env python3
"""Multi-instance stress: concurrent SQLite writers and index-job claims.

Every worker opens its own connection to the same database file, mirroring two
AstrBot processes sharing one plugin data directory. Verifies that concurrent
writes do not lose records and that a PENDING job is claimed exactly once.

Run locally (not part of CI):

    python scripts/stress_multi_instance.py --seconds 120 --workers 4
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.machinery
import json
import multiprocessing as mp
import random
import sys
import tempfile
import time
import types
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "data.plugins.astrbot_plugin_weave_memory"


def install_package_namespace() -> None:
    parent = None
    parts = PACKAGE.split(".")
    for index, part in enumerate(parts):
        name = ".".join(parts[:index + 1])
        module = sys.modules.get(name)
        if module is None:
            module = types.ModuleType(name)
            module.__path__ = [str(ROOT)] if index == len(parts) - 1 else []
            module.__package__ = name
            module.__spec__ = importlib.machinery.ModuleSpec(
                name, None, is_package=True,
            )
            sys.modules[name] = module
        if parent is not None:
            setattr(parent, part, module)
        parent = module


def _worker(
    db_path: str, scope_id: str, subject_id: str,
    seconds: float, worker_id: int, out,
) -> None:
    install_package_namespace()
    from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.database import (
        SQLiteDatabase,
    )
    from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.fts import (
        SQLiteFTSIndex,
    )
    from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.index_jobs import (
        SQLiteIndexJobStore,
    )
    from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.store import (
        SQLiteMemoryStore,
    )
    from data.plugins.astrbot_plugin_weave_memory.app.write_coordinator import (
        WriteCoordinator,
    )
    from data.plugins.astrbot_plugin_weave_memory.core.model import (
        MemoryEntity,
        MemoryRevision,
        MemorySource,
        MemoryStatus,
        MemoryType,
        OperatorType,
        SourceType,
    )
    from data.plugins.astrbot_plugin_weave_memory.core.normalization import (
        normalize_content,
    )

    rng = random.Random(worker_id)
    db = SQLiteDatabase(db_path)
    store = SQLiteMemoryStore(db)
    index = SQLiteFTSIndex(db)
    jobs = SQLiteIndexJobStore(db)
    coordinator = WriteCoordinator(store, index)

    created = 0
    claimed: list[str] = []
    errors: dict[str, int] = {}
    deadline = time.monotonic() + seconds

    async def run() -> None:
        nonlocal created
        while time.monotonic() < deadline:
            action = rng.random()
            try:
                if action < 0.45:
                    now = datetime.now(UTC)
                    memory_id = f"w{worker_id}-{uuid.uuid4().hex}"
                    content = f"压测记忆 {memory_id}"
                    await coordinator.create(
                        MemoryEntity(
                            id=memory_id, scope_id=scope_id,
                            subject_id=subject_id,
                            memory_type=MemoryType.FACT, content=content,
                            normalized_content=normalize_content(content),
                            status=MemoryStatus.ACTIVE,
                            source_type=SourceType.EXPLICIT,
                            current_revision=1, created_at=now, updated_at=now,
                        ),
                        MemoryRevision(
                            f"rev-{memory_id}", memory_id, 1, content,
                            MemoryStatus.ACTIVE, now, "created",
                        ),
                        MemorySource(
                            f"src-{memory_id}", memory_id, SourceType.EXPLICIT,
                            OperatorType.USER, subject_id, now,
                        ),
                    )
                    created += 1
                else:
                    job = jobs.claim_next(at=datetime.now(UTC))
                    if job is not None:
                        jobs.mark_done(job.id, at=datetime.now(UTC))
                        claimed.append(job.id)
            except Exception as exc:  # noqa: BLE001 - stress harness
                cause = exc.__cause__
                detail = f"{type(exc).__name__}: {exc}"
                if cause is not None:
                    detail += f" <- {type(cause).__name__}: {cause}"
                detail = detail[:160]
                errors[detail] = errors.get(detail, 0) + 1

    try:
        asyncio.run(run())
    finally:
        db.close()
    out.put({
        "worker": worker_id, "created": created,
        "claimed": claimed, "errors": errors,
    })


def _seed(db_path: Path, scope_id: str, subject_id: str, memories: int,
          jobs: int) -> None:
    install_package_namespace()
    from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.database import (
        SQLiteDatabase,
    )
    from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.index_jobs import (
        SQLiteIndexJobStore,
    )
    from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.store import (
        SQLiteMemoryStore,
    )
    from data.plugins.astrbot_plugin_weave_memory.core.model import (
        Identity,
        IndexJob,
        IndexJobStatus,
        IndexOperation,
        IndexTarget,
        Scope,
        ScopeType,
        SubjectType,
    )

    now = datetime.now(UTC)
    db = SQLiteDatabase(db_path)
    db.migrate()
    store = SQLiteMemoryStore(db)
    store.create_scope(
        Scope(
            scope_id,
            Identity(
                "astrbot", "default", ScopeType.PERSONAL, subject_id,
                SubjectType.USER, subject_id,
            ),
            now,
        ),
    )
    job_store = SQLiteIndexJobStore(db)
    for index in range(jobs):
        memory_id = f"seed-{index % memories}"
        job_store.create_or_get(
            IndexJob(
                id=uuid.uuid4().hex, memory_id=memory_id,
                target=IndexTarget.VECTOR, operation=IndexOperation.UPSERT,
                content_hash=f"hash-{index}", status=IndexJobStatus.PENDING,
                attempts=0, created_at=now, updated_at=now,
            ),
        )
    count = db.connection.execute(
        "select count(*) from index_jobs",
    ).fetchone()[0]
    db.close()
    print(f"seeded {count} index jobs", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--jobs", type=int, default=2000)
    parser.add_argument("--memories", type=int, default=50)
    parser.add_argument("--db", type=Path, default=None)
    args = parser.parse_args()

    db_path = args.db or (
        Path(tempfile.mkdtemp(prefix="weave_stress_")) / "stress.db"
    )
    scope_id, subject_id = "stress-scope", "stress-user"
    _seed(db_path, scope_id, subject_id, args.memories, args.jobs)

    context = mp.get_context("spawn")
    out = context.Queue()
    workers = [
        context.Process(
            target=_worker,
            args=(str(db_path), scope_id, subject_id, args.seconds, index, out),
        )
        for index in range(args.workers)
    ]
    started = time.monotonic()
    for process in workers:
        process.start()
    results = [out.get() for _ in workers]
    for process in workers:
        process.join(timeout=60)

    elapsed = time.monotonic() - started
    claimed_ids = [job_id for result in results for job_id in result["claimed"]]
    errors: dict[str, int] = {}
    for result in results:
        for name, count in result["errors"].items():
            errors[name] = errors.get(name, 0) + count
    created_total = sum(result["created"] for result in results)

    install_package_namespace()
    from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.database import (
        SQLiteDatabase,
    )

    db = SQLiteDatabase(db_path)
    memory_count = db.connection.execute(
        "select count(*) from memories",
    ).fetchone()[0]
    job_states = dict(
        db.connection.execute(
            "select status, count(*) from index_jobs group by status",
        ).fetchall()
    )
    db.close()

    report = {
        "db": str(db_path),
        "seconds": round(elapsed, 1),
        "workers": args.workers,
        "created": created_total,
        "memories_in_db": memory_count,
        "claims": len(claimed_ids),
        "unique_claims": len(set(claimed_ids)),
        "duplicate_claims": len(claimed_ids) - len(set(claimed_ids)),
        "job_states": job_states,
        "errors": errors,
        "ok": (
            created_total == memory_count
            and len(claimed_ids) == len(set(claimed_ids))
            and not errors
            and job_states.get("PENDING", 0) == 0
        ),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
