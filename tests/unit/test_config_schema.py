import json
from pathlib import Path

SCHEMA_PATH = Path(__file__).parents[2] / "_conf_schema.json"
SUPPORTED_TYPES = {
    "int", "float", "bool", "string", "text", "list", "file",
    "object", "template_list", "dict",
}


def test_config_schema_uses_astrbot_supported_types():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    for key, spec in schema.items():
        assert spec["type"] in SUPPORTED_TYPES, key
        if spec["type"] == "object":
            assert "items" in spec, f"{key}: AstrBot object schema requires items"


def test_v2_defaults_are_safe_and_frozen():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    expected = {
        "hybrid_retrieval_enabled": False,
        "vector_index_enabled": False,
        "rerank_enabled": False,
        "query_classifier_mode": "rules",
        "vector_candidate_limit": 20,
        "hybrid_candidate_limit": 24,
        "tag_candidate_limit": 10,
        "time_candidate_limit": 10,
        "vector_relevance_threshold": 0.3,
        "fts_relevance_ratio": 0.5,
        "embedding_provider_id": "",
        "embedding_model": "",
        "embedding_batch_size": 50,
        "embedding_cache_mb": 100,
        "embedding_cache_ttl_minutes": 30,
        "embedding_timeout_seconds": 5,
        "embedding_max_text_chars": 1200,
        "external_provider_opt_in": False,
        "external_provider_allowlist": [],
        "provider_call_audit_retention_days": 90,
        "index_job_max_attempts": 5,
        "index_worker_interval_seconds": 15,
        "archive_retention_days": 30,
        "purge_batch_size": 100,
        "purge_interval_seconds": 3600,
    }
    for key, default in expected.items():
        assert key in schema, key
        assert schema[key]["default"] == default, key

