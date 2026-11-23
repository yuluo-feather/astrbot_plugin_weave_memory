# V1.7 宿主烟测记录（2026-11-23）

状态：通过（个人命令导入导出的完整链路、v8 迁移、管理 API 失败/鉴权路径）；群命令与管理员白名单下的 API 成功路径未在宿主执行。

## 环境

- AstrBot 4.29.4，dashboard `http://localhost:6185`，用户名 `astrbot`（临时密码由主人提供，未写入本记录）。
- 运行插件目录：`D:\astrbot\AstrBotLauncher-0.1.5.6\AstrBot\data\plugins\astrbot_plugin_weave_memory`。
- 插件数据目录：`D:\astrbot\AstrBotLauncher-0.1.5.6\AstrBot\data\plugin_data\astrbot_plugin_weave_memory`。
- 烟测脚本与结果：`D:\astrbot\temp\weave_smoke_v17\`。

## 部署

- 使用 `Weave_Memory_Engine_dev/dist/astrbot_plugin_weave_memory`（`f53f34b`）同步运行目录。
- 同步前备份：`data/plugin_backups/weave_memory_runtime_20261123_230554`；日志文案修正后再次备份：`data/plugin_backups/weave_memory_runtime_20261123_231008`。
- 同步后 `dist` 与运行目录逐文件一致（排除 `__pycache__`）：无缺失、无多余、无内容差异。
- 通过 dashboard `POST /api/v1/plugins/astrbot_plugin_weave_memory/reload` 重载，返回 200「重载成功。」。

## 迁移与注册

- 迁移前自动备份：`migration_backups/weave_memory_v7_to_v8_20261123T150620886910Z.sqlite3`。
- 日志确认「织忆管理 API 已注册。」。
- 运行库只读核对：`schema_migrations` 最大版本 8；`import_jobs` 存在；`memory_sources.evidence_ref` 索引存在。
- 烟测结束后 `import_jobs` 保留 1 条 `FAILED` 任务（回滚所致，符合设计）；`source_type='IMPORTED'` 的 sources 0 条（回滚已物理删除）。

## 个人命令链路（webchat 私聊）

经 `POST /api/chat/send` 的 webchat 会话执行，附件经 `POST /api/chat/post_file` 上传。

| 步骤 | 结果 |
|---|---|
| `/织忆 帮助` | 含导入/导出/回滚命令 |
| `/织忆 保存 烟测V17导入源` | 创建源记忆 `c733948359ff401b953164d18a86c324` |
| `/织忆 导出 json` | 合法 JSON，`schema_version=weave-memory-export-v2`，含 `export_id`，条目字段含 `source_memory_id`/`source_revision`/`body_omitted` 等 |
| `/织忆 导出 json all` | 含 v2 格式 |
| 附件上传 | `status=ok`，返回 `attachment_id`/`filename`/`stored_filename`/`type` |
| `/织忆 导入 dry-run` + 文件 | 「试运行（未写入）…新建 1…」 |
| `/织忆 导入` + 文件 | 「导入完成…新建 1…」；任务 `24c6767eb5c845da847e1d18cd69f4d3` |
| `/织忆 列表` | 导入正文与源记忆均可见 |
| `/织忆 导入 回滚 <任务>` | 「删除 1 条，未找到 0 条」；导入正文消失，源记忆保留 |
| 清理 | `/织忆 删除 <源记忆ID> 确认` 成功；最终列表无烟测残留 |

## 管理 API 结果

请求来自已登录 dashboard 的浏览器上下文，经
`/api/v1/plugins/extensions/astrbot_plugin_weave_memory/v1/...`：

| 端点 | 结果 |
|---|---|
| `GET /v1/maintenance/status` | 200；`schema_version=8`；`secure_purge_pending=false`；索引 healthy |
| `GET /v1/settings` | 200；`api_admin_whitelist_enabled=false` |
| `GET /v1/groups` | 200；群 `aiocqhttp/default/1046500733` enabled，ACTIVE 0 |
| `GET /v1/export?platform=aiocqhttp&tenant_id=default&group_id=1046500733` | 403 `AuthorizationDenied`（白名单为空，fail-closed，符合 ADR-0017） |
| `GET /v1/memories?scope=PERSONAL&...` | 403 `AuthorizationDenied` |
| 未登录 `GET /v1/export` | 401（`Missing API key`） |

## 限制

- `api_admin_usernames` 当前为空，管理 API 的导出/导入成功路径未执行；需主人临时加入 dashboard 用户名后再验证（烟测后应恢复）。
- 群导入/导出命令需要在真实群聊中以群管理员身份执行；本次 webchat 无法模拟群消息。
- 烟测写入的源记忆已删除；回滚后的 `FAILED` import job 作为审计保留，可在后续清理演示数据时处理。
- dashboard 临时密码仍处于启用状态，烟测后建议主人改回强密码或恢复原密码。
