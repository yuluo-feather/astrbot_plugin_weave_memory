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
- V1.0 热重载语义：等待持有中的写锁、拒绝排队写入、关闭 SQLite 连接；因无后台任务，不设未完成任务快照；对写协调器关闭设超时上限，超时记录 warning 后继续释放连接。

## 8. Token 预算

V0.5 使用 provider-neutral estimate：CJK 字符和标点各计 1，ASCII 字母/数字连续段每 4 字符估 1 token；前缀/后缀计入；默认最多 4 条、384 estimated tokens。该估算不是 Provider tokenizer 的精确 token 数，详见 ADR-0005。

## 9. V0.5 Consent 与宿主注入契约（ADR-0005）

- V1.2 起 `personal_memory` 默认登记（`auto_enroll=true`）：首次符合条件的私聊事件自动 GRANTED 并发送一次告知；`auto_enroll=false` 时回到无记录默认拒绝。任何已存在的 GRANTED/REVOKED 事件优先，auto_enroll 不改写。
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

## 11. V1.1 Group shared 契约（M0 冻结）

### Core 端口与返回类型

- `GroupScopeStore`：在 V1.0 `ScopeStore` 基础上提供 `get_scope`（继承）、`get_admin_metadata`、`set_admin_metadata`、`enable_scope`、`disable_group_scope`、`backup_group_scope`、`delete_group_scope`、`count_active`；生命周期开关由 `enable_scope` / `disable_group_scope` 分别承担 consent 追加语义，不合并为单一 `set_enabled`；不暴露 SQL。本清单与 `core/ports/__init__.py` 的 Protocol 同步，由 `tests/contract` 的结构性测试机械对账。
- `AdminResolver`：`resolve(identity, sender_id) -> AdminDecision`；`AdminDecision = {source: PLATFORM_ROLE|EXPLICIT_CONFIG|NONE, admin_ids: tuple[str,...], allowed: bool, admin_available: bool}`。
- `GroupScopeStore`/`AdminResolver` 只处理平台无关的 Identity/Scope；AstrBot 角色 API 在 Adapter 实现。
- `GroupMemoryService` 复用 V1.0 `MemoryStore`、`WriteCoordinator`、`SearchIndex`；不新增群专用正文表。

### GroupScopeService

- `resolve_group(identity)`：只接受 `scope_type=GROUP`；`scope_external_id=subject_id=group_id`；`scope_id` 使用完整六元组 UUID5，格式与 Personal `stable_scope_id` 一致。
- `admin_decision(identity, sender_id)`：每次命令重新解析管理员；返回 `AdminDecision`，不接受调用方传入 `admin_id`。
- `status(scope)`：成员只读；返回 `created/enabled/count/admin_source/admin_available`，不返回记忆正文或管理员列表；无 Scope 时返回 `created=false, enabled=false, count=0, admin_source=NONE, admin_available=false`。
- 只负责 Scope 解析/管理员/状态；enable/disable/clear/destroy 属于 `GroupLifecycleService`。

### GroupLifecycleService

