# TEST_PLAN — V1.0 执行测试计划

状态：已按用户确认的既有审查结果作为 V0 执行基线；V0.2 决策见 ADR-0002  
依据：V0「起针」计划书 §5–§9；总计划书 §7。

## 1. 测试层次

| 层次 | 范围 | 主要目标 |
|---|---|---|
| Unit | 纯 Domain / Policy | 不依赖 AstrBot、SQLite；验证状态、身份、去重、冲突 |
| Contract | Ports 与 Provider | MemoryStore / SearchIndex 行为一致 |
| Integration | SQLite + FTS + App | 事务、迁移、索引重建、并发、删除 |
| Host integration | AstrBot Adapter | 私聊身份、命令、注入、重载 |
| Fault injection | 写入/索引/事务/生命周期 | 失败不静默、不泄漏、不留下权威半状态 |

## 2. V0.2 Domain 单元测试

- 状态转换合法/非法路径。
- Scope 隔离；不同用户不得互见。
- Revision 递增与乐观锁冲突。
- 去重键一致性；显式更新与完全重复区分。
- Consent 默认拒绝与允许路径。
- 归一化相同输入得到相同结果；边界字符按冻结规则测试。

## 3. V0.3 SQLite / FTS 测试

- 空库迁移到当前 V0 schema。
- 重复迁移幂等；迁移版本记录正确。
- Memory、初始 Revision、Source 同事务提交/回滚。
- ACTIVE/CANDIDATE partial unique index 生效；其他状态不阻止重建。
- FTS 查询结果与权威表状态一致。
- FTS 可从权威正文重建。
- 正文写入成功、索引失败时正文不丢失且索引不被误认为健康。
- 数据库重开后数据持久化。

## 4. V0.4 并发与生命周期测试

- 并发新增相同去重键：最多一个互斥活动记录，不静默吞冲突。
- 同条并发更新：乐观锁阻止静默覆盖。
- 删除后 FTS/普通检索/注入均不可见。
- 清空只影响目标 Personal Scope。
- 事务中断/异常回滚不留下半完成权威数据。
- 生命周期非法转换被拒绝。
- 关闭/重载后连接、任务与锁资源释放。

## 5. V0.5 AstrBot 验收

- 用户 A 私聊显式保存一条记忆，后续对话可检索/注入。
- 用户 B 不可见 A 的记忆。
- 删除后不可检索/注入。
- 插件重载后数据仍在，后台任务/连接干净。
- 注入失败时聊天照常。
- 群聊不写入 Personal Scope。

## 6. 质量门槛

- 核心包导入测试证明无 AstrBot / sqlite3 依赖泄漏。
- 测试不以宽泛 catch/吞异常通过。
- 并发测试验证结果与不变量，不只验证无异常抛出。
- 所有失败路径检查权威数据、FTS、审计和资源状态。
- 每阶段报告记录命令、环境、通过/失败数及未覆盖项。

## 7. V0.3 已执行结果

- SQLite schema migration：幂等、版本记录、回滚到空 schema 后可重新迁移。
- Memory/Revision/Source：同事务提交；关联写入失败时权威记录全部回滚。
- Partial unique index：ACTIVE/CANDIDATE 互斥，ARCHIVED 等状态允许重新创建。
- FTS：CJK 局部检索、作用域过滤、dirty fail-closed、索引失败后正文保留、全量重建恢复。
- 完全相同 create 重放幂等；乐观锁冲突可检测。
- 当前本地测试：27 passed（Python 3.13）。

## 8. V0.4 已执行结果

- WriteCoordinator 并发重复新增串行化；乐观锁冲突明确返回。
- LifecycleService 限制候选激活/替代恢复；DELETED 只能走物理删除。
- 删除后正文/Revision/Source 清除，最小 tombstone audit 保留且不含正文。
- 清空只影响指定 Scope；关闭 Scope 停读写但保留正文。
- FTS 删除/清空故障后 fail-closed；启动恢复可重建缺失索引。
- 当前全量测试：44 passed（Python 3.13）。

## 9. V0.5 已执行结果

