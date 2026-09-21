import base64
import json
import smtplib
from datetime import UTC, datetime

import httpx
import pytest

pytest.importorskip("cryptography")
from cryptography.fernet import Fernet

from research_agent import daily, daily_mail
from research_agent.cloud import quality_gate
from research_agent.cloud_state import GitHubState, StateError
from research_agent.daily_config import DailyConfig
from research_agent.news_sources import Article, relevant, story_key


class Remote:
    def __init__(self):
        self.content = None
        self.sha = None
        self.commits = 0
        self.disconnect = False

    def handle(self, request):
        if request.method == "GET":
            if self.content is None:
                return httpx.Response(404)
            return httpx.Response(200, json={"sha": self.sha, "content": self.content})
        body = json.loads(request.content)
        assert body["branch"] == "codex/daily-state"
        if body.get("sha") != self.sha:
            return httpx.Response(409)
        self.content = body["content"]
        self.commits += 1
        self.sha = str(self.commits)
        if self.disconnect:
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(200, json={"content": {"sha": self.sha}})

    def store(self, path, key, repo="owner/repo"):
        return GitHubState(
            repo,
            "test-token",
            key,
            path,
            client=httpx.Client(
                base_url="https://api.github.com", transport=httpx.MockTransport(self.handle)
            ),
        )


def test_state_encrypted_roundtrip_and_concurrent_writers(workspace):
    remote, key = Remote(), Fernet.generate_key()
    a = remote.store(workspace / "a", key)
    with daily.database(a.directory) as conn:
        conn.execute("INSERT INTO delivered VALUES('private@qq.com','article','today')")
        conn.commit()
        a.save(conn, initialize=True)
    assert b"private@qq.com" not in base64.b64decode(remote.content)
    b, c = remote.store(workspace / "b", key), remote.store(workspace / "c", key)
    b.restore()
    c.restore()
    with daily.database(b.directory) as conn:
        assert conn.execute("SELECT recipient FROM delivered").fetchone()[0] == "private@qq.com"
        b.save(conn)
    with daily.database(c.directory) as conn:
        with pytest.raises(StateError, match="concurrent_writer"):
            c.save(conn)


def test_state_missing_wrong_key_wrong_audience_and_ambiguous_write(workspace):
    remote, key = Remote(), Fernet.generate_key()
    store = remote.store(workspace / "a", key)
    with pytest.raises(StateError, match="404"):
        store.restore()
    with daily.database(store.directory) as conn:
        with pytest.raises(StateError, match="not_restored"):
            store.save(conn)
        remote.disconnect = True
        with pytest.raises(StateError, match="ambiguous"):
            store.save(conn, initialize=True)
    assert remote.commits == 1  # no blind PUT retry
    wrong_key = remote.store(workspace / "b", Fernet.generate_key())
    with pytest.raises(StateError, match="decryption"):
        wrong_key.restore()
    wrong_repo = remote.store(workspace / "c", key, "other/repo")
    with pytest.raises(StateError, match="payload"):
        wrong_repo.restore()


def report():
    return {
        "id": "test",
        "day": "2026-09-21",
        "collected_at": "2026-09-21T09:00:00+08:00",
        "lookback_hours": 36,
        "articles": [],
        "summaries": [],
        "degraded": False,
        "summary_status": "no new articles",
        "coverage": [
            {"company": company, "source": "test", "status": "ok", "recent_items": 0, "detail": ""}
            for company in ("英伟达", "腾讯")
        ],
    }


@pytest.fixture
def delivery(monkeypatch, workspace):
    cfg = DailyConfig(
        recipient="private@qq.com",
        sender="private@qq.com",
        data_dir=workspace,
        free_quota_only_confirmed=True,
    )
    monkeypatch.setattr(daily, "credentials", lambda _: {"api_key": "key", "smtp_password": "pw"})

    async def prepare(*args, **kwargs):
        return report()

    monkeypatch.setattr(daily, "prepare", prepare)
    return cfg


