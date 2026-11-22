# ADR-0002：V0.2 领域核心实现基线

- 状态：Accepted for V0.2 implementation
- 日期：2026-11-22
- 范围：只约束 V0.2 领域核心与内存测试适配器；不提前决定 SQLite schema 或 AstrBot API。
- 来源：V0「起针」计划书 §4–§5；总计划书 §4–§5。

## 决策

1. 领域时间统一使用带时区的 `datetime`，进入领域对象时转换为 UTC；拒绝 naive datetime。持久化精度在 V0.3 以 SQLite UTC ISO-8601 微秒格式实现。
2. ID 在领域层作为不透明、非空字符串；生成职责通过 `IdGenerator` 端口注入。具体格式由 adapter 决定，不在 Domain 解析。
3. 内容归一化 v1：Unicode NFKC → Unicode whitespace 折叠为单空格 → `casefold()` → 去除末尾句末标点集合 `.。!?！？;；:：…．｡`。原始正文保持不变；归一化结果独立保存。该算法需通过测试向量固定，变更必须升级 normalization version 并走迁移。
4. V0 只允许 `PERSONAL` Scope；Group/Project/Session 仅保留枚举，不允许构造可用 Scope。
5. V0 `MemoryType` 为 `PREFERENCE / FACT / EVENT`；来源枚举按 V0 计划书六类；`EXPLICIT` 来源必须由 `USER` 操作。
6. Revision 从 1 开始；领域状态转换每次产生新 revision。标点/空白等归一化后等价的正文变更不作为 V0 自动更新，显式更新由上层用例决定。`SUPERSEDED → ACTIVE` 必须由 USER 或 ADMIN 显式发起。
7. Consent Policy fail-closed：缺少匹配授权或最新状态不是 GRANTED 时拒绝。授权按 `(subject_id, scope_id, consent_type)` 精确匹配；最新事件按追加顺序决定，时间戳不参与排序。V0.5 的 SQLite 实现以 `rowid` 作为追加顺序（ADR-0005）。
8. `DELETED` 为终态；普通检索只返回 `ACTIVE`。候选按独立 workflow TTL 到期后归档，不复用语义 `valid_until`；只有 ACTIVE 的 `valid_until` 到期才转 EXPIRED。
9. 去重键按计划书 `(scope_id, subject_id, memory_type, normalized_content)`；仅 ACTIVE/CANDIDATE 参与互斥。
10. V0.2 内存 Store 仅用于测试，不作为生产存储，不承担持久化或跨进程并发保证。

## 明确不在本 ADR 决定

- V0.3 SQLite 事务/FTS 同步与索引健康恢复策略。
- V0.4 清空/关闭/删除的数据库原子性与审计白名单。
- V0.5 AstrBot 版本、命令注册和注入钩子。
- Token 预算估算器。V0.2 不实现 Injection。

## 理由

这些选择使 V0.2 能独立实现和测试，同时保留计划书要求的分层与扩展边界。未涉及的后续阶段细节不被伪装成已冻结事实。
