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

V0/V1 计划书字段列表中的 `source_id` 未落为 `memories` 列；当前模型用 `source_type` + `memory_sources` 关系表表达来源证据，避免维护指向“当前 source”的冗余外键。

V0 的 `memory_type` 仅使用 `PREFERENCE`、`FACT`、`EVENT`。总计划书中的 `DECISION / RELATION / PLAN / PROFILE` 不在 V0 实现范围。

## 3. Source 与 operator

来源类型按 V0 计划书冻结候选枚举：

`EXPLICIT / INFERRED / OBSERVED / IMPORTED / MANUAL / AGENT`

另记录 `operator_type`：`user / agent / admin / system`。`EXPLICIT` 与 `INFERRED` 不得混淆。V0 仅支持显式保存，不启用自动提取；`INFERRED` 不得因写入而自动成为用户确认事实。

## 4. Revision

Revision 表达重要变更历史。V0 计划要求内容/状态/作用域/来源变化触发 revision；标点等非实质修改是否触发、字段快照格式及保留策略仍待明确。

V1.0 执行口径：显式更新即使归一化内容不变也新增 revision；标点级合并未实现，留待 V1.3 评估。当前模型没有独立属性键，不能把同一 `MemoryType` 下的不同正文自动互替（ADR-0006）。

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
- 查重按 ACTIVE 状态匹配同键，不受 `valid_from/valid_until` 影响；显式保存前先把同键、已过 `valid_until` 的 ACTIVE 记忆转 `EXPIRED`，再查重，避免过期记录占用唯一索引。未来生效的 ACTIVE 同键记录仍视为已存在。
- 显式保存幂等键沿用 `(scope_id, subject_id, memory_type, normalized_content)`，只对 ACTIVE 精确重复返回既有 ID。
- 显式更新按 `memory_id + expected_revision` 定位；正文变化生成新 Revision 与 Source 记录。
- 当前模型没有独立“属性键”，不得把同一 `MemoryType` 下所有不同文本推断为同一事实并自动互相替代。


### V1.0 访问时间语义

`last_accessed_at` 记录实际进入注入上下文或被用户主动查询返回的时间；仅进入候选集但因预算未选中的记忆不更新该字段。访问时间属于派生使用元数据，不增加内容 Revision。排序初始规则见 `CONTRACTS.md`，权重校准留待 V1.4 评测基线。


### V1.0 `memory_relations` 预留结构

V1.0 增加最小有向边表：`id`、`source_memory_id`、`target_memory_id`、`relation_type`、`created_at`、`metadata_json`。两端引用 `memories`，禁止自环，按 source/target 建索引；不实现关系 API/Graph Retrieval。列结构是 ADR-0007 的实现选择，因为计划书只要求保留表，未指定字段。

V1.0 该表没有 `scope_id` 列，也没有应用层写入/查询 API；跨 Scope 边不影响检索。`memory_operations` 也没有 `result` 字段，V1.8 ledger 审计再引入。

### V1.0 宿主入口边界

- `/织忆 保存` 固定写入 `FACT`；`PREFERENCE`/`EVENT` 在领域层可用，但 V1.0 没有宿主命令入口。
- `memory_tags` 表存在但没有写入 API；标签检索留给后续版本。
- `valid_from`/`valid_until` 已参与检索过滤，但没有命令设置有效期；到期 ACTIVE 记忆会在下次同键显式保存前转 `EXPIRED`。

### V1.0 明确 deferred

- 候选 `expires_at`/workflow TTL 不持久化；V1.0 无 candidate 路径，留到 V1.5/V1.6。V1.0 没有 candidate 写入路径；CANDIDATE 与 ACTIVE 同键仍受唯一索引约束，候选功能在 V1.5/V1.6 设计时处理。
- 群管理员授权来源规则随 Group shared 在 V1.1 实现。
- `REJECTED` 的正文保留/清除策略随候选管理在 V1.6 实现。
- `memory_operations.result` 与 `memory_relations.scope_id` 留到 V1.8/V2 的审计与关系检索。

## 13. V1.1 Group shared 数据模型（M0 契约）

- 群身份使用六元组：`platform + tenant_id + GROUP + group_id + GROUP + group_id`；`scope_external_id` 与 `subject_id` 都是 group_id，不是字面量 `group`。
- `scope_id` 用完整六元组的 UUID5，序列化格式与 Personal `stable_scope_id` 一致；不做 `platform:group_id` 拼接。
- v5 `memory_scopes` 新增：
  - `scope_type CHECK IN ('PERSONAL','GROUP')`；
  - `subject_type CHECK IN ('USER','GROUP')`；
  - `admin_ids_json TEXT NOT NULL DEFAULT '[]'`；
  - `admin_source TEXT NOT NULL DEFAULT 'NONE' CHECK IN ('PLATFORM_ROLE','EXPLICIT_CONFIG','NONE')`；
  - 保留 `is_enabled/disabled_at` 与现有个人行。
