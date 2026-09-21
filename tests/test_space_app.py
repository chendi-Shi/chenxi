import threading

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from research_agent import space_app


@pytest.fixture
def web(monkeypatch, workspace):
    monkeypatch.setenv("APP_ACCESS_TOKEN", "a" * 40)
    monkeypatch.setenv("DAILY_DATA_DIR", str(workspace))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "model-secret")
    monkeypatch.setenv("FREE_QUOTA_ONLY_CONFIRMED", "true")
    with TestClient(space_app.app) as client:
        yield client


AUTH = {"Authorization": "Bearer " + "a" * 40}


def test_web_auth_and_no_secrets(web):
    assert web.get("/healthz").json() == {"status": "ok"}
    assert web.get("/").status_code == 200
    for path, method in (("/api/status", "get"), ("/api/report", "get"), ("/api/run", "post")):
        assert getattr(web, method)(path).status_code == 401
        assert (
            getattr(web, method)(
                path, headers={"Authorization": "Bearer 错误".encode()}
            ).status_code
            == 401
        )
    reply = web.get("/api/status", headers=AUTH)
    assert reply.json()["ready"] is True
    assert "model-secret" not in reply.text
    assert "a" * 40 not in reply.text
    assert reply.headers["cache-control"] == "no-store"
    assert web.get("/api/report", headers=AUTH).status_code == 404


def test_job_concurrency_cooldown_and_error_redaction(web, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    async def fail(*args):
        entered.set()
        release.wait(5)
        raise RuntimeError("model-secret should never be returned")

    monkeypatch.setattr(space_app, "prepare", fail)
    service = space_app.app.state.service
    assert web.post("/api/run", headers=AUTH).status_code == 202
    assert entered.wait(2)
    try:
        assert web.post("/api/run", headers=AUTH).status_code == 409
    finally:
        release.set()
    # Synchronize completion without relying on timing sleeps.
    for thread in threading.enumerate():
        if thread.name.endswith("(work)"):
            thread.join(5)
    assert service.busy is False
    assert web.post("/api/run", headers=AUTH).status_code == 429
    assert "model-secret" not in web.get("/api/status", headers=AUTH).text
    assert service.error


def test_missing_quota_guard_and_short_access_token(web, monkeypatch):
    space_app.app.state.service.config.free_quota_only_confirmed = False
    assert web.post("/api/run", headers=AUTH).status_code == 503
    monkeypatch.setenv("APP_ACCESS_TOKEN", "short")
    with pytest.raises(ValueError, match="at least 32"):
        space_app.Service()


def test_successful_report_persists_and_escapes_content(web, monkeypatch):
    report = {
        "id": "2026-09-21-test",
        "day": "2026-09-21",
        "collected_at": "2026-09-21T09:00:00+08:00",
        "lookback_hours": 36,
        "summary_status": "<script>untrusted()</script>",
        "degraded": True,
        "articles": [],
        "summaries": [],
        "coverage": [],
    }

    async def prepared(*args):
        return report

    monkeypatch.setattr(space_app, "prepare", prepared)
    service = space_app.app.state.service
    service.work()
    assert not service.error
    result = web.get("/api/report", headers=AUTH)
    assert result.status_code == 200
    assert "<script>untrusted()" not in result.text
    assert "&lt;script&gt;" in result.text
    assert web.get("/api/status", headers=AUTH).json()["report"]["degraded"] is True
    assert space_app.Service().report == report
