# ADR-0011：V1.1 Schema v5 迁移与 v4 回滚

状态：Accepted for V1.1 M0（v5 迁移与回滚已实现；迁移前备份已落地）
日期：2026-11-23

## 背景

V1.0 schema v4 的 `memory_scopes` CHECK 只允许 PERSONAL/USER，且没有管理员列。V1.1 需要群 Scope、管理员来源与 group consent；同时必须保留到 V1.0 的回滚路径。

## 决策

1. v5 重建 `memory_scopes`：
   - `scope_type CHECK IN ('PERSONAL','GROUP')`；
   - `subject_type CHECK IN ('USER','GROUP')`；
   - `admin_ids_json TEXT NOT NULL DEFAULT '[]'`；
   - `admin_source TEXT NOT NULL DEFAULT 'NONE' CHECK IN ('PLATFORM_ROLE','EXPLICIT_CONFIG','NONE')`；
   - 保留 `is_enabled`、`disabled_at`、现有个人行与唯一键。
2. 迁移 v4→v5：新建表 → 拷贝个人行 → 替换旧表 → 重建索引 → 写 `schema_migrations` marker；整个过程在一个 SQLite 事务中完成；迁移前用 SQLite backup API 备份。
3. `group_memory` 复用 `memory_consents`（`consent_type` 为 TEXT），不新增 consent 表。
4. `rollback_to_v10()`：
   - 只要存在 GROUP Scope、群记忆或 `group_memory` consent，就拒绝回滚，不静默丢群数据；
   - 无群数据时重建 v4 `memory_scopes`（去掉 admin 列、恢复 PERSONAL/USER CHECK），删除 v5 marker，版本回到 4；
   - 无群数据回滚后可再次迁移到 v5。
5. V1.0 代码不能直接打开 v5 库（schema newer）；降级到 V1.0 前必须先执行 `rollback_to_v10()`。
6. “群关闭”只是 `is_enabled=0`，不是 schema 回滚；v5 库、群数据与 admin 列保留。
7. 运维回滚路径（V1.1 无导出）：
   - 先备份数据库；
   - 若需保留群数据：留在 v5，执行 `群关闭`，不要降级代码；
   - 若必须回 V1.0：管理员对每个群执行 `群关闭 → 群清空 → 群销毁` 或 `群关闭 → 群销毁`，直到没有 GROUP Scope、群记忆或 `group_memory` consent，再执行 `rollback_to_v10()`；
   - `群销毁` 必须先关闭；服务内自动生成并校验备份 + sidecar manifest；删除顺序为记忆/关联 → consent 历史 → operations 审计 → scope；事务内标记 FTS dirty 与 secure purge pending，提交后先重建 FTS 再 purge；
   - `群关闭→群销毁` 的备份包含群记忆；`群清空→群销毁` 的备份是清空后快照，要保留群记忆必须先关闭直接销毁；
   - `群开启→群清空→群销毁` 不成立（清空不改变 enabled）；
   - 回滚失败/有群数据时不得强行改 schema 版本。
8. 测试：
   - v4→v5 幂等；个人行与索引保留；
   - 仅 GROUP Scope、仅有群记忆、仅有 `group_memory` consent 三种情况都拒绝回滚；
   - `群开启→群关闭→群销毁→rollback_to_v10` 成功；
   - `群开启→群关闭→群清空→群销毁→rollback_to_v10` 成功；
   - `群开启→群清空→群销毁` 被拒绝（未关闭）；
   - 备份生成/校验失败、未确认的销毁拒绝；
   - 子表删除顺序满足 FK；事务回滚不留部分删除；
   - 无群数据回滚到 v4 后再次迁移到 v5；
   - v5 库不能由 V1.0 直接打开；
   - 迁移中断回滚不留下半迁移状态。

## 后果

- 群数据不会被回滚静默删除；代价是 V1.1 无导出时需先清空才能降级。
- v5 引入两个 admin 列，不改变 memories/revisions/sources 的 V1.0 语义。
