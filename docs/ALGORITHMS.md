# 去重、分块、模型与校验算法

## 1. 增量和来源去重

文件原始字节 SHA-256 与路径、parser key 组合判断是否重新解析。文档 ID 为规范 JSON `{body,pages,parser_version}` 的 SHA-256。规范化采用 Unicode NFC、换行统一、首尾空白处理；内部空格和大小写保留，避免 `1 2` 变成 `12`。PDF 保留页间空白确保偏移正确。

同正文只存一个 document，不同文件分别保留 source；同路径变更保留旧版本。这是严格内容去重，没有语义近似去重。页码映射不同也视为不同文档，以免引用落到错误页。

## 2. 全量分块

默认每段 1,400 字符。PDF 先限定页范围，再分段。在上限后半范围寻找句号或换行，否则硬切。默认每块 6,000 字符，相邻块重叠一个 Segment；配置必须容纳重叠段及至少两个新段，确保推进。

segment ID = SHA-256(document_id:start:end) 前24位；chunk ID = SHA-256(有序 segment IDs)。分块输入固化到 jobs。

按字符而非精确 tokens 限制。模型可能因上下文上限拒绝，系统明确失败；不会只取全文开头。PDF 页码对应原文件，字符范围对应解析文本；不含 OCR、复杂表格/双栏结构恢复。[pypdf 官方提取说明](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)

## 3. Provider 合同

```python
async def extract(chunk: Chunk, feedback: list[dict] | None) -> CallResult: ...
async def close() -> None: ...
```

规则版按关键词统计分类、逐句摘录、保守正则提取财务槽位。超过30条事实时返回明确错误，需缩小 chunk。它是流程基线，不是模型推理。

模型版请求 `POST {base_url}/responses`，`instructions` 与资料 `input` 分开，`text.format.type=json_schema`、`strict=true`、`store=false`，返回后再经本地 Pydantic 验证。兼容 Responses 协议的其他 HTTPS 服务可配置，但没有验证其兼容性。[OpenAI 官方 Structured Outputs 文档](https://developers.openai.com/api/docs/guides/structured-outputs)

## 4. 提示词

完整 SYSTEM_PROMPT 在 providers.py，运行时计算 SHA-256。要求资料不能覆盖指令；引文须连续逐字；保留数字、单位、日期、否定、不确定性；观点/预测/传闻保持属性；槽位只摘原文；不提供买卖建议或虚构填充。

这是约束机制，不能保证彻底抵御提示词注入。当前没有模型可执行的外部工具，材料无法通过工具调用直接改变文件或发送消息。

## 5. 证据校验与修复

1. 引用的 segment ID 必须存在于当前块。
2. quote 必须是该段的非空连续原文。
3. 摘要数字 token 必须在引文出现：保留符号及百分号，去掉千位逗号，`-3%` 与 `3%` 不同。
4. entity、metric、period、value、unit 非空时必须逐字出现在引文。
5. 引文中的预设中文限定词如“未经审计、预计、可能、尚未”必须在摘要保留。

校验后以段落起点加 quote 首次出现位置生成证据偏移。重复引文取首次出现，附告警。

Schema 或证据失败时，把错误列表作为 validation_feedback 再次提取，默认最多一轮修复。失败块不写成功缓存，也不偷偷回退为规则模式。

数字包含关系不能证明数值与指标正确绑定；限定词表不能覆盖全部中英文否定/不确定表达；原文存在不能证明摘要逻辑成立。仍需人工复核。

## 6. HTTP 重试

网络错误/超时及 408、409、429、500、502、503、504 可重试。401/403 等直接失败。默认3次额外重试，单轮最多4次 HTTP 请求。

优先遵守 Retry-After 秒数/HTTP 日期；否则指数退避加随机抖动；等待上限60秒。HTTPX 分阶段超时之外增加 asyncio.timeout，总请求默认限60秒。共享 AsyncClient 复用连接。[HTTPX 官方异步说明](https://www.python-httpx.org/async/)

默认配置含一轮修复时，一个块理论最多8次请求。tokens 仅累加返回 usage 的调用；无 usage 的网络失败成本未知。没有自动按价格计算金额。

## 7. 缓存

键为 `(config_key,chunk_id)`。config_key 包含 provider/model/base_url、分段分块配置、修复次数、提示词哈希及工作流版本。

并发、HTTP超时/重试、watchlist 不影响抽取语义，不进入该键。关注项只影响报告排序，不过滤材料。

供应商在同一模型名称下更新权重不会自动失效；应使用模型快照或改变版本。缓存不是永久正确性保证。

## 8. 合并与冲突

Fact ID 对 citation 以外的结构化字段哈希。字段完全相同才合并，保留不同 document/start/end 的所有证据；不把“意思类似”直接当成同一事实。

疑似冲突按 entity+metric+period+unit+nature 分组，五个槽位非空且 value 不同才标注。没有实体别名解析、财年转换、币种换算或自动裁决，因此只提供复核线索。

## 9. 检索

英文/数字词元加中文单字和相邻双字进入 SQLite FTS5，查询全部词元 AND，按 BM25 排序。查询词以引号包裹并参数化，不执行用户写入的 FTS 操作符。

它是本地词法检索，不是向量 RAG。同义词不自动匹配；检索不参与抽取裁剪，避免把低排名文档静默漏掉。

## 10. 评估

金标格式为 JSONL：id、body、gold[{category,quote}]、synthetic。内置10条虚构案例，共14条关键事实。

集合元素 `(category,exact_quote)` 用于计算：

```text
exact_gold_recall = matched / gold_count
exact_gold_precision = matched / valid_output_count
validation_acceptance_rate = valid_output_count / candidate_output_count
```

分母为0返回 null。重叠块的相同分类/引文按集合去重。该指标不是语义判分，对模型可能采用的其他正确引文较严格。

规则基线实测：recall=100%、precision=87.5%、校验接受率=100%。无关办公室材料多输出2条，说明引用有效不等于业务相关。未宣称真实材料准确率或模型效果。真实评估方案见 PILOT.md。