- Consent 默认拒绝、授权、撤销、重授权与插件重载持久化。
- AstrBot Identity Adapter 使用平台实例和发送者隔离；群聊/身份字段缺失 fail-closed。
- 私聊显式保存、列表、主动查询、物理删除、清空确认、Scope 关闭。
- LLM Hook 使用临时 TextPart；未授权、群聊、无 Scope 或 FTS 故障均不注入。
- 注入预算、CJK 自然问句召回、上下文转义与候选上限。
- 自包含插件 ZIP 结构和核心包导入验证。
- 当前全量测试：69 passed（Python 3.13）。

## 10. V1.0 宿主验证状态与 OPEN

已完成（2026-11-23，AstrBot v4.29.4 / Python 3.12.10）：V1.0 schema v4 迁移、显式保存/更新、旧 revision 冲突、真实新会话注入、主动查询、撤销授权、热重载持久化、测试用户 Scope 永久关闭、FTS dirty 故障降级。详见 `docs/HOST_SMOKE_V1_20261123.md`。

V0.5 宿主报告已覆盖第二测试用户只能读取自己的 Scope，V1.0 未改变 Identity/Scope 键模型；V1.0 新增的更新与异步注入另由本版宿主烟测覆盖。

仍未覆盖或不作通过声明：
- token 级 Provider 请求抓包，直接确认临时 TextPart 不写入持久会话历史；当前以新会话问答结果作为端到端注入证据。
- 强制杀进程/断电级 crash-recovery；当前覆盖 SQLite 事务异常回滚、FTS dirty marker 持久化与启动重建。
- V1.0 真实宿主执行清空命令；本轮未执行破坏性清空。
- `rollback_to_v05()` 在真实宿主执行；目前只在自动化 SQLite 测试中验证。
- 第二个 AstrBot 平台实例与插件卸载场景。


## 11. V1.0 增补测试要求

- 显式同内容重复保存返回同一 ID，且不新增 memory/revision/source；并发重复保存也只能产生一条。
- 显式更新成功时 revision 恰增 1，source 追加；旧 revision 更新必须返回 Conflict 且数据不变。
- 不同 Scope、Subject、MemoryType 不得互相命中幂等查重。
- 有效期边界必须在 FTS LIMIT 前过滤；过期/未来记忆不可召回或注入。
- 删除/关闭/撤销 Consent 后，检索与注入不得泄漏；注入故障不阻塞聊天。
- AstrBot 注入 Hook 隔离普通集成异常，但必须让 `asyncio.CancelledError` 继续传播，避免吞掉宿主取消信号。


### V1.0 检索排序与访问记录

- 相关性相同的候选按最近访问时间排序；无访问记录时回退到创建时间。
- 手动查询与成功注入会更新访问时间；预算未选中的候选不更新。
- 访问元数据更新不得创建 Revision，不得将 FTS 标记为 dirty。
- MemoryType 只作为确定性末级 tie-break；其语义权重需等 V1.4 golden set 评测，不在 V1.0 人为调参。


### V1.0 SQLite 迁移与关系清理

- schema v3 → v4 升级可重复执行；迁移失败不得留下半应用版本标记。
- `memory_relations` 外键拒绝悬空引用；删除单条记忆清理入边/出边。
- 清空一个 Personal Scope 时，清除所有指向该 Scope 被清除记忆的关系，但不得删除其他 Scope 的记忆。


### V1.0 关闭与 Consent 故障注入

- Scope 关闭后必须立即阻断读写/检索/注入。
- Consent REVOKED 追加失败时，Scope 仍关闭、记忆仍不可见；相同 operation ID 重试后补齐撤销事件。
- 重试不得重复写入关闭审计，不得重新启用 Scope。


### V1.0 M3 并发/故障专项增补

