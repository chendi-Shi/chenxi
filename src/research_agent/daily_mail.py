from __future__ import annotations

import hashlib
import html
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate

from .daily_config import DailyConfig


def render(report: dict) -> tuple[str, str]:
    lines = [
        f"英伟达 & 腾讯资讯日报 · {report['day']}",
        "",
        f"抓取时间：{report['collected_at']}；回看 {report['lookback_hours']} 小时，过滤已发送链接。",
        f"摘要状态：{report['summary_status']}",
        "",
    ]
    summaries = {item["article_id"]: item for item in report["summaries"]}
    for company in ("英伟达", "腾讯"):
        lines += [company, ""]
        articles = [a for a in report["articles"] if a["company"] == company]
        articles.sort(
            key=lambda a: {"high": 0, "medium": 1, "low": 2}.get(
                summaries.get(a["id"], {}).get("priority"), 3
            )
        )
        if not articles:
            lines.append("本次未收录新增资讯；请结合下方采集状态判断覆盖情况。")
        for index, a in enumerate(articles, 1):
            summary = summaries.get(a["id"])
            lines += [
                f"{index}. {a['title']}",
                f"来源：{a['source']} | 发布时间：{a['published_at']}",
                f"资料范围：{a['level']}",
            ]
            if summary:
                lines += [
                    f"[{summary['kind']}] {summary['summary']}",
                    f"证据摘录：{summary['quote']}",
                ]
            else:
                lines.append("未生成 AI 摘要，请阅读原文。")
            lines += [f"原文：{a['url']}", ""]
    lines += ["采集状态（仅覆盖配置的资讯源，不代表全网）"]
    for source in report["coverage"]:
        lines.append(
            f"{source['source']}：{source['status']}；窗口内 {source['recent_items']} 条"
            + (f"；{source['detail']}" if source["detail"] else "")
        )
    lines += [
        "",
        "本邮件整理公开资料，标题级消息未核对全文；媒体观点不等于已发生事实。",
        f"运行编号：{report['id']}",
    ]
    text = "\n".join(lines)
    escaped = html.escape(text)
    # All source and model content is escaped; no model-produced HTML or links.
    body = (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><body '
        'style="font-family:Arial,sans-serif;background:#f4f6f8;padding:24px">'
        '<main style="max-width:850px;margin:auto;background:white;padding:28px;'
        'border-top:5px solid #2463a8"><pre style="white-space:pre-wrap;'
        'word-wrap:break-word;font-family:inherit;line-height:1.8">'
        + escaped
        + "</pre></main></body></html>"
    )
    return text, body


def message(config: DailyConfig, report: dict) -> EmailMessage:
    text, body = render(report)
    msg = EmailMessage()
    msg["From"] = config.sender
    msg["To"] = config.recipient
    degraded = " · 采集/摘要不完整" if report["degraded"] else ""
    msg["Subject"] = f"英伟达 & 腾讯资讯日报 | {report['day']}{degraded}"
    msg["Date"] = formatdate(localtime=False)
    domain = config.sender.split("@", 1)[1]
    msg["Message-ID"] = (
        f"<chenxi.{hashlib.sha256(report['id'].encode()).hexdigest()[:32]}@{domain}>"
    )
    msg.set_content(text)
    msg.add_alternative(body, subtype="html")
    return msg


def connect(config: DailyConfig, password: str):
    server = smtplib.SMTP_SSL(
        config.smtp_host, config.smtp_port, timeout=30, context=ssl.create_default_context()
    )
    try:
        server.login(config.sender, password)
    except BaseException:
        server.close()
        raise
    return server
