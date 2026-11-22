# CONTRACTS — V1.0 执行契约

状态：已按用户确认的既有审查结果作为 V0 执行基线；V0.2 决策见 ADR-0002  
依据：V0「起针」计划书 §3–§7；总计划书 §4。

> 本文记录 V0 的已实现端口与行为契约；未冻结的 V1+ 扩展另行标注 OPEN。

## 1. 通用约定

- 核心层只使用平台无关 DTO / 值对象。
- 所有写入都必须经过应用层 WriteCoordinator；Adapter 不得直接写表。
- 内容写入走 Coordinator 的异步写锁；Scope/Consent 元数据写入调用 `write_metadata`，与内容写入共享同一把可重入写锁。
- `write_metadata` 不等待 async `_lock`，因此不加入同一 FIFO 队列；内容写临界区不含 await，单事件循环下数据库写不会重叠。若未来引入 await/多线程写，必须把元数据写统一为 async 写入口。
- 失败不得被静默吞掉；错误必须能区分验证失败、未授权、冲突、未找到、存储失败、索引失败及关闭状态。
- 权威数据写入成功但 FTS 更新失败时，不得丢失权威正文；索引必须可检测并重建。
- 并发更新必须使用乐观锁，版本不匹配不得静默覆盖。
- 注入失败按无记忆处理，不得向宿主传播为聊天失败。

## 2. 端口职责

### MemoryStore

权威存储端口，负责 Memory、Revision、Source、Scope/Consent 所需数据的读取与事务写入。不得暴露 SQL 或 SQLite 类型。

候选操作：

- 按 ID 读取记忆及其必要关联信息。
- 按身份/作用域查询可见的 ACTIVE 记忆。
- 在事务中创建记忆并写入初始 Revision / Source。
- 基于预期 revision 更新记忆，版本冲突时返回明确冲突。
- 执行状态变更及删除所需的权威数据操作。

### SearchIndex

派生检索索引端口，V0 实现为 SQLite FTS5。负责索引更新、删除、查询与重建。不得成为正文权威来源。

### PlatformAdapter

将宿主身份/事件转换为平台无关身份 DTO；提供 V0 私聊命令所需的用户身份、回复与注入能力。平台对象不得穿透到 Domain。

### Clock

提供可注入的当前时间，便于确定性测试。领域输入必须带时区并统一转换 UTC；SQLite 持久化使用 ISO-8601 微秒精度。

### ConsentPolicy

对指定调用者、动作、主体与作用域执行授权判定；默认拒绝。V0 仅 Personal。`ConsentService.require` 必须加载 append-only 事件历史并调用该 Domain 策略，不在 Adapter 或命令层重复实现判定。该实现每次读取完整历史，复杂度 O(N)；V1.0 个人库可接受，latest-only 缓存留给后续版本。

### 预留端口（V1.0 只定义边界，不实现）

- `ManagementService`：V1.6/V2 JSON 管理 API 的边界；V1.0 不提供方法实现。
- `LlmExtractor`：V1.5 结构化提取端口；V1.0 不调用模型、不注册提取管线。
- `ExtractionService`：V1.5 应用服务，基于 `LlmExtractor`；V1.0 不创建占位实现。

## 3. 应用服务职责

### WriteCoordinator

统一串行化单进程写操作，管理事务与乐观锁；协调权威数据和派生索引的一致性；不得静默覆盖。

### Recall

按调用者可访问的 Personal Scope 检索 ACTIVE 记忆；先执行身份/授权过滤，再返回候选结果。V0 使用 FTS。

### Injection

将 Recall 结果按固定预算编排为上下文文本。预算计算必须与 composer 共用同一口径；注入异常隔离，不影响聊天。

### Lifecycle Service

唯一允许修改生命周期状态的应用服务。删除、清空、关闭的行为按 `LIFECYCLE.md`。

## 4. 候选结果/错误类别

| 类别 | 语义 | 是否允许静默降级 |
|---|---|---|
| ValidationError | 输入违反领域约束 | 否 |
| AuthorizationDenied | 身份或 Consent 不允许 | 否，拒绝操作 |
| NotFound | 目标不存在或对调用者不可见 | 不得泄漏跨用户存在性 |
| Conflict | 乐观锁/唯一约束冲突 | 否，要求显式处理 |
| StorageError | 权威存储操作失败 | 否 |
| IndexUnavailable | 派生索引不可用/未同步 | 检索 fail-closed；写入回执标记 PENDING_REBUILD；注入层按空注入处理 |
| ServiceClosed | 生命周期已关闭 | 否 |

