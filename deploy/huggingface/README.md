---
title: 晨曦资讯研究台
emoji: 🌅
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
---

# 晨曦资讯研究台

英伟达与腾讯公开资讯采集、百炼中文摘要、引文和数字校验、有界修正循环。

使用 Space Secrets 配置 `DASHSCOPE_API_KEY`、`APP_ACCESS_TOKEN`（至少 32 字符）、
`BAILIAN_BASE_URL` 和 `FREE_QUOTA_ONLY_CONFIRMED=true`。
后者只应在百炼控制台实际开启免费额度用完即停后设置。

本服务用于按需生成和查看日报；HF 限制 SMTP 出站端口，因此不在 Space 中发送 QQ 邮件。
免费实例可能休眠，默认磁盘在重启时清空；页面明确显示这些限制。
邮件定时任务需要独立执行器。不要把 QQ 授权码上传到此 Space。

源代码与详细说明：https://github.com/chendi-Shi/chenxi/tree/codex/python-research-agent
