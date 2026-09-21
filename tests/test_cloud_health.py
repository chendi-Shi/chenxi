import json
from datetime import UTC, datetime

import pytest

pytest.importorskip("cryptography")
from cryptography.fernet import Fernet
from test_cloud import Remote, report

from research_agent import cloud, daily
from research_agent.cloud_health import snapshot
from research_agent.daily_config import DailyConfig
from research_agent.models import digest


def config(path):
    return DailyConfig(recipient="test@qq.com", sender="test@qq.com", data_dir=path)


def insert(conn, cfg, day, status, suffix=""):
    run_id = day + "-" + digest(cfg.recipient)[:12] + suffix
    conn.execute(
        "INSERT INTO outbox VALUES(?,?,?,?,?,?)",
        (run_id, day, status, json.dumps(report()), day + "T01:00:00+00:00", ""),
    )
    conn.commit()


def test_deadline_timezone_and_test_mail_not_counted(workspace):
    cfg = config(workspace)
    with daily.database(workspace) as db:
        early, code = snapshot(db, cfg, datetime(2026, 9, 21, 1, 49, tzinfo=UTC))
        assert early["status"] == "not_due" and code == 0
        insert(db, cfg, "2026-09-21", "sent", "-migration-test")
        late, code = snapshot(db, cfg, datetime(2026, 9, 21, 1, 50, tzinfo=UTC))
        assert code == 2 and late["errors"] == ["daily_delivery_deadline_missed"]
        insert(db, cfg, "2026-09-21", "sent")
        healthy, code = snapshot(db, cfg, datetime(2026, 9, 21, 2, tzinfo=UTC))
        assert healthy["status"] == "healthy" and code == 0
        assert cfg.recipient not in json.dumps(healthy)
        assert healthy["receipt_verified"] is False
        insert(db, cfg, "2026-09-20", "unknown")
        unhealthy, code = snapshot(db, cfg, datetime(2026, 9, 21, 2, tzinfo=UTC))
        assert code == 2 and unhealthy["errors"] == ["delivery_uncertain_check_mailbox"]


async def test_cloud_health_has_no_remote_write_model_or_smtp(workspace, monkeypatch):
    remote, key = Remote(), Fernet.generate_key()
    cfg = config(workspace)
    store = remote.store(workspace / "seed", key)
    with daily.database(store.directory) as db:
        from zoneinfo import ZoneInfo

        today = datetime.now(ZoneInfo(cfg.timezone)).date().isoformat()
        insert(db, cfg, today, "sent")
        store.save(db, initialize=True)
    commits = remote.commits
    monkeypatch.chdir(workspace.resolve())
    monkeypatch.setenv("DAILY_CONFIG_JSON", cfg.model_dump_json())
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "read-only")
    monkeypatch.setenv("STATE_ENCRYPTION_KEY", key.decode())
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setattr(
        cloud, "GitHubState", lambda repo, token, key, path: remote.store(path, key)
    )

    async def forbidden(*args, **kwargs):
        pytest.fail("health must not invoke business processing")

    monkeypatch.setattr(daily, "run", forbidden)
    monkeypatch.setattr(daily, "check", forbidden)
    result, code = await cloud.execute("health")
    assert code == 0 and result["status"] == "healthy"
    assert remote.commits == commits
