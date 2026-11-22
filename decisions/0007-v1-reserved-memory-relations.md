# ADR-0007：V1.0 预留 memory_relations 表

状态：Accepted（仅存储结构；不开放写入 API）
日期：2026-11-23

## 背景

V1.0 计划书 §4 明确要求保留 `memory_relations` 表，但 V1.0 不做图检索；计划书没有规定该表的列结构。

## 决策（实现选择，非计划书原文）

采用最小有向边结构：`id`、`source_memory_id`、`target_memory_id`、`relation_type`、`created_at`、`metadata_json`；两端均外键引用 memories，禁止自环，同一方向同类型关系唯一。为两端建立索引。

V1.0 不提供关系创建/修改/查询服务，不将关系用于召回。该表只是未来版本可迁移的结构占位。

## 删除语义

删除单条记忆时，在同一权威事务中删除所有入边和出边；清空 Scope 时删除任一端指向该 Scope 被清除记忆的关系。关系删除与 tombstone、正文/Revision/Source/Tag 清理同事务，防止悬空引用。

## 后果

- V1.0 数据模型与计划书表清单一致。
- V1.0 不提前实现 Graph/关系检索。
- 未来若要增加关系属性或关系审计，需要独立迁移，不把当前占位结构视为最终 Graph schema。