- 同一 memory 的两个并发显式更新只允许一个 expected_revision=旧版本的写入成功；另一个返回 Conflict。
- 模拟 FTS 候选命中后、权威实体读取前发生删除，Recall 必须丢弃 NotFound 候选。
- 模拟更新时 FTS 表缺失：权威更新提交、回执 PENDING_REBUILD；启动恢复重建后只召回新正文。
- 模拟 v3→v4 迁移执行到部分 DDL 后失败：DDL 与 schema version marker 必须整体回滚，数据库保持 v3 可重试状态。
- Coordinator close 等待持有中的写锁，关闭完成后排队写入返回 ServiceClosed。
- Scope 关闭后的 Consent 撤销失败保持 Scope disabled；相同 operation ID 重试补齐撤销。


### V1.0 回滚验证

- schema v4 且关系表为空时可回滚到 v3，随后可再次迁移到 v4。
- 关系表非空时回滚必须拒绝，schema 版本与关系数据保持不变。


### V1.0 Recall / Injection 授权竞态

- 模拟 FTS 候选检索后、访问元数据写入前发生 Consent revoke：Composer 必须拒绝返回上下文，且不得更新 `last_accessed_at`。
- 模拟候选检索后、访问元数据写入前发生 Scope disable：Composer 必须拒绝返回上下文。


### V1.0 写入 Consent 竞态

- 模拟显式保存入口检查通过后、Coordinator 获得写锁前撤销 Consent：不得创建 Memory/Revision/Source。
- 模拟显式更新入口检查通过后、Coordinator 获得写锁前撤销 Consent：旧正文与 revision 必须保持不变。


### V1.0 Scope 关闭错误反馈

- 模拟 Scope 已关闭但 Consent REVOKED 事件追加失败：命令必须明确告知“Scope 已关闭、读写已停止、授权撤销待重试”，不能误报为 Scope 未关闭。
- 重试同一关闭命令后应补齐 Consent 撤销，且不重复关闭审计。


## 12. V1.0 M3 进程终止专项（2026-11-23）
- 未提交 WAL 写事务中强制终止子进程：重启后未提交行回滚，既有数据保留。
- COMMIT 后立即异常退出：已提交数据保留。
- v3→v4 DDL 与 marker 写入中途强制终止：DDL/marker 一起回滚，重启后可重试迁移。
- 已提交 FTS dirty marker 与索引差异后进程退出：启动恢复从权威表重建 FTS。
- 当前全量测试：283 passed（V1.0 P3 补测后 132；V1.1 M1/M2 实现后 143；迁移备份补测后 146；V1.2 默认登记/agent 工具后 153；导出/审计补测后 157；schema 校验补测后 158；审查裁定与 P1/P2/P3 修复后 174；V1.3/V1.4 修复后 203–213；V1.5 M1 与计划书对齐后 247–258；V1.6 候选 TTL/管理 API 后 283）。
- 限制：不代表物理断电、存储介质损坏或多进程并发恢复已验证。详见 `V1.0_M3_CRASH_RECOVERY_REPORT_20261123.md`。

### V1.2 Personal 默认登记与 agent 工具（2026-11-23）

- `auto_enroll=true` 时无 consent 记录的用户首次私聊自动 GRANTED，只发一次告知；第二次请求不再发送。
- `auto_enroll=false` 时注入不创建 consent；显式 `/织忆 授权` 后恢复保存/检索/注入。
- 已有 `REVOKED` 时 auto_enroll 不改写；撤销后注入与 agent 工具均拒绝。
- agent 工具写入 `source_type=AGENT`；撤销/关闭后拒绝，群聊拒绝；`agent_write_enabled` 默认 true，另有用例显式关闭验证拒绝。
- 个人导出只含当前 scope 的 ACTIVE 记忆；删除后不出现；撤销后拒绝。
- 群导出需管理员；disabled/跨群拒绝；只导当前群。
- `memory_operations` 记录 `result=SUCCESS` 且不含正文；删除/清空/关闭后注入不复活。
- 群共享记忆的开启/写入/读取语义不变；个人与群双向隔离保持不变。

### 迁移备份（2026-11-23）

- v4→v5 迁移前生成 `migration_backups/` 下的一致性备份；备份内 schema 版本为迁移前版本，且保留迁移前正文。
- 备份失败或校验失败时中止迁移，schema 与原数据保持不变。
- 全新库与已是 v5 的库不生成迁移备份。

