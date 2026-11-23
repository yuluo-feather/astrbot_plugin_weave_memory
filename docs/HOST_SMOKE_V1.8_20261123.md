# V1.8 宿主烟测记录（2026-11-23）

状态：通过（v8→v9 迁移、ledger-first 删除/清空、备份/列表/dry-run/merge/revert、旧备份不复活删除/清空、维护管理 API、备份产物与脱敏、单文件快照、disaster-rebuild 空库导入、群销毁恢复排除、附件删除恢复排除、真实群关闭→销毁成功分支）。群命令 UI 本身仍需真实群管理员执行；本次群清空/销毁/附件验证走宿主侧服务与真实 SQLite/账本。

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

## 补充验证（第二轮）

为避免破坏运行库，先做数据安全快照：
`data/plugin_backups/weave_memory_data_pre_v18_smoke2_20261123_235628/`（主库 + 账本）。以下测试只创建临时作用域/记忆，结束后已清理；真实群 `aiocqhttp/default/1046500733` 与原有 2 条个人记忆未受影响。

### disaster-rebuild 空库导入

使用生产数据的只读副本（主库 + 账本）构造 source，另建空 schema v9 target，target 使用同一账本副本：

| 项 | 结果 |
|---|---|
| source → backup | `20261123T155715773186Z-6b28e956a61a4eb6a19373d6e27f6d8a` |
| target 空库导入 | `restored=true`、`mode=disaster-rebuild` |
| 当前谱系轮转 | source `6b28e956…` → target `f5853b9e…` |
| 导入行 | 2/2；每行保留原 `db_lineage_id`/`creation_seq` |
| 旧 lineage 合并 | 拒绝（Conflict），不会产生重号 |
| 灾后新建 | 新谱系 `creation_seq=1`，不继承旧谱系高水位 |

### 群销毁恢复排除

在运行库创建临时群 `smoke/smoke/smoke_v18_group`（scope `82890ed63a1e5486bed31871becd6fb6`）与 1 条群记忆；创建 V1.8 备份后执行 `群关闭 → 群销毁`（默认备份）：

| 项 | 结果 |
|---|---|
| 销毁备份 | `group-82890ed63a1e5486bed31871becd6fb6-smoke-v18-group-destroy-20261123T155715905113.sqlite3/.json` |
| 销毁 | `affected_count=1`；scope 与群记忆均不存在 |
| ledger | `destroy_scope` intent/committed 成对写入 |
| 恢复旧备份（merge） | scope 未复活、群记忆 0 条（`destroy_scope` 排除生效） |
| 清理 | 临时 V1.8 备份、恢复前快照、群销毁备份/sidecar 已删除 |

### 附件删除恢复排除

在既有个人作用域 `webchat/webchat/weave_clear_smoke_u1` 创建临时记忆并插入 tag `smoke-v18-attach`；创建 V1.8 备份后调用宿主 `WriteCoordinator.remove_attachment`：

| 项 | 结果 |
|---|---|
| 记忆 | `58c1bf48823d47c9a5427c3e6fbbdc55` |
| remove_attachment | `removed=true`；tag 计数 0 |
| ledger | `remove_attachment` intent/committed 成对写入 |
| 恢复旧备份（merge） | tag 计数仍为 0（合并后最终排除生效） |
| 清理 | 临时记忆已 `delete_memory` 删除；备份与恢复前快照已删除 |

### 第三轮补充：个人/群清空

V1.8 的恢复**不能**回滚 ledger-first 清空（旧备份按 `clear_scope` 排除是设计目标），因此本轮仍使用外部整库快照
`data/plugin_backups/weave_memory_data_pre_v18_smoke3_20261124_000324/` 作为安全网；测试只用临时作用域/记忆。

| 场景 | 结果 |
|---|---|
| 个人清空 | 测试 scope `weave_clear_smoke_u1` 写 2 条记忆 → 备份 → `clear_scope`：`affected=2`、记忆 0；恢复备份 `excluded_count=2`，记忆仍未复活；`clear_scope` intent/committed 成对写入 |
| 群清空 | 临时群 `smoke_v18_group_clear`（scope `a7626c6d273a51ca97e9a28f43e5c3a1`）写 2 条群记忆 → 备份 → `群清空`：`affected=2`、记忆 0；恢复备份 `excluded_count=2`，记忆未复活且 scope 仍在；随后关闭→销毁，scope 移除 |
| 清理 | 测试 scope 已清空/销毁；V1.8 备份、恢复前快照、群销毁备份均删除 |

