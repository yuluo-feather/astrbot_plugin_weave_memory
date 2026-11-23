"""User-facing summaries for V1.7 import results."""

from __future__ import annotations

from ...app.import_service import ImportOutcome, ImportResult

_ISSUES = (ImportOutcome.FAILED, ImportOutcome.CONFLICT)
_MAX_ISSUES = 10


def format_import_result(result: ImportResult, *, scope_label: str) -> str:
    headline = "试运行（未写入）" if result.dry_run else "导入完成"
    lines = [
        f"{headline}：{scope_label}共 {result.total} 条；新建 {result.imported}，"
        f"仅元数据 {result.metadata_only}，跳过 {result.skipped}，"
        f"冲突 {result.conflicts}，失败 {result.failed}。",
    ]
    if result.resumed_from:
        lines.append(f"从第 {result.resumed_from + 1} 条继续。")
    if result.note:
        lines.append(result.note)
    if result.job_id and not result.dry_run:
        lines.append(f"任务：{result.job_id}（/织忆 导入 回滚 {result.job_id}）")
    issues = [entry for entry in result.entries if entry.outcome in _ISSUES]
    for entry in issues[:_MAX_ISSUES]:
        label = entry.source_memory_id or "?"
        lines.append(f"#{entry.index + 1} {label}：{entry.outcome.value} {entry.reason}".strip())
    if len(issues) > _MAX_ISSUES:
        lines.append(f"另有 {len(issues) - _MAX_ISSUES} 条问题未显示。")
    return "\n".join(lines)


def format_rollback_result(result: ImportResult, *, scope_label: str) -> str:
    return (
        f"已回滚{scope_label}导入任务 {result.job_id}："
        f"删除 {result.imported} 条，未找到 {result.failed} 条。{result.note}"
    )
