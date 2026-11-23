# ADR-0015：V1.3 Agent 工具与调用者绑定契约

**状态**：Accepted（V1.1：AstrBot 4.29.4 不提供执行前 host `tool_call_id`，改用插件侧 evidence_ref）
**日期**：2026-11-23
**范围**：V1.3 M0

## 决策

1. V1.3 Agent 工具只服务 Personal Scope；scope 与 subject 只能由当前 AstrBot 私聊事件身份解析。工具参数不得包含任意 `scope_id`、`subject_id`、平台 ID 或群 ID。群聊、身份字段不完整、scope 关闭或 consent 未授予/已撤销时 fail-closed。
2. 工具名统一使用 `weave_memory_` 前缀：V1.2 已有 `weave_memory_remember`；V1.3 新增 `weave_memory_search`、`weave_memory_get`、`weave_memory_update`、`weave_memory_list`、`weave_memory_history`。不注册 `save`、`forget` 或 `clear` 工具。
3. 工具按事件身份解析 Personal Scope；无法解析身份时 fail-closed。本版不承诺跨插件/Persona 调用者隔离。工具 handler 绑定检查只用于确认 AstrBot 当前执行的是本插件实例注册的 handler，不作为调用者隔离承诺。
4. `weave_memory_remember/update` 只有在用户明确要求时才允许写入；写入固定 `source_type=AGENT`、`operator_type=agent`。AstrBot 4.29.4 不提供执行前 host `tool_call_id`，因此 `MemorySource.evidence_ref` 记录**插件侧证据键**：`tool:<tool_name>#message:<message_id>#sha256:<normalized_content_hash>`；`message_id` 不可用时退回 `session:<unified_msg_origin>`，两者都没有才 fail-closed。该值不冒充宿主 `tool_call_id`。`MemorySource.metadata` 同时记录 `origin=agent_tool`、`tool_name`、`session_id`、`message_id`、`content_sha256`、`requested_persistence` 与可选 `reason`。用户命令写入仍为 `EXPLICIT` / `USER`。
5. `weave_memory_update` 必须携带 `expected_revision`，通过 WriteCoordinator 乐观锁更新，且只修改正文，不修改 `MemoryType`；不得静默覆盖。
6. 读工具均先解析 Personal Scope，再经服务层执行 ConsentPolicy。`weave_memory_search` 复用 RecallService 并记录访问时间；`get` 只返回 ACTIVE，非 ACTIVE 记录统一按 NotFound 处理；`list` 在 SQLite 层分页；`history` 只返回当前 scope 内 revisions/sources。
7. 不改变 schema 版本，不新增迁移；复用 schema v1 已有的 `memory_revisions`、`memory_sources`、`memory_operations`。
8. 检索诊断以 JSON Lines 记录，每次检索一行，包含 `trace_id`、identity/scope 摘要、候选 ID/BM25 排名与过滤原因、最终注入 ID、Token 估算、query hash；默认不记录查询原文或记忆正文。默认保留 7 天；原文记录必须显式 opt-in，并受配置保留期与常见联系方式脱敏约束。

## AstrBot 4.29.4 后端源码核查

核查来源：用户上传的 `AstrBot-4.29.4.zip` 完整后端源码（2,154 个归档条目），重点文件：

- `astrbot/core/agent/runners/tool_loop_agent_runner.py`
- `astrbot/core/astr_agent_hooks.py`
- `astrbot/core/astr_agent_tool_exec.py`
- `astrbot/core/agent/tool.py`
- `astrbot/core/star/register/star_handler.py`
- `astrbot/core/star/star_manager.py`
- `astrbot/core/platform/astr_message_event.py`

核查结果：

