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
- `群销毁` 是管理员显式破坏性操作：必须先关闭、必须确认；默认服务内自动备份并校验，写 sidecar manifest；删除顺序为记忆/关联 → consent 历史 → operations 审计 → scope；备份失败中止；被销毁群不可恢复。`群销毁 无备份 确认` / CLI `--no-backup` 不生成 sqlite 备份，写 `backup_mode=SKIPPED` 与 warning 审计；销毁备份默认保留 30 天，只用于审计与其他 scope 灾难恢复，不能复活被销毁群。
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
- V1.3 Agent 读工具仅访问当前 Personal Scope；事件身份无法解析、群聊、Consent 未授予/已撤销或 scope 关闭时 fail-closed。handler 绑定检查使用 AstrBot 的 `handler_module_path` 与 `functools.partial` 插件实例，不承诺跨插件/Persona 调用者隔离。
- AstrBot 4.29.4 的工具执行前 Hook 不暴露 `tool_call_id`，完成后的 LLMResponse Hook 时序太晚；V1.3 写工具改用插件侧 `evidence_ref`：`tool:<name>#message:<message_id>#sha256:<content_hash>`，缺 message_id 时退回 `session:<unified_msg_origin>`，两者都没有才 fail-closed。该值不冒充宿主 ID。`MemorySource.metadata` 记录 `origin=agent_tool`、`tool_name`、`session_id`、`message_id`、`content_sha256`、`requested_persistence` 与可选 `reason`；命令写入继续标记 `EXPLICIT` / `USER`。
- Personal/Group 检索诊断写入插件数据目录 `recall_diagnostics/recall-YYYY-MM-DD.jsonl`，每请求一行，含 trace_id、identity/scope 摘要、候选 ID/BM25 排名/过滤原因、最终注入 ID、Token 估算与 query hash；默认不写查询原文或记忆正文，默认保留 7 天。查询原文需显式 opt-in，并对邮箱、手机号与长数字串做常见模式脱敏；该脱敏不保证识别所有个人信息。
- 后台自动提取仍只产 `INFERRED` 候选，不自动升级 `ACTIVE`；本次变更不等于开启无确认的推断记忆。

## 12. V1.5 自动提取安全边界（M0 契约）

依据：V1.5 计划书 §4、§9；ADR-0015。

- 默认关闭：全局 `extraction_enabled=false`；V1.2 默认登记与 V1.3 Agent 工具写入都不构成捕获/提取/外部模型授权。
- 分层 opt-in：`capture / auto_extract / external_model / store_candidate` 分别记录、append-only；`external_model` 未授予时自动提取不执行，不静默外发。
- 捕获范围：仅 `scope_type=personal` 的私聊；群聊、群共享、Group personal 一律不捕获、不进入窗口或 LLM。
- 闸门顺序：身份 → scope 启用 → `personal_memory` 与 `extraction_*` 授权 → 敏感预过滤 → 窗口 → `LlmExtractor`。不得先建窗口或调用模型再过滤。
- 敏感前置过滤：凭据/密码/令牌/API key、身份证件号、银行卡号、精确住址默认拦截或脱敏；无法可靠识别但疑似敏感的内容默认不送外部模型；不记录正文到运行日志。
- 敏感类别进入候选必须命中 `extraction_sensitive_categories` 白名单；白名单为空时一律不保存敏感类别。
- 候选隔离：`CANDIDATE` 不参与检索/注入/导出；只有用户确认后转 `ACTIVE` 并进入 FTS。
- 失败隔离：模型超时、输出不合法、任务失败均不阻塞聊天；任务错误只记录类型化摘要，不记录正文。
- 安全验收：Sensitive Leakage Rate 必须为 0；Sensitive Leakage 与错误归属不能被其他平均指标抵消。
- 候选确认/拒绝仅用户命令；V1.5 不注册 Agent 候选工具，避免模型代替用户确认推断内容。

## 13. V1.6 候选与只读管理 API 安全边界（M0 契约）

依据：V1.6 计划书 §5.1；ADR-0016。

