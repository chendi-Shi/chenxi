# 架构、领域模型与数据库

## 技术栈与取舍

Python 3.11+ 的 asyncio/tomllib/sqlite3/email 分别负责异步编排、配置、持久化与邮件解析。Pydantic 2 定义输入输出合同并生成 JSON Schema；HTTPX 使用共享 AsyncClient 和连接池；pypdf 读取文本层及页码；SQLite WAL + FTS5 提供存储、状态恢复和本地检索。测试使用 pytest、pytest-asyncio、MockTransport、coverage 与 Ruff。

没有先引入 LangChain/LangGraph、Redis、向量数据库或消息队列：当前控制流明确，单机显式状态机更易验证故障与事务。未来需要复杂工具规划或多机队列时再扩展。

## 模块边界

| 模块 | 输入 → 输出 | 职责 |
|---|---|---|
| config.py | TOML/环境/CLI → Settings | 配置验证与哈希 |
| models.py | JSON → 领域对象 | 严格数据合同 |
| parsing.py | 字节 → 正文/段落/块 | 解析、分页、全量分块 |
| store.py | 对象 → SQLite | 增量、版本、缓存、租约、检索 |
| providers.py | Chunk → Extraction | 规则/LLM、超时与 HTTP 重试 |
| validation.py | 候选+原文 → Fact/Issue | 校验、合并、冲突候选 |
| pipeline.py | 文档选择 → Run | 编排、并发、修复、恢复 |
| reporting.py | Run → 文件 | JSON/Markdown、原子写、复核 |
| evaluation.py | 金标样本 → 指标 | 精确证据匹配 |
| cli.py | 命令 → 应用用例 | 参数、退出码与错误输出 |

## 数据合同

Pydantic 对象均 `extra=forbid`，不接受未定义字段。

- SourceInput：标题、正文、来源、日期、URL、页面区间、解析告警。页面区间由 PDF 解析器产生，JSON 用户不能自行提供。
- Segment：document ID、segment ID、start/end、text、page。字符范围为 Python Unicode code point 的左闭右开区间，不是字节偏移。
- Chunk：同一文档的多个 Segment，供应商只能引用本块出现的段落 ID。
- Candidate：summary、category、entity、metric、period、value、unit、nature、importance、citation。字段全部必填，不确定的槽位用空字符串。
- Fact：通过校验的候选，加上一个或多个 Evidence；nature 区分 reported/guidance/opinion/rumor，importance 为 high/medium/low，这些属性仍待人工复核。
- Evidence：document ID、segment ID、逐字引文、页码、正文绝对起止位置。

## SQLite 表

数据库位于 `<data_dir>/agent.sqlite3`，WAL 模式，开启外键，连接等待 20 秒。事务后显式关闭连接，避免 Windows 句柄残留。

| 表 | 主键/唯一键 | 内容 |
|---|---|---|
| documents | 内容哈希 | 不可变规范正文和页码映射 |
| sources | source_key+raw_hash+document_id | 不同来源及文件修订 |
| files | source_key | 已解析文件哈希、parser key |
| document_search | FTS5 | 分词索引 |
| runs | run_id | 状态、配置、文档选择、报告快照 |
| jobs | run_id+chunk_id | 分块输入/结果、状态、尝试、tokens、耗时 |
| cache | config_key+chunk_id | 校验通过的结果 |
| events | 自增 ID | started/repair/succeeded/failed 执行事件 |
| leases | name | pipeline 执行租约 |
| reviews | 自增 ID | 署名、状态、备注、时间 |

schema version=1；init 建表或校验版本，未知版本拒绝。目前没有跨版本迁移框架。

文件先全部解析，再按文件事务写入记录。JSON 中一条无效时该文件不部分入库；目录中的其他文件继续。原始字节按 SHA-256 存入 blobs，不用不可信文件名构造输出路径。

## 状态机

```text
run: pending → running → completed / partial / failed
                  └→ interrupted
job: pending → running → succeeded / failed
       └→ cached
resume: orphaned running → pending
resume --retry-failed: failed → pending
```

plan 先固化文档选择和分块，再执行。成功块不重做。partial 表示成功和失败块共存，failed 表示没有成功块。失败块中的部分通过校验的事实仍可保留，报告会同时列出失败原因。completed 仅表示处理结束，不代表真实准确率或覆盖率合格。

默认 Semaphore 限制同时处理 3 块，重试和修复也占槽位；共享 HTTP 连接池上限一致。

同一数据目录只允许一个 pipeline 进程：BEGIN IMMEDIATE 内检查并登记 owner，租约默认 120 秒，每 10 秒续约。其他执行者遇到有效租约立即拒绝。只能由相同 owner 续约或释放。

Ctrl+C 取消协程，保留未完成块并释放租约；强杀后等待租约过期再 resume。恢复将遗留 running 块改为 pending。

数据库结果幂等，但外部调用不是 exactly-once：模型返回后、结果落库前若崩溃，恢复时可能重复调用并付费。

## 原子性与安全边界

- 文档、来源、文件索引：单输入文件一个事务。
- 分块结果和成功缓存：同一事务。
- 报告：所有协程结束后保存快照。
- 导出：每个文件先写临时文件再 replace；两个输出文件不是跨文件事务。
- 原始 blob：先写文件后写数据库，失败可能留下无引用 blob；暂无回收命令。

模型没有 Shell、浏览器或交易执行工具。仅发送当前分块；输入提示明确将材料视为数据。错误日志不记录认证头、HTTP 响应正文或 Pydantic 输入回显。端点要求 HTTPS，无内嵌凭据/查询串，HTTP 不自动跟随重定向。

数据没有加密、多用户认证或恶意进程隔离。租约只协调执行，不能替代访问权限。SQLite 适合单机，不能直接当多机共享服务使用。
