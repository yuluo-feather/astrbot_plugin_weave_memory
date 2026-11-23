from __future__ import annotations

import argparse
import copy

import pytest

from scripts import evaluate_v14


def _args(**overrides):
    values = {
        "split": "all",
        "personal_max_memories": None,
        "personal_max_tokens": None,
        "personal_candidate_limit": None,
        "group_max_memories": None,
        "group_max_tokens": None,
        "group_candidate_limit": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _subset_report(*sample_ids):
    dataset, digest = evaluate_v14.load_dataset(evaluate_v14.DATASET)
    subset = copy.deepcopy(dataset)
    subset["samples"] = [
        next(item for item in dataset["samples"] if item["sample_id"] == sample_id)
        for sample_id in sample_ids
    ]
    return evaluate_v14.run_evaluation(subset, digest, _args())


def _without_trace_id(rows):
    return [{key: value for key, value in row.items() if key != "trace_id"} for row in rows]


def test_golden_set_meets_v14_minimum_and_privacy_contract():
    dataset, _ = evaluate_v14.load_dataset(evaluate_v14.DATASET)
    assert len(dataset["samples"]) == 50
    assert sum(sample["split"] == "holdout" for sample in dataset["samples"]) == 10
    assert dataset["data_policy"] == "synthetic_only_no_production_memory_content"
    assert dataset["annotation_status"] == "assistant_generated_synthetic_labels_pending_human_review"
    assert {sample["labeler"] for sample in dataset["samples"]} == {"assistant-generated-draft"}
    assert {sample["scope"]["scope_type"] for sample in dataset["samples"]} == {"PERSONAL", "GROUP"}


def test_empty_expected_metrics_are_na_and_nonempty_miss_is_zero():
    assert evaluate_v14.retrieval_metrics([], ["noise"], k=4, empty_expected=True) == {
        "recall": None, "precision": None, "mrr": None,
    }
    assert evaluate_v14.retrieval_metrics(["wanted"], [], k=4, empty_expected=False) == {
        "recall": 0.0, "precision": 0.0, "mrr": 0.0,
    }
    assert evaluate_v14.retrieval_metrics(["wanted"], ["wanted"], k=4, empty_expected=False)["precision"] == 0.25


def test_parameter_experiments_cannot_use_holdout():
    dataset, digest = evaluate_v14.load_dataset(evaluate_v14.DATASET)
    with pytest.raises(ValueError, match="train only"):
        evaluate_v14.run_evaluation(
            dataset, digest, _args(split="holdout", personal_max_tokens=256),
        )


def test_parameter_overrides_must_be_positive():
    dataset, digest = evaluate_v14.load_dataset(evaluate_v14.DATASET)
    with pytest.raises(ValueError, match="positive integers"):
        evaluate_v14.run_evaluation(
            dataset, digest, _args(split="train", group_max_memories=0),
        )


def test_same_synthetic_data_produces_same_retrieval_results():
    dataset, digest = evaluate_v14.load_dataset(evaluate_v14.DATASET)
    subset = copy.deepcopy(dataset)
    subset["samples"] = [
        next(item for item in dataset["samples"] if item["sample_id"] == sample_id)
        for sample_id in ("P-EF-01", "G-EF-01", "P-CS-01", "G-CS-01", "P-NM-01", "G-ED-01")
    ]
    first = evaluate_v14.run_evaluation(subset, digest, _args())
    second = evaluate_v14.run_evaluation(subset, digest, _args())
    # trace_id is a per-call UUID, so exclude only that field from the repeatability check.
    assert _without_trace_id(first["samples"]) == _without_trace_id(second["samples"])
    assert first["parameters"] == second["parameters"]
    cross_scope = [row for row in first["samples"] if row["category"] == "cross_scope"]
    assert len(cross_scope) == 2
    assert all(row["candidate_recall"] == 1.0 for row in cross_scope)
    assert all(not row["foreign_distractor_hits"] for row in cross_scope)


def test_report_records_fts_order_by():
    report = _subset_report("P-EF-01")
    order_by = report["parameters"]["fts"]["order_by"]
    assert "bm25" in order_by


def test_report_rows_include_trace_and_candidate_diagnostics():
    report = _subset_report("P-EF-01", "G-EF-01")
    for row in report["samples"]:
        assert isinstance(row["trace_id"], str) and row["trace_id"]
        assert isinstance(row["query_hash"], str) and row["query_hash"]
        assert row["candidate_diagnostics"]
        for item in row["candidate_diagnostics"]:
            assert {"id", "rank", "bm25_score", "filter_reason"} <= item.keys()
        assert isinstance(row["omitted_diagnostics"], list)


def test_extraction_conversation_set_is_protocol_only_and_covers_gate_cases():
    import json
    from pathlib import Path

    path = Path(evaluate_v14.ROOT) / "eval" / "v1.4" / "datasets" / "golden_conversations_v1.json"
    dataset = json.loads(path.read_text(encoding="utf-8"))
    assert dataset["status"] == "protocol_fixture_only_not_executed_in_v1_4"
    assert dataset["annotation_status"] == "assistant_generated_synthetic_labels_pending_human_review"
    assert len(dataset["conversations"]) >= 10
    assert any(item.get("scope_type") == "GROUP" and item["expected_action"] == "skip_before_window" for item in dataset["conversations"])
    assert any(item["consent"].get("auto_enroll") and not item["consent"].get("auto_extract") for item in dataset["conversations"])
    assert all("expected_candidates" in item and "expected_window_message_ids" in item for item in dataset["conversations"])


def test_committed_baseline_report_is_complete_and_safe():
    import json
    from pathlib import Path

    dataset, digest = evaluate_v14.load_dataset(evaluate_v14.DATASET)
    path = Path(evaluate_v14.ROOT) / "eval" / "v1.4" / "reports" / "baseline_v1.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["dataset_sha256"] == digest
    assert len(report["samples"]) == len(dataset["samples"]) == 50
    assert isinstance(report["environment"]["runtime_source_dirty"], bool)
    assert isinstance(report["environment"]["runtime_source_commit"], str)
    assert "bm25" in report["parameters"]["fts"]["order_by"]
    for row in report["samples"]:
        assert isinstance(row["trace_id"], str) and row["trace_id"]
        assert isinstance(row["query_hash"], str) and row["query_hash"]
        assert isinstance(row["candidate_diagnostics"], list)
        assert isinstance(row["omitted_diagnostics"], list)
    for scope_type in ("PERSONAL", "GROUP"):
        metrics = report["scope_metrics"][scope_type]
        assert metrics["sample_count"] == 25
        assert metrics["candidate_recall_at_k"]["mean"] == 1.0
        assert metrics["no_memory_correct_rate"]["mean"] == 1.0
        assert metrics["error_attribution_rate"] == 0.0
        assert metrics["expired_deleted_recall_rate"] == 0.0
        assert metrics["cross_scope_distractor_hit_count"] == 0
        assert metrics["injection_effectiveness"]["status"] == "pending_human_rubric"
