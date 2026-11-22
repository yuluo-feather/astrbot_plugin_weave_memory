# ADR-0012：Personal 默认登记与 opt-out

状态：Accepted for V1.2（契约变更；dev 实现中）
日期：2026-11-23

## 背景

V1.0/V1.1 的 Personal 记忆采用 fail-closed：没有 `/织忆 授权` 就不保存、不检索、不注入。这个边界保护强，但把「先开开关」的摩擦放在了用户第一次需要记忆之前。羽忆在 2026-11-22 的定稿方向已经改为默认登记 + `/羽忆 关闭` opt-out；AstrBot 插件市场上的主流记忆插件（LivingMemory、简单长期记忆、Memory Beyond、Angel Memory、我会牢牢记住你、Bluezeamer Memory）也都以默认开启/自动提取为常态，没有 per-user 授权门。

用户明确要求织忆不要重复羽忆早期「先授权才记」的老路。

## 决策

1. **Personal 默认登记**：新增 `auto_enroll` 配置，默认 `true`。新用户或没有任何 consent 记录的用户，在首次符合条件的私聊事件中自动建立 `personal_memory=GRANTED`，并发送一次告知；`auto_enroll=false` 时回退逐用户 opt-in。
2. **已有 consent 记录优先**：只要该 scope 已存在 `GRANTED` 或 `REVOKED` 事件，`auto_enroll` 不再改写。用户显式撤销或关闭后的状态必须保持。
3. **opt-out 语义**：`/织忆 关闭` 继续执行 `is_enabled=0` + `personal_memory=REVOKED`，永久生效；`/织忆 撤销授权` 停止保存、检索和注入，但保留数据；`/织忆 清空` 只删数据，不重新开启已关闭 scope；`/织忆 授权` 保留为显式 opt-in。
4. **群共享记忆不变**：群记忆仍由管理员 `/织忆 群开启` 显式开启，不参与个人默认登记；个人与群隔离不变。
5. **模型工具写入**：注册个人记忆写入工具，仅在用户明确提到长期偏好、背景或目标时调用；写入 `source_type=AGENT`、`operator_type=AGENT`，只保存记忆正文，不保存完整聊天原文。工具仍受 consent 与 scope enabled 约束。**V1.2 实现先默认关闭（`agent_write_enabled=false`）；V1.3 补齐确定性触发、候选确认与每日限额后再评估默认开启。**
6. **自动提取仍不自动确认**：V1.5 的后台自动提取继续只产 `INFERRED` 候选，不自动升级为 `ACTIVE`；本次变更不等于开启无确认的推断记忆。
7. **一次性告知**：首次自动登记时由插件直发一行告知，说明默认开启、记录范围、`/织忆 关闭` 与 `/织忆 清空`；告知不经过 LLM，不重复发送。

## 后果

- 新用户零命令即可获得 Personal 记忆；不再需要先发送 `/织忆 授权`。
- 已显式撤销/关闭的用户不会被自动登记复活；存量无 consent 记录的用户会在下一次私聊事件自动登记。
- README、SECURITY、CONTRACTS、DATA_MODEL、LIFECYCLE、TEST_PLAN 与计划书需要同步修订。
- 新增 `auto_enroll` / `agent_write_enabled` 配置、模型工具、首次告知与对应测试。
- 不改变本地 SQLite/FTS5、来源区分、审计、删除与注入失败隔离。

## 未采用

- **保持逐用户 opt-in**：隐私边界最强，但重复羽忆早期摩擦，与市场常态相反；保留为 `auto_enroll=false` 的部署选项。
- **市场式静默提取**：部分插件不告知、无用户级 opt-out；织忆不采用。
- **默认开启后台自动提取并直接 ACTIVE**：与「自动提取不等于自动确认」冲突；候选确认仍留给 V1.5/V1.6。

## 验收

- `auto_enroll=true` 时无 consent 记录的用户首次私聊自动获得 GRANTED，并只收到一次告知。
- 已有 GRANTED / REVOKED 记录时 auto_enroll 不改写；`/织忆 关闭` 后不再自动登记。
- `auto_enroll=false` 时行为回到显式 `/织忆 授权`。
- 模型工具只写个人 scope，来源为 AGENT，撤销/关闭后拒绝写入。
- 群共享记忆的开启/写入/读取语义不变；群与个人双向隔离。
