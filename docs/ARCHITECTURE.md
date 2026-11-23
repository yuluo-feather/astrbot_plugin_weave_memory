# ARCHITECTURE — V1.0 执行契约

状态：已按用户确认的既有审查结果作为 V0 执行基线；V0.2 决策见 ADR-0002  
依据：V0「起针」计划书 §1–§5；总计划书 §3–§4、§10。

## 1. 目标与边界

V0 建立可独立测试的领域核心、SQLite + FTS5 Provider、写协调与生命周期能力，并在 V0.5 跑通 AstrBot 私聊显式保存—检索—注入闭环。

V0 仅实现 Personal。Group、自动提取、向量/混合检索、WebUI、DSH、Project/Session、多实例/PostgreSQL 均不属于本阶段。

## 2. 分层与依赖方向

```text
plugin / adapters/astrbot
          ↓
      app services
          ↓
    core domain + ports
          ↑
adapters/sqlite implements ports
```

约束：

- `core` 不导入 AstrBot、`sqlite3` 或其他基础设施实现。
- `app` 依赖 `core` 定义的端口，不依赖具体 Adapter。
- `adapters` 实现端口并负责平台/数据库转换。
- `plugin` 只负责宿主注册、生命周期挂接与依赖组装，不承载领域规则。
- Domain 不直接执行 SQL、不调用 LLM、不引用平台事件对象。
- Adapter 不绕过应用服务直接修改数据库表。

## 3. 模块职责

| 模块 | 职责 | 禁止事项 |
|---|---|---|
| `core/model` | Memory、Scope、Source、Revision、Status 等领域对象 | 平台 DTO、SQL |
| `core/policy` | Consent、Lifecycle、Conflict/Dedup 规则 | 直接读写数据库 |
| `core/ports` | Store、SearchIndex、PlatformAdapter、Clock、ID 等抽象 | 依赖具体实现 |
| `app` | WriteCoordinator、Recall、Injection、Lifecycle 用例编排 | 平台 SDK / SQL 细节 |
| `adapters/sqlite` | SQLite Provider、FTS5、迁移 | 定义领域规则 |
| `adapters/astrbot` | 身份解析、命令翻译、注入上下文选择 | 直接写表、依赖 AstrBot SDK |
| `plugin` | AstrBot 插件入口和 composition root | 复杂业务逻辑 |

开发目录已按用户要求对齐星羽 dev，拍平为根 `main.py` + `core/` + `app/` + `adapters/`；上表的 `plugin` 是逻辑边界，不是物理目录。`adapters/astrbot` 将身份、命令与注入合并为三个模块，未单独建立 Message/Conversation/Tool/Lifecycle Adapter 类；Tool Adapter 在 V1.0 本就不注册。

## 4. V0 组件

- Domain：Memory Entity、Scope、Source、Revision、Status。
- Policy：ConsentPolicy、Lifecycle、Conflict/Dedup。
- Ports：MemoryStore、SearchIndex、PlatformAdapter、ScopeStore、ConsentRepository、SchemaVersionProvider、Clock、IdGenerator；均已建立 Protocol。V1.0 另外保留空边界 `ManagementService`（V1.6/V2）与 `LlmExtractor`（V1.5），不提供实现。
- Application：WriteCoordinator、LifecycleService、StartupRecoveryService、ConsentService、PersonalScopeService、MemoryApplicationService、RecallService、MemoryContextComposer。
- SQLite：权威表、FTS5、版本迁移、索引 dirty 标记与重建能力（ADR-0003）。
- AstrBot：V0.5 私聊身份、命令与 `on_llm_request` 临时注入适配。

## 5. 运行边界

V0 目标为单进程、单实例、SQLite。写操作由应用层统一协调。宿主启动/停止/重载必须释放资源；注入链路失败不得影响宿主正常聊天。

## 6. V0 实现状态与剩余事项

- MemoryStore 与 SearchIndex 共用同一 SQLiteDatabase 连接；权威写入和 FTS 同步分属两个事务。
- ID 已抽象为 IdGenerator 端口；Personal Scope 使用完整 Identity tuple 的 UUID5，其余实体使用 UUID4。
- 真实 AstrBot v4.29.4 宿主烟测已完成（2026-11-23）；stub 测试不能替代宿主验证。
- 宿主重载时 Coordinator 与 Database 关闭顺序由 terminate 实现，真实宿主重载烟测已完成（2026-11-23）。