- 群记忆仍写 `memories`：`scope_id` 为群 Scope；`memory_scopes.subject_type=GROUP`，`memories.subject_id=group_id`；`memories` 本身没有 `subject_type` 列，不写成员个人 subject。
- 类型映射：群话题 `FACT`；群约定/群氛围 `PREFERENCE`；共同经历 `EVENT`；metadata 固定键 `group_label`（`topic/agreement/atmosphere/experience`）、`group_id`、`admin_source`。
- 群操作审计使用 `GROUP_ENABLE`、`GROUP_DISABLE`、`GROUP_DESTROY`；`GROUP_DESTROY` 后群 Scope/群 consent/群审计删除，tombstone 保留在销毁前备份。
- 唯一索引沿用 `(scope_id, subject_id, memory_type, normalized_content)`，群/个人不互相覆盖。
- `group_memory` 复用 `memory_consents`，默认无记录拒绝；与 `personal_memory` 分离。
- FTS/读取除 `scope_id/subject_id` 外再做 `scope_type=GROUP` 防御过滤。
- 群解散/退群不自动删个人记忆；成员个人删除/退群在 V2.4。
- v5 迁移与 v4 回滚见 ADR-0011；有群数据时拒绝回滚，无群数据可回退并再次迁移。

## 14. V1.2 Personal 默认登记与 AGENT 来源

- 不新增 schema 版本；沿用 v5。`memory_consents` 追加 `GRANTED` 事件时使用 `source='auto_enroll'`，`metadata_json` 记录 `operator_id` 与 `auto_enrolled=true`。
- 已有 `GRANTED`/`REVOKED` 历史时不追加 auto_enroll 事件；`/织忆 撤销授权` 与 `/织忆 关闭` 的拒绝状态持久。
- 模型工具写入使用既有 `SourceType.AGENT` 与 `OperatorType.AGENT`；`memories.source_type=AGENT`，不引入新列。
- `MemoryApplicationService.save_agent` 与显式保存共用 `(scope_id, subject_id, memory_type, normalized_content)` 去重键、Revision/Source 和 WriteCoordinator guard。
- 后台自动提取仍使用 `SourceType.INFERRED` 与 `CANDIDATE`；不在 V1.2 写入 ACTIVE。

## 15. V1.5 自动提取数据模型（M0 契约）

依据：V1.5 计划书 §7；ADR-0015；ADR-0014。

### Consent

- `memory_consents.consent_type` 新增：
  - `extraction_capture`
  - `extraction_auto_extract`
  - `extraction_external_model`
  - `extraction_store_candidate`
  - `extraction_sensitive_categories`
- 事件 append-only；按 `(subject_id, scope_id, consent_type)` 最后一条为准。
- `extraction_sensitive_categories` 在白名单事件中写 `metadata_json.categories: []`；空列表表示不保存敏感类别。
- `auto_enroll` 只写 `personal_memory`，不写以上类型。

### extraction_jobs（schema v6）

| 列 | 类型/约束 | 说明 |
|---|---|---|
| `id` | TEXT PRIMARY KEY | 任务 ID |
| `platform` | TEXT NOT NULL | 平台 |
| `tenant_id` | TEXT NOT NULL | 平台实例/租户 |
| `scope_id` | TEXT NOT NULL | 只允许 Personal scope |
| `subject_id` | TEXT NOT NULL | 用户主体 |
| `conversation_id` | TEXT NOT NULL | 会话标识 |
| `watermark` | INTEGER NOT NULL | 窗口水位线 |
| `content_hash` | TEXT NOT NULL | 窗口内容指纹 |
| `status` | TEXT NOT NULL CHECK | 六态之一 |
| `attempts` | INTEGER NOT NULL DEFAULT 0 | 重试次数 |
| `last_error` | TEXT | 类型化错误摘要，不含正文 |
| `created_at` / `updated_at` | TEXT NOT NULL | UTC ISO-8601 |
| `metadata_json` | TEXT NOT NULL DEFAULT '{}' | 任务扩展信息 |

- 唯一索引：`(platform, tenant_id, scope_id, conversation_id, watermark, content_hash)`。
- 按 `status, created_at` 建索引，便于恢复与积压统计。
- 任务不保存完整对话原文；`metadata_json` 不得写正文。

### 候选

- 提取写入 `memories`：`status=CANDIDATE`、`source_type=INFERRED`，类型限 `PREFERENCE/FACT/EVENT`。
- 去重键沿用 `(scope_id, subject_id, memory_type, normalized_content)`；`ACTIVE` 与 `CANDIDATE` 互斥唯一索引不变。
- 同键 `ACTIVE` 存在时不创建候选；同键 `CANDIDATE` 存在时幂等更新。
- 候选来源证据写 `memory_sources`，`source_type=INFERRED`，`operator_type=system`；不得写成 `EXPLICIT/AGENT`。
- 候选不进入 FTS；确认后转为 `ACTIVE` 并进入索引。
- 候选过期转 `ARCHIVED`，不物理删除；`candidate_expires_at` 列与 TTL 在 V1.6。

### 迁移与回滚

- 迁移 `006_extraction_jobs.sql` 只新增 `extraction_jobs`；`memory_consents` 的 `consent_type` 无 CHECK 约束扩展，沿用现有表。
- 迁移前自动备份并校验，失败中止。
- v6→v5 回滚守卫：`extraction_jobs` 为空，或保留表停用、不删除数据。

