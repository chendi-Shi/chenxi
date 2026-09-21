# 配置、恢复与运行边界

## 配置

配置文件通过 `--config config.local.toml` 指定，路径相对当前工作目录。优先级 CLI > 环境 > TOML > 默认。

| 参数 | 默认 | 作用 |
|---|---:|---|
| data_dir | data | SQLite和原始 blobs |
| provider | rules | rules/openai |
| model | 空 | openai 时必填 |
| base_url | https://api.openai.com/v1 | Responses API根地址 |
| concurrency | 3 | 并发分块数，最多16 |
| request_timeout | 60 | 单次请求总超时秒数 |
| max_retries | 3 | HTTP额外重试数 |
| repair_attempts | 1 | Schema/证据修复数 |
| chunk_chars | 6000 | 单块字符上限 |
| segment_chars | 1400 | 单段字符上限 |
| overlap_segments | 1 | 重叠段数 |
| max_file_mb | 20 | 文件大小上限 |
| max_document_chars | 1000000 | 正文字符上限 |
| max_pdf_pages | 300 | PDF页数上限 |
| watchlist | [] | 仅调整报告排序 |

环境：RESEARCH_DATA_DIR、RESEARCH_PROVIDER、RESEARCH_MODEL、RESEARCH_BASE_URL。密钥优先 RESEARCH_API_KEY，其次 OPENAI_API_KEY。没有将密钥放进 Settings、缓存或数据库。

PDF 每页展开流另有10 MB检查，但解压流本身仍可能占内存，没有独立解析子进程或硬内存配额，不能承诺抵御恶意压缩文件。

## 日期与退出码

since 包含零点，until 不包含零点。默认UTC，可选IANA时区，Windows安装tzdata。JSON ISO时间和邮件Date转UTC，无时区输入按UTC。无效日期发告警并置空，原值保留在原始blob；过滤日期时排除缺失值。

- 0：命令完成，不代表人工审核完成。
- 1：配置/输入/操作错误。
- 2：导入有失败文件，或运行partial/failed。
- 130：用户中断，可检查状态并恢复。

run ID 先输出到 stderr，stdout 为 JSON，便于脚本调用。

## 恢复

```bash
research-agent --data-dir data/work status
research-agent --data-dir data/work status RUN_ID
research-agent --data-dir data/work resume RUN_ID --retry-failed
```

恢复使用该 run 固化的配置。提示词或工作流版本不匹配时拒绝，不混用版本；使用原代码版本恢复或创建新 run。成功块复用，失败块须明确指定重试。

| 错误 | 建议处理 |
|---|---|
| missing_RESEARCH_API_KEY | 设置有效且获准使用的密钥 |
| http_401 / http_403 | 检查账户权限，不会自动换密钥 |
| http_429 | 等待后恢复；新运行可降低并发 |
| network_or_timeout | 检查网络，必要时修改超时并新建运行 |
| invalid_extraction_schema | 检查模型结构化输出支持 |
| evidence_validation_failed | 核对原文和事件，不直接绕过校验 |
| rules_fact_limit_reduce_chunk_chars | 缩小分块，新建运行 |
| pdf_has_no_extractable_text_ocr_required | 外部OCR后导入文本 |
| encrypted_pdf | 使用有权解密的文件，不猜测密码 |
| another_pipeline_is_running_or_lease_not_expired | 确认前一任务结束；强杀后等120秒租约到期 |

## 备份与部署

停止运行后备份整个 data_dir，包含数据库及 blobs；不只复制有活动写入的 sqlite3 文件。SQLite 同步盘可能存在客户端文件争用，建议使用允许的非同步位置，不让多台机器共享同一库。

当前无Web服务、认证、多用户队列、邮件同步或定时调度。可在个人机器或专用执行环境运行 CLI；没有自动创建定时任务。若未来接入邮箱，需要另做增量游标、权限、附件和重复同步策略。