### V1.0 删除/清空物理擦除（ADR-0008）

- SQLite 连接默认 `PRAGMA secure_delete = 1`。
- 单条删除与 Scope 清空后，**连接未关闭时**数据库主文件及 WAL 均不含被删正文 canary；关闭后再复核一次。
- 第二个连接持有 WAL 读快照时，`wal_checkpoint(TRUNCATE)` 返回 busy：delete 与 clear 都返回 `purged=False`、pending 标记保持 `1`；释放快照后重试成功并清除。
- 非 WAL（`journal_mode=DELETE`）下 checkpoint 返回 `log < 0`：仍执行 `secure_delete` + VACUUM 并标记完成。
- 清空一个 Scope 后 VACUUM 不得删除其他 Scope 的 live 记忆。
- 过期重复扫描期间旧记忆被 delete/clear：显式保存不得以 NotFound 失败，应继续创建新记忆。
- FTS 同步/重建失败时不执行 VACUUM，`secure_purge_pending` 标记保留；启动恢复重建 FTS 后重试并清除。
- 补丁前 `secure_delete=OFF` 生成的旧库：启动时 baseline 缺失触发一次全库擦除，旧 canary 消失。
- 命令层在物理擦除未完成时提示下次插件启动自动重试，不把逻辑删除误报为失败。
- 限制：不覆盖存在外部读快照/备份工具的并发窗口，也不覆盖 SSD 磨损均衡、OS 缓存、文件系统空闲块或备份/快照层面的取证级擦除。

### V1.0 P3 补测（2026-11-23）

已补：
- baseline 启动擦除：reader busy 时保留 baseline 缺失并抛出 `StorageError`，释放后重试；checkpoint 失败与第二次 checkpoint busy 都不写 `baseline_done`。
- 非 WAL：delete、clear、baseline 三条路径在 `journal_mode=DELETE` 下仍标记完成并清除 canary。
- clear FTS dirty：`PENDING_REBUILD`、`purged=False`、pending 保留；启动恢复重建后重试清除。
- 多 Scope 清空：其他 Scope 的 `memories`、FTS、revisions、sources、tags 均在 VACUUM 后保留。
- 命令层 `purged=False`：delete/clear 文案提示“物理擦除未完成，将在下次插件启动时自动重试”。
- `:memory:`：写入完成标记；`secure_purge_if_pending()` 因无文件返回 False。
- `write_metadata`：可控阻塞 fake store 验证元数据写等待在途内容写完成；关闭后仍返回 `ServiceClosed`。
- 过期重复：delete/clear/revoke 发生在 expire 前时，save 继续查重或明确失败；并发同键 save 只产生一个 ACTIVE。
- Consent：多事件后最新状态由 `ConsentService.require` 的完整 history 路径生效。
- CANDIDATE 同键：与 ACTIVE 的唯一索引冲突为预期，V1.0 无 candidate 写入路径。

仍未覆盖：
- 非 WAL 与 FTS dirty/reader 的组合没有单独参数化；非 WAL 路径与 FTS dirty 路径分别有独立回归。
- update/disable 与过期扫描的竞态未稳定构造；delete/clear/revoke 已覆盖。
- 命令层真实 I/O 失败的 `purged=False` 未抓包，文案通过 fake receipt 验证。
- PERSIST/TRUNCATE 等 journal 模式的 rollback journal 文件不在“主库 + WAL”保证内。
- 外部读快照/备份工具的长窗口、SSD/OS/文件系统/备份层面的取证级擦除仍不承诺。

### V1.0 命令前缀（2026-11-23）

- 命令组注册名为 `织忆`，alias 仅 `{"/织忆"}`；不注册 `/记忆`。
- help、README、当前运行文档全部使用 `/织忆`；历史烟测保留 `/记忆` 并标注当前前缀。
- 测试覆盖 `/织忆 授权`、`/织忆 保存`、`/织忆 列表` 路径，以及 help/命令组不含 `/记忆`。
- 真实宿主 `/织忆 帮助/授权/保存/列表` 已验证（2026-11-23，见 `HOST_SMOKE_ZHUYI_PREFIX_20261123.md`）；旧 `/记忆 帮助` 未返回插件帮助，进入普通聊天。

