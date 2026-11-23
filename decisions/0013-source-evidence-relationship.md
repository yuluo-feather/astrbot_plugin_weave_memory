# ADR-0013：来源证据由关系表承载，不设 `memories.source_id`

状态：Accepted（文档口径校正，依据羽忆侧 2026-11-23 实现审查 §2.19）
日期：2026-11-23

## 背景

V0/V1.0 计划书的 `memories` 字段列表含 `source_id`，总计划书 §5.1 也写「来源证据引用」。实现与 `docs/DATA_MODEL.md` 采用 `source_type` + `memory_sources` 关系表（1:N）：每次创建/更新追加一条来源证据，删除时随记忆级联清理；`memory_sources.evidence_ref` 表达「来源证据引用」。当前没有端口或消费者读取单独来源。

羽忆侧审查 §2.19 将其列为需裁定口径：补 `source_id` 列，还是把计划书对齐到关系表。

## 决策

1. `memories` 不设 `source_id` 列；`memories.source_type` 保留来源类型，来源证据明细由 `memory_sources(memory_id, ...)` 以 1:N 承载。
2. `memory_sources.evidence_ref` 是计划书中「来源证据引用」的物理落点。
3. 计划书与总计划书的 `source_id` 字段改述为「逻辑引用由 `memory_sources` 承载」；总计划书 `source_type` 枚举补 `AGENT`。
4. 为 `memory_sources.memory_id` 补索引，支撑来源审计查询；在下一次 schema 迁移中执行。

## 后果

- 一条记忆保留多次写入/更新的来源证据，不丢失历史。
- 不存在「`memories` 当前来源指针」与「最新 source 行」两处真源。
- 需要「最新来源」时按 `captured_at` 排序查询，而非读列。
- 已发布数据库不需要移除列；仅新增索引需要一次迁移。
- 第三方需要来源明细时使用 `memory_sources`，不以 `source_id` 为接口。
