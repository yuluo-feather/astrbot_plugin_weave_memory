# V1.8 宿主烟测记录（2026-11-23）

状态：通过（v8→v9 迁移、ledger-first 删除、备份/列表/dry-run/merge/revert、旧备份不复活删除、维护管理 API、备份产物与脱敏、单文件快照）。`disaster-rebuild` 空库路径由本地测试覆盖，本次宿主只在非空库验证其拒绝。

## 环境

- AstrBot 4.29.4，dashboard `http://localhost:6185`，用户名 `astrbot`（临时密码由主人提供，未写入本记录）。
- 无头浏览器：Playwright + 本机 Microsoft Edge（headless）；脚本与结果在仓库外 `D:\astrbot\temp\weave_smoke_v18\`。
- 运行插件目录：`D:\astrbot\AstrBotLauncher-0.1.5.6\AstrBot\data\plugins\astrbot_plugin_weave_memory`。
- 插件数据目录：`D:\astrbot\AstrBotLauncher-0.1.5.6\AstrBot\data\plugin_data\astrbot_plugin_weave_memory`。

## 部署

- dev 提交 `bb7298e`（V1.8）构建包同步运行目录；烟测中发现并修复两个问题后重新构建同步：
  - 恢复报告 `excluded_count` 只计实际删除行，改为统计被 ledger 排除的备份行；
  - `backup_to` 目标库统一 checkpoint + `journal_mode=DELETE`，备份目录只保留 `main.sqlite3`，不再残留 `-wal`/`-shm`。
- 同步前备份：`data/plugin_backups/weave_memory_runtime_20261123_234328`。
- 同步后 `dist` 与运行目录逐文件一致（排除 `__pycache__`）：无缺失、无多余、无内容差异。
- dashboard `POST /api/v1/plugins/astrbot_plugin_weave_memory/reload` 返回 200「重载成功。」。

## 迁移与账本

- 迁移前自动备份：`migration_backups/weave_memory_v8_to_v9_20261123T154439348823Z.sqlite3`。
- 运行库只读核对：`schema_migrations` 最大版本 9；`memories` 含 `db_lineage_id`/`creation_seq`；`memory_lineage_meta` 1 行；`memory_meta.db_lineage_id` 32 位；既有 2 条记忆完成回填。
- `memory_ledger.sqlite3` 自动创建，`ledger_id` 32 位，初始 `last_seq=0`、事件 0；烟测删除后 `last_seq=4`、4 条 `delete_memory`（2 组 intent/committed）。
- 管理 API `GET /v1/maintenance/status` 返回 schema 9、索引 healthy、`secure_purge_pending=false`；`/v1/settings` 含 `ledger_first_enabled=true`、`backup_keep_last=5`、`backup_retention_days=30`。

## 个人命令链路（webchat 私聊）

经 `POST /api/chat/send` 的 webchat 会话执行。

| 步骤 | 结果 |
|---|---|
| `/织忆 帮助` | 含备份、恢复与 `disaster-rebuild` |
| `/织忆 保存 烟测V18备份源` | 创建 `2e0f4d9e7d7049739a67891ae6f5063d` |
| `/织忆 备份` | manifest schema 1 / backup format v1；`schema_version=9`、`ledger_seq_at_backup=0`、`creation_seq_at_backup=3`、sha256 64 位 |
| `/织忆 备份 列表` | 1 份，包含刚创建的 `backup_id` |
| `/织忆 恢复 <backup_id>` | dry-run：`total=3`、`will_revive=3`、`will_not_revive=0` |
| `/织忆 恢复 <backup_id> 确认` | merge 成功，`restored=true`、`excluded_count=0` |
| `/织忆 恢复 <backup_id> disaster-rebuild 确认` | 非空库拒绝：「disaster-rebuild requires an empty target database」 |
| `/织忆 恢复 no-such-backup` | 明确失败，无写入 |
| `/织忆 删除 <源ID> 确认` | 成功；列表不再出现 |
| 再次 `/织忆 恢复 <backup_id> 确认` | `restored=true`、`excluded_count=1`；源记忆仍不存在（旧备份未复活删除） |

## revert 模式

- 新建 `烟测V18回退源`（`5f222c9550aa4465b6dcb967ef8ffdb5`）→ 备份 → `/织忆 更新 <ID> 1 烟测V18回退后`。
- `/织忆 恢复 <backup_id> revert 确认` → `restored=true`、`mode=revert`；列表恢复旧正文、修改后正文不存在。
- 测试记忆已 `/织忆 删除 ... 确认` 清理。

## 维护管理 API

经已登录 dashboard 浏览器上下文，路由
`/api/v1/plugins/extensions/astrbot_plugin_weave_memory/v1/...`：

| 场景 | 结果 |
|---|---|
| 匿名 `POST /v1/maintenance/backup` | 401（`Missing API key`） |
| 白名单为空、已登录 `POST /v1/maintenance/backup {confirm:true}` | 403 `AuthorizationDenied`（fail-closed） |
| 临时 `api_admin_usernames=["astrbot"]` + 重载 | `api_admin_whitelist_enabled=true`；status schema 9 |
| `POST /v1/maintenance/backup {}` | 400 `ValidationError`（缺少 confirm） |
| `POST /v1/maintenance/backup {confirm:true}` | 200；备份 `20261123T154733433831Z-6b28e956a61a4eb6a19373d6e27f6d8a`，sha256 64 位 |
| `POST /v1/maintenance/restore {backup_id, dry_run:true}` | 200；`dry_run=true`、`total=2`、`will_revive=2` |
| `POST /v1/maintenance/reindex {}` | 400（缺少 confirm） |
| `POST /v1/maintenance/reindex {confirm:true}` | 200；`rebuilt=true`、`documents=1` |
| 恢复原空白名单并重载 | `api_admin_whitelist_enabled=false`；backup 再次 403 |

## 备份产物

- 目录布局：`backups/v1.8/<utc>-<db_lineage_id>/`，含 `main.sqlite3`、`sidecar.json`、`config.json`、`README.txt`。
- `sidecar.json` 字段齐全：format/manifest_schema/backup_id/created_at/schema_version/db_lineage_id/ledger_id/ledger_seq_at_backup/creation_seq_at_backup/lineage_high_watermarks/sha256/size/table_row_counts。
- 备份目录不含 `memory_ledger.sqlite3`；`config.json` 中 token/API key/password/secret 类键值递归脱敏，检查结果无未脱敏项。
- 修复后新备份只含 4 个文件，无 `-wal`/`-shm`；`main.sqlite3` sha256 64 位、size > 0。
- 恢复过程生成 `restore_snapshots/<utc>/main.sqlite3`，完成后 `restore_journal.json` 不存在（无半恢复窗口）。

## 清理

- 烟测创建的 4 份 V1.8 备份、4 份恢复前快照与临时配置备份已删除；最终 `backups/v1.8/` 与 `restore_snapshots/` 为空。
- 运行库保留原有 2 条记忆；账本保留 4 条 `delete_memory` 审计（2 组烟测删除的 intent/committed），不回滚账本。
- 白名单已恢复为空；dashboard 临时密码仍启用，烟测后建议主人改回强密码或恢复原密码。

## 限制

- `disaster-rebuild` 的空目标导入只在本地测试验证；本次宿主在非空库验证了拒绝路径。
- 群销毁/附件删除的 ledger-first 由本地测试覆盖，未在真实群聊执行。
- 未验证多进程/多实例并发、账本分段轮转的宿主长跑（本地测试覆盖轮转与分段读取）。
- 宿主日志未逐条抓取；迁移与注册以自动备份文件、运行库 schema 和 API/命令结果为准。
