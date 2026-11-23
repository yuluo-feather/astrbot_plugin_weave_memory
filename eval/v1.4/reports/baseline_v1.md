# V1.4 Golden Set 基线评测报告

- 数据集：`weave-memory-v1.4-golden` v`1.0.0`
- SHA-256：`1601b8c27989f238a2f4e95e49a1e250534ad6d5636a12935f4ca3ae6479894c`
- Split：`all`；参数实验：`否`
- 运行时代码基线 commit：`cc3ac42d91eb92d1597cdbdd3a364230445db05d`；工作区有未提交改动：`否`
- Python / SQLite：`3.12.10` / `3.49.1`
- 固定时间 / 随机种子：`2026-11-23T12:00:00+00:00` / `1401`

## 参数

- FTS ORDER BY：`bm25(memory_fts) ASC, COALESCE(m.last_accessed_at, m.created_at) DESC, CASE m.source_type WHEN 'EXPLICIT' THEN 0 ELSE 1 END ASC, m.memory_type ASC, m.created_at DESC, m.id ASC`
- Personal：max_memories=`4`，max_tokens=`384`，candidate_limit=`12`
- Group：max_memories=`2`，max_tokens=`192`，candidate_limit=`8`
- JSON 逐样本行包含 `candidate_diagnostics` / `omitted_diagnostics`，用于定位 false positive 与漏召回。

## Scope 指标

| Scope | 样本 | Candidate Recall@K | Candidate Precision@K | Candidate MRR | Injected Recall@K | Injected Precision@K | Injected MRR | 无记忆正确率 | 平均 Token | P50/P95 ms | 错误归属率 | 过期/删除召回率 | 跨 Scope 干扰命中 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| PERSONAL | 25 | 1.0000 | 0.0833 | 1.0000 | 1.0000 | 0.2500 | 1.0000 | 1.0000 | 75.9200 | 0.3583/0.7390 | 0.0000 | 0.0000 | 0 |
| GROUP | 25 | 1.0000 | 0.1250 | 1.0000 | 1.0000 | 0.5000 | 1.0000 | 1.0000 | 69.8800 | 0.4033/0.7326 | 0.0000 | 0.0000 | 0 |

## 分类统计

| 类别 | 样本 | Candidate Recall@K | Candidate Precision@K | Candidate MRR | 无记忆正确率 |
|---|---:|---:|---:|---:|---:|
| exact_fact | 10 | 1.0000 | 0.1042 | 1.0000 | N/A |
| preference | 10 | 1.0000 | 0.1042 | 1.0000 | N/A |
| recent_topic | 10 | 1.0000 | 0.1042 | 1.0000 | N/A |
| cross_scope | 5 | 1.0000 | 0.1083 | 1.0000 | N/A |
| expired_deleted | 5 | N/A | N/A | N/A | 1.0000 |
| no_memory | 10 | N/A | N/A | N/A | 1.0000 |

## 注入有效性

人工 Rubric 尚未评分；需至少 10 条样本双人评分（0=无关、1=部分相关、2=直接帮助），不一致由第三人裁定。

## 失败样本分析

| Query | 期望 | 实际 | 原因分类 | 建议 |
|---|---|---|---|---|
| 本群例会 周三 15:00 | ['g-fact-1'] | ['g-fact-1', 'g-fact-3', 'g-fact-5', 'g-pref-2', 'g-rec-5', 'g-cross-target-1', 'g-cross-target-2', 'g-cross-target-3'] | false_positive_candidates_shared_OR_terms | 将共享泛词命中列为后续 V2 候选排序/语义检索实验；V1.4 不改运行时。 |
| 最近安排 灰度发布演练 | ['g-rec-3'] | ['g-rec-3', 'g-fact-1', 'g-fact-2', 'g-rec-5', 'g-rec-2', 'g-rec-4', 'g-rec-1'] | false_positive_candidates_shared_OR_terms | 将共享泛词命中列为后续 V2 候选排序/语义检索实验；V1.4 不改运行时。 |
| 测试报告 未执行项目 | ['p-pref-5'] | ['p-pref-5', 'p-fact-1', 'p-fact-5', 'p-cross-target-1', 'p-cross-target-2', 'p-rec-1'] | false_positive_candidates_shared_OR_terms | 将共享泛词命中列为后续 V2 候选排序/语义检索实验；V1.4 不改运行时。 |
| 最近更新 值班交接清单 | ['g-rec-5'] | ['g-rec-5', 'g-fact-3', 'g-rec-3', 'g-rec-2', 'g-rec-4', 'g-rec-1'] | false_positive_candidates_shared_OR_terms | 将共享泛词命中列为后续 V2 候选排序/语义检索实验；V1.4 不改运行时。 |
| 最近整理 工具调用 审计链路 | ['p-rec-5'] | ['p-rec-5', 'p-rec-1', 'p-rec-4', 'p-rec-2', 'p-rec-3'] | false_positive_candidates_shared_OR_terms | 将共享泛词命中列为后续 V2 候选排序/语义检索实验；V1.4 不改运行时。 |
| 最近设计 离线 Golden Set | ['p-rec-4'] | ['p-rec-4', 'p-rec-2', 'p-rec-5', 'p-rec-3', 'p-rec-1'] | false_positive_candidates_shared_OR_terms | 将共享泛词命中列为后续 V2 候选排序/语义检索实验；V1.4 不改运行时。 |
| 最近准备 AstrBot 4.29.4 兼容性 | ['p-rec-3'] | ['p-rec-3', 'p-rec-4', 'p-rec-2', 'p-rec-5', 'p-rec-1'] | false_positive_candidates_shared_OR_terms | 将共享泛词命中列为后续 V2 候选排序/语义检索实验；V1.4 不改运行时。 |
| 最近评估 SQLite FTS 检索 | ['p-rec-2'] | ['p-rec-2', 'p-rec-4', 'p-rec-5', 'p-rec-3', 'p-rec-1'] | false_positive_candidates_shared_OR_terms | 将共享泛词命中列为后续 V2 候选排序/语义检索实验；V1.4 不改运行时。 |

## 口径与限制

- Golden Set labels are assistant-generated drafts and require human review before V1.4 exit acceptance.
- Injection effectiveness requires human rubric scoring; no score is fabricated by this harness.
- Baseline uses the current V1.3 SQLite FTS runtime services and does not alter runtime defaults.
- Cross-scope samples include an in-scope target and a foreign same-term distractor; target retrieval contributes to normal metrics and foreign hits are reported separately.
- 空期望集（`no_memory`、`expired_deleted`）的 Recall@K / Precision@K / MRR 记 N/A，不进入非空均值；无记忆正确率只统计这两类。
- Cross-scope 样本同时包含目标 Scope 的期望记忆和另一 Scope 的同词干扰记忆；参与正常 Recall/Precision/MRR，并单独统计 foreign distractor 命中。
- 检索延迟：每条样本 warmup 2 次、测量 30 次，统计进程内 Recall 调用，不含 Provider 网络。
- 参数实验只允许使用 train split；holdout 永不参与参数选择。