### 第四轮补充：真实群关闭→销毁成功分支

真实群 `aiocqhttp/default/1046500733`（scope `69afeb6795605a08af628c4702c6b149`）此前 `群销毁 确认` 因未关闭而正确拒绝；本轮先删除测试记忆，再执行关闭→销毁成功分支。由于无法以群管理员身份在群里发送消息，本轮使用宿主侧 `GroupLifecycleService` + `AdminDecision(PLATFORM_ROLE, 2834474787)` 走同一服务路径；安全网为整库快照 `weave_memory_data_pre_real_group_destroy_20261124_001652/`。

| 项 | 结果 |
|---|---|
| 测试记忆 | `f734f59781914f4db3b701fd473e6523` 已 `delete_memory` 删除，群记忆 0 |
| 关闭 | `disable_changed=true`；group_memory consent 撤销 |
| 销毁 | `destroy_affected=0`（记忆已先删）、`backup_mode=REQUIRED`、sha256 64 位 |
| 备份 | `group_backups/group-69afeb6795605a08af628c4702c6b149-smoke-real-group-destroy-20261123T161717104590.sqlite3/.json` |
| 销毁后 | scope 不存在、群记忆 0 |
| 恢复旧 V1.8 备份（merge） | scope/记忆均未复活（`excluded_count=1`） |
| ledger | `delete_memory`、`consent_change`、`destroy_scope` 各 intent/committed 成对 |
| 清理 | 本轮 V1.8 备份与恢复前快照已删除；销毁备份按保留策略保留 |

说明：真实群 scope 已销毁；管理员可用 `/织忆 群开启` 重新创建。群命令 UI 的真实管理员发送仍需在群内执行，本次验证的是同一服务/账本路径。

### 最终状态

- 真实群 `aiocqhttp/default/1046500733` 已按成功分支销毁（scope 不存在；可由管理员 `/织忆 群开启` 重建）；原有 2 条个人记忆保留；临时个人 scope `weave_clear_smoke_u1` 0 条；临时群 scope 不存在。
- `memory_tags=0`、FTS dirty `[]`；`backups/v1.8/`、`restore_snapshots/` 无残留；`restore_journal.json` 不存在；`group_backups/` 保留上述 1 份真实群销毁备份。
- 账本保留 `last_seq=32`：`clear_scope` 6、`consent_change` 10、`delete_memory` 8、`destroy_scope` 6、`remove_attachment` 2（intent/committed 各半），均为烟测审计，不回滚账本。

## 清理

- 烟测创建的 4 份 V1.8 备份、4 份恢复前快照与临时配置备份已删除；最终 `backups/v1.8/` 与 `restore_snapshots/` 为空。
- 运行库保留原有 2 条记忆；账本保留 4 条 `delete_memory` 审计（2 组烟测删除的 intent/committed），不回滚账本。
- 白名单已恢复为空；dashboard 临时密码仍启用，烟测后建议主人改回强密码或恢复原密码。

## 限制

- `disaster-rebuild` 空库导入使用生产数据的只读副本 + 空 target 验证，未在运行中的插件 API 上直接清空主库；这是为了避免破坏真实数据。
- 群销毁/清空/附件删除通过宿主侧服务与真实 SQLite/账本验证；群命令 UI 仍需真实群管理员在真实群聊执行。真实群 `1046500733` 的成功销毁分支已通过服务路径验证，群 scope 当前已销毁，需要时由管理员 `/织忆 群开启` 重建。
- V1.8 恢复按设计不会复活已删除/清空/销毁的数据；破坏性烟测的安全网是外部整库快照，不是 `/织忆 恢复`。
- 未验证多进程/多实例并发、账本分段轮转的宿主长跑（本地测试覆盖轮转与分段读取）。
- 宿主日志未逐条抓取；迁移与注册以自动备份文件、运行库 schema 和 API/命令结果为准。
