"""Checkpointed generator/reviewer loop with a two-generation budget."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from .bailian import PROMPT, generate, review, salvage
from .models import digest, stable_json
from .providers import ProviderError


def setup(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS news_checkpoints (
            key TEXT PRIMARY KEY, state TEXT NOT NULL, updated TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS news_trace (
            id INTEGER PRIMARY KEY, key TEXT NOT NULL, phase TEXT NOT NULL,
            attempt INTEGER NOT NULL, detail TEXT NOT NULL, created TEXT NOT NULL
        );
    """)


def save(conn, key, state, detail=""):
    now = datetime.now(UTC).isoformat()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO news_checkpoints VALUES(?,?,?)",
            (key, json.dumps(state, ensure_ascii=False), now),
        )
        conn.execute(
            "INSERT INTO news_trace(key,phase,attempt,detail,created) VALUES(?,?,?,?,?)",
            (key, state["phase"], state["attempts"], detail, now),
        )


async def execute(conn, articles, api_key, model, base_url, generate_fn=None):
    setup(conn)
    generate_fn = generate_fn or generate
    key = digest(
        stable_json(
            {
                "version": 3,
                "prompt": PROMPT,
                "model": model,
                "endpoint": base_url,
                "articles": [a.model_dump() for a in articles],
            }
        )
    )
    row = conn.execute("SELECT state FROM news_checkpoints WHERE key=?", (key,)).fetchone()
    state = (
        json.loads(row[0])
        if row
        else {
            "phase": "generate",
            "attempts": 0,
            "draft": "",
            "error": "",
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
    )
    for _ in range(8):
        if state["phase"] == "partial":
            result = salvage(state["draft"], articles)
            return result, {
                **state["usage"],
                "generations": state["attempts"],
                "checkpoint": key,
                "partial": True,
                "rejected_articles": len(articles) - len(result.items),
            }
        if state["phase"] == "done":
            return review(state["draft"], articles), {
                **state["usage"],
                "generations": state["attempts"],
                "checkpoint": key,
            }
        if state["phase"] == "failed":
            raise ProviderError(state["error"] or "agent_generation_budget_exhausted")
        if state["phase"] in {"generate", "calling"}:
            if state["attempts"] >= 2:
                partial = salvage(state["draft"], articles)
                state.update(
                    phase="partial" if partial.items else "failed",
                    error="agent_generation_budget_exhausted",
                )
                save(conn, key, state)
                continue
            state["attempts"] += 1
            state["phase"] = "calling"
            save(conn, key, state, "generation_reserved")
            feedback = (
                {"draft": state["draft"], "error": state["error"]} if state["draft"] else None
            )
            try:
                raw, usage = await generate_fn(
                    articles, api_key, model, base_url, feedback=feedback
                )
            except ProviderError as exc:
                state.update(phase="failed", error=exc.code)
                save(conn, key, state, exc.code)
                continue
            for field in ("input_tokens", "output_tokens"):
                state["usage"][field] += usage.get(field, 0)
            state.update(phase="review", draft=raw)
            save(conn, key, state, "generation_completed")
        if state["phase"] == "review":
            try:
                review(state["draft"], articles)
            except ProviderError as exc:
                state.update(phase="generate", error=exc.code)
                save(conn, key, state, "repair_requested:" + exc.code)
            else:
                state.update(phase="done", error="")
                save(conn, key, state, "evidence_checks_passed")
    raise ProviderError("agent_step_budget_exhausted")