领域异常类型已实现；跨平台稳定错误码和自动重试策略留待后续版本，不在 V0 静默重试。

## 5. 已落实的事务契约

- 创建记忆、初始 Revision、Source 在同一权威事务提交。
- FTS 使用应用层独立事务同步；dirty marker 与权威数据在同一事务提交。
- 正文事务提交后索引失败时保留 dirty marker；dirty 集合非空时检索拒绝执行；启动恢复或显式 rebuild 修复。
- 删除时先写最小 tombstone audit、标记 FTS dirty，再在同一事务物理删除正文关联数据；事务整体回滚。
- Create 使用稳定实体/Revision/Source ID 作为幂等键；完全相同重放成功，payload 不同冲突。

## 6. V0.3 SQLite / FTS 实现契约（ADR-0003）

- Memory、初始 Revision、Source 同事务提交。
- 权威写入提交后，memory ID 进入持久 dirty 集合；FTS 同步独立提交。
- dirty 集合非空时检索 fail-closed；FTS rebuild 从 ACTIVE 权威正文全量重建。
- 完全相同的 create 请求重放幂等成功；相同 ID 的不同 payload 或不同 ID 的自然键冲突返回 Conflict。
- SQLite 连接为单进程、单连接、线程亲和；V0 不承诺多实例写入。

## 7. V0.4 写协调与生命周期实现契约（ADR-0004）

- 所有应用写操作经单实例 WriteCoordinator 串行化；Scope/Consent 元数据写入走 `write_metadata`。
- 状态转换由 LifecycleService 守卫；DELETED 只能走物理清理用例。
- 删除/清空在一个权威事务内写最小 tombstone audit、标记 FTS dirty 并物理删除正文关联数据。
- 关闭 Scope 停止普通读写与检索，不删除正文。
- 删除/清空/关闭操作使用 operation ID；同操作重放幂等。
- 启动恢复检查 migration 和 FTS；V0 无后台任务队列。
- V1.0 热重载语义：等待持有中的写锁、拒绝排队写入、关闭 SQLite 连接；因无后台任务，不实现超时取消或未完成任务快照。

## 8. Token 预算

V0.5 使用 provider-neutral estimate：CJK 字符和标点各计 1，ASCII 字母/数字连续段每 4 字符估 1 token；前缀/后缀计入；默认最多 4 条、384 estimated tokens。该估算不是 Provider tokenizer 的精确 token 数，详见 ADR-0005。

## 9. V0.5 Consent 与宿主注入契约（ADR-0005）

- `personal_memory` Consent 无记录默认拒绝；授权后显式保存、主动检索、自动注入可用；撤销后停止三者但不删除历史。
- AstrBot 身份由 platform name + platform instance ID + sender ID 构成；仅私聊映射 Personal。
- 注入追加临时 TextPart，不修改 system prompt 或持久会话历史。
- 记忆内容作为不可信文本转义；注入失败按无记忆处理，不阻断宿主聊天。


## 10. V1.0 新增应用契约

- `MemoryStore.find_active_duplicate` 必须同时按 `scope_id`、`subject_id`、`memory_type`、`normalized_content` 精确过滤，只返回 ACTIVE。
- 显式保存前先把同键、已过 `valid_until` 的 ACTIVE 记忆转 `EXPIRED`，再执行查重，避免过期记录占用唯一索引；未来才生效的 ACTIVE 同键记录仍视为已存在。
- 该过期扫描在 WriteCoordinator 写锁之外执行，复杂度 O(N)，且先于最终写入/Consent guard；若随后保存失败，旧记忆可能已转 `EXPIRED`。V1.0 接受该语义，候选/批量维护在 V1.6 再统一。
- `WriteCoordinator.create_or_get_duplicate` 在同一写锁内完成查重与创建；命中时返回原记忆及原 revision，不新增 revision/source。
- `MemoryApplicationService.update_explicit` 必须由调用者指定 `memory_id` 和 `expected_revision`；只允许更新 ACTIVE 记忆。
- 旧 revision 不匹配返回 `Conflict`；自然键唯一约束冲突也显式返回 `Conflict`，不得静默覆盖或自动合并。
- 正文更新、Revision、Source 在同一权威事务中提交；FTS 同步仍为可重建派生步骤。


### V1.0 检索排序与访问元数据

