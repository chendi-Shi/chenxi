"""Public news -> Bailian -> durable outbox -> QQ SMTP. See docs/DAILY_MAIL.md."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import smtplib
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from . import daily_mail
from .bailian import summarize
from .daily_config import configure, credentials, load_config
from .models import digest
from .news_loop import execute as execute_loop
from .news_loop import setup as setup_loop
from .news_sources import Article, collect
from .providers import ProviderError
from .reporting import atomic_write


@contextmanager
def run_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "run.lock").open("a+b") as handle:
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("daily_run_already_active") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def database(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(directory / "daily.sqlite3", timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS outbox (
            id TEXT PRIMARY KEY, day TEXT NOT NULL, status TEXT NOT NULL,
            report TEXT NOT NULL, updated TEXT NOT NULL, error TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS delivered (
            recipient TEXT NOT NULL, article_id TEXT NOT NULL, sent_at TEXT NOT NULL,
            PRIMARY KEY(recipient, article_id)
        );
    """)
    try:
        yield conn
    finally:
        conn.close()


def update(conn, run_id, status, error=""):
    conn.execute(
        "UPDATE outbox SET status=?,updated=?,error=? WHERE id=?",
        (status, datetime.now(UTC).isoformat(), error, run_id),
    )
    conn.commit()


def mark_sent(conn, run_id, recipient, report):
    with conn:
        for a in report["articles"]:
            conn.execute(
                "INSERT OR IGNORE INTO delivered VALUES(?,?,?)",
                (recipient, a["id"], datetime.now(UTC).isoformat()),
            )
        conn.execute(
            "UPDATE outbox SET status='sent',error='',updated=? WHERE id=?",
            (datetime.now(UTC).isoformat(), run_id),
        )


async def prepare(config, secrets, conn, now):
    seen = {
        row[0]
        for row in conn.execute(
            "SELECT article_id FROM delivered WHERE recipient=?", (config.recipient,)
        )
    }
    collected = await collect(
        now, config.lookback_hours, config.articles_per_company, exclude_ids=seen
    )
    articles = [a for a in collected.articles if a.id not in seen]
    summaries, usage = [], {}
    status = "无新增条目，无需调用模型"
    failed = any(c.status != "ok" for c in collected.coverage)
    if articles:
        if not secrets["api_key"]:
            status, failed = "缺少百炼 API Key；仅原始资讯", True
        elif not config.free_quota_only_confirmed:
            status, failed = "尚未确认免费额度用完即停；未调用模型", True
        else:
            try:
                result, usage = await execute_loop(
                    conn, articles, secrets["api_key"], config.model, config.base_url
                )
                summaries = [item.model_dump() for item in result.items]
                status = f"百炼 {config.model}；引文和数字校验通过，未做人工语义核验"
                if usage.get("partial"):
                    failed = True
                    status = (
                        f"百炼 {config.model}；{len(summaries)}/{len(articles)} 条摘要通过校验；"
                        "其余仅保留原文，未做人工语义核验"
                    )
            except ProviderError as exc:
                status, failed = f"百炼摘要失败（{exc.code}）；仅原始资讯", True
    day = now.astimezone(ZoneInfo(config.timezone)).date().isoformat()
    return {
        "id": day + "-" + digest(config.recipient)[:12],
        "day": day,
        "collected_at": now.astimezone(ZoneInfo(config.timezone)).isoformat(),
        "lookback_hours": config.lookback_hours,
        "articles": [a.model_dump() for a in articles],
        "coverage": [c.model_dump() for c in collected.coverage],
        "summary_status": status,
        "summaries": summaries,
        "usage": usage,
        "degraded": failed,
    }


def export(config, report, preview=False):
    prefix = "preview-" if preview else ""
    root = config.data_dir / "exports" / (prefix + report["id"])
    text, html = daily_mail.render(report)
    atomic_write(root.with_suffix(".json"), json.dumps(report, ensure_ascii=False, indent=2))
    atomic_write(root.with_suffix(".txt"), text)
    atomic_write(root.with_suffix(".html"), html)
    return str(root.with_suffix(".html"))


