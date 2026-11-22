# ADR-0005：V0.5 Personal Consent 与 AstrBot 私聊注入

- 状态：Accepted for V0.5 implementation
- 日期：2026-11-22
- 来源：V0「起针」计划书 §4、§8；AstrBot 官方 v4.29.4 发布与插件 API 文档。

## 决策

1. V0 使用单一 `personal_memory` consent，默认无记录即拒绝。授权后允许显式保存、主动检索与自动注入；撤销后这三类普通功能全部停止，但不自动删除既有数据。
2. Delete/Clear 始终可用于用户主动清理数据，即使 Consent 已撤销；Scope Disable 是更强的关闭操作，会停止普通读写/检索，但保留数据供管理审计。
3. Consent 采用 append-only 事件记录；最新事件决定当前状态；相同时间戳时 REVOKED 优先。SQLite schema migration v3 新增 `memory_consents`。
4. AstrBot 身份使用 `get_platform_name()`、`get_platform_id()`、`get_sender_id()`；`platform_id` 作为 Tenant 隔离平台实例；Scope 只接受私聊事件（`message_obj.group_id` 必须为空）。身份字段缺失或群聊事件 fail-closed。
5. AstrBot 目标版本暂定 `>=4.29.4,<4.30`，宿主 Python `>=3.12`。4.29.4 是 2026-11-22 可确认的稳定发布版本；后续升级需重新验证插件 API。
6. 每轮动态记忆不修改 `req.system_prompt`，而是追加 `TextPart(...).mark_as_temp()` 到 `req.extra_user_content_parts`，避免将记忆写入会话历史；使用官方 `StarTools.get_data_dir()` 获取插件独立数据目录。
7. V0 注入预算采用 provider-neutral estimate：CJK 字符和标点各计 1，ASCII 字母/数字连续段每 4 字符估 1 token；前缀/后缀均计入；默认上限 384 estimated tokens、最多 4 条。该值是保守预算估算，不等同任一 Provider tokenizer 的精确 token 数。
8. 记忆内容视为不可信数据，XML 特殊字符转义，并在上下文块中明确禁止覆盖系统/开发者指令。
9. V0 只使用本地 SQLite/FTS5，不调用外部模型进行自动提取、分类或重排；不处理群聊、不做自动记忆写入。

## 关键保证

- 未授权用户不会存储、检索或注入个人记忆。
- 授权撤销立即阻断后续保存、检索与注入，但不会隐式删除历史数据。
- 不同平台实例、不同发送者之间的 Personal Scope 隔离。
- 群聊不映射为 Personal Scope。
- 注入仅作用于当前 LLM 请求，不进入持久会话历史。
- 注入失败被隔离，不阻断宿主聊天。

## 验证边界

本工作区使用 AstrBot API stubs 对插件加载、命令处理、身份边界、私聊注入、撤销授权、重载持久化与故障隔离进行自动化测试。真实 AstrBot v4.29.4 运行环境中的端到端烟测仍需在宿主实例执行，不能以 stub 测试代替。
