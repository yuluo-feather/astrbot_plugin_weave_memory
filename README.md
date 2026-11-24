<h1 align="center">织忆 Weave Memory Engine 🧵</h1>

<p align="center">
  「你愿意说，我才记住；你想忘记，我就删掉。」
</p>

<p align="center">
  <a href="https://astrbot.app"><img src="https://img.shields.io/badge/AstrBot-Plugin-ff9ecb" alt="AstrBot Plugin"/></a>
  <img src="https://img.shields.io/badge/version-v1.0.0--dev.1-f8a5c2" alt="v1.0.0-dev.1"/>
  <img src="https://img.shields.io/badge/Python-3.12%2B-3776AB" alt="Python 3.12+"/>
  <img src="https://img.shields.io/badge/AstrBot-%3E%3D4.29.4%2C%3C4.30-ff9ecb" alt="AstrBot 4.29.4"/>
  <img src="https://img.shields.io/badge/status-dev%20preview-orange" alt="dev preview"/>
  <img src="https://img.shields.io/badge/License-Proprietary-6b7280" alt="Proprietary"/>
</p>

<p align="center">🧵 📚 🔒 🌙</p>

> ⚠️ **开发预览**：本仓库尚未正式发版。V1.0 正式验收待签署，V1.1 真实宿主群聊烟测待补，V1.2 默认登记/工具写入的真实宿主烟测待补；README 描述的是当前 dev 分支已实现的能力，不代表已发布版本。

---

## 📖 目录

