"""Authenticated, single-worker HF service for on-demand daily report generation.

No SMTP credentials or scheduler in this process: HF blocks SMTP egress and sleeps.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, Response

from .daily import database, export, prepare, run_lock
from .daily_config import DailyConfig


class Service:
    def __init__(self):
        self.token = os.environ.get("APP_ACCESS_TOKEN", "")
        if len(self.token) < 32:
            raise ValueError("APP_ACCESS_TOKEN must contain at least 32 characters")
        self.config = DailyConfig(
            recipient="preview@example.invalid",
            sender="preview@example.invalid",
            model=os.environ.get("BAILIAN_MODEL", "qwen-plus"),
            base_url=os.environ.get(
                "BAILIAN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
            ),
            free_quota_only_confirmed=os.environ.get("FREE_QUOTA_ONLY_CONFIRMED") == "true",
            data_dir=Path(os.environ.get("DAILY_DATA_DIR", "data/space")),
        )
        self.key = os.environ.get("DASHSCOPE_API_KEY", "")
        self.lock = threading.Lock()
        self.busy = False
        self.last_start = float("-inf")
        self.error = ""
        self.report = None
        latest = self.config.data_dir / "latest.json"
        if latest.exists():
            self.report = json.loads(latest.read_text(encoding="utf-8"))

    def status(self):
        with self.lock:
            report = self.report
            return {
                "busy": self.busy,
                "ready": bool(self.key and self.config.free_quota_only_confirmed),
                "error": self.error,
                "model": self.config.model,
                "report": None
                if report is None
                else {
                    k: report[k]
                    for k in ("day", "collected_at", "summary_status", "coverage", "degraded")
                },
                "article_count": len(report["articles"]) if report else 0,
                "summary_count": len(report["summaries"]) if report else 0,
                "retry_after": max(0, int(300 - (time.monotonic() - self.last_start)))
                if self.last_start != float("-inf")
                else 0,
            }

    def start(self):
        with self.lock:
            if not self.key or not self.config.free_quota_only_confirmed:
                raise HTTPException(503, "请配置百炼密钥并确认免费额度用完即停")
            if self.busy:
                raise HTTPException(409, "已有任务正在运行")
            if time.monotonic() - self.last_start < 300:
                raise HTTPException(429, "每次生成至少间隔五分钟")
            self.busy = True
            self.error = ""
            self.last_start = time.monotonic()
            threading.Thread(target=self.work, daemon=True).start()

    def work(self):
        from .reporting import atomic_write

        try:
            with run_lock(self.config.data_dir), database(self.config.data_dir) as conn:
                report = asyncio.run(
                    prepare(self.config, {"api_key": self.key}, conn, datetime.now(UTC))
                )
                export(self.config, report, preview=True)
                atomic_write(
                    self.config.data_dir / "latest.json", json.dumps(report, ensure_ascii=False)
                )
            with self.lock:
                self.report = report
        except Exception:
            # Never return provider bodies, credentials, source text, or tracebacks to clients.
            with self.lock:
                self.error = "生成失败；请检查服务配置、数据源网络与可用额度"
        finally:
            with self.lock:
                self.busy = False


@asynccontextmanager
async def lifespan(app):
    app.state.service = Service()
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def authorized(authorization: str = Header(default="")):
    if not hmac.compare_digest(
        authorization.encode(), ("Bearer " + app.state.service.token).encode()
    ):
        raise HTTPException(401, "请输入正确的访问密钥")


@app.middleware("http")
async def headers(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "connect-src 'self'; frame-src 'self' blob:; base-uri 'none'; form-action 'none'"
    )
    return response


@app.get("/healthz")
def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def home():
    return (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")


@app.get("/api/status", dependencies=[Depends(authorized)])
def status():
    return app.state.service.status()


@app.post("/api/run", status_code=202, dependencies=[Depends(authorized)])
def generate():
    app.state.service.start()
    return {"status": "started"}


@app.get("/api/report", dependencies=[Depends(authorized)])
def report():
    from .daily_mail import render

    with app.state.service.lock:
        current = app.state.service.report
    if current is None:
        raise HTTPException(404, "尚未生成日报")
    _, html = render(current)
    html = html.replace("<a style=", '<a target="_blank" rel="noopener noreferrer" style=')
    return Response(html, media_type="text/html")
