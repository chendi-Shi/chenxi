from __future__ import annotations

import hashlib
import html
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate

from .daily_config import DailyConfig
from .news_sources import safe_url


def render(report: dict) -> tuple[str, str]:
    lines = [
        f"英伟达 & 腾讯资讯日报 · {report['day']}",
        "",
        f"抓取时间：{report['collected_at']}；回看 {report['lookback_hours']} 小时，过滤已发送链接。",
        f"摘要状态：{report['summary_status']}",
        "",
    ]
    summaries = {item["article_id"]: item for item in report["summaries"]}
    blocks = [
        '<h1 style="font-size:26px;margin:0;color:#172d49">英伟达 &amp; 腾讯</h1>',
        '<p style="color:#667085">每日资讯 · ' + html.escape(report["day"]) + "</p>",
        '<p style="padding:12px;background:#eef3fa;font-size:13px">'
        + html.escape(report["summary_status"])
        + "</p>",
    ]
    for company in ("英伟达", "腾讯"):
        lines += [company, ""]
        blocks.append(
            '<h2 style="border-bottom:2px solid #2463a8;padding-bottom:10px">' + company + "</h2>"
        )
        articles = [a for a in report["articles"] if a["company"] == company]
        articles.sort(
            key=lambda a: {"high": 0, "medium": 1, "low": 2}.get(
                summaries.get(a["id"], {}).get("priority"), 3
            )
        )
        if not articles:
            lines.append("本次未收录新增资讯；请结合下方采集状态判断覆盖情况。")
            blocks.append("<p>本次未收录新增资讯；请结合下方采集状态判断覆盖情况。</p>")
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
            url = html.escape(safe_url(a["url"]), quote=True)
            blocks += [
                '<div style="padding:14px 0;border-bottom:1px solid #e5e9ef">',
                '<h3 style="font-size:17px;margin:0 0 8px"><a style="color:#173d70;text-decoration:none" href="'
                + url
                + '">'
                + html.escape(a["title"])
                + "</a></h3>",
                '<p style="color:#667085;font-size:12px">'
                + html.escape(a["source"] + " · " + a["published_at"] + " · " + a["level"])
                + "</p>",
            ]
            if summary:
                blocks += [
                    "<p>" + html.escape(summary["summary"]) + "</p>",
                    '<blockquote style="margin:12px 0;padding:10px 14px;border-left:3px solid #cbd5e1;'
                    'color:#58677a;background:#f8fafc;font-size:13px">'
                    + html.escape(summary["quote"])
                    + "</blockquote>",
                ]
            else:
                blocks.append(
                    '<p style="font-size:13px;color:#667085">未生成 AI 摘要，请阅读原文。</p>'
                )
            blocks.append(
                '<a style="color:#2463a8;font-size:13px" href="' + url + '">阅读原文 ↗</a></div>'
            )
    lines += ["采集状态（仅覆盖配置的资讯源，不代表全网）"]
    for source in report["coverage"]:
        lines.append(
            f"{source['source']}：{source['status']}；窗口内 {source['recent_items']} 条"
            + (f"；{source['detail']}" if source["detail"] else "")
        )
    blocks.append('<h2 style="font-size:16px;margin-top:28px">采集状态</h2>')
    for source in report["coverage"]:
        blocks.append(
            '<p style="font-size:12px;color:#667085">'
            + html.escape(
                f"{source['source']}：{source['status']}；窗口内 {source['recent_items']} 条；{source['detail']}"
            )
            + "</p>"
        )
    blocks.append(
        '<p style="font-size:12px;color:#667085">仅覆盖配置的公开资讯源。标题级消息未核对全文；媒体观点不等于已发生事实。<br>'
        + html.escape("抓取时间：" + report["collected_at"] + " · 运行编号：" + report["id"])
        + "</p>"
    )
    lines += [
        "",
        "本邮件整理公开资料，标题级消息未核对全文；媒体观点不等于已发生事实。",
        f"运行编号：{report['id']}",
    ]
    text = "\n".join(lines)
    # All source and model content is escaped; no model-produced HTML or links.
    body = (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><body '
        'style="font-family:Arial,sans-serif;background:#f4f6f8;padding:24px">'
        '<main style="max-width:760px;margin:auto;background:white;padding:28px;'
        'border-top:5px solid #2463a8;word-wrap:break-word;line-height:1.8">'
        + "".join(blocks)
        + "</main></body></html>"
    )
    return text, body


def message(config: DailyConfig, report: dict) -> EmailMessage:
    text, body = render(report)
    msg = EmailMessage()
    msg["From"] = config.sender
    msg["To"] = config.recipient
    degraded = " · 采集/摘要不完整" if report["degraded"] else ""
    msg["Subject"] = f"英伟达 & 腾讯资讯日报 | {report['day']}{degraded}"
    if report.get("delivery_kind") == "migration-test":
        msg.replace_header("Subject", "【云端迁移验证】" + str(msg["Subject"]))
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