- 🧵 [功能特点](#-功能特点)
- 🗂 [命令手册](#-命令手册)
- ⚙ [工作流程](#-工作流程)
- 🎛 [配置项](#-配置项)
- 📦 [安装方法](#-安装方法)
- 🧱 [技术实现](#-技术实现)
- 📜 [版本与验收](#-版本与验收)
- 🌙 [注意事项](#-注意事项)
- ⭐ [支持与致谢](#-支持与致谢)

---

织忆是跑在 AstrBot 上的本地记忆引擎。默认开启、可随时关闭：它只记录你明确提到的长期偏好、背景或目标，不会保存完整聊天原文。记忆落在本机 SQLite，检索走 FTS5，回答前把少量相关记忆作为**临时上下文**注入，不写进持久会话历史。个人记忆只属于你，群记忆只属于当前群，两边不互通。

## 🧵 功能特点

- 🔒 **默认需要授权**：`auto_enroll=false`（默认）时，用户发送 `/织忆 授权` 后才会记录与检索；首次私聊只发送一次授权提示，不写入任何记忆。设为 `auto_enroll=true` 可回到首次私聊自动登记（opt-out）；群记忆仍需管理员 `/织忆 群开启`。
- 🏠 **本地优先，零第三方运行时依赖**：权威数据存本机 SQLite，检索用 FTS5；插件本身不依赖额外的 Python 包，AstrBot 与 SQLite 由宿主提供。
- 🧪 **Agent 记忆工具（V1.3）**：Personal 范围的 `weave_memory_search/get/list/history` 可用；`weave_memory_remember/update` 已启用写入，来源记录插件侧 `evidence_ref`（`message_id` 优先、`session` 回退、内容哈希），不冒充宿主 `tool_call_id`。`requested_persistence=false` 时不写库；只有工具返回成功后才能对用户确认已保存。后台自动提取仍只产候选，不会自动变成用户事实。
- 🧠 **自动注入，但不污染会话**：LLM 请求到来时检索少量相关记忆，通过临时 `TextPart` 追加到本次请求；不修改 system prompt，也不写入持久会话历史。个人默认最多 4 条、384 estimated tokens。
- 🧾 **检索诊断**：Personal/Group 检索按请求写入 JSONL，包含候选评分、过滤原因、最终注入 ID 与 Token 估算；默认不记录查询原文/记忆正文，默认保留 7 天。
- 👥 **群共享记忆（V1.1）**：管理员开启并写入群级内容，成员只能读取当前群；个人记忆不进群，群记忆不出现在私聊。
- ↩️ **授权可撤销**：`/织忆 撤销授权` 停止保存、检索与注入，但不会自动删除已有数据。
- 🧾 **更新有版本号**：`/织忆 更新` 需要同时提供记忆 ID 与当前 revision，版本不匹配就拒绝覆盖，不静默合并。
- 🧹 **删除是真的删**：删除/清空先写不含正文的墓碑审计，再物理删除正文与关联数据；应用连接启用 `secure_delete`，并在索引健康后执行 checkpoint + VACUUM。
- 🛡 **故障隔离**：索引、存储或注入链路出问题时不阻塞宿主聊天；注入失败按「这一轮没有记忆」处理，正文不会因此丢失。
- 🚧 **边界明确**：后台自动提取仍只产候选，不会自动变成用户事实。V1.7 已提供导入导出、dry-run、断点续跑与回滚；V1.8 已提供 ledger-first 删除/清空与可回滚备份恢复；WebUI/向量检索仍留给后续版本。
- 🧾 **检索调试不落原文**：日志记录查询哈希、scope、候选 ID、BM25 分数、过滤原因、最终注入 ID 与 Token 估算，不记录查询原文或记忆正文。

## 🗂 命令手册

命令前缀统一为 `/织忆`，不注册 `/记忆`，避免与其他插件冲突。

> 个人记忆默认开启；`/织忆 授权` 是显式 opt-in/恢复命令，`/织忆 撤销授权` 是 opt-out。

### 个人记忆（仅私聊生效）

| 功能 | 命令 | 描述 |
|------|------|------|
| 🆔 授权 | `/织忆 授权` | 显式开启个人记忆（默认配置下必需） |
| 🚫 撤销授权 | `/织忆 撤销授权` | 停止保存、检索和注入；已有数据保留，auto_enroll 不会自动复活 |
| 💾 保存 | `/织忆 保存 <内容>` | 显式保存一条事实；相同规范化内容重复保存沿用原 ID |
| ✏️ 更新 | `/织忆 更新 <记忆ID> <当前revision> <新内容>` | 显式修订指定记忆；revision 不匹配时拒绝覆盖 |
| 📋 列表 | `/织忆 列表` | 查看已保存的个人记忆（最多显示 30 条） |
| 🔍 查询 | `/织忆 查询 <问题>` | 主动检索个人记忆，并记录访问时间 |
| 📊 状态 | `/织忆 状态` | 查看 Personal Scope、授权、记忆数量与索引状态 |
| 📈 统计 | `/织忆 统计` | 查看记忆状态、类型、来源的数量分布 |
| 🧾 操作记录 | `/织忆 操作记录` | 查看不含正文的个人操作审计 |
| 📦 导出 | `/织忆 导出 [json\|md] [all]` | 导出当前个人空间 ACTIVE 记忆；`all` 附带其他状态元数据，默认 JSON |
| 📥 导入 | `/织忆 导入 [dry-run]` | 附上 v2 JSON 导出文件导入；dry-run 只预演，不写入 |
| ↩️ 导入回滚 | `/织忆 导入 回滚 <任务ID>` | 删除该任务本次新建的记忆，不依赖完整备份 |
| 🗑 删除 | `/织忆 删除 <记忆ID>` 后再次发送 `确认` | 二次确认后不可恢复地删除一条记忆 |
| 📥 归档（V2.0） | `/织忆 归档 <记忆ID> 确认` | 软删除：从检索/注入/普通导出隐藏，保留期内可恢复 |
| 📤 恢复记忆（V2.0） | `/织忆 恢复 <记忆ID>` | 恢复主动归档的记忆；候选过期/生命周期归档不可恢复 |
| 🧨 彻底删除（V2.0） | `/织忆 彻底删除 <记忆ID> 确认` | 二次确认后物理清除正文、索引与关联数据；ACTIVE 会先自动归档 |
| 🧹 清空 | `/织忆 清空 确认` | 不可恢复地清空个人记忆 |
| 🔐 关闭 | `/织忆 关闭 确认` | 永久关闭个人记忆空间；停止普通读写/检索/注入，数据保留，当前版本不支持重新开启 |
| 🧪 提取授权（V1.5） | `/织忆 提取 开启 <capture\|auto_extract\|external_model\|store_candidate\|全部>` | 分层授权私聊自动提取；默认全关，`auto_enroll` 不授予任何提取能力 |
| 🧪 提取状态 | `/织忆 提取 状态` | 查看有效授权、缺失条件、提取任务计数与待确认候选数 |
| 🧪 敏感白名单 | `/织忆 提取 敏感 <类别...>` | 允许进入候选的敏感类别；`清空` 恢复默认不保存 |
| 📝 候选管理 | `/织忆 候选 列表` / `确认 <ID>` / `拒绝 <ID> [原因]` | 候选不参与检索/注入；确认后转 ACTIVE，拒绝记录原因 |
| 🗃 备份（V1.8） | `/织忆 备份` / `/织忆 备份 列表` | 创建/查看实例级主库快照；备份不含账本，恢复不会复活已删除/撤销授权内容 |
| ♻️ 备份恢复（V1.8） | `/织忆 备份恢复 <备份ID> [merge\|revert\|disaster-rebuild] 确认` | 不带“确认”为 dry-run；旧备份按 ledger 重放排除 |
| ❓ 帮助 | `/织忆 帮助` | 查看个人记忆命令说明 |

### 群共享记忆（仅群聊生效，V1.1）

| 功能 | 命令 | 权限 |
|------|------|------|
| ❓ 群帮助 | `/织忆 群帮助` | 所有群成员 |
| 🟢 群开启 | `/织忆 群开启` | 群管理员 |
| 🔴 群关闭 | `/织忆 群关闭` | 群管理员；可重新开启，数据保留 |
| 💾 群保存 | `/织忆 群保存 <topic\|agreement\|atmosphere\|experience> <内容>` | 群管理员；需已开启 |
| 📋 群列表 | `/织忆 群列表` | 群成员；需已开启且已授权 |
| 🔍 群查询 | `/织忆 群查询 <问题>` | 群成员；需已开启且已授权 |
| 📊 群状态 | `/织忆 群状态` | 群成员；只返回创建/开启/条数/管理员来源，不返回正文或管理员列表 |
| 📦 群导出 | `/织忆 群导出 [json\|md] [all]` | 群管理员；导出当前群 ACTIVE 记忆，`all` 附带其他状态元数据 |
| 📥 群导入 | `/织忆 群导入 [dry-run]` | 群管理员；附 v2 JSON 文件导入，dry-run 只预演 |
| ↩️ 群导入回滚 | `/织忆 群导入 回滚 <任务ID>` | 群管理员；只删除该任务本次新建的群记忆 |
| 🧹 群清空 | `/织忆 群清空 确认` | 群管理员；不可恢复，只清当前群 |
| 💥 群销毁 | `/织忆 群销毁 确认` | 群管理员；必须先群关闭；自动备份后彻底移除当前群数据，不可恢复 |
| 💥 群销毁（无备份） | `/织忆 群销毁 无备份 确认` | 危险：不写备份直接销毁，需二次确认；备份不用于恢复本群 |

> 💡 **触发规则**：个人命令只在私聊响应，群命令只在群聊响应；群命令不会在私聊生效，个人命令也不会在群里代记。群管理员来源优先使用平台群主/管理员角色，平台无法解析时回退 `group_admin_ids` 配置；两者都没有时按无管理员处理，开启与写入一律拒绝。

群保存的类型映射：`topic → FACT`、`agreement / atmosphere → PREFERENCE`、`experience → EVENT`。

## 🧭 管理 API（V1.6 只读 + V1.7 群导入导出 + V1.8 维护）

通过 AstrBot 的 `register_web_api` 注册管理 JSON API，宿主暴露在
`/api/v1/plugins/extensions/astrbot_plugin_weave_memory/v1/...`（旧版宿主为 `/api/plug/...`）：

| 端点 | 说明 |
|---|---|
| `GET /v1/overview` | 记忆/候选/索引聚合与最近操作；不含正文 |
| `GET /v1/candidates` | 群作用域候选只读列表 |
| `GET /v1/memories`、`GET /v1/memories/<id>` | 群作用域 ACTIVE 记忆分页与详情 |
| `GET /v1/memories/<id>/history` | 版本与来源 |
| `GET /v1/groups` | 群列表（enabled、管理员来源、ACTIVE 计数） |
| `POST /v1/retrieval/debug` | 只读检索调试：候选 ID/评分/过滤原因、最终注入 ID、Token 估算 |
| `GET /v1/settings`、`GET /v1/maintenance/status` | 只读配置与维护状态 |
| `GET /v1/export` | V1.7 群导出；`all=1` 附带其他状态元数据 |
| `POST /v1/import` | V1.7 群导入；body 为 v2 导出 JSON，`dry_run=1` 只预演 |
| `POST /v1/maintenance/backup` | V1.8 创建实例备份；要求 `confirm=true` |
| `POST /v1/maintenance/restore` | V1.8 恢复；body `{backup_id, mode, confirm}`，`dry_run=true` 只输出将复活/不会复活清单 |
| `POST /v1/maintenance/reindex` | V1.8 重建 FTS；要求 `confirm=true` |

- 只读端点要求 dashboard 登录用户名；配置 `api_admin_usernames` 后只允许名单内用户。
- `GET /v1/export` 与 `POST /v1/import` 是 V1.7 的管理 API 写/读正文入口：必须显式配置非空 `api_admin_usernames` 且用户命中（fail-closed），只支持群作用域，要求群 `enabled=1` 且 `group_memory=GRANTED`；个人 scope 一律拒绝。
- `POST /v1/maintenance/backup|restore|reindex` 是 V1.8 的实例级维护入口：同样要求非空 `api_admin_usernames` 与 `confirm=true`；备份/恢复在写租约内完成。
- 群作用域只读端点同样要求 `enabled=1` 且 `group_memory=GRANTED`；个人正文不经 API。
- 检索调试不返回正文、不更新访问时间、不写诊断；候选确认/拒绝与群开关仍走命令。
- 部署者带外工具：`python scripts/weave_admin.py candidates|expire-candidates|maintenance-status ...`。

## ⚙ 工作流程
```
首次私聊（默认 auto_enroll=false）
   └─ 发送一次授权提示，不登记、不写入
        │
        ▼
/织忆 授权 → 登记 personal_memory=GRANTED
        │
        ▼
/织忆 保存 内容 或 /织忆 更新（EXPLICIT / USER）
   ├─ 规范化内容 → 同 Scope/Subject/类型下查重
   ├─ WriteCoordinator 串行写锁：入口校验 + 锁内二次校验 Consent
   ├─ SQLite 权威事务：记忆 + 初始 Revision + Source
   └─ FTS5 派生索引独立同步；索引未同步时检索 fail-closed

任意 LLM 请求（私聊 / 群聊）
   └─ on_llm_request
        ├─ 身份六元组 → 当前 Scope（个人或当前群）
        ├─ Consent / Scope enabled / 生命周期状态 / 有效期过滤
        ├─ FTS5 检索 → BM25 排序 → 预算裁剪
        │    个人：最多 4 条、384 estimated tokens
        │    群：默认 2 条、192 estimated tokens（可配置）
        └─ 临时 TextPart 追加到本次请求；不写持久会话历史
```

Agent 工具：`weave_memory_search/get/list/history` 仅访问当前 Personal Scope；`weave_memory_remember/update` 已启用写入，来源记录插件侧 `evidence_ref`（`tool:<name>#message:<message_id>#sha256:<content_hash>`；缺 `message_id` 时退回 `session:<unified_msg_origin>`），不冒充宿主 `tool_call_id`。`weave_memory_remember` 支持 `requested_persistence` 与 `reason`；`update` 仍要求 `expected_revision`。宿主不提供 `llm_tool` 时工具注册降级为空操作，命令入口仍可用。

**失败语义**：索引不可用时检索 fail-closed，但正文不丢；写事务提交后索引失败会保留 dirty 标记，由启动恢复或显式重建修复；注入链路任何异常都只影响本轮记忆，不影响聊天。

## 🎛 配置项

配置在 AstrBot 插件管理界面里填写。个人记忆与群共享记忆的配置都在这里：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `auto_enroll` | bool | `false` | Personal 记忆默认登记。默认关闭：用户发送 `/织忆 授权` 后才记录与检索，首次私聊只提示一次。设为 `true` 回到 opt-out：新用户首次私聊自动登记并告知。已有授权/撤销记录不会被改写。 |
| `agent_write_enabled` | bool | `true` | 控制 Agent 记忆写入工具开关。开启后 `weave_memory_remember/update` 可写入；来源记录插件侧 `evidence_ref`（message/session + 内容哈希）与工具 metadata，不冒充宿主 `tool_call_id`。 |
| `group_admin_ids` | object | `{}` | 群管理员回退配置。键为 `platform\|tenant_id\|group_id`，值为管理员 ID 数组；仅在平台无法解析群主/管理员角色时使用。键必须精确匹配平台、平台实例与群 ID，避免跨群/跨实例复用。 |
| `group_max_tokens` | int | `192` | 群记忆注入的 Token 预算（provider-neutral 估算值）。 |
| `group_max_memories` | int | `2` | 群记忆注入的最大条数。 |
| `group_candidate_limit` | int | `8` | 群检索候选上限。 |
| `group_max_active` | int | `200` | 单个群 ACTIVE 记忆软上限；超过后提示管理员清理。V1.1 只告警，不自动淘汰。 |
| `ledger_first_enabled` | bool | `true` | V1.8 ledger-first 删除/清空/consent/群销毁与备份恢复开关。关闭后回溯到 V1.7 删除语义，备份/恢复同时禁用；schema v9 的谱系与 `creation_seq` 分配保留。 |
| `ledger_rotate_events` | int | `0` | 账本多少事件后轮转到新分段；0 为不自动轮转。只移动文件，不裁剪事件。 |
| `backup_keep_last` | int | `5` | 至少保留最近 N 份备份；不删除最新一份。 |
| `backup_retention_days` | int | `30` | 备份保留天数；与份数条件同时生效。 |

示例（键里的三段依次是平台、平台实例、群 ID）：

```json
{
  "platform|tenant_id|group_id": ["admin-id-1", "admin-id-2"]
}
```

> 个人记忆的注入预算当前固定在代码里：最多 4 条、384 estimated tokens。token 数是中性估算（CJK 字符与标点各计 1，ASCII 字母/数字连续段每 4 字符计 1），不是某个模型 tokenizer 的精确计数。

## 📦 安装方法

> ⚠️ **运行环境要求**：**AstrBot >=4.29.4,<4.30**；**Python >=3.12**；SQLite 必须启用 **FTS5**。插件本身不依赖第三方 Python 包。

### 🌷 方式一：从 GitHub 仓库地址安装

1. 打开 AstrBot 管理面板，进入「插件市场 / 安装插件」
2. 选择「从 GitHub 仓库地址安装」，填入：

   `https://github.com/yuluo-feather/astrbot_plugin_weave_memory`

3. 等待插件下载完成，在「AstrBot 插件」页面找到「织忆」，点击「重载插件」

### 🌷 方式二：手动安装

把仓库克隆到 AstrBot 的 `data/plugins/` 下，目录名保持为 `astrbot_plugin_weave_memory`：

```bash
cd AstrBot/data/plugins
git clone https://github.com/yuluo-feather/astrbot_plugin_weave_memory.git
```

然后重启 AstrBot 或重载插件。

> 本地 ZIP 构建属于开发侧工具，不随公开仓分发；插件市场与直接 clone 均使用仓库快照。

> 📌 **白名单提示**：如果 AstrBot 的 `plugin_set` 是手工白名单，必须把 `astrbot_plugin_weave_memory` 加进去；否则插件能加载，但命令会被静默过滤。
>
> 安装后首次私聊只会收到一次授权提示，不会自动记录；用户发送 `/织忆 授权` 后开启。若希望恢复旧的自动登记行为，把 `auto_enroll` 设为 `true`。

## 🧱 技术实现

- **分层，依赖单向**：
  - `main.py`：AstrBot 插件入口，只做注册、生命周期挂接与依赖组装
  - `core/`：领域模型、策略与端口；不导入 AstrBot、`sqlite3` 或具体基础设施实现
  - `app/`：应用服务编排（写入协调、检索、注入、生命周期、Consent、群服务）
  - `adapters/`：AstrBot 平台适配与 SQLite/FTS5 实现
  - `scripts/weave_admin.py`：部署者带外群管理 CLI
- **权威数据与派生索引分离**：`memories`、`memory_revisions`、`memory_sources`、`memory_scopes`、`memory_tags`、`memory_operations` 等是权威表；`memory_fts` 是派生索引，可重建。索引未同步时检索 fail-closed，启动恢复会检查并重建。
- **写入串行化**：所有内容写入经过单实例 `WriteCoordinator`；内容写与 Scope/Consent 元数据写共享同一把可重入写锁。保存前按 `(scope_id, subject_id, memory_type, normalized_content)` 查重，规范化采用 Unicode NFKC、空白折叠、`casefold()` 与去除末尾句末标点。
- **乐观锁**：更新必须携带 `memory_id + expected_revision`；revision 不匹配返回冲突，不静默覆盖。正文、Revision、Source 在同一权威事务提交。
- **身份与授权**：身份键为 `platform + tenant_id + scope_type + scope_external_id + subject_type + subject_id`；先做身份、授权、状态与有效期过滤，再检索、排序和注入，不把跨作用域候选交给上层。
- **注入不可信文本**：记忆正文按不可信参考文本处理并做转义，不得覆盖系统或开发者指令；注入使用临时 `TextPart`，不修改持久会话历史。
- **删除与物理擦除**：删除/清空在同一权威事务内写不含正文的最小墓碑审计、标记 FTS dirty 并物理删除正文关联数据；所有应用连接启用 `PRAGMA secure_delete = ON`；索引恢复健康后执行 `wal_checkpoint(TRUNCATE)` → `VACUUM` → `wal_checkpoint(TRUNCATE)`。checkpoint busy 时保留 pending，下次启动重试。该保证以应用自身连接、无其他读快照、checkpoint 成功为条件，不承诺 SSD、操作系统缓存、文件系统空闲块或备份层面的取证级擦除，也不等同于数据库加密。
- **SQLite schema v9**：V1.0 为 v4（`memory_relations` 预留表），V1.1 升到 v5（群 Scope/admin 字段），V1.5 升到 v6（`extraction_jobs`），V1.6 升到 v7（`memories.candidate_expires_at`），V1.7 升到 v8（`import_jobs` 与 `memory_sources.evidence_ref` 索引），V1.8 升到 v9（`memories.db_lineage_id` / `creation_seq`、`memory_lineage_meta`）。从任意旧版本升级前，插件会在数据目录的 `migration_backups/` 生成一致性备份并校验完整性/版本；备份失败则中止迁移。升级前仍建议自行备份整个插件数据目录。有群数据/候选/提取任务/导入任务时对应回滚会被拒绝；降级前对每个群执行 `群关闭 → 群销毁`（想保留群记忆就不要先做 `群清空`，否则备份只是清空后的快照），或按 `群关闭 → 群清空 → 群销毁` 处理。
- **群生命周期**：群开启/关闭/清空/销毁均写操作审计；群关闭可逆，个人关闭永久；群销毁必须已经关闭，服务内自动备份并写 sidecar manifest，删除顺序为记忆/关联 → consent 历史 → operations 审计 → scope，提交后先恢复 FTS 再 secure purge。
- **按作用域导出与导入（V1.7）**：`export_personal` / `export_group` 默认只导当前 scope 的 ACTIVE 记忆，格式为 `weave-memory-export-v2`，带 `export_id` / `source_platform` / `source_memory_id` / `source_revision` 幂等四元组；导入只创建新记忆、走 WriteCoordinator、保留有效期，非 ACTIVE 状态只写不含正文的审计；支持 dry-run、断点续跑与按任务回滚。`memory_operations` 写入 `result=SUCCESS` 与操作元数据，不含正文；注入无缓存，删除/清空/关闭后下一次请求重新检索。
- **测试与 CI**：本地 `dev`（2026-11-23）完整测试 `333 passed`（Python 3.12.10）；ruff、mypy、`mypy .`、compileall 与插件包构建通过。GitHub Actions 在 Ubuntu/Windows × Python 3.12/3.13 上跑编译、ruff 与 pytest，另跑 mypy 与 AstrBot 4.29.4 真框架导入；`main` 分支额外构建 ZIP 作为 Actions 产物。真实宿主 WebUI/群聊角色/Provider 注入仍走本机烟测，CI 只做可重复的代码与契约检查。

## 📜 版本与验收

| 阶段 | 状态（2026-11-23） |
|------|--------------------|
| V0「起针」 | 已完成：领域核心、SQLite + FTS5、写协调、生命周期、私聊闭环 |
| V1.0「初织」 | 代码、私聊宿主烟测与专用 Scope 清空证据已完成；**正式验收待签署** |
| V1.1「引线」 | M1/M2 群共享记忆已实现；**真实宿主群聊烟测待补** |
| V1.2「锁边」 | Personal 默认登记 + opt-out 已实现；Agent 写入服务已具备（V1.3 启用写入） |
| V1.3「整经」 | 部分交付开发版本；Agent 读写工具已启用并记录插件侧 `evidence_ref`；私聊真机烟测通过，群聊/跨账号待补 |
| V1.4「验线」 | 进行中：60 条合成 Golden Set（含有效期与冲突/时序覆盖）、离线 Harness 与基线已建立，评分校验工具已补；人工标注复核及双人注入有效性评分待完成 |
| V1.5「添线」 | 核心实现：schema v6、分层 consent、单条/批量私聊捕获、敏感前置过滤、候选确认/拒绝、任务恢复与离线指标 harness；默认关闭，真实宿主模型调用与质量采集待补 |
| V1.6「候线」 | 核心实现：schema v7、候选 TTL/惰性归档、确认/拒绝/过期审计、拒绝默认清正文、只读管理 JSON API 与 `weave_admin` 候选/维护命令；真实宿主管理 API 烟测已通过 |
| V1.7「接线」 | 核心实现：schema v8、导出格式 v2、导入/导出/回滚/dry-run/断点续跑、群导入导出命令与管理 API 端点；宿主真机导入导出烟测待补 |
| V1.8「归档」 | 核心实现：schema v9 谱系/序号、独立 ledger-first 删除/清空/consent/群销毁、备份产物与三模式恢复、restore journal、维护 API 与私聊命令；真实宿主 v8→v9 迁移、备份/恢复、维护 API、空库 disaster-rebuild、群销毁与附件删除恢复排除烟测已通过（宿主烟测已通过） |
| 发布 | **未正式发版**；运行目录已存在（接收方，当前安装较早的开发包）；`metadata.yaml` 版本保持 `v1.0.0-dev.1`，版本号变更待项目负责人确认 |

逐版本变更记录见 [CHANGELOG.md](CHANGELOG.md)。

已完成的宿主验证：AstrBot v4.29.4 真实宿主加载、`/织忆 帮助`、`/织忆 授权`、`/织忆 保存`、`/织忆 列表` 私聊烟测通过；专用测试 Scope 的 `/织忆 清空 确认` 通过，主测试 Scope 未受影响；V1.8 的 v8→v9 迁移、`/织忆 备份|恢复`（merge/revert/dry-run）、旧备份不复活删除与维护管理 API 已通过（宿主烟测已通过）。

V1.4/V1.5 的评测数据与 Harness 在开发侧维护，不随插件分发。Group personal、向量检索、WebUI 等仍按路线图逐版推进。

## 🌙 注意事项

- **默认需要授权**：用户发送 `/织忆 授权` 后才记录与检索；首次私聊只提示一次。`/织忆 撤销授权` 或 `/织忆 关闭` 停止保存、检索与注入，已有数据保留。群记忆仍需管理员 `/织忆 群开启`。
- **撤销不等于删除**：`/织忆 撤销授权` 只停止普通功能，历史数据仍在；要删除请用 `/织忆 删除` 或 `/织忆 清空 确认`。
- **删除不可恢复**：个人 `删除` / `清空` 与群 `群清空` / `群销毁` 都是不可恢复操作；群销毁还会移除群 consent 与审计，只保留销毁前备份。
- **关闭语义不同**：个人 `关闭` 永久生效，当前版本不提供重新开启；群 `关闭` 可逆，数据保留。
- **物理擦除有条件**：应用会尽力擦除可恢复正文，但不承诺 SSD、系统缓存、文件系统空闲块或备份层面的取证级擦除；本插件也没有实现数据库加密。
- **群记忆对成员可见**：群记忆的可见范围是当前群；个人记忆不进群，群记忆不出现在私聊。群管理员写入的内容由管理员负责，V1.1 不做敏感内容自动检测。
- **开发预览**：接口、配置项与数据 schema 仍可能随版本调整；升级前请备份插件数据目录。

---

## ⭐ 支持与致谢

- 觉得有用？来 [GitHub](https://github.com/yuluo-feather/astrbot_plugin_weave_memory) 点个 ⭐
- 遇到问题或想提需求？欢迎提交 [Issue](https://github.com/yuluo-feather/astrbot_plugin_weave_memory/issues) 或 Pull Request
- **致谢**：AstrBot 框架、SQLite FTS5，以及每一位愿意把记忆交给本机保管的你。
