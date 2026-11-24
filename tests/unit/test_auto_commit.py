from __future__ import annotations

from datetime import UTC, datetime

from data.plugins.astrbot_plugin_weave_memory.core.model import (
    ExtractionCandidate,
    MemoryEntity,
    MemoryStatus,
    MemoryType,
    SourceType,
)
from data.plugins.astrbot_plugin_weave_memory.core.normalization import (
    normalize_content,
)
from data.plugins.astrbot_plugin_weave_memory.core.policy.auto_commit import (
    has_conflict,
    has_polarity_mismatch,
    is_explicit_first_person,
    should_auto_commit,
    similarity,
)

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


def _candidate(content: str, *, memory_type=MemoryType.FACT,
               confidence: float | None = 0.9) -> ExtractionCandidate:
    return ExtractionCandidate(memory_type, content, confidence=confidence)


def _active(content: str, *, memory_type=MemoryType.FACT) -> MemoryEntity:
    return MemoryEntity(
        id=f"m-{abs(hash(content))}", scope_id="s", subject_id="u",
        memory_type=memory_type, content=content,
        normalized_content=normalize_content(content), status=MemoryStatus.ACTIVE,
        source_type=SourceType.EXPLICIT, current_revision=1,
        created_at=NOW, updated_at=NOW,
    )


def _decide(candidate, messages, actives=(), **overrides):
    options = {
        "confidence_threshold": 0.8,
        "consolidation_similarity": 0.9,
    }
    options.update(overrides)
    return should_auto_commit(candidate, messages, actives, **options)


def test_first_person_statement_accepts_declarative_sentence():
    assert is_explicit_first_person(("我住在杭州。",))
    assert is_explicit_first_person(("今天下雨。我喜欢黑咖啡",))


def test_first_person_statement_rejects_question_hypothesis_and_request():
    assert not is_explicit_first_person(("我住在杭州吗？",))
    assert not is_explicit_first_person(("如果我是杭州人就好了",))
    assert not is_explicit_first_person(("帮我记住我喜欢黑咖啡",))
    assert not is_explicit_first_person(("我明天可能去杭州",))


def test_direct_write_requires_confidence_and_support():
    candidate = _candidate("用户住在杭州")
    assert _decide(candidate, ("我住在杭州",)).commit
    assert not _decide(candidate, ("我住在杭州",), confidence_threshold=0.95).commit
    assert not _decide(candidate, ("今天天气不错",)).commit


def test_event_and_vague_reference_stay_candidates():
    event = _candidate("用户今天去了杭州", memory_type=MemoryType.EVENT)
    assert not _decide(event, ("我今天去了杭州",)).commit
    vague = _candidate("用户喜欢那个")
    assert not _decide(vague, ("我喜欢那个",)).commit


def test_sensitive_category_stays_candidate_even_when_grounded():
    candidate = _candidate("用户的密码是 hunter2")
    decision = _decide(
        candidate, ("我的密码是 hunter2",),
        sensitive_detected=frozenset({"credential"}),
    )
    assert not decision.commit
    assert decision.rule == "sensitive_category"


def test_conflict_and_near_duplicate_are_distinguished():
    candidate = _candidate("用户喜欢绿茶")
    conflicting = (_active("用户喜欢黑咖啡"),)
    assert has_conflict(
        candidate, conflicting, consolidation_similarity=0.9,
    )
    assert not _decide(candidate, ("我喜欢绿茶",), conflicting).commit
    duplicate = _candidate("用户喜欢黑咖啡")
    near = (_active("用户喜欢喝黑咖啡"),)
    assert not has_conflict(duplicate, near, consolidation_similarity=0.9)
    assert similarity("用户喜欢黑咖啡", "用户喜欢喝黑咖啡") >= 0.9


def test_polarity_flip_is_a_conflict_not_a_duplicate():
    candidate = _candidate("用户不喜欢猫")
    existing = (_active("用户喜欢猫"),)
    assert has_polarity_mismatch("用户喜欢猫", "用户不喜欢猫")
    assert similarity("用户喜欢猫", "用户不喜欢猫") >= 0.9
    assert has_conflict(candidate, existing, consolidation_similarity=0.9)
    assert not _decide(candidate, ("我不喜欢猫",), existing).commit


def test_third_party_sentence_cannot_support_personal_fact():
    messages = ("我喜欢咖啡。我朋友住在杭州。",)
    assert not is_explicit_first_person(("我朋友住在杭州",))
    assert not _decide(_candidate("用户住在杭州"), messages).commit
