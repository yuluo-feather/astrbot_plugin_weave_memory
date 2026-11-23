#!/usr/bin/env python3
"""Offline V1.5 extraction quality harness.

Consumes the frozen extraction protocol fixture and a recorded model-response
file. It never calls an LLM itself; a real run records candidates per
conversation, then this script computes metrics and writes JSON/Markdown.

Matching is normalized string equality as a development proxy. The protocol
requires human semantic adjudication before treating these numbers as final.
"""
# ruff: noqa: E402 - imports below must follow the package namespace bootstrap.
from __future__ import annotations

import argparse
import importlib.machinery
import json
import sys
import types
from collections import Counter
from pathlib import Path
from typing import Any

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

from data.plugins.astrbot_plugin_weave_memory.app.extraction_metrics import (
    ExpectedCandidate,
    compute_metrics,
)
from data.plugins.astrbot_plugin_weave_memory.core.model import (
    ExtractionCandidate,
    MemoryType,
)

DEFAULT_DATASET = ROOT / "eval" / "v1.4" / "datasets" / "golden_conversations_v1.json"
REPORT_DIR = ROOT / "eval" / "v1.5" / "reports"


def _expected(item: dict[str, Any]) -> ExpectedCandidate:
    return ExpectedCandidate(
        memory_type=str(item.get("memory_type", "")),
        content=str(item.get("content", "")),
        subject=str(item.get("subject", "")),
    )


def _actual(item: dict[str, Any]) -> ExtractionCandidate:
    memory_type = MemoryType(str(item.get("memory_type", "")).upper())
    return ExtractionCandidate(memory_type=memory_type, content=str(item.get("content", "")))


def evaluate(dataset: dict[str, Any], responses: dict[str, Any]) -> dict[str, Any]:
    rows = []
    totals: Counter[str] = Counter()
    for conversation in dataset["conversations"]:
        conversation_id = conversation["conversation_id"]
        entry = responses.get(conversation_id, {})
        actual = [_actual(item) for item in entry.get("candidates", [])]
        metrics = compute_metrics(
            expected=[_expected(item) for item in conversation.get("expected_candidates", [])],
            actual=actual,
            forbidden=[_expected(item) for item in conversation.get("forbidden_candidates", [])],
            subject_id=str(conversation.get("subject_id", "synthetic-user")),
        )
        model_calls = int(entry.get("model_calls", 0))
        sensitive_case = conversation["expected_action"] == "filter_sensitive"
        sensitive_leak = metrics.sensitive_leak or (sensitive_case and model_calls > 0)
        totals["expected"] += metrics.expected_count
        totals["actual"] += metrics.actual_count
        totals["matched"] += metrics.matched_count
        totals["forbidden"] += metrics.forbidden_matched
        totals["sensitive_cases"] += int(sensitive_case)
        totals["sensitive_leaks"] += int(sensitive_leak)
        rows.append({
            "conversation_id": conversation_id,
            "case": conversation.get("case", ""),
            "expected_action": conversation.get("expected_action", ""),
            "model_calls": model_calls,
            "expected_count": metrics.expected_count,
            "actual_count": metrics.actual_count,
            "matched_count": metrics.matched_count,
            "forbidden_matched": metrics.forbidden_matched,
            "sensitive_leak": sensitive_leak,
            "precision": metrics.precision,
            "recall": metrics.recall,
            "false_memory_rate": metrics.false_memory_rate,
        })
    aggregate = {
        "conversations": len(rows),
        "expected_candidates": totals["expected"],
        "actual_candidates": totals["actual"],
        "matched_candidates": totals["matched"],
        "forbidden_matched": totals["forbidden"],
        "precision": (totals["matched"] / totals["actual"]) if totals["actual"] else None,
        "recall": (totals["matched"] / totals["expected"]) if totals["expected"] else None,
        "false_memory_rate": (totals["forbidden"] / totals["actual"]) if totals["actual"] else None,
        "sensitive_cases": totals["sensitive_cases"],
        "sensitive_leak_count": totals["sensitive_leaks"],
        "sensitive_leakage_rate": (
            totals["sensitive_leaks"] / totals["sensitive_cases"]
            if totals["sensitive_cases"] else None
        ),
    }
    return {
        "dataset_name": dataset.get("dataset_name", ""),
        "dataset_version": dataset.get("dataset_version", ""),
        "matching": "normalized_exact_string_equality_development_proxy",
        "aggregate": aggregate,
        "conversations": rows,
        "limitations": [
            "Candidates must be human-adjudicated for semantic equivalence before final acceptance.",
            "A real run must record model calls and candidates; this harness never calls a model.",
            "Sensitive leakage is a safety gate: any non-zero value fails V1.5 acceptance.",
        ],
    }


def render_markdown(report: dict[str, Any]) -> str:
    aggregate = report["aggregate"]
    def fmt(value: Any) -> str:
        return "N/A" if value is None else f"{value:.4f}" if isinstance(value, float) else str(value)
    lines = [
        "# V1.5 提取质量离线报告", "",
        f"- 数据集：`{report['dataset_name']}` v`{report['dataset_version']}`",
        f"- 匹配口径：`{report['matching']}`",
        f"- 对话样本：{aggregate['conversations']}；期望候选 {aggregate['expected_candidates']}；实际候选 {aggregate['actual_candidates']}",
        "",
        "## 汇总", "",
        "| 指标 | 值 |", "|---|---:|",
        f"| Extraction Precision | {fmt(aggregate['precision'])} |",
        f"| Extraction Recall | {fmt(aggregate['recall'])} |",
        f"| False Memory Rate | {fmt(aggregate['false_memory_rate'])} |",
        f"| Sensitive Leakage Rate | {fmt(aggregate['sensitive_leakage_rate'])} |",
        f"| 禁止候选命中数 | {aggregate['forbidden_matched']} |",
        "",
        "## 逐对话", "",
        "| 对话 | 用例 | 动作 | 模型调用 | 期望/实际/命中 | 禁止命中 | 敏感泄漏 |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for row in report["conversations"]:
        lines.append(
            f"| {row['conversation_id']} | {row['case']} | {row['expected_action']} "
            f"| {row['model_calls']} | {row['expected_count']}/{row['actual_count']}/{row['matched_count']} "
            f"| {row['forbidden_matched']} | {'是' if row['sensitive_leak'] else '否'} |"
        )
    lines += ["", "## 限制", ""]
    lines.extend(f"- {item}" for item in report["limitations"])
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--responses", type=Path, required=True)
    parser.add_argument("--output-prefix", default="extraction_baseline_v1")
    args = parser.parse_args()
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    responses = json.loads(args.responses.read_text(encoding="utf-8"))
    report = evaluate(dataset, responses)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = REPORT_DIR / f"{args.output_prefix}.json"
    md_path = REPORT_DIR / f"{args.output_prefix}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    aggregate = report["aggregate"]
    print(f"JSON: {json_path.relative_to(ROOT)}")
    print(f"Markdown: {md_path.relative_to(ROOT)}")
    print(
        "precision", aggregate["precision"], "recall", aggregate["recall"],
        "sensitive_leakage", aggregate["sensitive_leakage_rate"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
