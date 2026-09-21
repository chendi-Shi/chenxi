# Research Brief Agent

纯 Python 的投研材料处理项目：增量导入、长文分块、结构化事实提取、证据校验、失败修复、断点恢复、简报导出与人工复核。

**0.2.0：CLI + Python package，没有网页前端。** 当前是工程化原型，尚未通过真实投研业务和真实模型端到端验证。

## 快速启动

Python 3.11+：

```bash
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
python -m pip install -e ".[dev]"

research-agent --data-dir data/demo init
research-agent --data-dir data/demo ingest samples/demo.json
research-agent --data-dir data/demo run --out exports/demo
research-agent --data-dir data/demo status
```

也可用 `python -m research_agent` 替代 `research-agent`。虚构示例的预期结果为 6 条来源、5 份唯一正文、18 条证据事实。重复导入跳过未变化文件；重复运行命中已验证的分块缓存。输出同时包含 JSON 与 Markdown。

## 模型模式

默认 `rules` 是确定性的流程基线。`openai` 调用 Responses API，使用 JSON Schema 结构化输出。

```powershell
$env:RESEARCH_API_KEY = '你有权使用的密钥'
$env:RESEARCH_MODEL = '账户可用且支持结构化输出的模型名'
research-agent --data-dir data/work ingest inbox
research-agent --data-dir data/work run --provider openai --out exports/work
```

模型不写死，不自动换供应商；密钥从环境读取，不写数据库。`.env.example` 仅为示例，程序不自动加载 `.env`。配置项见 [config.example.toml](config.example.toml)。优先级：CLI > 环境 > TOML > 默认。

模型模式会发送所选文档分块到配置的模型服务。规则模式不联网。模型调用目前仅通过 MockTransport 契约/异常测试，尚未用真实 API Key 验证。

## 命令

```bash
# TXT / MD / JSON / JSONL / EML / 带文本层的 PDF
research-agent --data-dir data/work ingest inbox

# 发布时间过滤，左闭右开；日期缺失不会以导入时间代替
research-agent --data-dir data/work run --since 2026-09-21 --until 2026-09-22 --timezone Asia/Shanghai

research-agent --data-dir data/work status RUN_ID
research-agent --data-dir data/work resume RUN_ID --retry-failed
research-agent --data-dir data/work search "芯片 订单"
research-agent --data-dir data/work source DOCUMENT_ID
research-agent --data-dir data/work review RUN_ID --reviewer analyst-1 --status needs_revision --note "核对同比口径"
research-agent --data-dir data/work export RUN_ID --out exports/reviewed
research-agent evaluate evals/gold.jsonl --out artifacts/evaluation.json
```

全局参数 `--config` / `--data-dir` 放在子命令前。JSON 格式：

```json
[{"title":"经营数据","body":"正文全文","source":"公司公告","published_at":"2026-09-21T08:00:00+08:00","url":""}]
```

## 架构

```mermaid
flowchart LR
    A[Local files] --> B[Parse and deduplicate]
    B --> C[SQLite documents and source versions]
    C --> D[Page-aware segments and overlapping chunks]
    D --> E[Persistent jobs and cache]
    E --> F[Rules or LLM provider]
    F --> G[Evidence validation]
    G -->|bounded repair| F
    G --> H[Merge and flag conflicts]
    H --> I[JSON and Markdown]
    I --> J[Human review]
```

这是受控状态机式 Agent 工作流。程序决定调用、校验、修复和恢复，模型负责抽取；没有开放式自主研究、联网搜索或多 Agent 协作。

## 测试

```bash
python -m pytest --cov=research_agent --cov-report=term-missing
python -m ruff check src tests
python -m ruff format --check src tests
python -m build --no-isolation
```

[CI](.github/workflows/ci.yml) 配置了 Windows/Linux × Python 3.11–3.14。实际远端结果以 GitHub Actions 为准。

`requirements-dev.lock` 是本次验证环境的精确依赖版本快照。先 `pip install -r requirements-dev.lock`，再 `pip install --no-deps --no-build-isolation -e .`。它不是带哈希的跨平台供应链锁文件。

## 完整技术说明

- [架构、领域模型、数据库及状态机](docs/ARCHITECTURE.md)
- [去重、分块、模型、证据校验及评估算法](docs/ALGORITHMS.md)
- [配置、日期语义、故障恢复及部署边界](docs/OPERATIONS.md)
- [实测结果与限制](docs/VALIDATION.md)
- [产品范围](docs/PRD.md) · [真实试用计划](docs/PILOT.md)

## 数据边界

数据库、原始文件、导出和密钥不提交 Git。仓库样例均为虚构。数据没有加密，应选择获准目录；同步盘下的文件可能被同步客户端上传。扫描 PDF 需要外部 OCR，邮件附件不自动展开。引文存在不等于语义正确，真实效果仍需研究员复核和试用。
