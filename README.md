# 织忆（Weave Memory Engine）

> V1.0「初织」开发工作区。本仓库从 M3 崩溃恢复审查工作区移植而来；V1.0 M0/M1/M2 与 M3 自动化专项已完成，真实宿主核心烟测已通过（2026-11-23），最终验收待确认。原始计划书在工作区根目录：`../星羽塔罗_织忆_*.md`。

## 项目基线

本仓库以工作区根目录的 13 份 `星羽塔罗_织忆_*.md` 原始计划书为需求基线。原始文件只读保存；任何偏离或新增决策通过 `decisions/` 中的 ADR 记录。

- 项目：织忆（Weave Memory Engine / WeaveMem）
- V0 代号：起针
- 首个平台：AstrBot
- V0 存储：SQLite + FTS5（V0.3）
- V0 写入范围：Personal；显式保存
- V0 不做：Group、自动提取、向量/混合检索、WebUI、DSH、Project/Session、多实例/PostgreSQL

## 当前状态

- [x] V1.0 计划书评审状态确认（原文状态字段已更正；用户确认评审已完成）
- [x] V1.0 M0：架构、数据模型、端口、生命周期、安全与测试契约冻结
- [x] ADR-0006：显式写入幂等与显式更新边界
- [x] ADR-0007：预留 memory_relations 表结构与删除清理边界
- [x] V1.0 M1：显式保存幂等、并发查重、revision 乐观锁更新、排序/访问时间、SQLite v3→v4、生命周期与 Consent 关闭联动
- [x] V1.0 M2 代码：更新命令、身份/Scope、异步注入、访问时间写入、关闭命令
- [x] V1.0 M2：AstrBot v4.29.4 真实宿主复测（2026-11-23，见 `docs/HOST_SMOKE_V1_20261123.md`）
- [x] V1.0 M3 自动化可靠性与进程终止恢复：100 项测试通过（含 WAL 事务、迁移中断、FTS 启动恢复）
- [ ] V1.0 宿主层最终验收：真实宿主清空命令验证、token 级 Provider 持久历史证据及用户最终签署

- [x] 建立全新仓库目录与分层骨架
- [x] 保存 13 份原始计划书
- [x] V0.1 领域契约基线与 ADR-0002
- [x] V0.2 领域模型、生命周期、去重、Consent、端口与内存测试 Store
- [x] V0.2 单元/契约测试
- [x] V0.3 SQLite + FTS5、迁移、索引健康与重建
- [x] V0.3 单元/契约/集成测试：27 passed（Python 3.13 本地环境）
- [x] V0.4 写协调、删除/清空/关闭、审计与启动恢复
- [x] V0.4 总测试：44 passed（Python 3.13 本地环境）
- [x] V0.5 AstrBot 私聊命令、Consent、检索与临时注入
- [x] V0.5 自动化测试：69 passed（原 V0.5 基线报告）
- [x] V1.0 当前自动化测试：100 passed（Python 3.12.10 复核；源工作区 Python 3.13.5）
- [x] 真实 AstrBot v4.29.4 宿主加载与核心烟测（2026-11-23）
- [ ] 用户最终验收签署

## 目录

```text
main.py                 AstrBot 插件入口
metadata.yaml           插件元数据
core/                   纯领域核心与端口
app/                    应用服务
adapters/               存储与平台适配
tools/                  自包含插件包构建工具
tests/                  unit / contract / integration

docs/                   契约、测试与阶段报告
decisions/              ADR
pytest.ini / ruff.toml / mypy.ini
requirements.txt / requirements-dev.txt
```

## 开发纪律

1. `core` 不导入 `astrbot`、`sqlite3` 或具体基础设施实现。
2. 权威记忆数据与 FTS 等派生索引分离；索引可重建。
3. 并发更新不得静默覆盖；删除后不得检索或注入。
4. 记忆层故障不得阻塞宿主聊天；注入失败按无记忆处理。
5. 不提前实现 V1.1 及以后功能。
6. 尚未由计划书或 ADR 决定的细节必须明确标记 OPEN。

## 本地验证

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m ruff check .
python -m mypy
```

V0.5 基线保留；V1.0 仍为开发预览。当前 100 项测试不替代真实宿主最终验收。详见 `docs/V1.0_M3_CRASH_RECOVERY_REPORT_20261123.md` 与 `docs/V1.0_INDEPENDENT_ACCEPTANCE_REVIEW_20261123.md`。

安装提示：若 AstrBot 的 `plugin_set` 是手工白名单，必须把 `astrbot_plugin_weave_memory` 加进去；否则插件能加载，但命令会被静默过滤。

构建 AstrBot 可安装包：

```bash
python tools/build_astrbot_plugin.py
```

产物：`dist/astrbot_plugin_weave_memory.zip`。请安装整个 ZIP，不要只复制 `main.py`。


## 运行要求

- AstrBot：`>=4.29.4,<4.30`
- Python：`>=3.12`
- SQLite：必须启用 FTS5
- 插件本身不依赖第三方 Python 包

## 命令

- `/记忆 授权`：授权个人记忆；授权前默认拒绝存储、检索和注入。
- `/记忆 撤销授权`：停止存储、检索和注入，但保留已有数据。
- `/记忆 保存 <内容>`：显式保存一条事实；相同规范化内容重复保存沿用原 ID。
- `/记忆 更新 <记忆ID> <当前revision> <新内容>`：显式更新指定记忆；revision 不匹配时拒绝覆盖。
- `/记忆 列表`：查看记忆。
- `/记忆 查询 <问题>`：主动检索。
- `/记忆 删除 <记忆ID>`：不可恢复地删除一条记忆。
- `/记忆 清空 确认`：不可恢复地清空个人记忆。
- `/记忆 关闭 确认`：永久关闭个人记忆空间；停止普通读写/检索，保留数据，V1.0 不提供重新开启。

V1.0 只处理私聊，不做群聊、自动提取、外部模型调用或向量检索。

## 数据位置

数据库位于 AstrBot 为插件分配的 `data/plugin_data/astrbot_plugin_weave_memory/weave_memory.sqlite3`。

## 注入策略

使用 `on_llm_request` 将少量相关记忆追加到 `extra_user_content_parts`，并通过 `mark_as_temp()` 保证只作用于当前请求，不进入持久会话历史。默认最多 4 条、384 estimated tokens；token 数是 provider-neutral 估算值，不是特定模型 tokenizer 的精确值。