- `enable(scope, operation_id)`：解析管理员 → 追加 `group_memory=GRANTED` → `is_enabled=1` + `GROUP_ENABLE` 审计；GRANT 失败则不 enable；审计/enable 失败时保留 GRANT 但 disabled，可同 operation ID 重试补齐；同一 operation ID 重放幂等。
- `disable(scope, operation_id)`：`is_enabled=0` + `GROUP_DISABLE` 审计 → 追加 `group_memory=REVOKED`；保留数据；撤销追加失败返回 `GroupConsentRevocationPending`，Scope 保持 disabled；同一 operation ID 重试补齐。
- `clear(scope, operation_id)`：只清当前群；要求 `确认`；enabled/disabled 都可执行；写 `CLEAR_MEMORY`/`CLEAR_SCOPE` 审计；物理擦除沿用 ADR-0008；不改变 consent/enabled。
- `destroy(scope, operation_id) -> DestroyReceipt`：仅管理员、必须先 `群关闭`、必须 `确认`；服务内调用 SQLite backup API 生成唯一备份并校验目标群 Scope/记忆/consent，写 sidecar manifest（operation ID/scope ID/路径/sha256/行数/时间）；备份或校验失败立即中止。
- `destroy` 使用专用 `delete_group_scope` 事务，不调用会自行提交的 `clear_scope`；单事务删除：群记忆及关联 → consent 历史 → operations 审计 → scope，并同时标记 FTS dirty 与 `secure_purge_pending`。
- 提交后先重建/清理群 FTS（或由启动恢复重建），再 secure purge；FTS 未恢复时不得清 pending；purge 失败保留 pending，不回滚 Scope 删除；已有 pending 不阻止销毁。
- 备份语义：`群关闭→群销毁` 备份包含群记忆；`群清空→群销毁` 备份是清空后快照；要保留群记忆必须先关闭直接销毁。
- `DestroyReceipt = {operation_id, scope_id, backup_path, backup_sha256, affected_count, purged}`；`affected_count` 是删除的群记忆数；sidecar 另记各子表行数。
- sidecar 写在插件数据目录下的备份同级文件，temp+fsync+rename 原子落盘；权限继承数据目录；备份/sidecar 不在 V1.1 自动清理。
- 幂等：事务提交后 Scope 不存在视为已销毁；事务前/中崩溃整体回滚；重试不覆盖已有备份；`GROUP_DESTROY` tombstone 写 sidecar，不修改备份。
- `DestroyReceipt = {operation_id, scope_id, backup_path, backup_sha256, affected_count, purged}`。
- 群关闭可逆；个人关闭仍永久。`destroy` 只用于彻底移除群数据/回滚前置。

### AdminResolver / 管理员来源

- 来源优先级：`PLATFORM_ROLE` > `EXPLICIT_CONFIG` > `NONE`；无来源 fail-closed（不能 enable/写入/管理）。
- `group_admin_ids` 配置：对象映射，键为 `platform|tenant_id|group_id`，值为管理员 ID 数组；运行时也接受同字段对象数组；按三元组精确匹配，不允许全局列表跨群/跨实例复用。
- `admin_ids_json` 在 `PLATFORM_ROLE` 下是审计快照，不用于授权；`EXPLICIT_CONFIG` 下镜像配置；授权每次都重新解析。
- 角色/配置变化在下一命令生效；多管理员取并集；普通成员不得管理/写入。
- 来源丢失时：管理/写入 fail-closed；已启用群的成员读取/注入保留（需 enabled + group_memory GRANTED）；`群状态` 显示 `admin_source=NONE, admin_available=false`；保留 `EXPLICIT_CONFIG` 恢复路径。

### GroupMemoryService

- `save_group`：仅管理员；来源 `MANUAL + ADMIN`；`EXPLICIT` 仍只允许 USER；成员写入 `AuthorizationDenied`。
- 类型映射：`topic -> FACT`；`agreement/atmosphere -> PREFERENCE`；`experience -> EVENT`；metadata 键 `group_label/group_id/admin_source`。
- 去重、revision、source、WriteCoordinator 与 V1.0 一致。
- `list/query`：仅当前群、`scope_type=GROUP`，且 `is_enabled=1` 与 `group_memory=GRANTED` 同时满足；否则 `AuthorizationDenied`。
- `群状态` 的 `count` 只统计 ACTIVE；`group_max_active=200` 也按 ACTIVE 计数；不返回正文；不查个人；不在私聊查群；不在群查个人。
- `GroupContextComposer.compose(query, scope) -> GroupContext | None`：只接受当前群 GROUP Scope；必须 `is_enabled=1` + `group_memory=GRANTED`；使用群独立预算 `group_max_tokens/group_max_memories/group_candidate_limit`；FTS/读取加 `scope_type=GROUP`；失败按无记忆处理，不阻塞聊天。
- 个人事实口径（主人 2026-11-23 确认）：V1.1 不做 NLP 检测，采用管理员输入责任；成员不能写、个人记忆不提升；V2.4 再做敏感过滤/Group personal。

