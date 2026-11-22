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
- 当前全量测试：100 passed（Python 3.13）。
- 限制：不代表物理断电、存储介质损坏或多进程并发恢复已验证。详见 `V1.0_M3_CRASH_RECOVERY_REPORT_20261123.md`。

## 13. V1.0 独立验收状态

当前结论为条件通过（开发验收），正式发布签署未完成。真实 AstrBot 清空命令验证与 Provider 持久历史/token 级证据仍待补。详见 `V1.0_INDEPENDENT_ACCEPTANCE_REVIEW_20261123.md`。
