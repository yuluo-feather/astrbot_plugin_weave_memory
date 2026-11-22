# DATA_MODEL — V1.0 执行契约

状态：已按用户确认的既有审查结果作为 V0 执行基线；V0.2 决策见 ADR-0002  
依据：V0「起针」计划书 §4.1–§4.3、§6；总计划书 §5。

## 1. 身份层级

```text
Platform → Tenant → Scope → Subject
```

- `Platform`：V1 固定 AstrBot；身份字段保留来源平台。
- `Tenant`：平台内隔离单元；V1 默认单租户。
- `Scope`：可见性边界；V0 仅 Personal，枚举预留 `group`。
- `Subject`：记忆描述的对象；不是授权主体。
- `scope_id` 不得作为用户 ID；`subject_id` 不得作为权限主体。

身份键至少包含：`platform + tenant_id + scope_type + scope_external_id + subject_type + subject_id`。

## 2. Memory Entity

V0 领域实体至少表达以下信息（字段名/可空性需与 CONTRACTS 一并冻结）：

| 字段 | 语义 |
|---|---|
| `id` | 记忆唯一标识 |
| `scope_id` | 可见性边界标识 |
| `subject_id` | 记忆所描述的主体 |
| `memory_type` | `PREFERENCE / FACT / EVENT`（V0 冻结范围） |
| `content` | 记忆正文 |
| `normalized_content` | 按统一归一化规则生成并持久化的去重内容 |
| `status` | 生命周期状态 |
| `source_type` | 来源类型；证据记录在 `memory_sources` |
| `valid_from` / `valid_until` | 语义有效期 |
| `current_revision` | 乐观锁版本 |
| `superseded_by` | 替代该记忆的新记忆 ID |
| `created_at` / `updated_at` / `last_accessed_at` | 时间字段 |
| `metadata_json` | 扩展元数据 |

V0 的 `memory_type` 仅使用 `PREFERENCE`、`FACT`、`EVENT`。总计划书中的 `DECISION / RELATION / PLAN / PROFILE` 不在 V0 实现范围。

## 3. Source 与 operator

来源类型按 V0 计划书冻结候选枚举：

`EXPLICIT / INFERRED / OBSERVED / IMPORTED / MANUAL / AGENT`

另记录 `operator_type`：`user / agent / admin / system`。`EXPLICIT` 与 `INFERRED` 不得混淆。V0 仅支持显式保存，不启用自动提取；`INFERRED` 不得因写入而自动成为用户确认事实。

## 4. Revision

Revision 表达重要变更历史。V0 计划要求内容/状态/作用域/来源变化触发 revision；标点等非实质修改是否触发、字段快照格式及保留策略仍待明确。

## 5. Consent

`memory_consents` 计划字段：

- `id`
- `subject_id`
- `scope_id`
- `consent_type`
- `state`
- `source`
- `granted_at`
- `revoked_at`
- `metadata`

默认 fail-closed。V0 只涉及 Personal；群管理员来源规则为未来 Group 预留，不在 V0 实现管理员管理功能。

## 6. 生命周期状态

统一状态枚举：

`CANDIDATE / ACTIVE / SUPERSEDED / ARCHIVED / EXPIRED / DELETED / REJECTED`

`VALIDATED` 是写入管线阶段，不是持久状态。详细转换规则见 `LIFECYCLE.md`。

## 7. 去重

去重键：

`(scope_id, subject_id, memory_type, normalized_content)`

唯一索引只覆盖 `ACTIVE` 与 `CANDIDATE`。`ARCHIVED / EXPIRED / DELETED / REJECTED` 不阻止同内容重新创建。SQLite partial unique index 是计划指定实现。

归一化候选步骤包括空白折叠、句末标点处理、Unicode NFKC 与大小写策略；其中标点处理范围、大小写策略、语言边界必须在实现前冻结。

## 8. V1.8 预留字段

`creation_seq` 由主库独立单调计数器分配，与 ledger `seq` 分离。V0 可确定是否预留字段，但不得在 V0 实现 ledger/备份恢复系统；字段是否提前落库需 ADR 决定。

## 9. V0.2 已采用的基础规则（ADR-0002）

- 时间对象必须带时区，领域入口统一转换为 UTC；拒绝 naive datetime。
- ID 在 Domain 中是不透明非空字符串；由 `IdGenerator` 端口生成，不解析其格式。
- `normalized_content` v1：Unicode NFKC、空白折叠、`casefold()`、去除末尾句末标点 `.。!?！？;；:：…．｡`；原始正文不变。
- V0 仅允许 Personal Scope；其他 ScopeType 仅保留枚举。

## 10. SQLite 持久化映射（V0.3）

- 权威表：`memories`、`memory_revisions`、`memory_sources`、`memory_scopes`、`memory_tags`、`memory_operations`、`memory_meta`、`schema_migrations`。
- FTS5 `memory_fts` 为派生索引；`memory_meta.fts_dirty_ids` 持久记录尚未同步的 memory ID。
- `creation_seq` 暂不落库，保留给 V1.8 的独立序列语义。

## 11. 后续阶段 OPEN 决策

- ID 类型/生成策略与是否按实体类型分区。
- 时间统一表示、时区来源、精度、无时区输入处理。
- `content` 长度/空值/控制字符约束。
- `normalized_content` 详细算法及版本化策略。
- Revision 的字段快照、触发条件细节和保留策略。
- Consent 类型和值域、撤销后的既有记忆行为。
- JSON metadata 的 schema/version 与允许字段边界。


## 12. V1.0 执行补充

- `valid_from` 与 `valid_until` 在 FTS 候选查询前过滤；边界为 `valid_from <= now < valid_until`。
- 显式保存幂等键沿用 `(scope_id, subject_id, memory_type, normalized_content)`，只对 ACTIVE 精确重复返回既有 ID。
- 显式更新按 `memory_id + expected_revision` 定位；正文变化生成新 Revision 与 Source 记录。
- 当前模型没有独立“属性键”，不得把同一 `MemoryType` 下所有不同文本推断为同一事实并自动互相替代。


### V1.0 访问时间语义

`last_accessed_at` 记录实际进入注入上下文或被用户主动查询返回的时间；仅进入候选集但因预算未选中的记忆不更新该字段。访问时间属于派生使用元数据，不增加内容 Revision。排序初始规则见 `CONTRACTS.md`，权重校准留待 V1.4 评测基线。


### V1.0 `memory_relations` 预留结构

V1.0 增加最小有向边表：`id`、`source_memory_id`、`target_memory_id`、`relation_type`、`created_at`、`metadata_json`。两端引用 `memories`，禁止自环，按 source/target 建索引；不实现关系 API/Graph Retrieval。列结构是 ADR-0007 的实现选择，因为计划书只要求保留表，未指定字段。
