"""Rule-based query classification (no LLM, stdlib only)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class QueryType(StrEnum):
    FACT = "FACT"
    SEMANTIC = "SEMANTIC"
    RELATION = "RELATION"
    TEMPORAL = "TEMPORAL"


@dataclass(frozen=True, slots=True)
class QueryClassification:
    mode: str
    query_type: QueryType | None
    confidence: float
    rule_id: str


_TEMPORAL = (
    "今天", "昨天", "明天", "之前", "之后", "上次", "最近", "什么时候", "哪天",
    "时间", "上周", "这周", "上个月",
    "when", "last time", "date", "yesterday", "tomorrow", "today",
)
_RELATION = (
    "关系", "朋友", "同事", "和他", "和她", "她的", "他的", "谁的", "谁",
    "who is", "relationship", "friend", "colleague",
)
_FACT = (
    "是什么", "多少", "谁", "哪天", "生日", "在哪里", "几点", "几个",
    "what is", "how many", "where", "birthday",
)


def classify(query: str, *, mode: str = "rules") -> QueryClassification:
    text = (query or "").strip()
    normalized = text.lower()
    if mode == "off" or not normalized:
        return QueryClassification(mode="off", query_type=None,
                                   confidence=0.0, rule_id="off")
    if any(token in normalized for token in _TEMPORAL):
        return QueryClassification("rules", QueryType.TEMPORAL, 0.8, "temporal")
    if any(token in normalized for token in _RELATION):
        return QueryClassification("rules", QueryType.RELATION, 0.7, "relation")
    if (
        any(token in normalized for token in _FACT)
        or any(char.isdigit() for char in normalized)
        or '"' in text or "“" in text or "”" in text
    ):
        return QueryClassification("rules", QueryType.FACT, 0.7, "fact")
    return QueryClassification("rules", QueryType.SEMANTIC, 0.5, "semantic")
