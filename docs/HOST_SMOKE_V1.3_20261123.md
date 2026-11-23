# 织忆 V1.3 真实宿主烟测（私聊，2026-11-23）

**环境**：AstrBot `4.29.4`，插件 `v1.0.0-dev.1`，运行目录同步到 `958291a`，SQLite schema v5（`group_scopes_admin`），WebUI `http://localhost:6185`。
**方式**：WebUI 登录后通过 `POST /api/chat/send` 的 webchat 会话发送命令与自然语言；临时登录口令未记录。插件在 19:45 重载，6 个 `weave_memory_*` 工具均注册。
**测试数据**：仅使用合成内容 `织忆V13烟测-20261123-*` / `织忆V13烟测2-20261123-*`；测试结束已删除，数据库剩余 0 条。

## 1. 命令链

| 步骤 | 命令 | 结果 |
|---|---|---|
| 1 | `/织忆 帮助` | 通过；新文案含 `evidence_ref`，无 `/记忆` |
| 2 | `/织忆 状态` | `scope=PERSONAL`、`enabled=true`、`consent=GRANTED`、索引健康 |
| 3 | `/织忆 统计` | 含 `scope_type` 与 `recent_operations` |
| 4 | `/织忆 保存 <合成内容>` | 返回 32 位 ID，索引已同步 |
| 5 | `/织忆 列表` | 包含测试 ID 与正文 |
| 6 | `/织忆 查询 黑麦茶` | 命中测试记忆 |
| 7 | `/织忆 更新 <ID> 1 <新内容>` | revision 2 |
| 8 | `/织忆 更新 <ID> 1 <旧内容>` | `revision conflict`，未覆盖 |
| 9 | `/织忆 操作记录` | 不含正文；当前仅生命周期操作（DELETE/CLEAR/DISABLE） |
| 10 | `/织忆 导出 json` | `format=weave-memory-export-v1`，包含测试记忆 |
| 11 | `/织忆 删除 <ID> 确认` | 删除成功，后续查询不返回 |

未在主人账号执行 `/织忆 清空 确认`、`/织忆 关闭 确认`。

## 2. Agent 工具

Persona `主人格3.0-chat` 白名单已包含全部 6 个工具：

`weave_memory_remember`、`weave_memory_search`、`weave_memory_get`、`weave_memory_update`、`weave_memory_list`、`weave_memory_history`。

| 工具 | 触发 | 结果 |
|---|---|---|
| `weave_memory_remember` | “请记住：织忆V13烟测2-20261123-白茶……” | 写入成功；`source_type=AGENT`、`operator_type=agent` |
| `weave_memory_search` | “请使用 weave_memory_search 检索……” | 返回测试记忆 ID/正文 |
| `weave_memory_list` | “请使用 weave_memory_list 列出我的记忆” | 返回 total/items，包含测试记忆 |
| `weave_memory_get` | “请使用 weave_memory_get 读取 <ID>” | 返回 ACTIVE、revision 1 |
| `weave_memory_history` | “请使用 weave_memory_history 查看 <ID> 历史” | 返回 revision 与 AGENT 来源 |
| `weave_memory_update` | “请使用 weave_memory_update 更新 <ID>，expected_revision=1” | revision 2；来源 `tool:weave_memory_update#message:...` |
| stale update | 再次 `expected_revision=1` | `revision conflict`，未覆盖 |

写入来源实测：

- `source_type=AGENT`
- `operator_type=agent`
- `evidence_ref` 形如 `tool:<tool_name>#message:<message_id>#sha256:<content_hash>`
- `metadata_json` 含 `origin=agent_tool`、`tool_name`、`message_id`、`session_id`、`content_sha256`、`requested_persistence`、可选 `reason`

## 3. 限制与未覆盖

1. **跨账号隔离未做真机**：没有第二个测试账号；自动化测试已覆盖跨用户/群聊事件拒绝。
2. **群聊未做真机**：WebUI webchat 是私聊；V1.1 群共享烟测仍需真实群聊或测试群。
3. **破坏性个人命令未在主人账号执行**：清空/关闭仍需专用测试账号。
4. **操作审计范围**：`memory_operations` 按 V1.0 设计只审计删除/清空/关闭/群生命周期；SAVE/UPDATE 不进入审计表。
5. **注入 token 级证据**：未抓取 Provider 请求层的 `TextPart.mark_as_temp()` 细节。
6. **模型工具选择受 Persona 白名单影响**：白名单未包含的工具不会下发给模型；本次已补齐 6 个。

## 4. 结论

- V1.3 私聊命令链与 Agent 读写工具在真实 AstrBot 4.29.4 宿主通过；
- 插件侧 `evidence_ref` 与来源 metadata 按 ADR-0015 落库；
- V1.3 仍不能标记完全完成：群聊/跨账号真机烟测与正式验收待补。
