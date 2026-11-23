# V1.6 宿主烟测记录（2026-11-23）

状态：通过（核心只读管理 API 与 v7 迁移）；候选 TTL 的活体创建/过期未在宿主执行，由本地测试覆盖。

> 公开仓版本已脱敏：真实群号使用占位符。

## 环境

- AstrBot 4.29.4，dashboard `http://localhost:6185`，用户名 `astrbot`（临时密码由主人提供，未写入本记录）。
- 运行插件目录：`D:\astrbot\AstrBotLauncher-0.1.5.6\AstrBot\data\plugins\astrbot_plugin_weave_memory`。
- 插件数据目录：`D:\astrbot\AstrBotLauncher-0.1.5.6\AstrBot\data\plugin_data\astrbot_plugin_weave_memory`。
- 无头浏览器：Playwright + 本机 Microsoft Edge（headless）；脚本与截图在仓库外的 `D:\astrbot\temp\weave_smoke_v16\`。

## 部署

- 使用 `Weave_Memory_Engine_dev/dist/astrbot_plugin_weave_memory` 构建包同步运行目录。
- 同步前备份：`data/plugin_backups/weave_memory_runtime_20261123_223304`。
- 同步后 `dist` 与运行目录逐文件一致（排除 `__pycache__`）：无缺失、无多余、无内容差异。
- 通过 dashboard `POST /api/v1/plugins/astrbot_plugin_weave_memory/reload` 重载，返回 200「重载成功。」。

## 迁移与注册

- 日志确认插件重新加载为 `v1.0.0-dev.1`。
- 迁移前自动备份：`migration_backups/weave_memory_v6_to_v7_20261123T143601760029Z.sqlite3`。
- 日志确认「织忆只读管理 API 已注册。」，9 个路由注册成功。
- 只读核对运行库：`schema_migrations` 最大版本 7，`memories.candidate_expires_at` 存在，`CANDIDATE` 行 0。

## 管理 API 结果

请求均来自已登录 dashboard 的浏览器上下文（携带 JWT cookie），经宿主扩展路由
`/api/v1/plugins/extensions/astrbot_plugin_weave_memory/v1/...`：

| 端点 | 结果 |
|---|---|
| `GET /v1/overview` | 200；记忆 2（ACTIVE 2；FACT 1/PREFERENCE 1；EXPLICIT 1/AGENT 1）；候选 0；索引 healthy，documents 1；最近操作 5 条；响应无正文 |
| `GET /v1/groups` | 200；1 个群 scope（`aiocqhttp/default/<group-id>`，enabled，`PLATFORM_ROLE`） |
| `GET /v1/memories?platform=aiocqhttp&tenant_id=default&group_id=<group-id>` | 200；`total=0`，`items=[]`（该群当前无 ACTIVE 记忆） |
| `POST /v1/retrieval/debug`（同上群，query 为通用问题） | 200；字段 `candidates/estimated_tokens/injected_ids/omitted/omitted_count/query_hash`；候选 0、注入 0、Token 0；响应不含 `content` |
| `GET /v1/settings` | 200；含 `candidate_ttl_days=7`、`rejected_keep_body=false` 等公开键；不含 `api_admin_usernames` 名单 |
| `GET /v1/maintenance/status` | 200；`schema_version=7`、`secure_purge_pending=false`、索引 healthy |
| `GET /v1/memories?scope=PERSONAL&...` | 403 `AuthorizationDenied` |
| 未登录 `GET /v1/overview` | 401（`Missing API key`） |

## CLI

- `weave_admin.py groups`：列出上述已启用群。
- `weave_admin.py maintenance-status`：schema 7、索引 healthy、候选 0、`secure_purge_baseline_done=true`。
- `weave_admin.py candidates --user astrbot ...`：`total=0`、`archived=0`（只读候选列表，未产生写操作）。

## 限制

- 该群当前没有 ACTIVE 记忆，因此群记忆列表与检索调试只验证了鉴权/字段/无正文，未验证真实注入内容；个人正文按设计不经 API。
- 宿主没有执行候选 TTL 的活体创建与过期（需要私聊提取链路与模型调用）；该行为由本地 `test_candidate_lifecycle.py` 覆盖。
- dashboard 临时密码仍处于启用状态，烟测后建议主人改回强密码或恢复原密码。
- 未验证真实集群/多进程并发、SSE 与 dashboard 前端页面交互（本次直接调用只读 API）。
