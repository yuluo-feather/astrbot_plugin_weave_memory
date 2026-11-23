"""Pure V1.5 extraction quality metrics (protocol: extraction_evaluation_protocol_v1)."""

from __future__ import annotations

from dataclasses import dataclass

from ..core.model import ExtractionCandidate
from ..core.normalization import normalize_content
from .sensitive_filter import detect_sensitive_categories


@dataclass(frozen=True, slots=True)
class ExpectedCandidate:
    memory_type: str
    content: str
    subject: str = ""


@dataclass(frozen=True, slots=True)
class ExtractionMetrics:
    expected_count: int
    actual_count: int
    matched_count: int
    forbidden_matched: int
    attribution_wrong: int
    sensitive_leak: bool
    precision: float | None
    recall: float | None
    false_memory_rate: float | None
    attribution_accuracy: float | None


def _key(memory_type: str, content: str) -> tuple[str, str]:
    return memory_type.strip().upper(), normalize_content(content)


def compute_metrics(
    *, expected: list[ExpectedCandidate], actual: list[ExtractionCandidate],
    forbidden: list[ExpectedCandidate], subject_id: str,
) -> ExtractionMetrics:
    expected_keys = {_key(item.memory_type, item.content) for item in expected}
    forbidden_keys = {_key(item.memory_type, item.content) for item in forbidden}
    actual_keys = set()
    sensitive_leak = False
    for candidate in actual:
        actual_keys.add(_key(candidate.memory_type.value, candidate.content))
        if detect_sensitive_categories(candidate.content):
            sensitive_leak = True

    matched = len(actual_keys & expected_keys)
    forbidden_matched = len(actual_keys & forbidden_keys)
    attribution_wrong = sum(
        1 for item in expected if item.subject and item.subject != subject_id
    )
    return ExtractionMetrics(
        expected_count=len(expected_keys),
        actual_count=len(actual_keys),
        matched_count=matched,
        forbidden_matched=forbidden_matched,
        attribution_wrong=attribution_wrong,
        sensitive_leak=sensitive_leak,
        precision=(matched / len(actual_keys)) if actual_keys else None,
        recall=(matched / len(expected_keys)) if expected_keys else None,
        false_memory_rate=(forbidden_matched / len(actual_keys)) if actual_keys else None,
        attribution_accuracy=(
            (len(actual_keys) - attribution_wrong) / len(actual_keys)
            if actual_keys else None
        ),
    )