- 管理 API 通过宿主 `register_web_api` 注册；插件不自行监听端口，不引入第三方依赖。
- 宿主只透传 dashboard 登录用户名，不提供平台身份或角色；因此 API 全部只读，不映射 dashboard 角色到平台权限。
- 所有端点要求登录用户名存在；`api_admin_usernames` 非空时用户名必须在白名单。纯私聊、零群部署仍可读实例级端点，不得自举死锁。
- 群作用域数据（candidates/memories/history/retrieval debug）要求该群 `is_enabled=1` 且 `group_memory=GRANTED`；拒绝时不泄漏其他群存在性或正文。
- 个人 scope 经 API 一律 `AuthorizationDenied`；Overview 只返回聚合计数，不返回个人正文、ID 或候选内容。
- 检索调试必须显式指定群 scope；返回候选 ID、评分、过滤原因、最终注入 ID、Token 估算与截断原因，不返回正文、不更新访问时间、不写检索诊断/审计。
- `GET /v1/settings` 只返回非敏感公开配置，不返回 `api_admin_usernames`、`group_admin_ids` 等名单。
- 候选确认/拒绝、群 enable/disable、个人 opt-out 全部走命令或 dashboard 原生配置页，API 不提供写端点。
- 拒绝默认物理清理正文，只保留不含正文的 `CANDIDATE_REJECT` 审计；`rejected_keep_body` 显式开启才保留。
- API 失败按领域错误映射 HTTP 状态，不阻塞聊天，不扩大日志正文暴露面。

## 14. V1.7 导入导出安全边界（ADR-0017）

- 导入文件是不可信输入：只接受 UTF-8 JSON，限制 5 MiB / 5000 条；只解析，不执行，不导入 Python/脚本内容。
- 文件内的 `scope_id` / `subject_id` 被忽略；作用域始终由调用者解析。目标 Personal 拒绝 Group 文件，目标 Group 拒绝 Personal 文件；跨平台导入按 `source_platform` 记入幂等键。
- 导入只创建新记忆，走 `WriteCoordinator` 与 consent guard；不更新已有记忆、不确认候选、不覆盖冲突内容。
- 导入失败/回滚只删除本次任务创建的 `memory_id`，不触碰既有记忆；回滚不依赖备份。
- 非 ACTIVE 条目只写不含正文的 `IMPORT_METADATA` 审计；`memory_operations` 不写正文、不写完整来源文件。
- 群导入/导出要求群管理员（命令）或显式 dashboard 管理员白名单（API），且群 `is_enabled=1` + `group_memory=GRANTED`。
- 管理 API 的 `GET /v1/export` 与 `POST /v1/import` 是 V1.6 只读边界外的受控写/读端点：`api_admin_usernames` 为空即拒绝（fail-closed），个人 scope 一律拒绝，群正文不经未授权用户。
- 导出默认只含 ACTIVE 正文；`all` 的其他状态只含元数据。不导出 DELETED、CANDIDATE 或内部序号。


## 15. V1.8 账本与备份恢复安全边界（ADR-0018）

- `memory_ledger.sqlite3` 与主库同目录但独立文件；备份包不包含账本，恢复不覆盖当前账本。
- 删除/清空/consent/群销毁的意图先落账本；主库失败时保持 pending，不向用户报告“已删除”。
- 备份配置 `config.json` 递归脱敏：键名含 token/api_key/apikey/password/secret/credential 的值替换为 `***`；不导出账本、群销毁备份或恢复前快照。
- 恢复要求显式确认；命令仅私聊，管理 API 要求非空 `api_admin_usernames` 白名单且 `confirm=true`。
- 恢复按 ledger 排除删除/清空/撤销授权/附件删除/群销毁；旧备份不能复活这些内容。V1.8 之前的旧备份缺少 `destroy_scope`/`remove_attachment` 事件，文档显式声明不保证。
- 备份哈希/schema/`ledger_id`/seq 覆盖任一校验失败即拒绝恢复；journal 损坏或缺失 fail closed，不产生半恢复可读窗口。
- 恢复前快照用于失败回滚；恢复全程在写租约内完成，读/检索/注入不与本进程恢复并发。
- 物理擦除仍为 SQLite 级（`secure_delete` + checkpoint/VACUUM），不是 SSD 取证级保证；账本与主库同盘同时丢失时无法保证旧备份排除删除。