### 命令/角色矩阵

| 命令 | 群成员 | 群管理员 | 私聊 |
|---|---|---|---|
| `群保存` | 拒绝 | 允许（enabled + group_memory granted） | 不适用 |
| `群列表/查询` | 允许（当前群、enabled + granted） | 允许（同左） | 不适用 |
| `群状态` | 允许（可 disabled；不含正文/管理员列表） | 允许 | 不适用 |
| `群开启/群关闭/群清空/群销毁` | 拒绝 | 允许（群销毁需先关闭 + 备份 + 确认） | 不适用 |
| 个人命令 | 不适用 | 不适用 | 仅本人 |

### 错误与配置

- 非管理员/成员写入：`AuthorizationDenied`；disabled/consent 未授予时的 `群列表/查询`：`AuthorizationDenied`；跨群/不可见：`NotFound`；revision/去重：`Conflict`；索引：`IndexUnavailable`；关闭：`ServiceClosed`。
- 配置键：`group_admin_ids`（对象数组，按 platform+tenant_id+group_id 隔离）、`group_max_tokens=192`、`group_max_memories=2`、`group_candidate_limit=8`、`group_max_active=200`；默认不自动开启群 Scope。
- 群注入只走当前群 Scope；FTS/读取加 `scope_type=GROUP` 防御过滤；注入失败不阻塞聊天。
- V1.1 不实现导出；V1.2 再做导出与完整保留策略。
- v5 迁移/回滚见 ADR-0011；群销毁是回滚前置，见 ADR-0010/0011。
- v1–v4 升级到 v5 前，`SQLiteDatabase.migrate()` 自动在数据目录 `migration_backups/` 生成一致性备份并校验完整性/版本；备份失败则中止迁移，原 schema 与数据不变。

## 12. V1.2 Personal 默认登记与 agent 工具（ADR-0012）

### Consent

- `ConsentService.ensure_auto_enrolled(scope, personal_memory, operator_id)` 只在 scope 已启用且 `memory_consents` 无任何历史时追加 `GRANTED`，`source=auto_enroll`，`metadata.auto_enrolled=true`；返回是否新登记。
- 已有 `GRANTED` 或 `REVOKED` 事件时返回 false，不追加事件。`/织忆 撤销授权`、`/织忆 关闭` 的拒绝状态不会被 auto_enroll 覆盖；`/织忆 清空` 不改变 consent/enabled。
- `auto_enroll` 配置默认 true；false 时注入与 agent 工具都要求显式 `/织忆 授权`。

### 注入

- `AstrBotMemoryInjector` 在私聊 `on_llm_request` 中解析 Personal scope；scope 不存在且 auto_enroll=true 时创建；scope enabled 且无 consent 历史时自动登记，并 `event.send` 一条固定告知；告知失败被吞掉，不影响聊天。
- 群事件不进入 Personal 注入；群记忆仍需 `/织忆 群开启`。

### Agent 工具