## 13. V1.0 独立验收状态
当前结论为条件通过（开发验收）。专用测试 Scope 的真实宿主 `/织忆 清空 确认` 已通过，主测试 Scope 未受影响；临时 TextPart 已补对象级 dump 与目标版本源码契约证据；具体 Provider token 级抓包未执行。正式发布签署与最终确认待主人完成。详见 `V1.0_ACCEPTANCE_EVIDENCE_20261123.md` 与 `V1.0_INDEPENDENT_ACCEPTANCE_REVIEW_20261123.md`。

## 14. V1.1 Group shared 测试矩阵（M0 契约；V1.1 已实现，矩阵持续执行）

### 身份与隔离

- 群 A/B 隔离；平台实例 A/B 同 group_id 不共享 Scope；同 platform 名不同实例隔离。
- `scope_external_id=subject_id=group_id`；`scope_id` 与 Personal UUID5 序列化一致。
- 个人/群双向隔离：个人记忆不进群；群记忆不出现在私聊。

### 管理员与 Consent

- PLATFORM_ROLE、EXPLICIT_CONFIG、NONE 三种来源；无来源 fail-closed；首次开启必须有管理员。
- 多管理员、角色变化、配置变更、下一命令生效；不接受 `admin_id` 参数。
- 普通成员不能 `群保存/群开启/群关闭/群清空/群销毁`；`群状态` 允许；`群列表/查询` 仅在 enabled + GRANTED 时允许。
- `group_memory` 默认拒绝；开启授予；关闭撤销且可逆重开；清空不改 consent；个人关闭仍永久。
- `群列表/查询` 在 disabled 或 consent 撤销后拒绝；`群状态` 可读但不含正文；无 Scope 的 `群状态` 返回 `created=false`。
- `群销毁`：未关闭/未确认/备份生成或校验失败拒绝；删除顺序满足 FK（记忆/关联 → consent → operations → scope）；事务回滚不留部分删除；销毁事务标记 FTS dirty，提交后重建再 purge，群 canary 不在 FTS/主库/WAL；`群关闭→群销毁` 备份含群记忆，`群清空→群销毁` 备份不含已清空记忆；关闭+清空+销毁后无群 Scope/群 consent/群审计；两种关闭后销毁路径都能执行 `rollback_to_v10`。
- `experience→EVENT` 与 `topic/agreement/atmosphere` 映射测试。

### 生命周期与擦除

- enable/disable/re-enable/clear/status 全路径；群关闭后读写/检索/注入停止；重开恢复。
- 群清空只清当前群；个人/其他群不受影响；`CLEAR_SCOPE` 审计；物理擦除沿用 ADR-0008。
- 群解散 fake event：disable + revoke + 数据保留。
- `group_max_active=200` 告警按 ACTIVE 计数；群状态不泄漏管理员列表；`count` 只统计 ACTIVE。

### Schema/回滚

- v4→v5 幂等；个人行/索引保留；迁移前备份。
- 仅 GROUP Scope、仅有群记忆、仅有 `group_memory` consent 三种情况都拒绝 `rollback_to_v10`。
- `群开启→群关闭→群销毁→rollback_to_v10` 成功；`群开启→群关闭→群清空→群销毁→rollback_to_v10` 成功；`群开启→群清空→群销毁` 因未关闭被拒绝；未确认/备份失败的销毁拒绝。
- 无群数据回滚到 v4 后再次迁移到 v5；V1.0 不能直接打开 v5。
- 迁移中断回滚不留下半迁移状态。

### 入口与注入

- 群命令只在群聊；个人命令只在私聊；群注入只在当前群；独立预算 `192/2/8`。
- FTS/读取 `scope_type=GROUP` 防御过滤；注入失败不阻塞聊天。
- 群级 Agent 工具写入不在 V1.1；导出不在 V1.1。

