from __future__ import annotations

import pytest
from data.plugins.astrbot_plugin_weave_memory.app.extraction_metrics import (
    ExpectedCandidate,
    compute_metrics,
)
from data.plugins.astrbot_plugin_weave_memory.core.model import (
    ExtractionCandidate,
    MemoryType,
)


def test_metrics_match_normalized_expected_candidates():
    metrics = compute_metrics(
        expected=[ExpectedCandidate("PREFERENCE", "用户喜欢黑咖啡。", "u1")],
        actual=[ExtractionCandidate(MemoryType.PREFERENCE, "用户喜欢黑咖啡")],
        forbidden=[ExpectedCandidate("FACT", "用户住在火星")],
        subject_id="u1",
    )
    assert metrics.matched_count == 1
    assert metrics.precision == 1.0
    assert metrics.recall == 1.0
    assert metrics.false_memory_rate == 0.0
    assert metrics.attribution_accuracy == 1.0
    assert metrics.sensitive_leak is False


def test_metrics_flag_forbidden_and_sensitive_candidates():
    metrics = compute_metrics(
        expected=[],
        actual=[
            ExtractionCandidate(MemoryType.FACT, "用户住在火星"),
            ExtractionCandidate(MemoryType.FACT, "用户密码是 hunter2"),
        ],
        forbidden=[ExpectedCandidate("FACT", "用户住在火星")],
        subject_id="u1",
    )
    assert metrics.precision == 0.0
    assert metrics.recall is None
    assert metrics.forbidden_matched == 1
    assert metrics.false_memory_rate == pytest.approx(0.5)
    assert metrics.sensitive_leak is True


def test_metrics_empty_actual_and_empty_expected_are_na():
    metrics = compute_metrics(
        expected=[], actual=[], forbidden=[], subject_id="u1",
    )
    assert metrics.precision is None
    assert metrics.recall is None
    assert metrics.false_memory_rate is None
    assert metrics.attribution_accuracy is None


def test_metrics_flag_third_party_expected_subject_mismatch():
    metrics = compute_metrics(
        expected=[ExpectedCandidate("PREFERENCE", "用户喜欢 Vim", "someone-else")],
        actual=[ExtractionCandidate(MemoryType.PREFERENCE, "用户喜欢 Vim")],
        forbidden=[],
        subject_id="u1",
    )
    assert metrics.attribution_wrong == 1
    assert metrics.attribution_accuracy == 0.0
