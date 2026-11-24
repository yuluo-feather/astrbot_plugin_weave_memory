"""V1.9 deterministic direct-write rule (ADR-0030).

The model supplies extraction and a confidence value, but only this module
decides whether a candidate may be written as ACTIVE. Everything here is
deterministic and platform-neutral.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from ..model import ExtractionCandidate, MemoryEntity, MemoryType
from ..normalization import normalize_content

STABLE_TYPES = (MemoryType.FACT, MemoryType.PREFERENCE)
CONFLICT_SIMILARITY = 0.6
SUPPORT_SIMILARITY = 0.5

_FIRST_PERSON = re.compile(r"(?<![一-龥A-Za-z])我(?!们)|本人")
_UNCERTAIN = re.compile(r"可能|也许|大概|应该|或许|说不定|好像|似乎")
_HYPOTHETICAL = re.compile(r"如果|假如|要是|万一|假设|若")
_REQUEST = re.compile(r"^(?:请|帮我|给我|能不能|可以|要不要)")
_QUESTION_TAIL = re.compile(r"[?？]\s*$|(?:吗|呢)\s*$")
_VAGUE_REFERENCE = re.compile(
    r"他(?!们)|她(?!们)|它(?!们)|这个|那个|这些|那些|上面|刚才|之前说|上述"
)
_SENTENCE_SPLIT = re.compile(r"[。！？!?；;\n]+")
_HIGH_RISK_CATEGORIES = frozenset({"credential", "id_number", "bank_card", "address"})


@dataclass(frozen=True, slots=True)
class AutoCommitDecision:
    commit: bool
    rule: str
    reason: str


def similarity(left: str, right: str) -> float:
    """Sequence ratio on normalized text; stdlib and reproducible."""
    return SequenceMatcher(
        None, normalize_content(left), normalize_content(right),
    ).ratio()


def is_explicit_first_person(messages: tuple[str, ...]) -> bool:
    """True when some declarative sentence is an explicit first-person statement."""
    for message in messages:
        for sentence in _SENTENCE_SPLIT.split(message):
            text = sentence.strip()
            if not text or not _FIRST_PERSON.search(text):
                continue
            if _QUESTION_TAIL.search(text) or _REQUEST.search(text):
                continue
            if _HYPOTHETICAL.search(text) or _UNCERTAIN.search(text):
                continue
            return True
    return False


def has_support(candidate: ExtractionCandidate, messages: tuple[str, ...]) -> bool:
    """The candidate must be grounded in at least one captured user message."""
    return any(
        similarity(candidate.content, message) >= SUPPORT_SIMILARITY
        for message in messages
    )


def has_vague_reference(candidate: ExtractionCandidate) -> bool:
    return _VAGUE_REFERENCE.search(candidate.content) is not None


def has_conflict(
    candidate: ExtractionCandidate, active_memories: tuple[MemoryEntity, ...],
    *, consolidation_similarity: float,
) -> bool:
    """Same-type, similar-but-not-identical ACTIVE memory means uncertainty."""
    for active in active_memories:
        if active.memory_type is not candidate.memory_type:
            continue
        score = similarity(candidate.content, active.content)
        if score < CONFLICT_SIMILARITY:
            continue
        if consolidation_similarity > 0 and score >= consolidation_similarity:
            continue
        return True
    return False


def should_auto_commit(
    candidate: ExtractionCandidate, messages: tuple[str, ...],
    active_memories: tuple[MemoryEntity, ...], *,
    confidence_threshold: float, consolidation_similarity: float,
    sensitive_detected: frozenset[str] = frozenset(),
) -> AutoCommitDecision:
    if candidate.memory_type not in STABLE_TYPES:
        return AutoCommitDecision(False, "type_not_stable", "EVENT is candidate-only")
    if candidate.confidence is None or candidate.confidence < confidence_threshold:
        return AutoCommitDecision(False, "low_confidence", "below threshold")
    if not is_explicit_first_person(messages):
        return AutoCommitDecision(
            False, "no_explicit_statement", "no declarative first-person sentence",
        )
    if not has_support(candidate, messages):
        return AutoCommitDecision(False, "insufficient_support", "not grounded")
    if has_vague_reference(candidate):
        return AutoCommitDecision(False, "vague_reference", "unresolved reference")
    if sensitive_detected & _HIGH_RISK_CATEGORIES:
        return AutoCommitDecision(
            False, "sensitive_category", "high-risk category stays candidate",
        )
    if has_conflict(
        candidate, active_memories,
        consolidation_similarity=consolidation_similarity,
    ):
        return AutoCommitDecision(False, "conflict", "conflicting active memory")
    return AutoCommitDecision(True, "explicit_first_person_stable_attribute", "ok")
