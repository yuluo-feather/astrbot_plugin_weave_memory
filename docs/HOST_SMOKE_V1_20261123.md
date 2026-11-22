# 织忆 V1.0 真实 AstrBot 宿主烟测（2026-11-23）

> 历史记录：当前命令前缀为 `/织忆`；下文 `/记忆 …` 为当时执行记录。

环境：AstrBot v4.29.4，插件版本 `v1.0.0-dev.1`，schema v4，WebUI `http://localhost:6185`。

- 测试前已用 SQLite backup API 备份 v0.5 数据库（备份留在源 review workspace，不随本仓库迁移）。
- 已完成 v3 → v4 migration（新增 `memory_relations`，当前 0 行）。
- 自动化复核：`python -m pytest -q` → 94 passed（Python 3.12.10）；`compileall` 通过。

## 加载日志

```text
[2026-11-23 02:21:31] Terminating plugin astrbot_plugin_weave_memory ...
[2026-11-23 02:21:32] Plugin astrbot_plugin_weave_memory (v1.0.0-dev.1) by Weave Memory Engine: 独立的个人记忆引擎，支持显式保存、检索和私聊上下文注入。
[2026-11-23 02:23:18] Loading plugin astrbot_plugin_weave_memory ...
[2026-11-23 02:25:19] Loading plugin astrbot_plugin_weave_memory ...
```

## 8 步验证

| # | 操作 | 结果 |
|---|---|---|
| 1 | `/记忆 授权` | 返回“已授权个人记忆……”。 |
| 2 | `/记忆 保存 V1.0烟测：我喜欢乌龙茶` | 返回新 ID `59d27b95ce3b46019670e9eeb862de11`，索引已同步。 |
| 3 | `/记忆 列表` | 新记忆显示 `[revision 1]`。 |
| 4 | `/记忆 更新 <ID> 1 V1.0烟测：我喜欢乌龙茶（更新）` | 返回 `revision 2`；再次用旧 revision 1 更新返回 `Conflict`，列表仍为 revision 2。 |
| 5 | 新会话询问“V1.0 烟测里我喜欢什么茶” | 返回“乌龙茶”，异步临时上下文注入生效。 |
| 6 | `/记忆 查询 V1.0烟测` | 返回更新后的记忆，`last_accessed_at` 已写入。 |
| 7 | `/记忆 撤销授权` | 列表返回“尚未授权或个人记忆空间已关闭”；新会话回答“不知道”；日志出现 `AuthorizationDenied` 注入跳过。 |
| 8 | 重新授权 + 插件热重载 | 重载后列表仍返回 revision 2 的记忆，`/记忆 查询` 正常，数据持久化。 |

## 附加验证

- **关闭流程**：在测试用户 `smoke_user_b` 上执行 `/记忆 关闭 确认`。返回永久关闭提示；随后列表失败；数据库中该 Scope `is_enabled=0`，Consent 追加 `REVOKED/scope_disabled`，原有记忆仍保留。未对 `astrbot` 主测试账号执行关闭。
- **FTS 故障**：把 `fts_dirty_ids` 标记为脏后发送普通聊天，宿主正常回复；日志出现 `织忆本轮未注入记忆：IndexUnavailable`；恢复 `[]` 后查询恢复。
- **注入异常隔离加固**：注入 hook 改为捕获任意异常并记录类型，避免宿主 API 的意外异常打断聊天；加固后重新加载并复核列表与异步注入均正常。

## 测试数据

- `astrbot` Scope：`7057abac90894686addfea2bc4ffe388`（黑咖啡，revision 1）、`59d27b95ce3b46019670e9eeb862de11`（V1.0 乌龙茶，revision 2）。
- `smoke_user_b` Scope：`3a9ac1f353234c9081ccd4f011b09d4d`（京都，revision 1），该 Scope 已被 `/记忆 关闭 确认` 永久关闭，数据保留。
- `memory_relations` 为空；`fts_dirty_ids=[]`。

## 限制

- 未执行 `/记忆 清空 确认`；关闭只在测试用户 B 上执行。
- `rollback_to_v05()` 未在宿主执行：它是开发代码接口，不是插件管理命令。
- 真实 Provider 注入以新会话回答为证据，未做 token 级请求抓包。
- 本轮是 `v1.0.0-dev.1` 预览验证，不替代最终验收签署。