async def run(config, send=False, due=False, now=None):
    now = now or datetime.now(UTC)
    local = now.astimezone(ZoneInfo(config.timezone))
    if due and local.hour < config.send_hour:
        return {"status": "not_due"}, 0
    secrets = credentials(config)
    if send and not secrets["smtp_password"]:
        raise ValueError("missing_SMTP_PASSWORD_run_configure")
    # Do not silently turn a misconfigured scheduled service into a headline service.
    if send and (not secrets["api_key"] or not config.free_quota_only_confirmed):
        raise ValueError("configure_bailian_and_confirm_free_quota_guard_first")
    run_id = local.date().isoformat() + "-" + digest(config.recipient)[:12]
    with run_lock(config.data_dir), database(config.data_dir) as conn:
        existing = conn.execute("SELECT * FROM outbox WHERE id=?", (run_id,)).fetchone()
        if send and existing and existing["status"] == "sent":
            return {"status": "already_sent", "id": run_id}, 0
        if send and existing and existing["status"] in {"sending", "unknown"}:
            return {"status": "delivery_uncertain_check_mailbox", "id": run_id}, 2
        report = (
            json.loads(existing["report"])
            if send and existing
            else await prepare(config, secrets, conn, now)
        )
        output = export(config, report, preview=not send)
        if not send:
            return {
                "status": "preview",
                "html": output,
                "articles": len(report["articles"]),
                "summary_status": report["summary_status"],
                "coverage": report["coverage"],
            }, 0
        if not existing:
            conn.execute(
                "INSERT INTO outbox(id,day,status,report,updated) VALUES(?,?,?,?,?)",
                (
                    run_id,
                    report["day"],
                    "prepared",
                    json.dumps(report, ensure_ascii=False),
                    now.isoformat(),
                ),
            )
            conn.commit()
        try:
            server = daily_mail.connect(config, secrets["smtp_password"])
        except (smtplib.SMTPException, OSError) as exc:
            update(conn, run_id, "prepared", type(exc).__name__)
            return {"status": "smtp_login_or_connection_failed", "id": run_id}, 2
        try:
            msg = daily_mail.message(config, report)
            update(conn, run_id, "sending")  # Commit before DATA; crash implies uncertain delivery.
            try:
                rejected = server.send_message(msg)
                if rejected:
                    raise smtplib.SMTPRecipientsRefused(rejected)
            except (smtplib.SMTPException, OSError) as exc:
                update(conn, run_id, "unknown", type(exc).__name__)
                return {"status": "delivery_uncertain_check_mailbox", "id": run_id}, 2
            mark_sent(conn, run_id, config.recipient, report)
        finally:
            server.close()
        return {
            "status": "smtp_accepted",
            "id": run_id,
            "html": output,
            "articles": len(report["articles"]),
            "degraded": report["degraded"],
        }, 0


async def check(config, live=False):
    secrets = credentials(config)
    result = {
        "api_key_present": bool(secrets["api_key"]),
        "smtp_password_present": bool(secrets["smtp_password"]),
        "free_quota_guard_confirmed_by_user": config.free_quota_only_confirmed,
        "model_probe": "not_run",
        "smtp_login": "not_run",
    }
    if live and all(secrets.values()) and config.free_quota_only_confirmed:
        probe = Article(
            id="probe",
            company="测试",
            title="虚构测试",
            url="https://example.com/",
            published_at=datetime.now(UTC).isoformat(),
            source="虚构数据",
            level="测试",
            text="虚构测试公司2025年营收12亿元，未经审计。",
        )
        try:
            await summarize([probe], secrets["api_key"], config.model, config.base_url)
            result["model_probe"] = "passed"
        except ProviderError as exc:
            result["model_probe"] = exc.code
        try:
            server = daily_mail.connect(config, secrets["smtp_password"])
            server.close()
            result["smtp_login"] = "passed"
        except (smtplib.SMTPException, OSError) as exc:
            result["smtp_login"] = type(exc).__name__
    ok = (
        all(secrets.values())
        and config.free_quota_only_confirmed
        and (not live or result["model_probe"] == result["smtp_login"] == "passed")
    )
    return result, 0 if ok else 2


async def dispatch(args):
    if args.command == "configure":
        return configure(args.config), 0
    config = load_config(args.config)
    if args.command == "check":
        return await check(config, args.live)
    if args.command == "run":
        return await run(config, args.send, args.due)
    with run_lock(config.data_dir), database(config.data_dir) as conn:
        if args.command == "trace":
            setup_loop(conn)
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT key,phase,attempt,detail,created FROM news_trace ORDER BY id DESC LIMIT 100"
                )
            ], 0
        if args.command == "status":
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT id,day,status,updated,error FROM outbox ORDER BY day DESC LIMIT 30"
                )
            ], 0
        row = conn.execute("SELECT * FROM outbox WHERE id=?", (args.id,)).fetchone()
        if not row or row["status"] not in {"sending", "unknown"}:
            raise ValueError("only_uncertain_delivery_can_be_resolved")
        if args.outcome == "received":
            mark_sent(conn, args.id, config.recipient, json.loads(row["report"]))
        else:
            update(conn, args.id, "prepared", "user_confirmed_not_received")
        return {"status": "resolved", "id": args.id, "outcome": args.outcome}, 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="英伟达与腾讯每日邮件")
    parser.add_argument("--config", type=Path, default=Path("daily.local.json"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("configure", help="Masked local credential setup")
    commands.add_parser("status")
    commands.add_parser("trace", help="Inspect generation, review and repair decisions")
    checking = commands.add_parser("check")
    checking.add_argument("--live", action="store_true")
    running = commands.add_parser("run")
    running.add_argument(
        "--send", action="store_true", help="Actually deliver to configured recipient"
    )
    running.add_argument(
        "--due", action="store_true", help="Skip before configured local send hour"
    )
    resolve = commands.add_parser("resolve")
    resolve.add_argument("id")
    resolve.add_argument("--outcome", choices=["received", "not-received"], required=True)
    args = parser.parse_args(argv)
    try:
        result, code = asyncio.run(dispatch(args))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return code
    except (ValueError, OSError, sqlite3.Error, ValidationError) as exc:
        # Never print config values, credentials, HTTP bodies, or Pydantic input reprs.
        print(json.dumps({"error": str(exc) if type(exc) is ValueError else type(exc).__name__}))
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