- V1.3 工具面按计划书使用 `weave_memory_` 前缀：`search/get/list/history` 仅访问当前用户 Personal Scope；`update` 只更新正文并要求 `expected_revision`；`remember` 沿用 V1.2 创建入口。AstrBot 4.29.4 未向工具执行前 handler 暴露真实 `tool_call_id`，因此写入使用插件侧 `evidence_ref`，不冒充宿主 ID。
- 工具 handler 绑定检查依据 `FunctionTool.handler_module_path`、AstrBot 加载时创建的 `functools.partial` 与当前插件实例；这仅确认 handler 归属，不构成跨插件/Persona 调用者隔离承诺。
- Personal 与 Group 检索均写入插件数据目录 `recall_diagnostics/recall-YYYY-MM-DD.jsonl`；每次检索一行，默认只写 query hash、不写查询原文/记忆正文，默认保留 7 天。查询原文需显式开启配置，写入前对邮箱、手机号与长数字串做常见模式脱敏。
- 原因：宿主 `OnUsingLLMToolEvent` 不提供 `tool_call_id`；`OnLLMResponseEvent` 在 Agent 完成后才触发，无法在写入前取得当前工具调用 ID。插件侧证据键为 `tool:<tool_name>#message:<message_id>#sha256:<normalized_content_hash>`；缺 `message_id` 时退回 `session:<unified_msg_origin>`，两者都没有才 fail-closed。
- 应用层 `MemoryApplicationService.save_agent/update_agent` 要求非空 `evidence_ref`，并记录 `source_type=AGENT`、`operator_type=agent`；`MemorySource.metadata` 记录 `origin=agent_tool`、`tool_name`、`session_id`、`message_id`、`content_sha256`、`requested_persistence` 与可选 `reason`；沿用去重、Revision、Source、WriteCoordinator 与 Consent guard。
- `weave_memory_remember` 的 `requested_persistence=false` 时不写入；`reason` 仅用于审计。`weave_memory_update` 仍必须携带 `expected_revision`，只修改正文，不改 `MemoryType`。
- 当前通过 `/织忆 保存`、`/织忆 更新` 完成用户写入；命令写入仍为 `EXPLICIT` / `USER`。群聊、撤销/关闭、跨用户读取均 fail-closed。
- 后台自动提取不在本版实现；V1.5 仍只产 `INFERRED` 候选。

### 导出与审计

- `MemoryApplicationService.export_personal(scope)`：经 `list_active` 重新校验 consent，只导出该 Personal scope 的 ACTIVE 记忆；不包含其他用户。
- `GroupMemoryService.export_group(scope, decision)`：要求管理员 `decision.allowed`，再经 `list_group` 校验 enabled + group consent；只导当前群。
- `app/export.py` 提供 `build_export`、`to_json`、`to_markdown`；V1.3 增加 `/织忆 导出 [json|md]`，仅导出当前 Personal Scope 的 ACTIVE 记忆；导出不等于备份，完整备份恢复在 V1.8。
- V1.3 个人删除、清空、关闭均需二次确认；删除格式为 `/织忆 删除 <记忆ID> 确认`。状态/统计/操作记录/导出只读取当前 Personal Scope，操作记录不含正文。
- `memory_operations.metadata_json` 默认包含 `result=SUCCESS`，不记录正文；失败以类型化异常/日志呈现，ledger 级 result 留到 V1.8。
- 注入无缓存；删除/清空/关闭后下一次注入重新检索，不得出现旧记忆。

## 13. V1.5 自动提取契约（M0 冻结）

依据：`星羽塔罗_织忆_V1.5_添线_计划书.md` V1.5；ADR-0015；ADR-0014（schema v6）。

### 授权类型

- `PersonalConsentType` 新增 `EXTRACTION_CAPTURE`、`EXTRACTION_AUTO_EXTRACT`、`EXTRACTION_EXTERNAL_MODEL`、`EXTRACTION_STORE_CANDIDATE`、`EXTRACTION_SENSITIVE_CATEGORIES`，值分别为 `extraction_capture / extraction_auto_extract / extraction_external_model / extraction_store_candidate / extraction_sensitive_categories`。
- 全部 append-only；按 `(subject_id, scope_id, consent_type)` 取最后一条事件为准，不 UPDATE 历史行。
- `extraction_sensitive_categories` 的 `metadata_json.categories` 保存敏感类别白名单；空列表表示不保存敏感类别。
- V1.2 的 `auto_enroll` 只写 `personal_memory`，不授予以上任何一项。

### 有效条件

有效条件是执行闸门，不是新的 consent 事件；拒绝某一能力不改写其他能力的记录。

| 动作 | 必须同时满足 |
|---|---|
| 捕获 | 私聊事件 + `personal_memory=GRANTED` + scope enabled + `extraction_capture=GRANTED` + `extraction.enabled=true` |
| 自动提取 | 捕获条件 + `extraction_auto_extract=GRANTED` + `extraction_external_model=GRANTED` |
| 保存候选 | 自动提取条件 + `extraction_store_candidate=GRANTED` |
| 敏感类别入候选 | 保存候选条件 + 类别在白名单 |