- `ToolLoopAgentRunner._handle_function_tools` 的调用循环从 `llm_response.tools_call_name`、`tools_call_args`、`tools_call_ids` 三个并行数组取出 `func_tool_id`；该 ID 在 runner 局部用于 tool-call 消息、结果关联与结果消息。
- 执行前调用 `agent_hooks.on_tool_start(run_context, func_tool, valid_params)`，没有传入 `func_tool_id`（`tool_loop_agent_runner.py:1279-1284`）。
- `MainAgentHooks.on_tool_start` 只向 `OnUsingLLMToolEvent` 分发 `tool` 与 `tool_args`（`astr_agent_hooks.py:43-54`）；注册契约为 `event, tool, tool_args`。
- `FunctionToolExecutor._execute_local` 与 `call_local_llm_tool`（`astr_agent_tool_exec.py:733-737, 794-818`）没有接收或转交 `func_tool_id`；插件 handler 只接收 `event` 与工具参数。
- `MainAgentHooks.on_tool_end` 分发 `tool`、`tool_args`、`tool_result`，同样没有 ID。
- `OnLLMResponseEvent` 由 `MainAgentHooks.on_agent_done` 触发；ToolLoopAgentRunner 在工具循环结束、最终响应完成时调用该 hook，时序晚于工具执行，不能用于写入前关联当前 ID。
- `FunctionTool` 本身没有每次调用的 `tool_call_id` 字段。
- 后续 M2/M4 定向复核发现 `MemoryApplicationService.get_memory` 原先未过滤非 ACTIVE 状态；已修复为非 ACTIVE 统一抛出 `NotFound`，并添加归档记忆读取拒绝回归测试。
- `FunctionTool.handler_module_path` 用于保留 handler 来源（`agent/tool.py:51-55`）；插件加载时 `StarManager` 将原始 handler 包装为 `functools.partial(raw_handler, plugin_instance)`，并设置 `handler_module_path`（`star_manager.py:1281-1306`）。因此检查 `tool.handler.__module__` 不成立；本插件改为核验 `handler_module_path`、partial 原始函数及其绑定实例。
- `AstrMessageEvent.plugins_name` 存在；当其为非空白名单时可用于确认本插件在宿主启用插件集合中，但不代表调用者身份隔离。

## M0 结论与实施边界

AstrBot 4.29.4 确实在 runner 内部持有真实 host `tool_call_id`，但没有把它传给执行前 hook、FunctionToolExecutor 或插件 handler。`OnLLMResponseEvent` 又在工具执行之后触发，因此本插件无法在写入前可靠取得当前工具调用 ID。

同类记忆插件的共同做法是不依赖 host `tool_call_id`：天使之魂按事件 scope 直接写入；LivingMemory 记录 `session_id` + `triggered_by=agent_tool` + `tool_name` 元数据；MemoryCompanion 记录 `message_id` + `evidence` + 确定性内容 ID，并用内容指纹去重。织忆采用同一方向，但保留更明确的来源字段：

- `weave_memory_search/get/list/history` 可用；按当前事件身份解析 Personal Scope，并由服务层执行 ConsentPolicy 与 scope 隔离。
- `weave_memory_remember/update` 启用写入；来源记录插件侧 `evidence_ref` 与 metadata，不伪装成宿主 ID。
- `requested_persistence=false` 时不写入；`reason` 仅用于审计。
- 应用层 `save_agent/update_agent` 要求非空 `evidence_ref`，并可独立测试归因、Revision、Source 与乐观锁语义。
- 若宿主未来把 `tool_call_id` 暴露到工具执行前 hook 或 handler 上下文，可按同一接口把宿主 ID 作为更优先的 evidence_ref 写入，不改变存储模型。

## 后果与限制

- 不修改 AstrBot 后端，不依赖其私有运行时对象。
- 不承诺跨插件/Persona 调用者隔离；Personal 数据授权仍以当前消息事件身份、私聊边界、ConsentPolicy 和 Scope 为准。
- 本版启用 Agent 写入，但不新增 Agent 删除/清空工具；删除/清空仍只接受用户命令并二次确认。
- 真实 AstrBot 宿主烟测仍未执行；当前结论由自动化测试与 AstrBot 4.29.4 源码核查支撑。

## 修订记录

- V1.1（2026-11-23）：AstrBot 4.29.4 不提供执行前 host `tool_call_id`；改为插件侧 `evidence_ref`（message_id 优先、session 回退、内容哈希）并启用 `remember/update` 写入；补充 `requested_persistence`/`reason`、宿主无工具时的注册降级与对应测试。影响面：决策 4、M0 结论、后果与限制。
- V1.0（2026-11-23）：初始版本；因宿主未暴露 `tool_call_id` 将写工具设为 fail-closed。
