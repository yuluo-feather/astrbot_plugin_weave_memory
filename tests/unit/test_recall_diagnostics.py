import json
from datetime import date

from data.plugins.astrbot_plugin_weave_memory.adapters.astrbot.recall_diagnostics import (
    JsonlRecallDiagnostics,
)


def test_jsonl_diagnostics_writes_one_redacted_record_and_prunes_old_files(tmp_path):
    directory = tmp_path / "diagnostics"
    old = directory / "recall-2026-11-01.jsonl"
    old.parent.mkdir(parents=True)
    old.write_text("{}\n", encoding="utf-8")
    writer = JsonlRecallDiagnostics(
        directory, retention_days=7, include_query=True,
    )
    writer.cleanup(today=date(2026, 11, 23))
    assert not old.exists()

    writer.write({
        "trace_id": "trace-1",
        "query_hash": "abc123",
        "query": "联系 me@example.com 或 13800138000，记住咖啡偏好",
    })
    path = next(directory.glob("recall-*.jsonl"))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["trace_id"] == "trace-1"
    assert "me@example.com" not in record["query"]
    assert "13800138000" not in record["query"]
    assert "[EMAIL]" in record["query"]
    assert "[PHONE]" in record["query"]


def test_jsonl_diagnostics_omits_query_by_default(tmp_path):
    writer = JsonlRecallDiagnostics(tmp_path, retention_days=7)
    writer.write({"trace_id": "trace-2", "query": "私密原文"})
    record = json.loads(next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8"))
    assert record == {"trace_id": "trace-2"}