- `external_model` 未授予时自动提取不执行，不做本地模型兜底、不静默外发；`auto_extract` 的 consent 记录保持不变。
- `提取 开启` 不重开已关闭 scope；scope 关闭时返回 `AuthorizationDenied` 并提示先恢复 scope。
- `提取 开启 全部` 逐项追加四项 GRANTED 事件；部分失败必须报告已授予项与失败项。

### 命令面

| 命令 | 私聊 | 群聊 |
|---|---|---|
| `/织忆 提取 开启 <capture\|auto_extract\|external_model\|store_candidate>` | 允许（本人） | 拒绝 |
| `/织忆 提取 开启 全部` | 允许（本人） | 拒绝 |
| `/织忆 提取 关闭 <能力>` | 允许（本人） | 拒绝 |
| `/织忆 提取 敏感 <类别...>` | 允许（本人） | 拒绝 |
| `/织忆 提取 状态` | 允许（本人，无正文） | 拒绝 |
| `/织忆 候选 列表` | 允许（本人，无正文 ID 泄漏到其他 scope） | 拒绝 |
| `/织忆 候选 确认 <ID>` / `/织忆 候选 拒绝 <ID> [原因]` | 允许（本人） | 拒绝 |

- 群聊、群共享、Group personal 一律不捕获、不提取、不进入模型。
- 候选确认/拒绝仅用户命令，不注册 Agent 工具；批量/管理 API 在 V1.6。

### 端口

- `LlmExtractor`：平台无关模型端口；输入受限会话窗口与允许类别，输出结构化提取结果或 `None`；实现位于 Adapter，不进入 `core`。
- `ExtractionService`：应用服务；负责有效条件、任务幂等、候选写入、失败隔离与启动恢复；不依赖 AstrBot/SQLite 具体实现。
- `core` 不调用 LLM、不读取宿主事件对象；`adapters/astrbot` 只做事件捕获与命令翻译。

### 任务与候选

- `extraction_jobs` 状态：`PENDING / RUNNING / SUCCEEDED / SKIPPED_DUPLICATE_ACTIVE / FAILED / CANCELLED`。
- 幂等键：`(platform, tenant_id, scope_id, conversation_id, watermark, content_hash)`；重复提交不重复写入。
- 同键已有 `ACTIVE`：不创建候选，任务记 `SKIPPED_DUPLICATE_ACTIVE`，状态可见。
- 同键已有 `CANDIDATE`：幂等更新候选内容与来源，不新增行。
- 显式保存 / AGENT 工具写入撞到同键 `CANDIDATE`：返回 `Conflict`，要求先确认或拒绝；不静默替换。
- 候选不参与检索/注入；确认后才进入 FTS；拒绝记录原因；过期转 `ARCHIVED`，不物理删除（TTL 在 V1.6）。
- 模型失败、超时、输出不合法：任务失败/有限重试，聊天继续；不得传播为聊天失败。

### 配置

- 已实现键：`extraction_enabled=false`、`extraction_min_messages=5`、`extraction_max_retries=3`、`extraction_timeout_seconds=30`、`extraction_queue_size=100`。
- `extraction_min_messages` 按会话累计私聊消息触发；用户说“记住……”时立即提交当前缓冲。`extraction_queue_size` 超限时新任务记 `FAILED` + `last_error=queue_full`，不阻塞聊天。
- `extraction_include_assistant` 暂未实现；窗口默认只含用户消息。
- 关闭 `extraction_enabled` 后行为退回 V1.4；既有候选保留但不自动激活。

### Schema 与回滚

- schema v6，迁移 `006_extraction_jobs.sql`；新增 `extraction_jobs`，不修改既有正文表。
- 迁移前自动备份并校验；失败中止迁移。
- v6→v5 回滚守卫：`extraction_jobs` 为空，或保留表停用且不删除数据；回滚后关闭全部提取行为。

