# ADR-0006：V1.0 显式写入幂等与更新边界

状态：Accepted（V1.0 M0 契约冻结）
日期：2026-11-23

## 背景

V1.0 要求相同显式内容重复保存返回同一记忆 ID；正文更新必须产生 Revision/Source，并使用 `current_revision` 乐观锁。计划书同时提出“同作用域同属性冲突”可由 SUPERSEDED + 新版本解决，但当前 V1.0 数据模型只有 `MemoryType`，没有可识别稳定属性的独立 attribute key。

## 决策

1. 显式保存的幂等键为 `(scope_id, subject_id, memory_type, normalized_content)`。
2. 仅当命中同 Scope、同 Subject、同 MemoryType 的 ACTIVE 规范化相同记忆时，返回既有记忆 ID/revision，不追加 Revision/Source。
3. 查重与创建在 `WriteCoordinator` 同一把锁内执行，避免单进程并发重复保存竞态；SQLite partial unique index 仍作为最终约束。
4. 显式更新必须由调用者指定 `memory_id` 与 `expected_revision`；仅更新 ACTIVE 记忆。成功时正文、Revision、Source 同一权威事务提交，FTS 后续同步。
5. 旧 revision 或自然键唯一约束冲突均显式返回 Conflict，不静默覆盖。
6. 不根据 `MemoryType` 猜测“同一属性”。不同正文是否互相冲突，当前只允许显式指定目标记忆更新；自动 SUPERSEDED/候选冲突归并需待未来存在明确属性模型与消费者后再决定。

## 后果

- 重复命令幂等，且不产生历史膨胀。
- 用户可显式修订某条记忆，旧 revision 不会覆盖新版本。
- V1.0 不会把“FACT”类别内的不同事实错误地自动替代。
- 自动冲突推理仍不在 V1.0 范围。
