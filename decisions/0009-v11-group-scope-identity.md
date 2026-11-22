# ADR-0009：V1.1 Group Scope 身份与管理员来源

状态：Accepted for V1.1 M0（契约冻结；未实现）
日期：2026-11-23

## 背景

V1.1 引入 Group shared。V1.0 的 `Identity` 是六元组，`Scope` 只允许 PERSONAL，`memory_scopes` CHECK 只允许 PERSONAL/USER。群 Scope 不能退化为 `platform:group_id` 字符串，也不能把群 ID 当作用户 ID 或权限主体。

## 决策

1. 群身份使用完整六元组：
   - `platform`：平台名；
   - `tenant_id`：平台实例 ID（AstrBot `get_platform_id()`），不是群 ID；
   - `scope_type = GROUP`；
   - `scope_external_id = group_id`；
   - `subject_type = GROUP`；
   - `subject_id = group_id`。
2. `scope_id` 由完整六元组的 JSON 序列化取 UUID5，序列化格式与 `PersonalScopeService.stable_scope_id` 完全一致；禁止字符串拼接 `platform:group_id`。
3. `group_id` 是平台提供的不透明非空字符串，最长 256 字符；不解析分隔符，不用群名做键。
4. `memory_scopes.admin_ids_json` 存平台用户 ID 的 JSON 数组，不存昵称；`admin_source = PLATFORM_ROLE / EXPLICIT_CONFIG / NONE`。
5. 管理员来源优先级：
   - Adapter 能解析平台群主/管理员角色时用 `PLATFORM_ROLE`；
   - 平台角色 API 不可用时才回退到显式配置 `group_admin_ids`（`EXPLICIT_CONFIG`）；
   - 两者都没有时 `NONE`，群 Scope 不得 enable、不得写入、不得管理，fail-closed。
6. 首次群开启必须解析出至少一个管理员；无管理员不得创建可用的群 Scope。
7. 所有群管理/写入命令都按事件 sender 重新解析管理员；不接受用户传入 `admin_id`。平台角色变化、配置变更、多管理员在下一命令生效；要有测试。
8. `admin_ids_json` 语义：
   - `PLATFORM_ROLE`：记录最近一次成功解析的审计快照；授权每次都重新解析平台角色，快照不用于授权；
   - `EXPLICIT_CONFIG`：镜像当前配置；配置按 `platform + tenant_id + group_id` 键隔离，不允许全局列表跨群/跨实例复用。运行配置使用对象映射（键 `platform|tenant_id|group_id`），也接受对象数组；
   - `NONE`：空数组。
9. 管理员来源丢失后：管理/写入 fail-closed；已启用群 Scope 的成员读取/注入仍可用（群数据本来就是群可见），记录 warning；保留 `EXPLICIT_CONFIG` 恢复路径。若需要更严格，M0 复审时改为读也 fail-closed。
10. `ScopeType.GROUP` 允许构造；`Scope` 限制改为 PERSONAL/GROUP；v5 `memory_scopes` CHECK 同步为 PERSONAL/GROUP 与 USER/GROUP。

## 后果

- 同 platform 名、不同平台实例、同 group_id 不会共享 Scope。
- 管理员伪造/越权由 Adapter + AdminResolver + ConsentPolicy 多层防护。
- `admin_ids_json` 只作审计/状态展示，不泄漏给普通成员；`群状态` 只返回 `admin_source` 与数量，不返回管理员列表。
- 群管理员来源无法判定时不会静默降级为“所有人可管理”。

## 测试

- 群 A/B、平台实例 A/B、同 group_id 跨实例隔离。
- 平台角色、配置回退、无来源 fail-closed；多管理员；角色变化；不接受 `admin_id` 参数。
- `scope_id` 与 Personal 的 UUID5 序列化一致性。
- `群状态` 不返回管理员列表。
