import json
import os
import smtplib
from datetime import UTC, datetime

import httpx
import pytest

from research_agent import daily, daily_mail
from research_agent.bailian import generate, review
from research_agent.daily_config import DailyConfig, protect
from research_agent.news_loop import execute
from research_agent.news_sources import (
    Article,
    Collection,
    Coverage,
    collect,
    parse_rss,
    parse_tencent,
)
from research_agent.providers import ProviderError


def sample():
    return Article(
        id="a",
        company="腾讯",
        title="业绩",
        url="https://example.com/a",
        published_at="2026-09-21T00:00:00+00:00",
        source="测试",
        level="标题",
        text="腾讯2026年营收12亿元，未经审计。",
    )


def draft(summary=None, quote=None):
    return json.dumps(
        {
            "items": [
                {
                    "article_id": "a",
                    "summary": summary or sample().text,
                    "quote": quote or sample().text,
                    "kind": "公司公告",
                    "priority": "high",
                }
            ]
        },
        ensure_ascii=False,
    )


def config(workspace):
    return DailyConfig(
        recipient="test@qq.com",
        sender="test@qq.com",
        data_dir=workspace,
        free_quota_only_confirmed=True,
    )


def test_reviewer_rejects_bad_citations_numbers_qualifiers_and_missing_items():
    for raw in (
        draft(quote="不存在的证据"),
        draft(summary="腾讯营收99亿元，未经审计。"),
        draft(summary="腾讯2026年营收12亿元。"),
        '{"items":[]}',
        "not json",
    ):
        with pytest.raises(ProviderError):
            review(raw, [sample()])
    assert len(review(draft(), [sample()]).items) == 1


async def test_loop_repairs_with_original_draft_and_reuses_checkpoint(workspace):
    calls = []

    async def fake(*args, feedback=None):
        calls.append(feedback)
        if len(calls) == 1:
            return draft(summary="营收99亿元，未经审计。"), {"input_tokens": 10, "output_tokens": 5}
        assert "99" in feedback["draft"]
        assert "unsupported_number" in feedback["error"]
        return draft(), {"input_tokens": 12, "output_tokens": 6}

    with daily.database(workspace) as conn:
        result, usage = await execute(conn, [sample()], "secret", "qwen-plus", "https://test", fake)
        assert result.items[0].article_id == "a"
        assert usage["input_tokens"] == 22
        await execute(conn, [sample()], "secret", "qwen-plus", "https://test", fake)
        assert len(calls) == 2
        assert (
            conn.execute(
                "SELECT count(*) FROM news_trace WHERE detail LIKE 'repair_requested%'"
            ).fetchone()[0]
            == 1
        )
        assert "secret" not in conn.execute("SELECT state FROM news_checkpoints").fetchone()[0]


async def test_loop_budget_survives_process_interruption(workspace):
    calls = 0

    async def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise KeyboardInterrupt()

    with daily.database(workspace) as conn:
        for _ in range(2):
            with pytest.raises(KeyboardInterrupt):
                await execute(conn, [sample()], "key", "model", "url", interrupted)
        with pytest.raises(ProviderError, match="budget_exhausted"):
            await execute(conn, [sample()], "key", "model", "url", interrupted)
        assert calls == 2