@pytest.mark.parametrize("failure_phase, expected_sent", [("sending", 0), ("sent", 1)])
async def test_checkpoint_failures_never_cause_blind_resend(
    delivery, monkeypatch, failure_phase, expected_sent
):
    sent, persisted = [], []

    class SMTP:
        def send_message(self, msg):
            sent.append(msg)
            return {}

        def close(self):
            pass

    def checkpoint(conn):
        phase = conn.execute("SELECT status FROM outbox").fetchone()[0]
        if phase == failure_phase:
            raise StateError("github_state_network_ambiguous")
        persisted.append(phase)

    monkeypatch.setattr(daily_mail, "connect", lambda *args: SMTP())
    with pytest.raises(StateError):
        await daily.run(delivery, send=True, checkpoint=checkpoint)
    assert len(sent) == expected_sent
    assert persisted[-1] == ("prepared" if failure_phase == "sending" else "sending")


async def test_unknown_from_yesterday_blocks_today(delivery, monkeypatch):
    calls = []

    class SMTP:
        def send_message(self, msg):
            calls.append(msg)
            raise smtplib.SMTPServerDisconnected("after DATA")

        def close(self):
            pass

    monkeypatch.setattr(daily_mail, "connect", lambda *args: SMTP())
    for day in (20, 21):
        result, code = await daily.run(
            delivery, send=True, now=datetime(2026, 9, day, 1, tzinfo=UTC)
        )
        assert code == 2 and result["status"] == "delivery_uncertain_check_mailbox"
    assert len(calls) == 1


async def test_quality_gate_stops_smtp(delivery, monkeypatch):
    def no_smtp(*args):
        pytest.fail("SMTP must not connect after rejected report")

    def reject(data):
        raise ValueError("quality_gate_insufficient_verified_summaries")

    monkeypatch.setattr(daily_mail, "connect", no_smtp)
    with pytest.raises(ValueError, match="quality_gate"):
        await daily.run(delivery, send=True, delivery_policy=reject)


def test_quality_gate_rejects_uncovered_company_and_failed_empty_sources():
    data = report()
    quality_gate(data)
    data["coverage"][0]["status"] = "failed"
    with pytest.raises(ValueError, match="uncovered"):
        quality_gate(data)
    data = report()
    data["articles"] = [{}, {}, {}]
    data["summaries"] = [{}]
    with pytest.raises(ValueError, match="summaries"):
        quality_gate(data)


def test_reprints_and_entertainment_noise():
    def article(title):
        return Article(
            id="x",
            company="腾讯",
            title=title,
            url="https://example.com/",
            source="媒体",
            published_at="2026-09-21T00:00:00+00:00",
            level="标题",
            text=title,
        )

    assert story_key(article("腾讯发布财报 - 来源A")) == story_key(article("腾讯发布财报 - 来源B"))
    assert not relevant(article("腾讯热度年冠口碑逆袭 - 娱乐新闻"))
    assert not relevant(article("腾讯体育社区·汇聚心跳"))
    assert relevant(article("腾讯财报：影视业务营收增长，收视率提升"))


async def test_fresh_runner_recovers_remote_sending_and_refuses_duplicate(
    delivery, monkeypatch, workspace
):
    remote, key, messages = Remote(), Fernet.generate_key(), []
    store = remote.store(workspace / "remote", key)
    with daily.database(delivery.data_dir) as db:
        store.save(db, initialize=True)

    class SMTP:
        def send_message(self, msg):
            messages.append(msg)
            return {}

        def close(self):
            pass

    def checkpoint(db):
        phase = db.execute("SELECT status FROM outbox").fetchone()[0]
        if phase == "sent":
            raise StateError("simulated_crash_after_acceptance")
        store.save(db)

    monkeypatch.setattr(daily_mail, "connect", lambda *args: SMTP())
    with pytest.raises(StateError):
        await daily.run(delivery, send=True, checkpoint=checkpoint)
    recovered = remote.store(workspace / "fresh-runner", key)
    recovered.restore()
    config = delivery.model_copy(update={"data_dir": recovered.directory})
    result, code = await daily.run(config, send=True, checkpoint=recovered.save)
    assert code == 2 and result["status"] == "delivery_uncertain_check_mailbox"
    assert len(messages) == 1