- FTS 候选先完成 Scope/Subject/Consent/Scope enabled/status/validity 过滤，再按 BM25 相关性排序。
- 相关性相同时，按 `COALESCE(last_accessed_at, created_at)` 倒序；再以 EXPLICIT 来源优先，最后以 MemoryType 字典序与 ID 稳定打破平局。MemoryType 当前只作确定性末级 tie-break，不代表已经校准的语义权重。
- 上下文 Composer 只为最终通过 Token/条数预算的 `selected_memory_ids` 记录访问时间；未注入候选不更新 `last_accessed_at`。
- 手动查询同样记录实际返回记忆的访问时间。访问元数据由 WriteCoordinator 串行写入，不创建内容 Revision，不触发 FTS dirty。
- `compose` 与 AstrBot Injector 为异步调用，以便访问元数据写入遵守统一写协调契约。


### V1.0 SQLite Schema 迁移

- V1.0 schema version 为 4；从 V0.5 schema v3 通过 migration `004_memory_relations.sql` 升级。
- `memory_relations` 只保留最小有向关系结构，不提供应用层写入/查询 API，不参与检索。
- 删除单条记忆或清空 Scope 时，所有引用被删除记忆的入边/出边必须在同一权威事务内删除。


### V1.0 Scope disable / Consent 关闭联动

- LifecycleService 依赖 ScopeStore 与 ConsentService。
- 关闭流程先提交 Scope disabled，再追加 `personal_memory` REVOKED 事件。
- Consent 追加失败向调用方显式抛出；Scope disabled 已提交，所有普通读取仍不可用。相同 operation ID 重试必须补齐撤销事件，不重复关闭审计。


### V1.0 → V0.5 Schema 回滚

- 当前 V1.0 唯一新增 schema 为 v4 `memory_relations` 预留表。`SQLiteDatabase.rollback_to_v05()` 仅在该表为空时删除它并回退 migration marker 到 v3。
- 若关系表已有数据则拒绝回滚，不丢弃关系；V1.0 不提供关系写入 API，因此正常运行期间该表应为空。
- 该方法不回滚业务数据与代码版本；插件回滚仍需同时安装 V0.5 包。


### V1.0 Recall → Injection 的授权竞态

- 访问时间写入由 Coordinator 在 writer lock 内执行 guard；guard 重新校验 Consent 与 Scope enabled。
- 访问写入 await 返回后，Recall 再同步校验一次 Consent/Scope；该校验之后到 Adapter 追加临时 TextPart 之间不再 await。
- 若在候选检索后、访问写入前发生 revoke/disable，则拒绝上下文，不更新 last_accessed_at，不注入旧候选。


### V1.0 写入路径的 Consent 竞态

- `save_explicit` 与 `update_explicit` 在入口先校验 Consent，并通过 WriteCoordinator guard 在写锁内再次校验。
- Consent 在入口检查后、写入获得锁前被撤销时，写操作必须返回 AuthorizationDenied，不得提交正文/Revision/Source。


### V1.0 关闭的部分失败错误语义

Consent 撤销事件追加失败时抛出 `ConsentRevocationPending`（StorageError 子类）。它表示 Scope disabled 已提交、普通访问已阻断，但 append-only REVOKED 事件待重试；AstrBot 命令层必须向用户准确表达这一部分成功状态。


### V1.0 删除/清空的物理擦除（ADR-0008）

- 所有由本应用打开的 SQLite 连接启用 `PRAGMA secure_delete = ON`。
- `delete` / `clear_scope` 在权威事务内写 `memory_meta.secure_purge_pending = '1'`。
- 仅当 FTS 同步/重建成功、索引 `HEALTHY` 后，才执行 `wal_checkpoint(TRUNCATE)` → `VACUUM` → `wal_checkpoint(TRUNCATE)`；两次 checkpoint 都校验 `busy` 与 `log`，`busy != 0` 或 `log > 0` 时保留 pending 并返回 `purged=False`；`log < 0` 表示非 WAL 模式，继续 VACUUM。
- 成功后清 pending 并写 `secure_purge_baseline_done='1'`；启动时若 baseline 缺失，执行一次全库基线擦除，清理升级前的旧残留。
- 索引未恢复或擦除失败时保留标记；插件下次启动完成 FTS 恢复后重试。
- 命令层在擦除未完成时提示「将在下次插件启动时自动重试」，不得把逻辑删除误报为失败。
- 有条件保证：应用自身连接、`secure_delete=ON`、无其他连接持有 WAL 读快照、checkpoint 成功时，SQLite 主库与 WAL 不保留可恢复正文；只读连接/备份快照/backup API 会令 checkpoint busy，此时状态为 pending。
- 不承诺 SSD、OS 缓存、文件系统空闲块或备份层面的取证级擦除。