## 7. V1.0 执行边界

- V1.0 唯一平台 Adapter 为 AstrBot；Personal 是唯一启用的 Scope。
- V0.5 已存在的核心、SQLite/FTS 与宿主入口继续演进，不重写目录结构。
- V1.0 分 M0 契约冻结、M1 核心/SQLite、M2 AstrBot Adapter、M3 可靠性验收。
- Group、Agent 工具、导出、自动提取、向量/混合检索、DSH、WebUI 均不实现。

## 8. V1.1 Group shared 边界（M0 契约）

- V1.1 在 Personal 之外启用 `ScopeType.GROUP`；V1.0 的 Personal 写入/检索/注入语义不变。
- 组件边界：
  - `adapters/astrbot`：群事件身份解析、群管理员角色解析、群命令入口、群注入 Hook；
  - `app`：`GroupScopeService`（Scope/管理员/状态）、`GroupMemoryService`（管理员写入/成员读取）、`GroupLifecycleService`（enable/disable/clear/destroy/consent）、群上下文 Composer；
  - `core`：Group Scope 身份/管理员/生命周期策略与端口，不依赖 AstrBot/SQLite。
- 群入口只在群聊事件生效；个人命令仍只在私聊事件生效；群注入只在群 LLM 请求生效。
- V1.1 不注册群级 Agent 工具；不实现导出；不做 Group personal/Project/Session。
- 群身份/管理员/consent 的冻结决策见 ADR-0009/0010；v5 迁移与回滚见 ADR-0011。
- 本段是 M0 契约；V1.1 已实现（M1/M2），真实宿主群聊烟测未执行。

## 9. V1.2 Personal 默认登记与 agent 工具

- `AstrBotMemoryInjector` 负责私聊 auto_enroll 与首次告知；不改变 `MemoryContextComposer` 的预算与过滤顺序。
- `AstrBotMemoryCommands.remember` 作为模型工具入口，调用 `MemoryApplicationService.save_agent`；来源 `AGENT`，不绕过 Consent/Scope/WriteCoordinator。
- 群共享路径（`AstrBotGroupCommands` / `GroupContextComposer`）不参与 auto_enroll。
- 后台自动提取组件（`LlmExtractor` / `ExtractionService`）仍为预留边界，不在 V1.2 实现；V1.5 只产候选。

## 10. V1.5 自动提取边界（M0 契约）

依据：V1.5 计划书 §4–§7；ADR-0015。

- 组件边界：
  - `core`：提取相关的领域约束（允许类型、候选状态、授权有效条件）与 `LlmExtractor` 端口，不调用 LLM、不读取宿主事件。
  - `app`：`ExtractionService` 负责闸门、任务幂等、候选写入、失败隔离与恢复；复用 `WriteCoordinator`、`MemoryStore`、`ConsentService`。
  - `adapters/sqlite`：实现 `extraction_jobs` 与候选读写；迁移 `006_extraction_jobs.sql`。
  - `adapters/astrbot`：私聊事件捕获、提取命令、候选命令与模型 Provider 适配；不承载领域规则。
- 数据流：私聊事件 → 身份/scope/consent 闸门 → 敏感预过滤 → 窗口 → `LlmExtractor` → Schema 校验 → 归一化/去重/冲突 → `CANDIDATE`。
- `CANDIDATE` 不进入召回/注入/FTS；确认后由生命周期服务转 `ACTIVE`。
- 群事件不进入该管线；Group personal 与群提取不在 V1.5。
- 失败隔离：提取任务异常不影响聊天；注入与显式写入路径不变。
- M1 已实现：schema v6、分层 consent、`extraction_jobs`、`ExtractionService`、私聊捕获（累计阈值/“记住…”/回合结束）、敏感前置过滤、候选命令与离线指标 harness。真实宿主模型调用与质量采集待补。
- 实现不改 V1.4 显式写入、检索、注入与群路径语义。

