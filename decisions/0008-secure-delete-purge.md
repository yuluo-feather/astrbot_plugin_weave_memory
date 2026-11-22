# ADR-0008：删除/清空的物理擦除口径

状态：Accepted（V1.0 M3 复审修复）
日期：2026-11-23

## 背景

羽忆 V0 独立审查发现 P1：删除/清空只做 SQLite 行删除，默认 `PRAGMA secure_delete=0`，也未做 WAL checkpoint 或 VACUUM。按 V0/V1 计划书「彻底删除不得保留可恢复正文」的口径，删除后的正文仍可能从数据库文件中恢复。

复现方式：创建一条带唯一 canary 的记忆，经 `SQLiteMemoryStore.delete` 删除并关闭连接后，`memories=0`、`memory_fts=0`，但数据库主文件仍能搜到 canary。

## 决策

1. 所有由本应用打开的 SQLite 连接启用 `PRAGMA secure_delete = ON`。
2. `delete` / `clear_scope` 在同一权威事务内写入 `memory_meta.secure_purge_pending = '1'`；若事务回滚，标记一并回滚。
3. FTS 同步或重建成功、索引状态为 `HEALTHY` 后，执行：
   `wal_checkpoint(TRUNCATE)` → `VACUUM` → `wal_checkpoint(TRUNCATE)`。
   两次 checkpoint 都检查 `busy` 与 `log`；`busy != 0` 或 `log > 0` 视为未完成，保留 pending 标记并抛出 `StorageError`，不执行后续步骤。`log < 0` 表示数据库不在 WAL 模式，checkpoint 为空操作，继续 VACUUM。
4. 只有两次 checkpoint 均成功后才把 `secure_purge_pending` 置 `'0'`，并写 `secure_purge_baseline_done='1'`。
5. 索引未恢复或擦除失败时保留 `'1'` 标记；插件下次启动完成 FTS 恢复后自动重试。
6. 升级基线：`secure_purge_if_pending` 在 `secure_purge_baseline_done` 缺失时执行一次全库 `secure_purge()`，清理补丁前 `secure_delete=OFF` 留下的旧空闲页/WAL 残留。
7. 命令层如实报告部分成功：逻辑删除已完成但物理擦除未完成时，提示「将在下次插件启动时自动重试」。

## 边界

- **有条件保证**：在应用自身连接、`secure_delete=ON`、没有其他连接持有 WAL 读快照、两次 checkpoint 均成功时，SQLite 主库与 WAL 文件不保留可恢复正文。
- **不保证**：存在只读连接/备份工具/SQLite backup API 持有旧快照时，checkpoint 会返回 busy；此时 pending 标记保留、命令返回 `purged=False`，等快照释放后由启动或下一次擦除重试。
- **存量升级**：基线 purge 只清理当前 SQLite 文件中的旧空闲页与 WAL；不回溯 SSD 磨损均衡、操作系统缓存、文件系统空闲块、磁盘镜像、备份/快照。
- **非 WAL 回退**：若文件系统不支持 WAL、`journal_mode` 回退到 DELETE，`wal_checkpoint` 返回 `log < 0`；此时没有 WAL 文件可清，`secure_delete` + `VACUUM` 仍清理主库，擦除可标记完成。PERSIST/TRUNCATE 等模式的 rollback journal 文件不在“主库 + WAL”保证内。
- `VACUUM` 会阻塞写入并占用事件循环，首次启动 baseline 还可能增加启动延迟和临时磁盘占用；个人库规模小可接受，大库需评估。
- 多进程/多实例写入不在 V1.0 范围；多实例下的 checkpoint/VACUUM 协调留给 V2.5。

## 后果

- 新删除的主路径、reader busy 失败路径与存量升级基线都已覆盖；羽忆已确认 P1 按“WAL/应用连接/无外部读快照/checkpoint 成功”条件式闭环，P2-1/P2-2 修复待复核。
- 删除/清空操作变慢，但个人记忆库规模小，可接受。
- 新增 `memory_meta.secure_purge_pending` 与 `memory_meta.secure_purge_baseline_done` 两个键，不改变 schema version。
- 启动恢复多一步：若 pending 为 `'1'` 或基线未完成，在 FTS 恢复后执行擦除；失败只记 warning，不阻塞插件加载。
