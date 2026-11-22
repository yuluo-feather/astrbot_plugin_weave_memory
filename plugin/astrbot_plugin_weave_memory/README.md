# 织忆 AstrBot 插件（V1.0 开发预览）

## 运行要求

- AstrBot：`>=4.29.4,<4.30`
- Python：`>=3.12`（由当前 AstrBot 稳定版要求）
- SQLite：必须启用 FTS5
- 插件本身不依赖第三方 Python 包

## 构建可安装插件包

在项目根目录执行：

```bash
python tools/build_astrbot_plugin.py
```

构建产物：`dist/astrbot_plugin_weave_memory.zip`。该包包含插件入口及平台无关的 `weavemem` 包，可直接作为 AstrBot 插件目录安装；不要只复制 `main.py`。

## 命令

- `/记忆 授权`：授权个人记忆；授权前默认拒绝存储、检索和注入。
- `/记忆 撤销授权`：停止存储、检索和注入，但保留已有数据。
- `/记忆 保存 <内容>`：显式保存一条事实；相同规范化内容重复保存沿用原 ID。
- `/记忆 更新 <记忆ID> <当前revision> <新内容>`：显式更新指定记忆；revision 不匹配时拒绝覆盖。
- `/记忆 列表`：查看记忆。
- `/记忆 查询 <问题>`：主动检索。
- `/记忆 删除 <记忆ID>`：不可恢复地删除一条记忆。
- `/记忆 清空 确认`：不可恢复地清空个人记忆。
- `/记忆 关闭 确认`：关闭个人记忆空间；停止普通读写/检索，但保留数据。

V1.0 只处理私聊，不做群聊、自动提取、外部模型调用或向量检索。

## 数据位置

数据库位于 AstrBot 为本插件分配的 `data/plugin_data/astrbot_plugin_weave_memory/weave_memory.sqlite3`。

## 注入策略

使用 `on_llm_request` 将少量相关记忆追加到 `extra_user_content_parts`，并通过 `mark_as_temp()` 保证只作用于当前请求，不进入持久会话历史。注入预算默认最多 4 条、384 estimated tokens；token 数是 provider-neutral 估算值，不是特定模型 tokenizer 的精确值。

本开发预览尚未在真实 AstrBot v4.29.4 宿主复测；自动化测试使用 API stubs，不替代宿主验证。