### 退出条件

1. Group shared 读写/注入跑通，成员只读、管理员管理；
2. 群/个人双向隔离通过；
3. enable/disable/clear/status 与可逆群关闭通过；
4. 管理员来源 fail-closed、角色变化/多管理员通过；
5. v5 迁移与 v4 回滚通过；
6. 未提前实现 Group personal/Project/Session/导出/自动提取；
7. 文档、ADR、测试与实现一致，羽忆审查通过。

### M0 状态

- 本测试矩阵是 M0 契约；V1.1 代码已实现并持续执行，具体测试数以 `TODO.md` 为准。
- 个人事实口径：主人 2026-11-23 确认管理员责任、不做 NLP 检测；V2.4 再做敏感过滤。
- 个人事实不提升：V1.2 模型工具写入只写 `ACTIVE`，不得把 `INFERRED` 候选自动升级为 `ACTIVE`。

## 16. V1.5 自动提取测试矩阵（M1 已实现，宿主烟测待补）

状态：核心管线已实现并执行自动化测试（schema v6、分层 consent、单条/批量捕获、敏感前置过滤、候选确认/拒绝、任务恢复、队列上限、指标计算）；真实宿主模型调用与正式质量采集待补。

### 授权与捕获

- 未 opt-in 用户（含 `auto_enroll=true` 但无 `extraction_capture`）的消息不捕获、不进入窗口/LLM。
- `personal_memory=GRANTED` 但 `extraction_capture` / `auto_extract` / `external_model` / `store_candidate` 任一缺失：按有效条件停止，不产生候选。
- `external_model` 未授予时：不调用 `LlmExtractor`，不产生候选；`auto_extract` consent 记录不被改写。
- `extraction_store_candidate` 未授予时：模型结果不落库；不得先保存后丢弃。
- 群消息、群共享消息、Group personal 消息不捕获、不进入窗口/LLM。
- scope disabled、consent revoked、全局 `extraction_enabled=false`：全部停止。
- `提取 开启` 不重开已关闭 scope，返回明确错误。
- `提取 开启 全部` 部分失败时报告已授予项与失败项。
- `auto_enroll` 不产生任何 `extraction_*` consent 事件。

### 敏感信息

- 凭据/密码/令牌/API key、身份证件号、银行卡号、精确住址在进入窗口前被拦截或脱敏。
- 疑似敏感但无法识别的内容默认不送外部模型。
- 白名单为空时敏感类别不进入候选；白名单命中时需人工确认。
- Sensitive Leakage Rate = 0；测试日志与 `metadata_json` 不含正文。

### 任务与恢复

- 幂等键重复提交不重复写入。
- 有限重试、超时、模型异常不阻塞聊天。
- 启动恢复未完成任务；失败任务可见。
- 回合结束钩子提交当前缓冲；宿主无该钩子时退回累计阈值与“记住……”快通道。
- 热重载停止接收新任务 → 等待/取消 → 保存状态。
- 队列有界；积压计数可见。

### 候选

- 候选不参与检索/注入；确认后参与；拒绝后不参与。
- 同键已有 ACTIVE：不建候选，任务记 `SKIPPED_DUPLICATE_ACTIVE`。
- 同键已有 CANDIDATE：幂等更新，不新增行。
- 显式保存 / AGENT 工具写入撞候选：返回 `Conflict`，不静默替换。
- 候选被更新/删除后确认：`expected_revision` 冲突。
- 拒绝记录原因；候选过期转 `ARCHIVED`，不物理删除。

### Schema 与回滚

- v5→v6 迁移幂等，迁移前备份并校验；备份失败中止。
- v6→v5 回滚守卫：`extraction_jobs` 非空时拒绝或保留表停用；回滚后提取关闭。
- 关闭提取后行为退回 V1.4，显式写入/检索/注入/群路径语义不变。

### 质量指标（V1.5 采集）

- Extraction Precision / Recall、False Memory Rate、Sensitive Leakage Rate、Attribution Accuracy、Candidate Acceptance Rate。
- Sensitive Leakage Rate 与错误归属为安全验收项；阈值在 V1.5 评测集冻结。


