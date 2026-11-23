"""Pure core tests for query classification, RRF, privacy and trace DTO."""

import json

from data.plugins.astrbot_plugin_weave_memory.core.model.retrieval_trace import (
    ChannelCandidate,
    ChannelTrace,
    RetrievalTrace,
)
from data.plugins.astrbot_plugin_weave_memory.core.policy.query_classifier import (
    QueryType,
    classify,
)
from data.plugins.astrbot_plugin_weave_memory.core.privacy import redact_query
from data.plugins.astrbot_plugin_weave_memory.core.retrieval import (
    reciprocal_rank_fusion,
)


def test_query_classifier_rules_and_off_mode():
    assert classify("上次买的咖啡多少钱").query_type is QueryType.TEMPORAL
    assert classify("她和我是什么关系").query_type is QueryType.RELATION
    assert classify("我的工号是多少").query_type is QueryType.FACT
    assert classify("聊聊今天的感觉").query_type is QueryType.SEMANTIC
    off = classify("任何问题", mode="off")
    assert off.mode == "off" and off.query_type is None


def test_rrf_uses_rank_fusion_and_stable_ties():
    result = reciprocal_rank_fusion({
        "FTS": ["a", "b", "c"],
        "VECTOR": ["b", "a", "d"],
    })
    assert result.method == "RRF"
    assert [item.memory_id for item in result.candidates] == ["a", "b", "c", "d"]
    assert result.candidates[0].score >= result.candidates[1].score
    single = reciprocal_rank_fusion({"FTS": ["x", "y"]})
    assert single.method == "NONE"
    assert [item.memory_id for item in single.candidates] == ["x", "y"]


def test_privacy_redacts_email_phone_and_long_numbers():
    text = redact_query("mail me a@b.com or 13800138000 id 123456789012345")
    assert "a@b.com" not in text
    assert "13800138000" not in text
    assert "123456789012345" not in text


def test_trace_is_body_free_bounded_and_query_is_opt_in():
    secret = "这是绝不能被写进 trace 的正文"
    trace = RetrievalTrace(
        trace_id="t1", occurred_at="2026-11-24T12:00:00+00:00",
        identity={"platform": "astrbot", "tenant_hash": "x", "subject_hash": "y"},
        scope={"id": "s1", "type": "PERSONAL"},
        query_hash="hash", query_length=7,
        classifier={"mode": "rules", "query_type": "SEMANTIC"},
        channels=(
            ChannelTrace(
                name="FTS", enabled=True, limit=100, ok=True, latency_ms=1,
                candidates=tuple(
                    ChannelCandidate(str(index), index, 1.0, 1.0, "eligible")
                    for index in range(1, 130)
                ),
            ),
        ),
        fusion={"method": "NONE", "k": 60, "candidates": []},
        rerank={"enabled": False},
        final={"selected_ids": ["1"], "injected_ids": [], "omitted": [],
               "estimated_tokens": 0, "budget": 0},
        provider={}, index_state={"target": "VECTOR", "status": "HEALTHY"},
        latency_ms={"total": 1}, query_text=secret,
    )
    payload = trace.to_dict(include_query_text=False)
    assert "text" not in payload["query"]
    assert secret not in json.dumps(payload, ensure_ascii=False)
    assert len(payload["channels"][0]["candidates"]) == 100
    assert payload["truncated"] is True
    with_query = trace.to_dict(include_query_text=True)
    assert with_query["query"]["text"] == secret
