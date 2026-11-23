# V1.4 Golden Set 基线评测报告

- 数据集：`weave-memory-v1.4-golden` v`1.2.0`
- SHA-256：`c01bbd16d2f1c8743d5c9bd3cecfa99d85ebdcd0a78cd1a34c30348013ce7bd0`
- Split：`all`；参数实验：`否`
- 运行时代码基线 commit：`9a036741772d7d30fd0c73085cf30a924bb15bdc`；工作区有未提交改动：`是`
- Python / SQLite：`3.12.10` / `3.49.1`
- 固定时间 / 随机种子：`2026-11-23T12:00:00+00:00` / `1401`

## 参数

- FTS ORDER BY：`bm25(memory_fts) ASC, COALESCE(m.last_accessed_at, m.created_at) DESC, CASE m.source_type WHEN 'EXPLICIT' THEN 0 ELSE 1 END ASC, m.memory_type ASC, m.created_at DESC, m.id ASC`
- Personal：max_memories=`4`，max_tokens=`384`，candidate_limit=`12`
- Group：max_memories=`2`，max_tokens=`192`，candidate_limit=`8`
- JSON 逐样本行包含 `candidate_diagnostics` / `omitted_diagnostics`，用于定位 false positive 与漏召回。

## Scope 指标

| Scope | 样本 | Candidate Recall@K | Candidate Precision@K | Candidate MRR | Injected Recall@K | Injected Precision@K | Injected MRR | 无记忆正确率 | 平均 Token | P50/P95 ms | 错误归属率 | 过期/删除召回率 | 跨 Scope 干扰命中 | 冲突共注入率 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| PERSONAL | 30 | 1.0000 | 0.0833 | 0.9750 | 1.0000 | 0.2500 | 0.9750 | 1.0000 | 89.4667 | 0.4060/0.8299 | 0.0000 | 0.0000 | 0 | 1.0000 |
| GROUP | 30 | 1.0000 | 0.1250 | 0.8968 | 0.9524 | 0.4762 | 0.8810 | 1.0000 | 76.6667 | 0.4756/1.0464 | 0.0000 | 0.0000 | 0 | 0.3333 |

## 分类统计

| 类别 | 样本 | Candidate Recall@K | Candidate Precision@K | Candidate MRR | 无记忆正确率 | 冲突共注入率 |
|---|---:|---:|---:|---:|---:|---:|
| exact_fact | 10 | 1.0000 | 0.1042 | 0.8833 | N/A | N/A |
| preference | 10 | 1.0000 | 0.1042 | 1.0000 | N/A | N/A |
| recent_topic | 10 | 1.0000 | 0.1042 | 1.0000 | N/A | N/A |
| cross_scope | 5 | 1.0000 | 0.1083 | 1.0000 | N/A | N/A |
| expired_deleted | 9 | N/A | N/A | N/A | 1.0000 | N/A |
| no_memory | 10 | N/A | N/A | N/A | 1.0000 | N/A |
| conflict_temporal | 6 | 1.0000 | 0.1042 | 0.7500 | N/A | 0.6667 |

## 注入有效性

人工 Rubric 尚未评分；需至少 12 条样本双人评分（0=无关、1=部分相关、2=直接帮助），不一致由第三人裁定。 模板：`eval/v1.4/datasets/injection_rubric_template.csv`；填充后用 `python scripts/injection_rubric.py` 校验。

## 失败样本分析