## 17. 计划书审计对齐回归（2026-11-23）

- 注入行包含 `created_at` 日期（`YYYY-MM-DD`），个人与群两条 composer 均覆盖；不改变检索排序。
- `群销毁 无备份 确认` 与 CLI `--no-backup` 写 `backup_mode=SKIPPED`、sidecar 与 warning 审计，不生成 sqlite 备份。
- 群销毁备份按 `group_destroy_backup_retention_days` 清理，保留最新一份；清理 sqlite 与 sidecar 成对进行。
- `weave_admin.py groups / rollback-preview / group-destroy / cleanup-backups` 可执行；`group-destroy` 需 `--confirm`，无群聊天可达。
- `rollback_to_v10 --dry-run`/`rollback_preview()` 只读列出群 scope、记忆数与步骤，不修改数据。
- 管理失败区分 `NOT_GROUP_ADMIN` / `ADMIN_SOURCE_UNAVAILABLE` / `GROUP_NOT_CREATED`，提示含 `group_admin_ids` 格式。
- 首次群开启的响应包含记录范围、群关闭与群销毁方式。
- `/织忆 状态` 显示 `consent_source`；导出帮助写明“导出不是备份”；关闭帮助并列撤销授权（可逆）与关闭（永久）。
- 群注入诊断记录 `omitted_count`；`群状态` 显示上次注入截断条数。
- V1.4 `conflict_temporal` 类别覆盖同 scope 新旧 ACTIVE；报告输出 `conflict_co_injection_rate`，只作诊断。
- V1.5 候选生成时通知摘要、ID 与确认/拒绝命令；通知失败不影响任务与聊天。

## 18. V1.6 候选 TTL 与只读管理 API

### Schema 与迁移

- v6→v7 迁移新增 `candidate_expires_at` 与索引，既有候选回填 `created_at + 7 天`。
- 迁移幂等、迁移前备份并校验；备份失败中止。
- v7→v6 回滚守卫：存在 `CANDIDATE` 时拒绝；为空时回滚成功且正文不受影响。

### 候选 TTL 与审计

- 新候选写入 `candidate_expires_at = created_at + candidate_ttl_days`。
- 到期候选在读取列表时转 `ARCHIVED`，写 `CANDIDATE_EXPIRE`，不物理删除。
- 确认写 `CANDIDATE_CONFIRM`；拒绝写 `CANDIDATE_REJECT`；metadata 不含正文。
- 拒绝默认清理正文与关联行，`memory_id` 只留在 operation 审计；`rejected_keep_body=true` 时保留 `REJECTED`。
- 拒绝后同内容可重新提交为新候选。
- 候选确认/拒绝后 `expected_revision` 冲突仍拒绝覆盖。

### 管理 API

- 未登录用户名拒绝；`api_admin_usernames` 非空时名单外拒绝。
- 群作用域端点：群不存在/未开启/撤销授权分别拒绝；跨群不可读。
- 个人 scope 的 memories/candidates/debug 一律 `AuthorizationDenied`；Overview 只含聚合计数。
- 检索调试字段完整（候选 ID、评分、过滤原因、最终注入 ID、Token、截断原因），不出现正文、不更新访问时间、不写诊断。
- Settings 不返回管理员名单；Maintenance 只读。
- 分页 `limit`/`cursor` 边界正确；错误映射 400/403/404/409/503。
- API 失败不阻塞聊天；关闭 API 后退回命令能力。

## 19. V1.7 导入导出测试矩阵（已实现）

