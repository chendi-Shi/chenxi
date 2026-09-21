# Hugging Face 部署

## 已上传的 Space

目标：[fall2028/chenxi](https://huggingface.co/spaces/fall2028/chenxi)。已由 Static 改为 Docker，
上传的 Dockerfile 对应 `deploy/huggingface/pinned.Dockerfile`：构建时下载 GitHub 提交
`f81a999c6b605128c1fe7888891fc9cfaed1aa86`，使用 SHA-256 校验源码归档，再按锁文件安装运行依赖。
该提交已通过 GitHub 的 Python 3.11–3.14、Windows/Linux 测试矩阵。
固定提交避免上游分支变化自动改变部署；更新时需要同步提交号与归档哈希。

2026-09-21 部署状态：代码已上传，HF 提示 CPU Basic quota limit，Space 仍暂停。
进一步核验账号的新建页面：Docker 与 Gradio 选项被禁用，并明确要求付费套餐。
经用户授权暂停旧计算实例后，新服务仍无法恢复，随后已成功恢复旧实例。
因此不能把此问题视为只需暂停一个旧 Space 就能解决；新计算服务受当前账号套餐限制。
用户授权后，百炼密钥、网页访问密钥、百炼地址及免费额度保护四项 HF Secrets 已配置。
尚未完成线上生成验证；不能把此状态视作服务已经可用。
已转用 GitHub Actions：2026-09-21 云端试发成功并核对入箱，同日重跑去重通过，本机旧任务已停用。
当前实际运行入口是 [每日邮件工作流](https://github.com/chendi-Shi/chenxi/actions/workflows/daily-news.yml)，技术与运维详见 [运行手册](PRODUCTION_RUNBOOK.md)。

## 服务范围

`research_agent.space_app` 使用 FastAPI/Uvicorn 提供浏览器研究台，复用真实采集、百炼、
引文/数字校验及有界修正循环，不使用模拟数据。单 worker 后台线程执行任务，避免 HTTP
请求等待整条链路；所有受保护接口使用恒定时间 Bearer token 比较，无跨域授权。
同一进程只允许一个生成任务，每次至少间隔五分钟，保留原来的 SQLite 文件锁。
密钥只从环境读取，不返回前端、不记录到访问日志。HTML 报告继续转义所有来源及模型文本。

HF 只允许 80/443/8080 出站连接，QQ SMTP 的 465 端口不可用。
免费 CPU 会休眠；默认磁盘在重启时丢失，SQLite 检查点和历史仅保证当前磁盘生命周期。
因此本 Space **不承诺定时发信，不存放 QQ SMTP 授权码**。
当前使用 GitHub Actions 发信；SQLite 一致性快照经过认证加密，保存于独立状态分支。
任务缺失状态或无法持久化时拒绝发送，不使用易失 cache 维护发件账本。GitHub 定时调度仍可能延迟。
HF 仍保留先前配置的四项后端 Secrets；不要直接改为 Static SDK，静态部署的密钥暴露模型不同。

## 部署步骤

1. 在目标 HF 账号创建 **private / Docker / CPU Basic** Space。若账号需要升级付费套餐，先停止创建，不自动购买。
2. 将 `deploy/huggingface/README.md` 作为 Space 根 README；上传根 Dockerfile、pyproject.toml、
   requirements-dev.lock 和 src 目录。锁文件仅作版本约束，Docker 只安装 web 运行依赖。
   使用上传白名单，不能上传 `.git`、data、artifacts、daily.local.json 或任何凭据文件。
   可运行 `python deploy/huggingface/package_space.py --out artifacts/hf-space` 生成白名单目录；
   为防残留文件混入，目标目录必须不存在或为空。之后只上传该目录中的文件。
3. 在 Space Secrets 设置：
   - `DASHSCOPE_API_KEY`：当前百炼北京工作空间的 Key。
   - `BAILIAN_BASE_URL`：该工作空间实际的 compatible-mode/v1 地址。
   - `FREE_QUOTA_ONLY_CONFIRMED`：实际已在百炼控制台开启免费额度用完即停后设 `true`。
   - `APP_ACCESS_TOKEN`：随机生成至少 32 字符的独立网页访问密钥。
   - 可选 `BAILIAN_MODEL`：默认 qwen-plus。
4. 构建成功后，健康检查 `/healthz` 返回 ok。在页面输入访问密钥，生成一份真实日报并核对来源。

当前 HF 文档说明 Docker/Gradio Space 的创建可能需要付费账号，即使 CPU Basic 本身不按小时收费。
以账号创建页为准；静态 Space 不能运行这项 Python 服务。

## 本地验证

```sh
pip install -e '.[dev,web]'
# 用私密环境配置以上 secrets，不把值写进命令行或 Git
uvicorn research_agent.space_app:app --host 127.0.0.1 --port 7860 --workers 1
pytest tests/test_space_app.py
```

接口：GET `/healthz`、GET `/` 无需登录；GET `/api/status`、GET `/api/report`、
POST `/api/run` 必须提供 `Authorization: Bearer <APP_ACCESS_TOKEN>`。
无密钥启动失败；未配置百炼免费额度保护时拒绝生成。报告缺失返回 404，重复执行返回 409，
频率限制返回 429。页面显式展示摘要降级和采集覆盖率，不把部分结果标成完全成功。

官方参考：[网络与休眠](https://huggingface.co/docs/hub/spaces-overview)、
[Docker 运行与 Secrets](https://huggingface.co/docs/hub/spaces-sdks-docker)、
[存储生命周期](https://huggingface.co/docs/hub/spaces-storage)。