| Query | 期望 | 实际 | 原因分类 | 建议 |
|---|---|---|---|---|
| 测试报告 未执行项目 | ['p-pref-5'] | ['p-pref-5', 'p-fact-1', 'p-fact-5', 'p-conf-new-3', 'p-conf-old-3', 'p-cross-target-1', 'p-cross-target-2', 'p-rec-1'] | false_positive_candidates_shared_OR_terms | 将共享泛词命中列为后续 V2 候选排序/语义检索实验；V1.4 不改运行时。 |
| 我的测试机器人现在叫什么 | ['p-conf-new-3'] | ['p-fact-5', 'p-conf-new-3', 'p-conf-old-3', 'p-conf-new-2', 'p-pref-5', 'p-cross-target-1', 'p-cross-target-2', 'p-rec-1'] | false_positive_candidates_shared_OR_terms | 将共享泛词命中列为后续 V2 候选排序/语义检索实验；V1.4 不改运行时。 |
| 最近更新 值班交接清单 | ['g-rec-5'] | ['g-rec-5', 'g-fact-3', 'g-conf-new-2', 'g-conf-old-2', 'g-rec-3', 'g-rec-2', 'g-rec-4', 'g-rec-1'] | false_positive_candidates_shared_OR_terms | 将共享泛词命中列为后续 V2 候选排序/语义检索实验；V1.4 不改运行时。 |
| 最近安排 灰度发布演练 | ['g-rec-3'] | ['g-rec-3', 'g-fact-1', 'g-conf-old-3', 'g-fact-2', 'g-conf-new-3', 'g-rec-5', 'g-rec-2', 'g-rec-4'] | false_positive_candidates_shared_OR_terms | 将共享泛词命中列为后续 V2 候选排序/语义检索实验；V1.4 不改运行时。 |
| 本群例会 周三 15:00 | ['g-fact-1'] | ['g-conf-old-1', 'g-conf-new-1', 'g-fact-1', 'g-fact-3', 'g-fact-5', 'g-pref-2', 'g-conf-new-2', 'g-cross-target-1'] | not_injected,false_positive_candidates_shared_OR_terms | 将共享泛词命中列为后续 V2 候选排序/语义检索实验；V1.4 不改运行时。 |
| 本群发布窗口现在哪一天 | ['g-conf-new-3'] | ['g-fact-2', 'g-conf-new-3', 'g-conf-old-3', 'g-rec-3', 'g-fact-3', 'g-fact-5', 'g-conf-new-1', 'g-conf-new-2'] | false_positive_candidates_shared_OR_terms | 将共享泛词命中列为后续 V2 候选排序/语义检索实验；V1.4 不改运行时。 |
| 本群值班负责人现在是谁 | ['g-conf-new-2'] | ['g-fact-3', 'g-conf-new-2', 'g-conf-old-2', 'g-rec-5', 'g-fact-5', 'g-conf-new-1', 'g-conf-old-1', 'g-pref-2'] | false_positive_candidates_shared_OR_terms | 将共享泛词命中列为后续 V2 候选排序/语义检索实验；V1.4 不改运行时。 |
| 本群例会现在什么时候 | ['g-conf-new-1'] | ['g-conf-new-1', 'g-conf-old-1', 'g-fact-1', 'g-fact-3', 'g-fact-5', 'g-conf-new-2', 'g-pref-2', 'g-rec-3'] | false_positive_candidates_shared_OR_terms | 将共享泛词命中列为后续 V2 候选排序/语义检索实验；V1.4 不改运行时。 |

## 口径与限制

- Golden Set labels are assistant-generated drafts and require human review before V1.4 exit acceptance.
- Injection effectiveness requires human rubric scoring; no score is fabricated by this harness.
- Baseline covers all declared splits for descriptive reporting; the holdout split is never used for parameter selection, so future experiments must use train only.
- Baseline uses the current V1.3 SQLite FTS runtime services and does not alter runtime defaults.
- Cross-scope samples include an in-scope target and a foreign same-term distractor; target retrieval contributes to normal metrics and foreign hits are reported separately.
- Conflict-temporal samples pair an old and a new ACTIVE memory in the same scope; conflict_co_injection_rate is diagnostic only and has no pass threshold in V1.4.
- 空期望集（`no_memory`、`expired_deleted`）的 Recall@K / Precision@K / MRR 记 N/A，不进入非空均值；无记忆正确率只统计这两类。
- Cross-scope 样本同时包含目标 Scope 的期望记忆和另一 Scope 的同词干扰记忆；参与正常 Recall/Precision/MRR，并单独统计 foreign distractor 命中。
- 检索延迟：每条样本 warmup 2 次、测量 30 次，统计进程内 Recall 调用，不含 Provider 网络。
- 参数实验只允许使用 train split；holdout 永不参与参数选择。