| 项 | 覆盖 |
|---|---|
| 导出格式 v2 | `schema_version` / `export_id` / `source_platform` / 条目四元组字段；每次导出新 `export_id` |
| 导出过滤 | 默认只 ACTIVE；`all` 含 ARCHIVED 等元数据且正文为空；DELETED 与 CANDIDATE 不导出 |
| 作用域隔离 | Personal 只导出本 scope；Group 需要管理员、enabled 与 GRANTED；跨账号不可见 |
| 导入成功 | ACTIVE 创建新 `memory_id`、`SourceType.IMPORTED`、四元组写入 `evidence_ref`、有效期与 metadata 保留 |
| 伪造作用域 | 文件内 `scope_id` / `subject_id` 被忽略；GROUP→Personal 与 Personal→Group 均拒绝 |
| 幂等 | 同文件重复导入不重复写入；同内容 ACTIVE 记跳过；同内容 CANDIDATE 记冲突 |
| 断点续跑 | 预置 `cursor` 的任务只处理剩余条目；`DONE` 返回幂等摘要；`FAILED` 不续跑 |
| 状态映射 | ARCHIVED/EXPIRED/SUPERSEDED/REJECTED/DELETED 只写 `IMPORT_METADATA`，不建正文；CANDIDATE 条目失败 |
| dry-run | 不写记忆、不写任务，返回逐条预判 |
| 回滚 | 只删除本次新建记忆；任务置 FAILED；之后重新导入新建任务 |
| schema v8 | 迁移创建 `import_jobs` 与 evidence_ref 索引；v8→v7 空表回滚；非空拒绝且版本不变 |
| 命令层 | 个人导入 dry-run/导入/回滚；无附件报错；`导出 all`；群导入管理员限制 |
| 管理 API | `/v1/export` 与 `/v1/import` 白名单 fail-closed；匿名/非白名单 403；管理员参数透传；禁用群拒绝 |

验证口径（2026-11-23）：305 passed；`ruff check .`、`mypy`、`mypy .`、`compileall`、插件构建通过。宿主真机导入/导出烟测待补。

## 20. V1.8 账本与备份恢复测试矩阵（已实现）

| 项 | 覆盖 |
|---|---|
| schema v9 | 迁移后 `db_lineage_id`/`creation_seq` 非空、唯一、按 `(created_at, id)` 回填；高水位等于最大值 |
| 序号分配 | 所有创建路径经写租约分配；同一谱系唯一单调；恢复/灾后重建后新分配不复用 |
| ledger-first | delete/clear/consent/destroy 产生成对 intent/committed；主库失败保留 pending |
| pending 重放 | 仅落 intent 时启动 drain 继续删除并补 committed；备份前 strict drain 失败即中止 |
| clear 边界 | 按 `scope_id + db_lineage_id + creation_seq <= boundary` 删除；清空后新建保留；跨谱系隔离 |
| 旧备份不复活 | 备份 → 删除/清空/撤销授权 → 恢复：删除/清空/REVOKED 仍生效 |
| merge | 快照后新建保留；同 ID 以较新 revision/updated_at 为准 |
| revert | 同 ID 当前版本被备份版本覆盖；备份中不存在的行不动 |
| disaster-rebuild | 空目标才允许；生成新当前谱系；旧 lineage 备份的 merge 被拒绝 |
| 附件 | `remove_attachment` 后恢复旧备份不复活 source/tag/relation |
| 群销毁 | `destroy_scope` 后恢复旧备份不复活该群 memories/consents/operations/scope |
| 账本分段 | 轮转后 seq 连续、`ledger_id` 不变、恢复读取全部分段 |
| 校验失败 | 备份哈希/尺寸/schema/`ledger_id`/seq 覆盖/水位任一失败即拒绝，不半恢复 |
| dry-run | 输出将复活/不会复活清单；不修改主库 |
| journal | 合并阶段中断后重启按 journal 重做；损坏/缺失 fail closed |
| 配置 | `config.json` 递归脱敏；关闭 `ledger_first_enabled` 后备份/恢复禁用 |
| 高水位 | 恢复后推进到最终表最大值、备份水位与 ledger clear 边界之上 |
| 入口 | 私聊备份/恢复命令；管理 API 管理员白名单 + `confirm=true` |

验证口径（2026-11-23）：333 passed；`ruff check .`、`mypy`、`mypy .`、`compileall`、插件构建通过。真实宿主备份/恢复烟测已通过（`docs/HOST_SMOKE_V1.8_20261123.md`）。
