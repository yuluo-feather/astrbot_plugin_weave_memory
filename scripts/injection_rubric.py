#!/usr/bin/env python3
"""Validate and summarize the human injection-effectiveness rubric.

The rubric is filled by human raters; this script never invents scores. It only
checks completeness, derives adjudication needs, and reports agreement so the
V1.4 report can stop claiming "pending" once humans finish scoring.

Usage:
    python scripts/injection_rubric.py            # human-readable summary
    python scripts/injection_rubric.py --check    # exit 1 until rubric is ready
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RUBRIC = ROOT / "eval" / "v1.4" / "datasets" / "injection_rubric_template.csv"
DATASET = ROOT / "eval" / "v1.4" / "datasets" / "golden_v1.json"

REQUIRED_COLUMNS = (
    "sample_id",
    "scope_type",
    "category",
    "rater_a_score_0_2",
    "rater_b_score_0_2",
    "adjudicator_score_0_2",
    "rater_a_note",
    "rater_b_note",
    "adjudication_note",
    "status",
)
SCORES = {"0", "1", "2"}
MINIMUM_DOUBLE_SCORED = 10
DOUBLE_SCORED_RATIO = 0.2


@dataclass(frozen=True)
class RubricRow:
    sample_id: str
    scope_type: str
    category: str
    rater_a: int | None
    rater_b: int | None
    adjudicator: int | None
    declared_status: str

    @property
    def status(self) -> str:
        return derive_status(self.rater_a, self.rater_b, self.adjudicator)

    @property
    def double_scored(self) -> bool:
        return self.rater_a is not None and self.rater_b is not None

    @property
    def final_score(self) -> int | None:
        if self.adjudicator is not None:
            return self.adjudicator
        if self.rater_a is not None and self.rater_a == self.rater_b:
            return self.rater_a
        return None


def parse_score(value: str, *, field: str, sample_id: str) -> int | None:
    text = value.strip()
    if not text:
        return None
    if text not in SCORES:
        raise ValueError(f"{sample_id}: {field} must be empty or one of 0/1/2, got {value!r}")
    return int(text)


def derive_status(rater_a: int | None, rater_b: int | None, adjudicator: int | None) -> str:
    if adjudicator is not None:
        return "complete"
    if rater_a is None or rater_b is None:
        return "pending"
    if rater_a == rater_b:
        return "complete"
    return "needs_adjudication"


def load_rubric(path: Path = RUBRIC) -> list[RubricRow]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = set(REQUIRED_COLUMNS) - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"rubric missing columns: {sorted(missing)}")
        rows = []
        for raw in reader:
            sample_id = raw["sample_id"].strip()
            if not sample_id:
                raise ValueError("rubric contains a blank sample_id")
            row = RubricRow(
                sample_id=sample_id,
                scope_type=raw["scope_type"].strip(),
                category=raw["category"].strip(),
                rater_a=parse_score(raw["rater_a_score_0_2"], field="rater_a_score_0_2", sample_id=sample_id),
                rater_b=parse_score(raw["rater_b_score_0_2"], field="rater_b_score_0_2", sample_id=sample_id),
                adjudicator=parse_score(
                    raw["adjudicator_score_0_2"], field="adjudicator_score_0_2", sample_id=sample_id,
                ),
                declared_status=raw["status"].strip() or "pending",
            )
            rows.append(row)
    return rows


def validate_against_dataset(rows: list[RubricRow], dataset: dict[str, Any]) -> None:
    samples = {item["sample_id"]: item for item in dataset["samples"]}
    seen: set[str] = set()
    for row in rows:
        if row.sample_id in seen:
            raise ValueError(f"duplicate rubric sample_id: {row.sample_id}")
        seen.add(row.sample_id)
        sample = samples.get(row.sample_id)
        if sample is None:
            raise ValueError(f"rubric sample_id not in golden set: {row.sample_id}")
        if row.scope_type != sample["scope"]["scope_type"]:
            raise ValueError(f"{row.sample_id}: scope_type mismatch with golden set")
        if row.category != sample["category"]:
            raise ValueError(f"{row.sample_id}: category mismatch with golden set")
        if row.declared_status != row.status:
            raise ValueError(
                f"{row.sample_id}: declared status {row.declared_status!r} "
                f"does not match derived status {row.status!r}"
            )


def required_double_scored(total_samples: int) -> int:
    return max(MINIMUM_DOUBLE_SCORED, math.ceil(total_samples * DOUBLE_SCORED_RATIO))


def summarize(rows: list[RubricRow], *, total_samples: int) -> dict[str, Any]:
    doubled = [row for row in rows if row.double_scored]
    statuses = {"pending": 0, "needs_adjudication": 0, "complete": 0}
    for row in rows:
        statuses[row.status] += 1
    agreements = [row for row in doubled if row.rater_a == row.rater_b]
    finals = [score for row in rows if (score := row.final_score) is not None]
    required = required_double_scored(total_samples)
    ready = (
        len(doubled) >= required
        and statuses["pending"] == 0
        and statuses["needs_adjudication"] == 0
    )
    return {
        "total_samples": total_samples,
        "rubric_rows": len(rows),
        "double_scored_count": len(doubled),
        "required_double_scored": required,
        "exact_agreement_count": len(agreements),
        "exact_agreement_rate": (len(agreements) / len(doubled)) if doubled else None,
        "final_scored_count": len(finals),
        "final_score_mean": (sum(finals) / len(finals)) if finals else None,
        "pending_count": statuses["pending"],
        "needs_adjudication_count": statuses["needs_adjudication"],
        "complete_count": statuses["complete"],
        "ready": ready,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rubric", type=Path, default=RUBRIC)
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    parser.add_argument("--check", action="store_true", help="exit 1 unless the rubric is ready")
    args = parser.parse_args()

    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    rows = load_rubric(args.rubric)
    validate_against_dataset(rows, dataset)
    summary = summarize(rows, total_samples=len(dataset["samples"]))

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"rubric rows: {summary['rubric_rows']} / samples: {summary['total_samples']}")
        print(
            "double scored: {double_scored_count} (required {required_double_scored})".format(**summary)
        )
        print(
            "status: pending={pending_count} needs_adjudication={needs_adjudication_count} "
            "complete={complete_count}".format(**summary)
        )
        print(
            "final scores: {final_scored_count}, mean {final_score_mean}".format(**summary)
        )
        print(f"ready: {summary['ready']}")
    if args.check and not summary["ready"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
