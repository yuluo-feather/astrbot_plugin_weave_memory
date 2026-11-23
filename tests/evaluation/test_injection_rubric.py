from __future__ import annotations

import json

import pytest

from scripts import injection_rubric
from scripts.injection_rubric import RubricRow


def _row(sample_id: str, a: int | None, b: int | None, adjudicator: int | None = None) -> RubricRow:
    return RubricRow(
        sample_id=sample_id,
        scope_type="PERSONAL",
        category="exact_fact",
        rater_a=a,
        rater_b=b,
        adjudicator=adjudicator,
        declared_status=injection_rubric.derive_status(a, b, adjudicator),
    )


def test_committed_template_covers_required_double_scored_share_and_stays_pending():
    dataset = json.loads(injection_rubric.DATASET.read_text(encoding="utf-8"))
    rows = injection_rubric.load_rubric()
    injection_rubric.validate_against_dataset(rows, dataset)
    summary = injection_rubric.summarize(rows, total_samples=len(dataset["samples"]))
    assert len(rows) == 12
    assert summary["double_scored_count"] == 0
    assert summary["required_double_scored"] == injection_rubric.required_double_scored(
        len(dataset["samples"])
    )
    assert len(rows) >= summary["required_double_scored"]
    assert summary["pending_count"] == 12
    assert summary["ready"] is False


def test_parse_score_accepts_empty_and_valid_scores_only():
    assert injection_rubric.parse_score("", field="rater_a", sample_id="X") is None
    assert injection_rubric.parse_score("0", field="rater_a", sample_id="X") == 0
    assert injection_rubric.parse_score("2", field="rater_a", sample_id="X") == 2
    with pytest.raises(ValueError, match="0/1/2"):
        injection_rubric.parse_score("3", field="rater_a", sample_id="X")


def test_status_derivation_requires_adjudication_on_disagreement():
    assert injection_rubric.derive_status(None, None, None) == "pending"
    assert injection_rubric.derive_status(1, None, None) == "pending"
    assert injection_rubric.derive_status(1, 1, None) == "complete"
    assert injection_rubric.derive_status(1, 2, None) == "needs_adjudication"
    assert injection_rubric.derive_status(1, 2, 2) == "complete"


def test_summary_computes_agreement_and_readiness_only_after_adjudication():
    rows = [_row(f"S-{index}", 2, 2) for index in range(11)]
    rows.append(_row("S-11", 1, 2, adjudicator=2))
    summary = injection_rubric.summarize(rows, total_samples=54)
    assert summary["double_scored_count"] == 12
    assert summary["required_double_scored"] == 11
    assert summary["exact_agreement_count"] == 11
    assert summary["exact_agreement_rate"] == pytest.approx(11 / 12)
    assert summary["needs_adjudication_count"] == 0
    assert summary["final_scored_count"] == 12
    assert summary["ready"] is True


def test_summary_is_not_ready_with_pending_or_unadjudicated_rows():
    pending = [_row(f"S-{index}", 2, None) for index in range(12)]
    assert injection_rubric.summarize(pending, total_samples=54)["ready"] is False
    disputed = [_row(f"S-{index}", 2, 2) for index in range(11)]
    disputed.append(_row("S-11", 0, 2))
    assert injection_rubric.summarize(disputed, total_samples=54)["ready"] is False


def test_validate_rejects_unknown_ids_and_declared_status_mismatch():
    dataset = json.loads(injection_rubric.DATASET.read_text(encoding="utf-8"))
    unknown = [_row("NOT-IN-DATASET", 2, 2)]
    with pytest.raises(ValueError, match="not in golden set"):
        injection_rubric.validate_against_dataset(unknown, dataset)

    mismatched = [
        RubricRow(
            sample_id="P-EF-01",
            scope_type="PERSONAL",
            category="exact_fact",
            rater_a=2,
            rater_b=2,
            adjudicator=None,
            declared_status="pending",
        )
    ]
    with pytest.raises(ValueError, match="declared status"):
        injection_rubric.validate_against_dataset(mismatched, dataset)
