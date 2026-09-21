# 验证记录 · 0.2.0

日期：2026-09-21。环境：Windows，Python 3.14.7。依赖版本见 requirements-dev.lock。

## 实测结果

- pytest：47 项通过。
- coverage（包含分支）：88%；证据校验模块100%、工作流模块91%。这是测试覆盖率，不是业务准确率。
- Ruff check 与 format：通过。
- wheel 与源码分发包：构建成功（0.2.0）。
- CLI 导入：6条虚构来源，5份唯一正文，保留6条来源记录。
- CLI 运行：5/5块成功，18条事实，规则模式0次HTTP调用。
- 10条合成案例、14条金标事实：精确引文召回100%、精度87.5%、校验接受率100%。

无关办公材料多输出2条，未隐藏这类误报。引用正确并不保证业务相关性。

## 覆盖的关键行为

增量跳过、来源去重、文件修订、坏文件隔离、JSON文件内原子性、日期过滤、邮件编码/HTML/附件提示、PDF分页与空白页告警、OCR缺失报错、长度超限拒绝、长文全覆盖与重叠、中文/英文检索。

伪造段落/引文拦截、数字/正负号/百分号校验、单位/实体槽位校验、限定语保留、Unicode位置、事实合并、冲突分组边界。

429重试、401不重试、超时重试上限、模型拒绝/未完成/Schema错误、有界修复、失败不入缓存、成功缓存复用、配置失效、断点恢复、取消与租约释放、租约互斥、并发上限、部分失败可见、复核历史与导出、CLI全流程。

## 尚未验证及限制

- 未使用真实模型 API Key；模型网络交互通过 MockTransport 模拟。
- 未用真实公司材料测量语义准确率、覆盖率、引用支持率、人工节省时间。
- GitHub Actions 多系统/多版本矩阵已配置，是否通过以远端执行记录为准；本地通过不代表远端已通过。
- 当前没有 OCR、团队权限、自动邮箱同步、分布式执行或多机数据库支持。
- 8个HTTP尝试/块是默认重试+修复上限，不代表实际用量；网络失败未返回usage时无法完整核算tokens。
- 外部调用结果落库前崩溃可能重复调用，不保证exactly-once计费。

## 复现

```bash
python -m pip install -r requirements-dev.lock
python -m pip install --no-deps --no-build-isolation -e .
python -m pytest --cov=research_agent --cov-report=term-missing
python -m ruff check src tests
python -m ruff format --check src tests
research-agent --data-dir artifacts/validation ingest samples/demo.json
research-agent --data-dir artifacts/validation run --out artifacts/reports
research-agent evaluate evals/gold.jsonl --out artifacts/evaluation.json
```
