"""GitHub Actions entry point; durable state is mandatory before sending."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import daily
from .cloud_state import GitHubState, StateError
from .daily_config import DailyConfig


def quality_gate(report):
    sources = report["coverage"]
    for company in ("英伟达", "腾讯"):
        if not any(s["company"] == company and s["status"] == "ok" for s in sources):
            raise ValueError("quality_gate_company_uncovered")
    total = len(report["articles"])
    if total and len(report["summaries"]) / total < 0.5:
        raise ValueError("quality_gate_insufficient_verified_summaries")
    if not total and any(s["status"] != "ok" for s in sources):
        raise ValueError("quality_gate_empty_with_source_failure")


def prune(conn):
    cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    with conn:
        conn.execute("DELETE FROM delivered WHERE sent_at < ?", (cutoff,))
        for table, column in (("news_checkpoints", "updated"), ("news_trace", "created")):
            if table in tables:
                conn.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff,))
        if "news_batches" in tables:
            conn.execute("DELETE FROM news_batches WHERE created < ?", (cutoff,))
        conn.execute(
            "DELETE FROM outbox WHERE status='sent' AND day < ?",
            ((datetime.now(UTC) - timedelta(days=30)).date().isoformat(),),
        )
    conn.execute("VACUUM")


async def execute(mode, outcome="", run_id=""):
    config = DailyConfig.model_validate_json(os.environ["DAILY_CONFIG_JSON"])
    # Never accept a data path from a secret; the cloud runner owns one fresh workspace.
    config.data_dir = Path("data/cloud")
    if config.smtp_host != "smtp.qq.com" or config.smtp_port != 465:
        raise ValueError("cloud_requires_qq_tls_endpoint")
    if mode == "verify":
        return await daily.check(config, live=True)
    store = GitHubState(
        os.environ["GITHUB_REPOSITORY"],
        os.environ["GITHUB_TOKEN"],
        os.environ["STATE_ENCRYPTION_KEY"],
        config.data_dir,
    )
    try:
        store.restore()
        with daily.database(config.data_dir) as conn:
            prune(conn)
            store.save(conn)  # Acquire a fresh CAS version before any provider work.
            if mode == "resolve":
                if outcome not in {"received", "not-received"}:
                    raise ValueError("explicit_resolution_required")
                row = conn.execute("SELECT * FROM outbox WHERE id=?", (run_id,)).fetchone()
                if not row or row["status"] not in {"sending", "unknown"}:
                    raise ValueError("only_uncertain_delivery_can_be_resolved")
                if outcome == "received":
                    daily.mark_sent(conn, run_id, config.recipient, json.loads(row["report"]))
                else:
                    daily.update(conn, run_id, "prepared", "operator_confirmed_not_received")
                store.save(conn)
                return {"status": "resolved", "id": run_id, "outcome": outcome}, 0
        return await daily.run(
            config,
            send=mode in {"send", "send-test"},
            due=mode == "send",
            checkpoint=store.save,
            delivery_policy=quality_gate,
            delivery_kind="migration-test" if mode == "send-test" else "daily",
        )
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["verify", "preview", "send", "send-test", "resolve"])
    parser.add_argument("--outcome", default="")
    parser.add_argument("--id", default="")
    args = parser.parse_args()
    started = time.monotonic()
    try:
        result, code = asyncio.run(execute(args.mode, args.outcome, args.id))
    except Exception as exc:
        # Only explicit state errors have a safe error vocabulary. Never emit HTTP bodies.
        safe = str(exc) if isinstance(exc, StateError) else type(exc).__name__
        if type(exc) is ValueError and str(exc).startswith("quality_gate_"):
            safe = str(exc)
        result, code = {"status": "failed", "error": safe}, 2
    result.pop("coverage", None)  # Source diagnostics belong in the private/encrypted record.
    result.pop("html", None)
    result["duration_seconds"] = round(time.monotonic() - started, 2)
    result["code_revision"] = os.environ.get("GITHUB_SHA", "local")
    print(json.dumps(result, ensure_ascii=False))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as output:
            output.write("## Daily research agent\n\n```json\n")
            output.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n```\n")
    if result.get("degraded"):
        print("::warning::Some sources or summaries degraded; inspect the report coverage.")
    if code:
        print("::error::Daily agent failed or needs delivery reconciliation. See job summary.")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
