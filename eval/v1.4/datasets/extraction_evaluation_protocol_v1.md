# V1.5 自动提取评测协议（V1.4 仅定义）

**版本**：1.0.0
**用途**：为 V1.5 Personal 自动提取建立评测口径；V1.4 只冻结协议和合成对话样本，不运行提取器、不采集错误率。
**数据策略**：仅使用合成对话，不含生产聊天或生产记忆正文。

## 1. 评测边界

- 仅评测 Personal 私聊；Group 样本用于验证消息在进入窗口/LLM 前被排除，不计算 Group 提取质量分。
- `personal_memory`、`capture`、`auto_extract`、`external_model`、`store_candidate` 分层授权；任何必需授权缺失都应在窗口构造和模型调用之前短路。
- `auto_enroll` 仅代表 `personal_memory`，不视为任何 extraction opt-in。
- 敏感信息必须在进入窗口/LLM 前过滤或脱敏；不能以模型输出后再删除替代。
- 只评价 `CANDIDATE`，不得把提取结果自动激活为 `ACTIVE`。
- `AGENT` 显式写入不属于后台提取样本，二者不得混用。

## 2. 标注单位与字段

标注单位为一段按时间排序的对话窗口。每段至少包含：

- `conversation_id`、`dataset_version`、`scope_type`、`subject_id`、`consent_state`；
- 消息的 `message_id`、`role`、`speaker`、`timestamp`、合成 `content`；
- `expected_window_message_ids`：允许进入提取窗口的消息；
- `expected_candidates`：期望候选（`memory_type`、规范化内容、主体、证据消息 ID）；
- `forbidden_candidates`：不应生成的内容及原因（玩笑、临时信息、第三方归属、敏感信息等）；
- `expected_action`：`extract_candidate`、`skip_before_window`、`skip_before_llm`、`filter_sensitive` 或 `no_candidate`。

Assistant 回复、工具输出默认不进入窗口；除非未来计划明确改变此规则，否则不得标注为提取证据。

## 3. 指标口径（V1.5 实施时冻结阈值）

| 指标 | 计算口径 |
|---|---|
| Extraction Precision | 匹配的有效候选数 / 实际生成候选数；无实际候选时记 N/A |
| Extraction Recall | 匹配的有效候选数 / 标注期望候选数；无期望候选时记 N/A |
| False Memory Rate | 不应生成的候选数 / 实际生成候选数；同时报告绝对数量 |
| Sensitive Leakage Rate | 敏感内容进入候选或未经授权发送到外部模型的样本数 / 敏感测试样本数；安全门槛必须为 0 |
| Attribution Accuracy | 主体归属正确的候选数 / 可评估候选数；第三方内容错误归到用户计为错误 |
| Candidate Acceptance Rate | 用户确认的候选数 / 已作出确认或拒绝决定的候选数；未决候选不进入分母 |

候选匹配采用人工标注的语义等价判定，并保留原始候选、规范化候选、判定者和争议裁定。不得只以字符串完全相等代替语义匹配。

## 4. 授权与安全专项

必须单独覆盖：未 opt-in、仅 auto_enroll、capture 缺失、auto_extract 缺失、external_model 缺失、store_candidate 缺失、personal_memory 撤销、Scope 关闭、群聊、敏感类别白名单为空，以及敏感类别显式允许但仍需人工确认的情况。

每个授权失败样本必须断言：消息未进入窗口、`LlmExtractor` 调用次数为 0、未写入候选。群聊样本同样要求调用次数为 0。

敏感信息专项至少覆盖凭据/密码/令牌、身份证/银行卡、精确住址。应检查送入模型的实际输入，而非仅检查最终候选。

## 5. 对话集使用纪律

- 当前文件 `golden_conversations_v1.json` 仅供 V1.5 开发前评测协议与测试设计。
- V1.4 不执行 LLM，不统计 Extraction Precision/Recall 等提取质量结果。
- 对话样本只用合成文本；不得把生产私聊复制进仓库。
- 评测数据集必须版本化；留出集不得用于提取提示词/阈值调优。
- V1.5 开工时应复核本协议与 V1.5 计划书；若实现契约变化，先修订协议再执行评测。
