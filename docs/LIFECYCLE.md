# LIFECYCLE — V1.0 执行契约

状态：已按用户确认的既有审查结果作为 V0 执行基线；V0.2 决策见 ADR-0002  
依据：V0「起针」计划书 §4.1、§7；总计划书 §5.4。

## 1. 持久状态

| 状态 | 普通检索 | 含义 |
|---|---:|---|
| `CANDIDATE` | 否 | 等待用户确认 |
| `ACTIVE` | 是 | 当前有效记忆 |
| `SUPERSEDED` | 否 | 已被新版本替代 |
| `ARCHIVED` | 否 | 已归档 |
| `EXPIRED` | 否 | ACTIVE 已超过语义有效期 |
| `DELETED` | 否 | 已删除 |
| `REJECTED` | 否 | 用户拒绝的候选 |

`VALIDATED` 仅是写入管线阶段，不是持久状态。

## 2. 已由计划书明确的规则

- 候选按独立 workflow TTL 过期后转为 `ARCHIVED`；不得把候选 TTL 与语义 `valid_until` 混用。`EXPIRED` 只用于 `ACTIVE` 的 `valid_until` 到期。
- `REJECTED` 保留拒绝原因/来源元数据；正文默认不保留可恢复副本；允许重新提交为新候选。
- `SUPERSEDED` 只能由用户/管理员显式操作恢复为 `ACTIVE`，并生成 Revision。
- `DELETED` 保留最小审计元数据；彻底删除不得保留可恢复正文；不可恢复。
- 状态只能由 Lifecycle Service 修改。
- 普通检索只返回 `ACTIVE`；注入不得绕过状态和授权过滤。

## 3. V0 范围

V0 只做 Personal。必须支持显式保存、检索、删除；V0.4 计划清空/关闭/删除的级联与操作审计。群作用域及管理员授权流程不在 V0 实现范围。

## 4. 删除/清空/关闭

V0 计划的清理顺序：墓碑 → 正文 → FTS/标签 → 缓存。V0.4 只做 Personal。

必须满足：

- 删除后不可检索、不可注入。
- 清空只影响目标 Personal Scope。
- 事务中断不得留下半完成的权威数据状态。
- 操作写入 `memory_operations` 审计表。

## 5. V0.4 已实现语义

- 删除：`memory_operations` 写入不含正文的 `DELETED` tombstone；同一事务标记 FTS dirty 并物理删除 memories/revisions/sources/tags。
- 清空：同一事务对目标 Scope 的每条记忆写 `CLEAR_MEMORY` tombstone，再写 `CLEAR_SCOPE` 汇总记录并清理正文关联数据。
- 关闭：`memory_scopes.is_enabled=0`；普通写入、读取、检索不可用，但正文保留供管理审计；**V0 关闭是永久的，不提供重新开启命令**；V0 没有注入缓存。
- FTS 同步未完成时检索 fail-closed；启动恢复会检查并重建索引。
- 删除/清空/关闭操作用 operation ID 幂等重放。

## 6. 后续仍需落实

- 候选 workflow TTL 的具体时长与配置来源；语义 `valid_until` 已与其分离。
- Consent 撤销不删除既有数据；当前由单一 `personal_memory` Consent 控制普通存储/检索/注入（ADR-0005）。更细粒度授权留 V1.5。
- 敏感数据分类、日志脱敏和本地数据库文件权限责任。
- 注入预算已实现 384 estimated tokens / 最多 4 条；V0 未引入注入缓存，后续若引入必须补失效机制。
- AstrBot 插件重载时 Coordinator、SQLite 连接关闭顺序已实现；V1.0 已于 2026-11-23 在 AstrBot v4.29.4 完成热重载与数据持久化烟测，详见 `HOST_SMOKE_V1_20261123.md`。


## 7. V1.0 生命周期执行补充

- FTS 检索阶段直接排除 `valid_from > now` 或 `valid_until <= now` 的 ACTIVE 记忆，且必须在 LIMIT 前过滤。
- 语义过期不等于物理删除；`valid_until` 到期的记忆不可检索/注入，物理清除仍走显式删除用例。
- 更新正文不改变生命周期状态；任何状态变化仍只能经 LifecycleService。


### V1.0 关闭与 Consent 联动

- `LifecycleService.disable_scope` 先通过 WriteCoordinator 永久关闭 Scope；Scope disabled 是首要 fail-closed 边界，立即阻断普通读写/检索/注入。
- 随后追加 `personal_memory=REVOKED` Consent 事件。若 Consent 追加失败，Scope 仍保持关闭；使用同一 operation ID 重试时，关闭操作幂等返回，再补写撤销事件。
- V1.0 无重新启用 Scope 的命令；不会因 Consent 撤销失败而重新开放已关闭 Scope。
