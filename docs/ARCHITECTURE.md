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

## 4. V0 组件

- Domain：Memory Entity、Scope、Source、Revision、Status。
- Policy：ConsentPolicy、Lifecycle、Conflict/Dedup。
- Ports：MemoryStore、SearchIndex、PlatformAdapter、ScopeStore、ConsentRepository、SchemaVersionProvider、Clock、IdGenerator；均已建立 Protocol。
- Application：WriteCoordinator、LifecycleService、StartupRecoveryService、ConsentService、PersonalScopeService、MemoryApplicationService、RecallService、MemoryContextComposer。
- SQLite：权威表、FTS5、版本迁移、索引 dirty 标记与重建能力（ADR-0003）。
- AstrBot：V0.5 私聊身份、命令与 `on_llm_request` 临时注入适配。

## 5. 运行边界

V0 目标为单进程、单实例、SQLite。写操作由应用层统一协调。宿主启动/停止/重载必须释放资源；注入链路失败不得影响宿主正常聊天。

## 6. V0 实现状态与剩余事项

- MemoryStore 与 SearchIndex 共用同一 SQLiteDatabase 连接；权威写入和 FTS 同步分属两个事务。
- ID 已抽象为 IdGenerator 端口；Personal Scope 使用完整 Identity tuple 的 UUID5，其余实体使用 UUID4。
- 真实 AstrBot v4.29.4 宿主烟测；stub 测试不能替代宿主验证。
- 宿主重载时 Coordinator 与 Database 关闭顺序已由 terminate 实现，仍需真实运行验证。


## 7. V1.0 执行边界

- V1.0 唯一平台 Adapter 为 AstrBot；Personal 是唯一启用的 Scope。
- V0.5 已存在的核心、SQLite/FTS 与宿主入口继续演进，不重写目录结构。
- V1.0 分 M0 契约冻结、M1 核心/SQLite、M2 AstrBot Adapter、M3 可靠性验收。
- Group、Agent 工具、导出、自动提取、向量/混合检索、DSH、WebUI 均不实现。