async def test_bailian_protocol_and_quota_no_retry():
    calls = []

    def handler(request):
        calls.append(request)
        body = json.loads(request.content)
        assert request.url.path == "/compatible-mode/v1/chat/completions"
        assert body["response_format"] == {"type": "json_object"}
        assert body["enable_thinking"] is False
        return httpx.Response(403, json={"error": {"code": "AllocationQuota.FreeTierOnly"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError, match="403"):
            await generate(
                [sample()],
                "secret",
                "qwen-plus",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
                client,
            )
    assert len(calls) == 1


async def test_bailian_success_and_feedback_contract():
    def handler(request):
        messages = json.loads(request.content)["messages"]
        assert messages[-2]["role"] == "assistant"
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": draft()}}],
                "usage": {"prompt_tokens": 9, "completion_tokens": 5},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        raw, usage = await generate(
            [sample()],
            "secret",
            "qwen-plus",
            "https://test",
            client,
            feedback={"draft": "{}", "error": "missing_items"},
        )
    assert review(raw, [sample()]).items
    assert usage["input_tokens"] == 9


def test_rss_and_tencent_parse():
    xml = "<rss><channel><item><title>News</title><link>https://example.com/?utm_source=x</link><pubDate>Mon, 21 Sep 2026 00:00:00 GMT</pubDate><description>Text</description></item></channel></rss>"
    assert parse_rss(xml, "腾讯", "source")[0].url == "https://example.com/"
    with pytest.raises(ValueError, match="unsafe_xml"):
        parse_rss("<!DOCTYPE x><rss/>", "腾讯", "s")
    html = '<article class="tc-blog-grid"><h2><a href="https://www.tencent.com/a/">新闻</a></h2><div class="tc-blogpost-date">2026年09月21日</div></article>'
    item = parse_tencent(html)[0]
    assert item.published_at == "2026-09-20T16:00:00+00:00"
    with pytest.raises(ValueError, match="structure_changed"):
        parse_tencent("<html>new layout</html>")


async def test_collection_filters_dates_and_reports_failed_sources():
    def handler(request):
        if request.url.host != "news.google.com":
            return httpx.Response(503)
        items = "".join(
            f"<item><title>{title}</title><link>https://example.com/{i}</link><pubDate>{date}</pubDate></item>"
            for i, title, date in [
                (1, "NVIDIA new 腾讯", "Mon, 21 Sep 2026 00:00:00 GMT"),
                (2, "old", "Mon, 01 Jan 2024 00:00:00 GMT"),
                (3, "future", "Tue, 22 Sep 2026 00:00:00 GMT"),
            ]
        )
        return httpx.Response(200, text="<rss><channel>" + items + "</channel></rss>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await collect(datetime(2026, 9, 21, 1, tzinfo=UTC), client=client)
    assert result.articles and all(a.title == "NVIDIA new 腾讯" for a in result.articles)
    assert len([s for s in result.coverage if s.status == "failed"]) == 2


@pytest.fixture
def fake_dependencies(monkeypatch):
    monkeypatch.setattr(daily, "credentials", lambda _: {"api_key": "key", "smtp_password": "pass"})

    async def fetch(*args, **kwargs):
        return Collection(
            articles=[sample()],
            coverage=[Coverage(source="test", company="腾讯", status="ok", recent_items=1)],
        )

    async def loop(*args):
        return review(draft(), [sample()]), {"input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(daily, "collect", fetch)
    monkeypatch.setattr(daily, "execute_loop", loop)


async def test_delivery_once_per_day_and_preview_no_send(workspace, monkeypatch, fake_dependencies):
    messages = []

    class Server:
        def send_message(self, message):
            messages.append(message)
            return {}

        def close(self):
            pass

    monkeypatch.setattr(daily_mail, "connect", lambda *a: Server())
    cfg = config(workspace)
    now = datetime(2026, 9, 21, 1, tzinfo=UTC)
    result, _ = await daily.run(cfg, now=now)
    assert result["status"] == "preview" and not messages
    result, code = await daily.run(cfg, send=True, now=now)
    assert code == 0 and result["status"] == "smtp_accepted"
    result, _ = await daily.run(cfg, send=True, now=now)
    assert result["status"] == "already_sent" and len(messages) == 1
    assert messages[0]["To"] == cfg.recipient


async def test_uncertain_delivery_never_automatically_retries(
    workspace, monkeypatch, fake_dependencies
):
    calls = []

    class Server:
        def send_message(self, message):
            calls.append(message)
            raise smtplib.SMTPServerDisconnected("connection lost after DATA")

        def close(self):
            pass

    monkeypatch.setattr(daily_mail, "connect", lambda *a: Server())
    for _ in range(2):
        result, code = await daily.run(config(workspace), send=True)
        assert code == 2 and result["status"] == "delivery_uncertain_check_mailbox"
    assert len(calls) == 1


async def test_due_and_free_quota_guard(workspace, fake_dependencies):
    cfg = config(workspace)
    result, _ = await daily.run(cfg, send=True, due=True, now=datetime(2026, 9, 21, 0, tzinfo=UTC))
    assert result["status"] == "not_due"
    cfg.free_quota_only_confirmed = False
    with pytest.raises(ValueError, match="free_quota_guard"):
        await daily.run(cfg, send=True)


def test_email_html_is_escaped(workspace):
    report = {
        "id": "test",
        "day": "2026-09-21",
        "collected_at": "now",
        "lookback_hours": 36,
        "summary_status": "<script>bad()</script>",
        "summaries": [],
        "articles": [],
        "coverage": [],
        "degraded": False,
    }
    _, html = daily_mail.render(report)
    assert "<script>" not in html and "&lt;script&gt;" in html


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI only")
def test_windows_credential_encryption_roundtrip():
    raw = b"not-a-real-secret"
    encrypted = protect(raw)
    assert raw not in encrypted
    assert protect(encrypted, decrypt=True) == raw
