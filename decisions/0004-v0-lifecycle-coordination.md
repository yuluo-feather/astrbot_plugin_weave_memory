# ADR-0004：V0.4 写协调、删除与 Scope 关闭

- 状态：Accepted for V0.4 implementation
- 日期：2026-11-22
- 来源：V0「起针」计划书 §7；总计划书 §5.4、隐私审查补充条款 C。

## 决策

1. `WriteCoordinator` 是应用层唯一写入口，使用单实例 `asyncio.Lock` 串行化当前事件循环内的内容写操作；Scope/Consent 元数据写入通过 `write_metadata` 与内容写入共享同一把可重入写锁。SQLite 连接保持线程亲和，数据库调用在所属事件循环线程同步执行。V0 不支持多实例写协调。
2. `LifecycleService` 负责状态转换，调用 Domain Lifecycle 守卫后交由 WriteCoordinator 持久化 Revision；候选激活和 SUPERSEDED 恢复要求 USER/ADMIN 显式操作，系统只能执行到期归档/过期。
3. 删除采用单事务 tombstone-and-purge：先写 `memory_operations` 最小审计记录（状态标记 DELETED，不含正文），标记 FTS dirty，再删除 tags、sources、revisions、memory 正文。事务失败时全部回滚；成功后 FTS 在独立事务移除索引项。
4. 清空 Personal Scope 在单事务内为每条记忆写 `CLEAR_MEMORY` 最小 tombstone、写 `CLEAR_SCOPE` 汇总审计、标记所有索引项 dirty，并删除目标 Scope 的 tags/sources/revisions/memories。随后全量重建 FTS。其他 Scope 不受影响。
5. 关闭 Scope 对应总计划书的 disable 语义：停写、停普通读取/检索/注入；不删除正文。SQLite 增加 `is_enabled` / `disabled_at`。管理审计仍可读；V0 尚无注入缓存，因此无缓存清理实现。
6. Delete/Clear/Disable 操作使用调用方提供的稳定 operation ID；相同 operation ID 和相同操作重放幂等，不同操作复用 ID 返回 Conflict。
7. 启动恢复检查 schema 版本和 FTS integrity；dirty 时 rebuild，FTS 表缺失/损坏时重建派生表后全量回填。V0 无后台任务队列，不创建空任务系统。

## 关键保证

- 写协调器内的新增/更新/删除/清空/关闭顺序可线性化。
- 删除后正文、Revision、Source、Tag 不可恢复；审计只保留最小字段。
- 清空仅影响指定 Personal Scope。
- 关闭不删除数据，但普通读写路径不可见。
- FTS 未同步期间检索 fail-closed；启动恢复可修复 dirty/missing index。

## 后续待办

- V0.5 接入 AstrBot 时验证是否所有命令/事件写入均通过同一个 WriteCoordinator 实例。
- V0 没有 injection cache；如未来引入缓存，必须补 scope/memory 级失效端口与测试。
- V0.3 原始表清单未显式列出 `memory_consents`；该遗漏已由 ADR-0005 解决，schema migration v3 已加入 append-only Consent 表。
