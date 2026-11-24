"""Validated provider/index configuration (ADR-0026 defaults live here)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

logger = logging.getLogger(__name__)


def _int(config: Mapping[str, Any], key: str, default: int,
         minimum: int, maximum: int) -> int:
    try:
        value = int(config.get(key, default))
    except (TypeError, ValueError):
        logger.warning("config %s is not an integer; using %s", key, default)
        return default
    return max(minimum, min(maximum, value))


def _bool(config: Mapping[str, Any], key: str, default: bool) -> bool:
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    logger.warning("config %s is not a boolean; using %s", key, default)
    return default


def _str(config: Mapping[str, Any], key: str, default: str = "") -> str:
    value = config.get(key, default)
    return str(value).strip() if value is not None else default


def _float(config: Mapping[str, Any], key: str, default: float,
           minimum: float, maximum: float) -> float:
    try:
        value = float(config.get(key, default))
    except (TypeError, ValueError):
        logger.warning("config %s is not a number; using %s", key, default)
        return default
    return max(minimum, min(maximum, value))


def _list(config: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = config.get(key, [])
    if not isinstance(value, list):
        return ()
    return tuple(
        str(item).strip() for item in value
        if isinstance(item, (str, int, float)) and str(item).strip()
    )


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    embedding_provider_id: str
    embedding_model: str
    embedding_batch_size: int
    embedding_cache_mb: int
    embedding_cache_ttl_minutes: int
    embedding_timeout_seconds: int
    embedding_max_text_chars: int
    provider_failure_cooldown_seconds: int
    external_provider_opt_in: bool
    external_provider_allowlist: tuple[str, ...]
    provider_call_audit_retention_days: int
    rerank_provider_id: str
    rerank_model: str
    rerank_timeout_seconds: int
    rerank_enabled: bool
    vector_index_enabled: bool
    hybrid_retrieval_enabled: bool
    index_job_max_attempts: int
    index_worker_interval_seconds: int
    vector_candidate_limit: int
    hybrid_candidate_limit: int
    query_classifier_mode: str
    tag_candidate_limit: int
    time_candidate_limit: int
    vector_relevance_threshold: float
    fts_relevance_ratio: float
    vector_snapshot_cache_mb: int

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any]) -> "ProviderConfig":
        return cls(
            embedding_provider_id=_str(config, "embedding_provider_id"),
            embedding_model=_str(config, "embedding_model"),
            embedding_batch_size=_int(config, "embedding_batch_size", 50, 1, 256),
            embedding_cache_mb=_int(config, "embedding_cache_mb", 100, 1, 1024),
            embedding_cache_ttl_minutes=_int(
                config, "embedding_cache_ttl_minutes", 30, 1, 1440,
            ),
            embedding_timeout_seconds=_int(
                config, "embedding_timeout_seconds", 5, 1, 30,
            ),
            embedding_max_text_chars=_int(
                config, "embedding_max_text_chars", 1200, 200, 8000,
            ),
            provider_failure_cooldown_seconds=_int(
                config, "provider_failure_cooldown_seconds", 60, 0, 3600,
            ),
            external_provider_opt_in=_bool(
                config, "external_provider_opt_in", False,
            ),
            external_provider_allowlist=_list(
                config, "external_provider_allowlist",
            ),
            provider_call_audit_retention_days=_int(
                config, "provider_call_audit_retention_days", 90, 1, 3650,
            ),
            rerank_provider_id=_str(config, "rerank_provider_id"),
            rerank_model=_str(config, "rerank_model"),
            rerank_timeout_seconds=_int(
                config, "rerank_timeout_seconds", 5, 1, 30,
            ),
            rerank_enabled=_bool(config, "rerank_enabled", False),
            vector_index_enabled=_bool(config, "vector_index_enabled", False),
            hybrid_retrieval_enabled=_bool(
                config, "hybrid_retrieval_enabled", False,
            ),
            index_job_max_attempts=_int(
                config, "index_job_max_attempts", 5, 1, 100,
            ),
            index_worker_interval_seconds=_int(
                config, "index_worker_interval_seconds", 15, 1, 3600,
            ),
            vector_candidate_limit=_int(
                config, "vector_candidate_limit", 20, 1, 100,
            ),
            hybrid_candidate_limit=_int(
                config, "hybrid_candidate_limit", 24, 1, 100,
            ),
            query_classifier_mode=_str(
                config, "query_classifier_mode", "rules",
            ) or "rules",
            tag_candidate_limit=_int(
                config, "tag_candidate_limit", 10, 1, 100,
            ),
            time_candidate_limit=_int(
                config, "time_candidate_limit", 10, 1, 100,
            ),
            vector_relevance_threshold=_float(
                config, "vector_relevance_threshold", 0.35, 0.0, 1.0,
            ),
            fts_relevance_ratio=_float(
                config, "fts_relevance_ratio", 0.5, 0.0, 1.0,
            ),
            vector_snapshot_cache_mb=_int(
                config, "vector_snapshot_cache_mb", 64, 0, 1024,
            ),
        )
