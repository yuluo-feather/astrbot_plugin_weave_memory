#!/usr/bin/env python3
"""Run the V1.4 offline golden-set evaluation against the real SQLite/FTS services.

The harness builds a temporary synthetic database; it never opens the plugin's
production database and never changes runtime defaults or configuration.
"""
# ruff: noqa: E402 - imports below must follow the package namespace bootstrap.
from __future__ import annotations

import argparse
import hashlib
import importlib.machinery
import json
import platform
import random
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import types
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "eval" / "v1.4" / "datasets" / "golden_v1.json"
REPORT_DIR = ROOT / "eval" / "v1.4" / "reports"
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
from data.plugins.astrbot_plugin_weave_memory.adapters.system import UUID4IdGenerator
from data.plugins.astrbot_plugin_weave_memory.app.consent_service import (
    ConsentService,
    GroupConsentType,
    PersonalConsentType,
)
from data.plugins.astrbot_plugin_weave_memory.app.group_recall_service import (
    GroupContextComposer,
    GroupRecallService,
)
from data.plugins.astrbot_plugin_weave_memory.app.recall_service import (
    MemoryContextComposer,
    RecallService,
)
from data.plugins.astrbot_plugin_weave_memory.app.scope_service import stable_scope_id
from data.plugins.astrbot_plugin_weave_memory.app.write_coordinator import (
    WriteCoordinator,
)
from data.plugins.astrbot_plugin_weave_memory.core.model import (
    AdminSource,
    Identity,
    MemoryEntity,
    MemoryRevision,
    MemorySource,
    MemoryStatus,
    MemoryType,
    OperatorType,
    Scope,
    ScopeType,
    SourceType,
    SubjectType,
)
from data.plugins.astrbot_plugin_weave_memory.core.normalization import (
    normalize_content,
)


class FixedClock:
    def __init__(self, instant: datetime) -> None:
        self._instant = instant

    def now(self) -> datetime:
        return self._instant


def parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def load_dataset(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    dataset = json.loads(raw)
    # Normalize CRLF so the fingerprint is independent of the checkout's line endings.
    digest = hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()
    validate_dataset(dataset)
    return dataset, digest


def validate_dataset(dataset: dict[str, Any]) -> None:
    required_top = {
        "dataset_name", "dataset_version", "data_policy", "annotation_status", "fixed_clock",
        "fixed_random_seed", "minimum_sample_count", "holdout_ratio", "categories", "scopes", "memories", "samples",
    }
    missing = required_top - dataset.keys()
    if missing:
        raise ValueError(f"dataset missing top-level fields: {sorted(missing)}")
    if dataset["data_policy"] != "synthetic_only_no_production_memory_content":
        raise ValueError("V1.4 dataset must be synthetic-only")
    samples = dataset["samples"]
    memories = dataset["memories"]
    if len(samples) < dataset["minimum_sample_count"]:
        raise ValueError("golden set is below the declared minimum sample count")
    if len({item["sample_id"] for item in samples}) != len(samples):
        raise ValueError("sample_id values must be unique")
    memory_ids = {item["id"] for item in memories}
    if len(memory_ids) != len(memories):
        raise ValueError("memory fixture IDs must be unique")
    counts = Counter(item["category"] for item in samples)
    for category in dataset["categories"]:
        if counts[category] < 5:
            raise ValueError(f"category {category!r} has fewer than five samples")
    holdout = sum(item["split"] == "holdout" for item in samples)
    expected_holdout = round(len(samples) * dataset["holdout_ratio"])
    if holdout != expected_holdout:
        raise ValueError("holdout split does not match declared ratio")
    required_sample_fields = {
        "sample_id", "query", "scope", "subject_id", "expected_memory_ids",
        "expected_memory_content", "distractor_memory_ids", "excluded_memory_ids",
        "category", "source_type", "memory_type", "valid_from", "valid_until",
        "created_at", "caller", "dataset_version", "labeler", "split",
    }
    scope_by_id = {scope["scope_id"]: scope for scope in dataset["scopes"].values()}
    memory_by_id = {memory["id"]: memory for memory in memories}
    for item in samples:
        missing_fields = required_sample_fields - item.keys()
        if missing_fields:
            raise ValueError(f"sample {item['sample_id']} missing fields: {sorted(missing_fields)}")
        if item["split"] not in {"train", "holdout"}:
            raise ValueError(f"sample {item['sample_id']} has invalid split")
        if item["dataset_version"] != dataset["dataset_version"]:
            raise ValueError(f"sample {item['sample_id']} has mismatched dataset_version")
        sample_scope = scope_by_id.get(item["scope"]["scope_id"])
        if sample_scope is None:
            raise ValueError(f"sample {item['sample_id']} references unknown scope")
        if item["subject_id"] != sample_scope["subject_id"]:
            raise ValueError(f"sample {item['sample_id']} subject does not match scope")
        refs = set(item["expected_memory_ids"] + item["distractor_memory_ids"] + item["excluded_memory_ids"])
        if not refs <= memory_ids:
            raise ValueError(f"sample {item['sample_id']} references unknown memory IDs")
        if len(item["expected_memory_ids"]) != len(item["expected_memory_content"]):
            raise ValueError(f"sample {item['sample_id']} expected ID/content mismatch")
        for memory_id, expected_content in zip(item["expected_memory_ids"], item["expected_memory_content"]):
            fixture = memory_by_id[memory_id]
            fixture_scope = dataset["scopes"][fixture["scope_key"]]
            if fixture_scope["scope_id"] != sample_scope["scope_id"] or fixture_scope["subject_id"] != item["subject_id"]:
                raise ValueError(f"sample {item['sample_id']} expected memory is outside its scope")
            if fixture["content"] != expected_content:
                raise ValueError(f"sample {item['sample_id']} expected content does not match fixture")
        for memory_id in item["distractor_memory_ids"]:
            fixture_scope = dataset["scopes"][memory_by_id[memory_id]["scope_key"]]
            if fixture_scope["scope_id"] == sample_scope["scope_id"]:
                raise ValueError(f"sample {item['sample_id']} distractor must be outside its scope")
        for memory_id in item["excluded_memory_ids"]:
            fixture = memory_by_id[memory_id]
            fixture_scope = dataset["scopes"][fixture["scope_key"]]
            valid_until = parse_time(fixture.get("valid_until"))
            fixed_clock = parse_time(dataset["fixed_clock"])
            expired = (
                valid_until is not None
                and fixed_clock is not None
                and valid_until <= fixed_clock
            )
            if fixture_scope["scope_id"] != sample_scope["scope_id"] or not (fixture["status"] == "DELETED" or expired):
                raise ValueError(f"sample {item['sample_id']} excluded memory is not expired/deleted in-scope")
        if item["category"] == "cross_scope" and (not item["expected_memory_ids"] or not item["distractor_memory_ids"]):
            raise ValueError(f"cross_scope sample {item['sample_id']} needs an in-scope target and foreign distractor")
    for category in dataset["categories"]:
        if not any(s["category"] == category and s["split"] == "holdout" for s in samples):
            raise ValueError(f"category {category!r} must be represented in holdout")


def build_runtime(dataset: dict[str, Any], db_path: Path) -> dict[str, Any]:
    now = datetime.fromisoformat(dataset["fixed_clock"])
    clock = FixedClock(now)
    db = SQLiteDatabase(db_path)
    db.migrate()
    store = SQLiteMemoryStore(db)
    index = SQLiteFTSIndex(db)
    consent_repo = SQLiteConsentStore(db)
    coordinator = WriteCoordinator(store, index)
    ids = UUID4IdGenerator()
    consent = ConsentService(consent_repo, coordinator, clock, ids)

    scopes: dict[str, Scope] = {}
    for key, raw in dataset["scopes"].items():
        scope_type = ScopeType(raw["scope_type"])
        subject_type = SubjectType.USER if scope_type is ScopeType.PERSONAL else SubjectType.GROUP
        identity = Identity(
            raw["platform"], raw["tenant_id"], scope_type,
            raw["scope_external_id"], subject_type, raw["subject_id"],
        )
        scope_id = stable_scope_id(identity)
        if scope_id != raw["scope_id"]:
            raise ValueError(f"dataset scope ID does not match canonical identity for {key}")
        scope = Scope(scope_id, identity, now)
        store.create_scope(scope)
        if scope_type is ScopeType.GROUP:
            store.set_admin_metadata(scope.id, ("synthetic-admin",), AdminSource.EXPLICIT_CONFIG)
            store.enable_scope(
                scope.id, operation_id="v14-synthetic-group-enable",
                operator_type=OperatorType.ADMIN, operator_id="synthetic-admin",
                occurred_at=now,
            )
            consent.grant(scope, GroupConsentType.GROUP_MEMORY, operator_id="synthetic-admin")
        else:
            consent.grant(scope, PersonalConsentType.PERSONAL_MEMORY, operator_id=identity.subject_id)
        scopes[key] = scope

    for fixture in dataset["memories"]:
        scope = scopes[fixture["scope_key"]]
        status = MemoryStatus.ACTIVE
        memory = MemoryEntity(
            id=fixture["id"], scope_id=scope.id,
            subject_id=scope.identity.subject_id,
            memory_type=MemoryType(fixture["memory_type"]),
            content=fixture["content"], normalized_content=normalize_content(fixture["content"]),
            status=status, source_type=SourceType(fixture["source_type"]),
            current_revision=1, created_at=parse_time(fixture["created_at"]) or now,
            updated_at=parse_time(fixture["created_at"]) or now,
            valid_from=parse_time(fixture.get("valid_from")),
            valid_until=parse_time(fixture.get("valid_until")),
        )
        revision = MemoryRevision(
            id=f"{fixture['id']}-r1", memory_id=fixture["id"], revision=1,
            content=fixture["content"], status=status,
            changed_at=memory.created_at, change_reason="synthetic V1.4 fixture",
            snapshot={"scope_id": scope.id, "subject_id": scope.identity.subject_id,
                      "memory_type": memory.memory_type.value, "source_type": memory.source_type.value},
        )
        operator_type = OperatorType.USER if memory.source_type is SourceType.EXPLICIT else OperatorType.ADMIN
        source = MemorySource(
            id=f"{fixture['id']}-s1", memory_id=fixture["id"],
            source_type=memory.source_type, operator_type=operator_type,
            operator_id=scope.identity.subject_id, captured_at=memory.created_at,
        )
        import asyncio
        asyncio.run(coordinator.create(memory, revision, source))
        if fixture["status"] == "DELETED":
            asyncio.run(coordinator.delete(
                memory.id, scope_id=scope.id, subject_id=scope.identity.subject_id,
                operation_id=f"v14-delete-{memory.id}", operator_type=OperatorType.SYSTEM,
                operator_id="v14-fixture-builder", occurred_at=now,
            ))

    diagnostics: list[dict[str, Any]] = []
    personal = RecallService(
        store, index, consent, clock, coordinator, store, diagnostics.append,
    )
    group = GroupRecallService(
        store, index, consent, clock, coordinator, store, diagnostics.append,
    )
    return {
        "db": db, "store": store, "index": index, "scopes": scopes,
        "personal": personal, "group": group, "diagnostics": diagnostics,
        "now": now,
    }


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def retrieval_metrics(
    expected: list[str], actual: list[str], *, k: int, empty_expected: bool,
) -> dict[str, float | None]:
    if empty_expected:
        return {"recall": None, "precision": None, "mrr": None}
    if k < 1:
        raise ValueError("K must be positive")
    relevant = set(expected)
    hits = [memory_id for memory_id in actual[:k] if memory_id in relevant]
    recall = len(set(hits)) / len(relevant)
    precision = len(hits) / k
    first_rank = next((rank for rank, memory_id in enumerate(actual, 1) if memory_id in relevant), None)
    mrr = 1 / first_rank if first_rank is not None else 0.0
    return {"recall": recall, "precision": precision, "mrr": mrr}


def summarize(rows: list[dict[str, Any]], key: str) -> dict[str, float | int | None]:
    values = [row[key] for row in rows if row[key] is not None]
    return {"n": len(values), "mean": statistics.fmean(values) if values else None}


def run_evaluation(dataset: dict[str, Any], dataset_hash: str, args: argparse.Namespace) -> dict[str, Any]:
    overrides = any(value is not None for value in (
        args.personal_max_memories, args.personal_max_tokens, args.personal_candidate_limit,
        args.group_max_memories, args.group_max_tokens, args.group_candidate_limit,
    ))
    if overrides and args.split != "train":
        raise ValueError("parameter experiments may use train only; holdout is reserved")

    override_values = (
        args.personal_max_memories, args.personal_max_tokens, args.personal_candidate_limit,
        args.group_max_memories, args.group_max_tokens, args.group_candidate_limit,
    )
    if any(value is not None and value < 1 for value in override_values):
        raise ValueError("parameter overrides must be positive integers")

    def configured(value: int | None, default: int) -> int:
        return default if value is None else value

    params = {
        "personal": {
            "max_memories": configured(args.personal_max_memories, 4),
            "max_tokens": configured(args.personal_max_tokens, 384),
            "candidate_limit": configured(args.personal_candidate_limit, 12),
        },
        "group": {
            "max_memories": configured(args.group_max_memories, 2),
            "max_tokens": configured(args.group_max_tokens, 192),
            "candidate_limit": configured(args.group_candidate_limit, 8),
        },
    }
    selected_samples = [s for s in dataset["samples"] if args.split == "all" or s["split"] == args.split]
    random.Random(dataset["fixed_random_seed"]).shuffle(selected_samples)
    result_rows: list[dict[str, Any]] = []
    latencies: dict[str, list[float]] = defaultdict(list)

    with tempfile.TemporaryDirectory(prefix="weave-v14-") as temp_dir:
        runtime = build_runtime(dataset, Path(temp_dir) / "synthetic.db")
        try:
            for sample in selected_samples:
                scope_key = next(k for k, v in dataset["scopes"].items() if v["scope_id"] == sample["scope"]["scope_id"])
                scope = runtime["scopes"][scope_key]
                scope_type = sample["scope"]["scope_type"]
                service = runtime["personal"] if scope_type == "PERSONAL" else runtime["group"]
                recall_method = service.recall_with_trace if scope_type == "PERSONAL" else service.recall_group_with_trace
                config = params["personal"] if scope_type == "PERSONAL" else params["group"]

                for _ in range(2):
                    recall_method(sample["query"], scope, limit=config["candidate_limit"])
                durations: list[float] = []
                for _ in range(30):
                    start = time.perf_counter()
                    recall_method(sample["query"], scope, limit=config["candidate_limit"])
                    durations.append((time.perf_counter() - start) * 1000)
                latencies[scope_type].extend(durations)

                candidates, _trace = recall_method(sample["query"], scope, limit=config["candidate_limit"])
                candidate_ids = [memory.id for memory in candidates]
                expected = sample["expected_memory_ids"]
                empty_policy = not expected
                candidate_score = retrieval_metrics(
                    expected, candidate_ids, k=config["candidate_limit"], empty_expected=empty_policy,
                )

                records = runtime["diagnostics"]
                before = len(records)
                if scope_type == "PERSONAL":
                    composer = MemoryContextComposer(
                        service, max_tokens=config["max_tokens"],
                        max_memories=config["max_memories"], candidate_limit=config["candidate_limit"],
                    )
                else:
                    composer = GroupContextComposer(
                        service, max_tokens=config["max_tokens"],
                        max_memories=config["max_memories"], candidate_limit=config["candidate_limit"],
                    )
                import asyncio
                context = asyncio.run(composer.compose(sample["query"], scope))
                diagnostic = records[-1] if len(records) > before else {}
                injected_ids = list(diagnostic.get("final_injected_ids", []))
                token_count = context.estimated_tokens if context is not None else 0
                injected_score = retrieval_metrics(
                    expected, injected_ids, k=config["max_memories"], empty_expected=empty_policy,
                )

                all_returned = candidate_ids
                wrong_scope = 0
                for memory_id in all_returned:
                    fixture = next(item for item in dataset["memories"] if item["id"] == memory_id)
                    fixture_scope = dataset["scopes"][fixture["scope_key"]]
                    if fixture_scope["scope_id"] != sample["scope"]["scope_id"] or fixture_scope["subject_id"] != sample["subject_id"]:
                        wrong_scope += 1
                foreign_hits = sorted(set(candidate_ids) & set(sample["distractor_memory_ids"]))
                excluded_hits = sorted(set(candidate_ids) & set(sample["excluded_memory_ids"]))
                no_memory_correct = None
                if sample["category"] in {"no_memory", "expired_deleted"}:
                    no_memory_correct = 1.0 if not candidate_ids else 0.0

                result_rows.append({
                    "sample_id": sample["sample_id"], "split": sample["split"], "query": sample["query"],
                    "scope_type": scope_type, "category": sample["category"],
                    "expected_ids": expected, "candidate_ids": candidate_ids,
                    "injected_ids": injected_ids,
                    "false_positive_ids": sorted(set(candidate_ids) - set(expected)),
                    "candidate_recall": candidate_score["recall"],
                    "candidate_precision": candidate_score["precision"],
                    "candidate_mrr": candidate_score["mrr"],
                    "injected_recall": injected_score["recall"],
                    "injected_precision": injected_score["precision"],
                    "injected_mrr": injected_score["mrr"],
                    "no_memory_correct": no_memory_correct,
                    "wrong_scope_count": wrong_scope,
                    "foreign_distractor_hits": foreign_hits,
                    "excluded_memory_hits": excluded_hits,
                    "estimated_injected_tokens": token_count,
                    "injection_effectiveness": None,
                })
        finally:
            runtime["db"].close()

    by_scope: dict[str, Any] = {}
    for scope_type in ("PERSONAL", "GROUP"):
        rows = [row for row in result_rows if row["scope_type"] == scope_type]
        by_scope[scope_type] = {
            "sample_count": len(rows),
            "candidate_recall_at_k": summarize(rows, "candidate_recall"),
            "candidate_precision_at_k": summarize(rows, "candidate_precision"),
            "candidate_mrr": summarize(rows, "candidate_mrr"),
            "injected_recall_at_k": summarize(rows, "injected_recall"),
            "injected_precision_at_k": summarize(rows, "injected_precision"),
            "injected_mrr": summarize(rows, "injected_mrr"),
            "no_memory_correct_rate": summarize(rows, "no_memory_correct"),
            "average_injected_tokens": statistics.fmean(row["estimated_injected_tokens"] for row in rows) if rows else None,
            "retrieval_latency_ms": {
                "warmup_runs_per_query": 2, "measured_runs_per_query": 30,
                "p50": percentile(latencies[scope_type], 0.50),
                "p95": percentile(latencies[scope_type], 0.95),
                "measurement_count": len(latencies[scope_type]),
            },
            "wrong_scope_count": sum(row["wrong_scope_count"] for row in rows),
            "returned_memory_count": sum(len(row["candidate_ids"]) for row in rows),
            "error_attribution_rate": (
                sum(row["wrong_scope_count"] for row in rows) /
                sum(len(row["candidate_ids"]) for row in rows)
                if sum(len(row["candidate_ids"]) for row in rows) else 0.0
            ),
            "expired_deleted_recall_rate": (
                sum(len(row["excluded_memory_hits"]) for row in rows) /
                sum(len(next(s for s in selected_samples if s["sample_id"] == row["sample_id"])["excluded_memory_ids"]) for row in rows)
                if sum(len(next(s for s in selected_samples if s["sample_id"] == row["sample_id"])["excluded_memory_ids"]) for row in rows) else 0.0
            ),
            "cross_scope_distractor_hit_count": sum(len(row["foreign_distractor_hits"]) for row in rows),
            "injection_effectiveness": {"status": "pending_human_rubric", "rated_count": 0, "double_scored_minimum": 10},
        }

    categories = {}
    for category in dataset["categories"]:
        rows = [row for row in result_rows if row["category"] == category]
        categories[category] = {
            "sample_count": len(rows),
            "candidate_recall_at_k": summarize(rows, "candidate_recall"),
            "candidate_precision_at_k": summarize(rows, "candidate_precision"),
            "candidate_mrr": summarize(rows, "candidate_mrr"),
            "no_memory_correct_rate": summarize(rows, "no_memory_correct"),
        }

    report = {
        "report_version": "1.0.0",
        "project_version": "V1.4",
        "dataset_name": dataset["dataset_name"],
        "dataset_version": dataset["dataset_version"],
        "dataset_sha256": dataset_hash,
        "split": args.split,
        "parameter_experiment": overrides,
        "parameters": params,
        "environment": {
            "python": platform.python_version(), "platform": platform.platform(),
            "sqlite": sqlite3.sqlite_version,
            "runtime_source_commit": subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip(),
            "fixed_clock": dataset["fixed_clock"], "random_seed": dataset["fixed_random_seed"],
        },
        "scope_metrics": by_scope,
        "category_metrics": categories,
        "samples": result_rows,
        "limitations": [
            "Golden Set labels are assistant-generated drafts and require human review before V1.4 exit acceptance.",
            "Injection effectiveness requires human rubric scoring; no score is fabricated by this harness.",
            "Baseline uses the current V1.3 SQLite FTS runtime services and does not alter runtime defaults.",
            "Cross-scope samples include an in-scope target and a foreign same-term distractor; target retrieval contributes to normal metrics and foreign hits are reported separately.",
        ],
    }
    return report


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# V1.4 Golden Set 基线评测报告", "",
        f"- 数据集：`{report['dataset_name']}` v`{report['dataset_version']}`",
        f"- SHA-256：`{report['dataset_sha256']}`",
        f"- Split：`{report['split']}`；参数实验：`{'是' if report['parameter_experiment'] else '否'}`",
        f"- 运行时代码基线 commit：`{report['environment']['runtime_source_commit']}`",
        f"- Python / SQLite：`{report['environment']['python']}` / `{report['environment']['sqlite']}`",
        f"- 固定时间 / 随机种子：`{report['environment']['fixed_clock']}` / `{report['environment']['random_seed']}`", "",
        "## Scope 指标", "",
        "| Scope | 样本 | Candidate Recall@K | Candidate Precision@K | Candidate MRR | Injected Recall@K | Injected Precision@K | Injected MRR | 无记忆正确率 | 平均 Token | P50/P95 ms | 错误归属率 | 过期/删除召回率 | 跨 Scope 干扰命中 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    def fmt(value: Any) -> str:
        return "N/A" if value is None else f"{value:.4f}" if isinstance(value, float) else str(value)
    for scope, metrics in report["scope_metrics"].items():
        latency = metrics["retrieval_latency_ms"]
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {}/{} | {} | {} | {} |".format(
            scope, metrics["sample_count"],
            fmt(metrics["candidate_recall_at_k"]["mean"]), fmt(metrics["candidate_precision_at_k"]["mean"]),
            fmt(metrics["candidate_mrr"]["mean"]), fmt(metrics["injected_recall_at_k"]["mean"]),
            fmt(metrics["injected_precision_at_k"]["mean"]), fmt(metrics["injected_mrr"]["mean"]),
            fmt(metrics["no_memory_correct_rate"]["mean"]), fmt(metrics["average_injected_tokens"]),
            fmt(latency["p50"]), fmt(latency["p95"]), fmt(metrics["error_attribution_rate"]),
            fmt(metrics["expired_deleted_recall_rate"]), metrics["cross_scope_distractor_hit_count"],
        ))
    lines += ["", "## 分类统计", "", "| 类别 | 样本 | Candidate Recall@K | Candidate Precision@K | Candidate MRR | 无记忆正确率 |", "|---|---:|---:|---:|---:|---:|"]
    for category, metrics in report["category_metrics"].items():
        lines.append("| {} | {} | {} | {} | {} | {} |".format(
            category, metrics["sample_count"], fmt(metrics["candidate_recall_at_k"]["mean"]),
            fmt(metrics["candidate_precision_at_k"]["mean"]), fmt(metrics["candidate_mrr"]["mean"]),
            fmt(metrics["no_memory_correct_rate"]["mean"]),
        ))
    lines += [
        "", "## 注入有效性", "",
        "人工 Rubric 尚未评分；需至少 10 条样本双人评分（0=无关、1=部分相关、2=直接帮助），不一致由第三人裁定。",
        "", "## 失败样本分析", "",
        "| Query | 期望 | 实际 | 原因分类 | 建议 |",
        "|---|---|---|---|---|",
    ]
    failures = [row for row in report["samples"] if (
        (row["expected_ids"] and (
            not set(row["expected_ids"]) & set(row["candidate_ids"])
            or not set(row["expected_ids"]) & set(row["injected_ids"])
        ))
        or row["foreign_distractor_hits"] or row["excluded_memory_hits"]
        or row["no_memory_correct"] == 0.0 or row["false_positive_ids"]
    )]
    if not failures:
        lines.append("本轮未发现候选漏召回、误召回、跨 Scope 干扰命中或过期/删除记忆召回。")
    else:
        failures.sort(key=lambda row: (len(row["false_positive_ids"]), row["sample_id"]), reverse=True)
        for row in failures[:8]:
            reasons = []
            if row["expected_ids"] and not set(row["expected_ids"]) & set(row["candidate_ids"]):
                reasons.append("candidate_miss")
            if row["expected_ids"] and not set(row["expected_ids"]) & set(row["injected_ids"]):
                reasons.append("not_injected")
            if row["foreign_distractor_hits"]:
                reasons.append("cross_scope_leak")
            if row["excluded_memory_hits"]:
                reasons.append("expired_deleted_recall")
            if row["no_memory_correct"] == 0.0:
                reasons.append("unexpected_recall_on_empty_query")
            if row["false_positive_ids"]:
                reasons.append("false_positive_candidates_shared_OR_terms")
            suggestion = (
                "将共享泛词命中列为后续 V2 候选排序/语义检索实验；V1.4 不改运行时。"
                if row["false_positive_ids"]
                else "检查 Scope/有效期边界，不在本版改变运行时。"
            )
            lines.append(
                f"| {row['query']} | {row['expected_ids']} | {row['candidate_ids']} "
                f"| {','.join(reasons)} | {suggestion} |"
            )
    lines += ["", "## 口径与限制", ""]
    lines.extend(f"- {item}" for item in report["limitations"])
    lines += ["- 空期望集（`no_memory`、`expired_deleted`）的 Recall@K / Precision@K / MRR 记 N/A，不进入非空均值；无记忆正确率只统计这两类。", "- Cross-scope 样本同时包含目标 Scope 的期望记忆和另一 Scope 的同词干扰记忆；参与正常 Recall/Precision/MRR，并单独统计 foreign distractor 命中。", "- 检索延迟：每条样本 warmup 2 次、测量 30 次，统计进程内 Recall 调用，不含 Provider 网络。", "- 参数实验只允许使用 train split；holdout 永不参与参数选择。", ""]
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--split", choices=("all", "train", "holdout"), default="all")
    parser.add_argument("--output-prefix", default="baseline_v1")
    parser.add_argument("--personal-max-memories", type=int)
    parser.add_argument("--personal-max-tokens", type=int)
    parser.add_argument("--personal-candidate-limit", type=int)
    parser.add_argument("--group-max-memories", type=int)
    parser.add_argument("--group-max-tokens", type=int)
    parser.add_argument("--group-candidate-limit", type=int)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    dataset, digest = load_dataset(args.dataset)
    report = run_evaluation(dataset, digest, args)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = REPORT_DIR / f"{args.output_prefix}.json"
    md_path = REPORT_DIR / f"{args.output_prefix}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print(f"JSON: {json_path.relative_to(ROOT)}")
    print(f"Markdown: {md_path.relative_to(ROOT)}")
    for scope, metrics in report["scope_metrics"].items():
        print(scope, "candidate_recall", metrics["candidate_recall_at_k"],
              "candidate_precision", metrics["candidate_precision_at_k"],
              "mrr", metrics["candidate_mrr"],
              "P50/P95", metrics["retrieval_latency_ms"]["p50"], metrics["retrieval_latency_ms"]["p95"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
