# ADR-0010：V1.1 Group consent、可逆 disable 与 clear

状态：Accepted for V1.1 M0（契约冻结；未实现）
日期：2026-11-23

## 背景

V1.0 只有 `personal_memory` consent，Scope 关闭是永久的。V1.1 需要群管理员 `enable / disable / clear / status`，并保持个人语义不变。

## 决策

1. 新增 `group_memory` consent，默认无记录即拒绝；与 `personal_memory` 完全分离，复用 `memory_consents` 表。
2. `群开启`（管理员）：解析管理员 → 追加 `group_memory=GRANTED` → 置 `is_enabled=1` 并写 `GROUP_ENABLE` 审计；同一 operation ID 重放幂等。
3. `群关闭`（管理员）：置 `is_enabled=0` 并写 `GROUP_DISABLE` 审计 → 追加 `group_memory=REVOKED`；保留群数据；**V1.1 群关闭可逆**，`群开启` 可重新启用。撤销追加失败时返回 `GroupConsentRevocationPending`，Scope 保持 disabled；同一 operation ID 重试补齐。
4. 重新开启：追加新的 `GRANTED` 事件，最新事件生效；旧 consent 历史保留。`admin_ids_json/admin_source` 在重新开启时重新解析。
5. `群清空 确认`（管理员）：只清当前群 Scope；要求 `确认`；在 enabled 或 disabled 状态都可执行；写 `CLEAR_MEMORY`/`CLEAR_SCOPE` 审计；物理擦除沿用 ADR-0008；不改变 `group_memory` consent 与 `is_enabled` 状态。
6. `群销毁 确认`（管理员，V1.1 新增）：仅用于 V1.0 回滚/彻底移除群数据。
   - 前置：必须先 `群关闭`（`is_enabled=0` 且 `group_memory=REVOKED`）；enabled 状态直接拒绝（`InvalidTransition`）。
   - 备份：`destroy` 在删除前自行调用 SQLite backup API 生成唯一备份文件（包含 operation ID/scope ID/时间戳），打开备份校验目标群 Scope/记忆/consent 可见，并写 sidecar JSON manifest（operation ID、scope ID、备份路径、sha256、行数、时间）；备份或校验失败立即中止，不执行任何删除。
   - 删除顺序（单事务，使用 destroy 专用 `delete_group_scope`，不调用会自行提交的 `clear_scope`）：群记忆及关联（memories/revisions/sources/tags/relations）→ 删除该群 `group_memory` consent 历史 → 删除该群 `memory_operations` 审计 → 删除群 `memory_scopes` 行；事务内同时标记 FTS dirty 与 `secure_purge_pending='1'`。
   - 事务提交后先重建/清理群 FTS（或由启动恢复重建），再执行 secure purge；FTS 未恢复时不得把 pending 置 0；purge 失败只保留 pending，不恢复群 Scope。
   - 备份语义：`群关闭→群销毁` 的备份包含群记忆；`群清空→群销毁` 的备份是清空后的快照，不含已清空记忆。要保留群记忆必须先 `群关闭→群销毁`，不要先清空。
   - 幂等：事务提交后 Scope 不存在视为已销毁；崩溃在事务前/中则整体回滚；重试不得重复删除或覆盖已有备份。
   - `GROUP_DESTROY` tombstone 写入 sidecar manifest，不修改只读备份；备份是唯一保留路径。
   - 不可恢复；没有备份不得执行。
7. 读取矩阵：
   - `群列表/查询`：仅当 `is_enabled=1` 且 `group_memory=GRANTED`；否则 `AuthorizationDenied`；
   - `群状态`：成员始终可读；返回 `created/enabled/count/admin_source/admin_available`，不返回记忆正文或管理员列表；
   - 群从未创建/无 Scope：`群状态` 返回 `created=false, enabled=false, count=0, admin_source=NONE, admin_available=false`，不返回 `NotFound`。
8. 普通成员不能开启/关闭/清空/销毁/保存；失败返回 `AuthorizationDenied`，不泄漏其他群存在性。
9. 群解散/退群：若 Adapter 提供群解散事件，置 disabled + 撤销 `group_memory`，保留数据，不自动删除；没有事件时由管理员 `群关闭/群清空`。成员个人删除/退群清理在 V2.4。
10. V1.1 无导出；clear/disable 后“导出不可见”改为 V1.2 的前向约束。
11. 个人 `disable` 仍永久、无重新开启；群 `disable` 可逆是 V1.1 的明确差异，帮助与文档必须写清。

## 后果

- 群关闭后写入/检索/注入停止，数据保留，管理员可重新开启或清空。
- `群开启→群关闭→群清空→群销毁` 与 `群开启→群关闭→群销毁` 都能到达“无群 Scope/无群 consent”的回滚前置状态；`群开启→群清空→群销毁` 不成立（清空不改变 enabled）。
- 群清空只影响目标群，主个人 Scope 与其他群不受影响。
- 群清空后 consent 状态不变；若清空前为 disabled，清空后仍 disabled。
- `群销毁` 会删除群数据与群审计；销毁前自动备份 + sidecar manifest 是唯一保留路径。

## 测试

- enable/disable/re-enable/clear/destroy/status 全路径；成员越权拒绝；管理员允许。
- 群关闭后 `群列表/查询` 拒绝、`群状态` 可读但不含正文；重新开启恢复。
- 无 Scope 的 `群状态` 返回 created=false；不泄漏其他群。
- 群清空只清当前群；个人/其他群不受影响。
- `群开启→群关闭→群清空→群销毁→rollback_to_v10` 与 `群开启→群关闭→群销毁→rollback_to_v10` 成功；`群开启→群清空→群销毁` 被拒绝；未备份/备份校验失败/未确认的销毁拒绝。
- 子表删除顺序/FK：consent 与 operations 在 scope 前删除；事务回滚不留下部分删除；purge pending 时 Scope 已删除但启动会继续擦除。
- 群 consent 最新事件生效；撤销追加失败保持 disabled 并可重试。
- 群解散 fake event 的 disable + revoke + 数据保留。
- 群状态不泄漏管理员列表。
