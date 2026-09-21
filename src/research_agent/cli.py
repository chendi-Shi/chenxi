from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError

from . import __version__
from .config import load_settings
from .evaluation import evaluate
from .pipeline import Pipeline
from .providers import ProviderError, build_provider
from .reporting import atomic_write, export_report
from .store import Store


def date_bound(value: str | None, tz: str):
    if not value:
        return ""
    zone = UTC if tz == "UTC" else ZoneInfo(tz)
    return datetime.combine(date.fromisoformat(value), time.min, zone).astimezone(UTC).isoformat()


def parser():
    root = argparse.ArgumentParser(
        prog="research-agent", description="Evidence-first research pipeline"
    )
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--config", type=Path)
    root.add_argument("--data-dir", type=Path)
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create or validate local database")
    ingest = commands.add_parser("ingest", help="Incrementally ingest a local file or directory")
    ingest.add_argument("path", type=Path)
    run = commands.add_parser("run", help="Plan and execute document extraction")
    run.add_argument("--provider", choices=["rules", "openai"])
    run.add_argument("--model")
    run.add_argument("--since", help="Inclusive publication date YYYY-MM-DD")
    run.add_argument("--until", help="Exclusive publication date YYYY-MM-DD")
    run.add_argument("--timezone", default="UTC")
    run.add_argument("--document", action="append")
    run.add_argument("--out", type=Path, default=Path("exports"))
    resume = commands.add_parser(
        "resume", help="Resume interrupted jobs; optional retry of failed chunks"
    )
    resume.add_argument("run_id")
    resume.add_argument("--retry-failed", action="store_true")
    resume.add_argument("--out", type=Path, default=Path("exports"))
    status = commands.add_parser("status", help="Read run state, job counters and metrics")
    status.add_argument("run_id", nargs="?")
    search = commands.add_parser("search", help="Local FTS5 BM25 search, with CJK tokens")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    source = commands.add_parser("source", help="Read canonical text and retained source metadata")
    source.add_argument("document_id")
    export = commands.add_parser("export", help="Export existing run with current review history")
    export.add_argument("run_id")
    export.add_argument("--out", type=Path, default=Path("exports"))
    review = commands.add_parser(
        "review", help="Append attributed review; previous reviews are retained"
    )
    review.add_argument("run_id")
    review.add_argument("--reviewer", required=True)
    review.add_argument(
        "--status", choices=["pending", "accepted", "needs_revision"], required=True
    )
    review.add_argument("--note", default="")
    evaluation = commands.add_parser("evaluate", help="Run a JSONL gold-evidence benchmark")
    evaluation.add_argument("dataset", type=Path)
    evaluation.add_argument("--out", type=Path)
    return root


async def dispatch(args):
    settings = load_settings(
        args.config,
        data_dir=args.data_dir,
        provider=getattr(args, "provider", None),
        model=getattr(args, "model", None),
    )
    store = Store(settings.data_dir)
    command = args.command
    if command == "init":
        return {"database": str(store.db), "schema_version": 1}, 0
    if command == "ingest":
        result = store.ingest(args.path, settings)
        return result, 2 if result["errors"] else 0
    if command == "run":
        since, until = date_bound(args.since, args.timezone), date_bound(args.until, args.timezone)
        if since and until and since >= until:
            raise ValueError("since_must_precede_until")
        pipeline = Pipeline(store, settings)
        run_id = pipeline.plan(args.document, since, until)
        print(json.dumps({"event": "run_planned", "run_id": run_id}), file=sys.stderr, flush=True)
        result = await pipeline.execute(run_id)
        outputs = export_report(store, run_id, args.out)
        return {
            "run_id": run_id,
            "status": result["status"],
            "metrics": result["metrics"],
            "exports": outputs,
        }, 0 if result["status"] == "completed" else 2
    if command == "resume":
        # Resume with the run's original model/chunk/prompt-compatible configuration.
        from .config import Settings

        original = Settings.model_validate_json(store.get_run(args.run_id)["config_json"])
        original.data_dir = settings.data_dir
        result = await Pipeline(store, original).execute(args.run_id, args.retry_failed)
        return {
            "run_id": args.run_id,
            "status": result["status"],
            "metrics": result["metrics"],
            "exports": export_report(store, args.run_id, args.out),
        }, 0 if result["status"] == "completed" else 2
    if command == "status":
        if not args.run_id:
            with store.connection() as conn:
                return [
                    dict(row)
                    for row in conn.execute(
                        "SELECT id,status,created_at FROM runs ORDER BY created_at DESC,id"
                    )
                ], 0
        run = store.get_run(args.run_id)
        jobs = store.jobs(args.run_id)
        return {
            "run_id": args.run_id,
            "status": run["status"],
            "config_key": run["config_key"],
            "jobs": [
                {
                    k: j[k]
                    for k in (
                        "chunk_id",
                        "document_id",
                        "state",
                        "error",
                        "attempts",
                        "input_tokens",
                        "output_tokens",
                    )
                }
                for j in jobs
            ],
        }, 0
    if command == "search":
        if not 1 <= args.limit <= 100:
            raise ValueError("limit_must_be_between_1_and_100")
        return store.search(args.query, args.limit), 0
    if command == "source":
        return {
            "id": args.document_id,
            "document": store.get_document(args.document_id).model_dump(),
            "sources": store.sources(args.document_id),
        }, 0
    if command == "export":
        return export_report(store, args.run_id, args.out), 0
    if command == "review":
        store.add_review(args.run_id, args.reviewer, args.status, args.note)
        return {"status": "review_recorded", "run_id": args.run_id}, 0
    if command == "evaluate":
        provider = build_provider(settings)
        try:
            result = await evaluate(args.dataset, settings, provider)
        finally:
            await provider.close()
        if args.out:
            atomic_write(args.out, json.dumps(result, ensure_ascii=False, indent=2))
        return result, 0
    raise ValueError("unknown_command")


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result, code = asyncio.run(dispatch(args))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return code
    except ValidationError as exc:
        # Pydantic's default text includes invalid input values; print only field/type.
        issues = [{"field": ".".join(map(str, e["loc"])), "type": e["type"]} for e in exc.errors()]
        print(
            json.dumps({"error": "invalid_configuration_or_input", "issues": issues}),
            file=sys.stderr,
        )
        return 1
    except (ValueError, OSError, ZoneInfoNotFoundError, ProviderError) as exc:
        message = (
            str(exc)
            if type(exc) is ValueError or isinstance(exc, ProviderError)
            else type(exc).__name__
        )
        print(json.dumps({"error": message}), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('{"error":"interrupted; use status and resume"}', file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
