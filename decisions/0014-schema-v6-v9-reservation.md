# ADR-0014：V1.5–V1.8 schema 版本预留（v6–v9）

状态：Accepted（计划层冻结；实现时按本编号落迁移）
日期：2026-11-23

## 背景

当前 schema 为 v5（迁移 `001`–`005`）。V1.5–V1.8 计划分别新增表或列；若等到实现时再定编号，容易因实现顺序、回滚或并行开发产生跳号/复用。计划书已约定「具体编号在实现前由 ADR 冻结」，本 ADR 完成该冻结。

## 决策

| 版本 | schema | 迁移文件 | 内容 |
|---|---|---|---|
| V1.5 | v6 | `006_extraction_jobs.sql` | `extraction_jobs`；`memory_consents` 提取授权类型 |
| V1.6 | v7 | `007_candidate_expiry.sql` | `memories.candidate_expires_at` |
| V1.7 | v8 | `008_import_jobs.sql` | `import_jobs` |
| V1.8 | v9 | `009_lineage_ledger.sql` | `memories.db_lineage_id` / `creation_seq`、`memory_lineage_meta`、`memory_meta.db_lineage_id` |

约束：

- 每个迁移前自动备份并校验；备份失败中止迁移。
- 回滚守卫按各版本计划书；`v6→v5` 要求 `extraction_jobs` 为空或保留表停用，`v8→v7` 要求 `import_jobs` 为空或保留表停用。
- 实现顺序变化时，先修订本 ADR 与对应计划书，不得跳号、复用或插入中间版本。

## 后果

- 四个版本的 schema 编号固定，计划书与测试可以按编号断言。
- 未实现期间本 ADR 只约束计划；真正迁移仍需各自的 ADR/评审。
- 若 V1.5–V1.8 范围变化导致某版本不再需要 schema 变更，保留编号但该迁移为空文件，避免后续版本重排。
