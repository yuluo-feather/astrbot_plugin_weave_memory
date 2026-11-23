from __future__ import annotations

import json

import pytest

from scripts import evaluate_v15_extraction as harness


def _dataset():
    return json.loads(harness.DEFAULT_DATASET.read_text(encoding="utf-8"))


def _oracle_responses(dataset):
    responses = {}
    for conversation in dataset["conversations"]:
        action = conversation["expected_action"]
        model_calls = 0 if action in {
            "skip_before_window", "skip_before_llm", "filter_sensitive",
        } else 1
        responses[conversation["conversation_id"]] = {
            "candidates": [
                {"memory_type": item["memory_type"], "content": item["content"]}
                for item in conversation.get("expected_candidates", [])
            ],
            "model_calls": model_calls,
        }
    return responses


def test_harness_scores_oracle_responses_without_sensitive_leakage():
    dataset = _dataset()
    report = harness.evaluate(dataset, _oracle_responses(dataset))
    aggregate = report["aggregate"]
    assert aggregate["expected_candidates"] > 0
    assert aggregate["precision"] == 1.0
    assert aggregate["recall"] == 1.0
    assert aggregate["forbidden_matched"] == 0
    assert aggregate["sensitive_leak_count"] == 0
    assert aggregate["sensitive_leakage_rate"] == 0.0


def test_harness_flags_sensitive_case_that_called_the_model():
    dataset = _dataset()
    responses = _oracle_responses(dataset)
    responses["EC-006"]["model_calls"] = 1
    report = harness.evaluate(dataset, responses)
    assert report["aggregate"]["sensitive_leak_count"] == 1
    assert report["aggregate"]["sensitive_leakage_rate"] == pytest.approx(1 / 3)


def test_harness_render_markdown_contains_aggregate_table():
    dataset = _dataset()
    report = harness.evaluate(dataset, _oracle_responses(dataset))
    markdown = harness.render_markdown(report)
    assert "# V1.5 提取质量离线报告" in markdown
    assert "Sensitive Leakage Rate" in markdown
    assert "EC-001" in markdown
