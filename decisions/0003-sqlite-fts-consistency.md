# ADR-0003：V0.3 SQLite + FTS5 存储与索引一致性

- 状态：Accepted for V0.3 implementation
- 日期：2026-11-22
- 来源：V0「起针」计划书 §6；总计划书 §3–§5。

## 决策

1. SQLite 是 V0 唯一权威持久化存储；FTS5 是派生索引，不得成为正文权威来源。
2. 使用单进程、单连接、线程亲和模型；启用外键、WAL（文件数据库）、`synchronous=FULL` 与 5 秒 busy timeout。V0 不承诺多实例或跨线程并发写入。
3. Schema migration 采用递增版本号；V0.3 初始 schema 为版本 1，SQL 文件位于 `weavemem/adapters/sqlite/migrations/001_initial.sql`。DDL 与 migration marker 在同一事务执行；开发/测试可显式 rollback 到空 schema。
4. `memories`、`memory_revisions`、`memory_sources` 的初次创建必须在同一 SQLite 事务中完成；任一失败全部回滚。
5. 活动/候选去重使用计划书规定的 partial unique index，仅覆盖 `ACTIVE` 与 `CANDIDATE`。
6. 完全相同的创建请求（包括实体、初始 Revision、Source 的稳定 ID 与字段）重放视为幂等成功；同一 memory ID 对应不同内容则 Conflict；自然去重键冲突但 memory ID 不同也返回 Conflict。
7. 权威写入事务内将 memory ID 加入 `memory_meta.fts_dirty_ids`。随后由 FTS Adapter 在独立事务同步索引并移除该 dirty 标记。进程在两步之间退出时，dirty 标记保留。
8. 只要 `fts_dirty_ids` 非空，检索一律 fail-closed，返回 `IndexUnavailable`；全量 rebuild 在单事务中重建所有 ACTIVE 文档并清空 dirty 标记。
9. FTS5 只存储派生的检索文本。为支持中文/日文/韩文局部查询，索引前将 CJK 字符边界显式分词；原始正文与 `normalized_content` 不因此改变。
10. `creation_seq` 不在 V0.3 schema 落库；它属于 V1.8 ledger/clear 语义的预留能力，避免 V0 引入未使用的序列系统。

## 关键保证

- 正文/Revision/Source 同事务；索引失败不会回滚或丢失已提交正文。
- 索引失败不会被当成健康状态；未完成同步期间禁止检索。
- FTS 可从 `memories` 中所有 ACTIVE 记录重建。
- 删除/归档等状态更新先让权威表生效，再通过 dirty 标记确保索引同步；未同步时检索整体 fail-closed。

## 不在本 ADR 决定

- V0.4 WriteCoordinator 的异步/任务编排与清空线性化边界。
- 删除/清空的最终物理删除顺序、审计字段白名单与关闭语义。
- V0.5 AstrBot 的具体 API 版本和注入钩子。
