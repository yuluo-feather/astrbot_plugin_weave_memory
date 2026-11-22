# 织忆 `/织忆` 前缀真实宿主烟测（2026-11-23）

环境：AstrBot `4.29.4`，插件 `v1.0.0-dev.1`，SQLite schema v4，WebUI `http://localhost:6185`。
方式：WebUI 登录后通过 `/api/v1/unified-chat/ws` 的 webchat 会话发送命令；先用 `/api/v1/plugins/astrbot_plugin_weave_memory/reload` 重载新构建。临时登录口令未记录。

## 1. 结果

| 步骤 | 命令 | 结果 |
|---|---|---|
| 1 | `/织忆 帮助` | 返回织忆帮助，全部命令均为 `/织忆 …`，不含 `/记忆 …` |
| 2 | `/织忆 授权` | 返回“已授权个人记忆……”，自动注入开启 |
| 3 | `/织忆 保存 织忆宿主烟测：乌龙茶` | 返回新 ID `f63802df2c3148d484230fdb97321efa`，索引已同步 |
| 4 | `/织忆 列表` | 列表包含该记忆；此前测试记忆仍在，未做清空/删除 |
| 5 | `/记忆 帮助` | 未返回织忆帮助，作为普通聊天交给 LLM；说明旧前缀未注册为插件命令 |

## 2. 结论

- 真实 AstrBot 宿主能识别 `/织忆 帮助`、`/织忆 授权`、`/织忆 保存`、`/织忆 列表`；
- 命令组只注册 `/织忆`，旧 `/记忆` 不再由插件处理；
- 插件重载成功，SQLite schema v4 正常，保存与列表走真实宿主插件链路；
- 未执行 `/织忆 清空 确认`、`/织忆 关闭 确认` 等破坏性命令。

## 3. 限制

- 使用 WebUI webchat 会话，不是外部 IM Provider；未抓取 Provider 请求层的 token 级 `TextPart.mark_as_temp()` 证据。
- 未验证旧 `/记忆` 在宿主命令过滤层被完全拒绝的底层行为；只确认它没有返回织忆帮助，而是进入普通聊天。
- 测试宿主插件目录已从 `dist/astrbot_plugin_weave_memory` 覆盖安装；旧插件代码已备份，旧 `weavemem/` 子目录仍留在目录中但新入口不引用。
- 宿主数据库在重载前已用 SQLite backup API 备份；未读取或记录记忆正文以外的敏感内容。
- 本记录不是发布授权；V1.0 正式验收仍待主人确认，`/织忆 清空 确认` 与 token 级注入证据仍未补。

## 4. 复现要点

1. WebUI 登录；
2. 调用 `POST /api/v1/plugins/astrbot_plugin_weave_memory/reload`，确认返回“重载成功”；
3. 连接 `ws://localhost:6185/api/v1/unified-chat/ws?token=<JWT>`；
4. 发送 `{"ct":"chat","t":"send","message":[{"type":"plain","text":"/织忆 帮助"}]}` 等消息，读取 `ct=chat` 的文本事件；
5. 旧前缀对照发送 `/记忆 帮助`。
