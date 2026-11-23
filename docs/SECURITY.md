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

`memory_consents` 自 V1.2 起采用 **默认登记 + opt-out**：`auto_enroll=true`（默认）时，首次符合条件的私聊事件自动建立 `personal_memory=GRANTED`，并发送一次告知；`auto_enroll=false` 时回到 fail-closed，必须显式 `/织忆 授权`。任何已存在的 `GRANTED` 或 `REVOKED` 事件都优先，auto_enroll 不改写用户已做出的决定。V0 的显式保存仍不能被理解为对未来自动提取、外部模型处理或群共享的授权。

V1.2 起默认登记；`/织忆 授权` 仍是显式 opt-in/恢复命令，`/织忆 撤销授权` 停止保存、主动检索与自动注入但不自动删除历史。Consent 事件 append-only，按追加顺序取最后一条；不再用时间戳决定先后。详见 ADR-0005、ADR-0012。

## 4. 来源可信度

- `EXPLICIT`、`AGENT` 与 `INFERRED` 分开记录。
- V1.2 增加受约束的模型工具写入（`source_type=AGENT`）；后台自动提取仍不启用。
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

## 10. V1.1 Group shared 安全边界（M0 契约）

- 群身份用六元组；`tenant_id` 是平台实例，群 ID 只在同一平台实例内解释；不同实例/平台同名群不共享 Scope。
- 管理员来源 `PLATFORM_ROLE/EXPLICIT_CONFIG/NONE`；无来源不得 enable/写入/管理；所有管理/写入按事件 sender 重新解析，不接受 `admin_id` 参数。
- `group_admin_ids` 回退配置按 `platform + tenant_id + group_id` 隔离，不允许全局跨群复用。
- 普通成员只能读当前群；`群列表/查询` 需要 enabled + GRANTED，`群状态` 可在 disabled 读但不含正文；不能开启/关闭/清空/销毁/保存；错误不得泄漏其他群存在性。
- `群列表/查询` 仅在 `is_enabled=1` 且 `group_memory=GRANTED` 时允许；disabled/撤销后拒绝。
- `群状态` 只返回 `created/enabled/count/admin_source/admin_available`，`count` 只统计 ACTIVE；不返回记忆正文或管理员 ID 列表；无 Scope 时 `created=false`。
- `群销毁` 是管理员显式破坏性操作：必须先关闭、必须确认；服务内自动备份并校验，写 sidecar manifest；删除顺序为记忆/关联 → consent 历史 → operations 审计 → scope；备份失败中止；不可恢复。
- 群数据对群成员可见是预期；个人记忆不进群、群记忆不出现在私聊；FTS/读取加 `scope_type=GROUP` 防御过滤。
- 个人事实口径（主人 2026-11-23 确认）：V1.1 不做 NLP 检测，采用管理员输入责任；成员不能写、个人记忆不自动提升；V2.4 再做 Group personal 与敏感过滤。该风险由主人接受，P1-4 降为 P2。
- 管理员来源丢失后：管理/写入 fail-closed；已启用群的成员读取/注入保留，记录 warning；保留 `EXPLICIT_CONFIG` 恢复路径。若要求更严格，M0 复审可改为读也 fail-closed。
- 群解散/退群只做 disable + revoke + 数据保留；成员个人删除/退群在 V2.4。
- v5 迁移前备份；有群数据拒绝回滚；V1.0 不能直接打开 v5；群关闭 ≠ schema 回滚。
- V1.1 无导出；不得宣称已实现群导出/完整审计。

## 11. V1.2 Personal 默认登记与 opt-out

- `auto_enroll` 默认 `true`：新用户或没有任何 consent 记录的用户，首次符合条件的私聊事件自动建立 `personal_memory=GRANTED`；插件发送一次告知，不经过 LLM。`auto_enroll=false` 回到逐用户 `/织忆 授权`。
- auto_enroll 只在 scope 已启用且 consent 历史为空时生效；已有 `GRANTED`/`REVOKED` 事件优先，`/织忆 撤销授权` 与 `/织忆 关闭` 不会被下一次对话复活。`/织忆 清空` 只删数据，不重新开启已关闭 scope。
- 群共享记忆不参与 auto_enroll；仍由管理员 `/织忆 群开启` 显式开启，个人与群隔离不变。
- 新增模型工具写入：只在用户明确提到长期偏好、背景或目标时调用；来源固定 `source_type=AGENT`、`operator_type=agent`，只写记忆正文，不保存完整聊天原文。工具受 Consent、scope enabled 与 `agent_write_enabled` 约束。
- 后台自动提取仍只产 `INFERRED` 候选，不自动升级 `ACTIVE`；本次变更不等于开启无确认的推断记忆。
