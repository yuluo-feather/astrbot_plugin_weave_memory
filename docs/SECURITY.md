# SECURITY — V1.0 执行契约

状态：已按用户确认的既有审查结果作为 V0 执行基线；V0.2 决策见 ADR-0002  
依据：V0「起针」计划书 §4、§7–§10；总计划书 §3、§5、§8–§9。

## 1. 安全原则

- 默认最小权限；Consent 默认 fail-closed。
- 身份解析失败、作用域不匹配或授权状态不明时拒绝读写。
- `scope_id` 是可见性边界，不是用户 ID；`subject_id` 是记忆描述对象，不是授权主体。
- 身份键保留 `platform + tenant_id + scope_type + scope_external_id + subject_type + subject_id`。
- 先做身份/授权过滤，再检索、排序和注入；不得先把跨作用域候选交给上层再过滤。
- 注入失败不得影响宿主聊天；记忆层故障按无记忆处理。

## 2. V0 身份与作用域

- 平台固定 AstrBot。
- V0 只支持 Personal。
- V0.5 已使用 `get_platform_name()`、`get_platform_id()`、`get_sender_id()`；platform instance ID 纳入 Tenant 隔离。
- 群聊不读写记忆；不得因事件来自群聊而落入 Personal。
- 无法确认私聊身份时 fail-closed。

## 3. Consent

`memory_consents` 默认 fail-closed。必须在执行保存、检索、注入等动作前判定对应 Consent。V0 的显式保存不能被理解为对未来自动提取、外部模型处理或群共享的授权。

V0 使用单一 `personal_memory` Consent：无记录默认拒绝；`/织忆 授权` 开启保存、主动检索与自动注入；`/织忆 撤销授权` 停止这三类普通功能但不自动删除历史。Consent 事件 append-only，按追加顺序取最后一条；不再用时间戳决定先后。详见 ADR-0005。

## 4. 来源可信度

- `EXPLICIT` 与 `INFERRED` 分开记录。
- V0 只实现用户显式保存，不启用自动提取。
- 不得将助手输出或工具输出伪装为用户显式陈述。
- `operator_type` 与 `source_type` 分开保存。

## 5. 内容与敏感数据

总计划书强调隐私最小化；V0 未实现敏感类别枚举、内容过滤或数据库加密。运行日志只记录错误类型，不记录记忆正文。仍需决定：

- V0 是否接受用户显式输入的敏感个人信息，若接受需怎样提示/限制。
- 正文、来源、异常、调试日志的原文保留策略。
- SQLite 文件、备份文件的本地访问权限责任边界。
- 数据库损坏、文件复制、日志泄漏的威胁模型。

不得自行宣称数据库加密或端到端加密已经具备。

## 6. 删除与审计

删除/清空在权威事务中先写不含正文的最小 tombstone audit，再物理删除正文、Revision、Source、Tag，并标记 FTS dirty。索引未同步时检索 fail-closed。审计字段白名单见 ADR-0004。

物理擦除按 ADR-0008：所有由本应用打开的 SQLite 连接启用 `PRAGMA secure_delete = ON`；删除/清空在同一事务内写入 `secure_purge_pending`；仅在 FTS 同步/重建成功且索引 `HEALTHY` 后执行 `wal_checkpoint(TRUNCATE)` → `VACUUM` → `wal_checkpoint(TRUNCATE)`，两次 checkpoint 的 `(busy, log)` 都必须为 0，否则保留 pending 并报告未完成。升级时若 `secure_purge_baseline_done` 缺失，启动执行一次基线擦除。该保证以“应用自身连接、无其他读快照、checkpoint 成功”为条件；不承诺 SSD、操作系统缓存、文件系统空闲块或备份层面的取证级擦除，也不等同于数据库加密。

## 7. Scope 关闭

- 关闭采用停写 + 停普通读取/检索/注入语义，不物理删除正文；V0 关闭是永久的，不提供重新开启命令。
- 管理审计仍保留显式访问例外；V0 尚未提供管理 API/UI。
- V0 没有注入缓存，后续若引入必须补失效机制。

## 8. V0 明确不做

- 群共享记忆与群管理员权限。
- 自动提取与 LLM 处理。
- 外部 Embedding / Rerank Provider。
- WebUI、远程管理 API、多实例。


## 9. V1.0 执行补充

- 查重与更新都必须在指定 Personal Scope + subject 下完成，不允许仅凭 memory ID 暴露跨用户存在性。
- 显式更新必须经过当前 Consent，并验证目标记忆属于调用者 Scope。
- FTS 检索先做 Scope、Subject、Consent、Scope enabled、状态及语义有效期过滤，再排序与 LIMIT。
- 记忆正文继续视为不可信参考文本；不得作为系统/开发者指令。


### V1.0 关闭故障语义

Scope 关闭与 Consent 撤销是两个顺序提交步骤，非跨表单事务。先关闭 Scope，确保 Consent 事件写入失败时仍 fail-closed；随后追加撤销事件。失败需显式报错，使用相同 operation ID 重试可补齐 Consent 撤销。任何时候都不得因撤销事件写入失败而恢复 Scope 可访问状态。


### V1.0 注入竞态收口

Recall 候选查找与上下文注入之间可能发生 Consent revoke 或 Scope disable。V1.0 在访问记录写入的 Coordinator 临界区重新校验授权与 Scope，并在异步写入完成后、返回上下文前再次同步校验；拒绝时不返回上下文。该策略确保单事件循环内不会把关闭/撤销前已取出的候选在关闭/撤销后注入。


### V1.0 写入授权二次校验

显式保存与显式更新在进入用例时检查 Consent，并在 WriteCoordinator 临界区内再次检查，避免“入口检查后等待写锁期间撤销授权”仍完成写入。
