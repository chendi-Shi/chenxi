import asyncio
import json

import pytest

from research_agent.config import Settings
from research_agent.models import CallResult
from research_agent.pipeline import Pipeline
from research_agent.providers import PROMPT_HASH, ProviderError, RulesProvider
from research_agent.reporting import export_report


def add_text(store, settings, workspace, text="云岚科技2026年上半年营收12亿元。"):
    path = workspace / "input.txt"
    path.write_text(text, encoding="utf-8")
    store.ingest(path, settings)
    return store.select_documents()


async def test_full_pipeline_cache_and_provenance(store, settings, workspace):
    ids = add_text(store, settings, workspace)
    pipeline = Pipeline(store, settings)
    first = await pipeline.execute(pipeline.plan(ids))
    second = await pipeline.execute(pipeline.plan(ids))
    assert first["status"] == "completed"
    assert first["metrics"]["cache_hits"] == 0
    assert second["metrics"]["cache_hits"] == second["metrics"]["chunks"]
    fact = first["facts"][0]
    evidence = fact["evidence"][0]
    body = store.get_document(ids[0]).body
    assert body[evidence["start"] : evidence["end"]] == evidence["quote"]
    assert first["metrics"]["http_attempts"] == 0


def test_cache_key_changes_for_model_prompt_and_chunking(settings):
    key = settings.extraction_key(PROMPT_HASH)
    assert settings.extraction_key("new-prompt") != key
    assert settings.model_copy(update={"model": "other"}).extraction_key(PROMPT_HASH) != key
    assert settings.model_copy(update={"chunk_chars": 7000}).extraction_key(PROMPT_HASH) != key
    assert settings.model_copy(update={"watchlist": ["new"]}).extraction_key(PROMPT_HASH) == key


class RepairProvider(RulesProvider):
    def __init__(self):
        self.calls = 0

    async def extract(self, chunk, feedback=None):
        self.calls += 1
        result = await super().extract(chunk)
        if self.calls == 1:
            result.extraction.facts[0].citation.quote = "invented evidence"
        else:
            assert feedback[0]["reason"] == "quote_not_exact_substring"
        return result


async def test_invalid_evidence_triggers_bounded_repair(store, settings, workspace):
    ids = add_text(store, settings, workspace)
    provider = RepairProvider()
    pipeline = Pipeline(store, settings, provider)
    result = await pipeline.execute(pipeline.plan(ids))
    assert provider.calls == 2
    assert result["status"] == "completed"
    with store.connection() as conn:
        assert conn.execute("SELECT count(*) FROM events WHERE event='repair'").fetchone()[0] == 1


class AlwaysInvalid(RulesProvider):
    async def extract(self, chunk, feedback=None):
        result = await super().extract(chunk)
        result.extraction.facts[0].citation.quote = "not in source"
        return result


async def test_permanent_validation_failure_not_cached(store, settings, workspace):
    ids = add_text(store, settings, workspace)
    pipeline = Pipeline(store, settings, AlwaysInvalid())
    result = await pipeline.execute(pipeline.plan(ids))
    assert result["status"] == "failed"
    assert result["facts"] == []
    assert result["failures"][0]["error"] == "evidence_validation_failed"
    with store.connection() as conn:
        assert conn.execute("SELECT count(*) FROM cache").fetchone()[0] == 0


class FailingProvider(RulesProvider):
    async def extract(self, chunk, feedback=None):
        raise ProviderError("http_429", attempts=4)


async def test_failed_job_can_be_resumed_without_reprocessing_success(store, settings, workspace):
    ids = add_text(store, settings, workspace)
    pipeline = Pipeline(store, settings, FailingProvider())
    run_id = pipeline.plan(ids)
    first = await pipeline.execute(run_id)
    assert first["status"] == "failed"
    assert first["metrics"]["http_attempts"] == 4
    second = await Pipeline(store, settings, RulesProvider()).execute(run_id, retry_failed=True)
    assert second["status"] == "completed"
    assert second["metrics"]["http_attempts"] == 4


async def test_interrupted_running_jobs_recovered(store, settings, workspace):
    ids = add_text(store, settings, workspace)
    pipeline = Pipeline(store, settings)
    run_id = pipeline.plan(ids)
    with store.connection() as conn:
        conn.execute("UPDATE jobs SET state='running' WHERE run_id=?", (run_id,))
        conn.execute("UPDATE runs SET status='interrupted' WHERE id=?", (run_id,))
    assert (await pipeline.execute(run_id))["status"] == "completed"


def test_lease_prevents_duplicate_executors_and_expired_lease_recovers(store):
    store.acquire_lease("first")
    with pytest.raises(ValueError, match="another_pipeline"):
        store.acquire_lease("second")
    with store.connection() as conn:
        conn.execute("UPDATE leases SET expires=0")
    store.acquire_lease("second")
    store.release_lease("first")
    with pytest.raises(ValueError, match="another_pipeline"):
        store.acquire_lease("third")
    store.release_lease("second")


class SlowProvider(RulesProvider):
    def __init__(self):
        self.active = 0
        self.peak = 0
        self.started = asyncio.Event()

    async def extract(self, chunk, feedback=None):
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.started.set()
        try:
            await asyncio.sleep(0.02)
            return await super().extract(chunk)
        finally:
            self.active -= 1


async def test_concurrency_limit(store, settings, workspace):
    settings = Settings(
        data_dir=settings.data_dir, chunk_chars=600, segment_chars=100, concurrency=2
    )
    ids = add_text(
        store,
        settings,
        workspace,
        "云岚科技2026年上半年营收12亿元，同比增长3%，相关经营数据未经审计，口径仍需核实。" * 100,
    )
    provider = SlowProvider()
    pipeline = Pipeline(store, settings, provider)
    result = await pipeline.execute(pipeline.plan(ids))
    assert result["status"] == "completed"
    assert provider.peak == 2


async def test_cancellation_keeps_run_resumable_and_releases_lease(store, settings, workspace):
    ids = add_text(store, settings, workspace)
    provider = SlowProvider()
    pipeline = Pipeline(store, settings, provider)
    run_id = pipeline.plan(ids)
    task = asyncio.create_task(pipeline.execute(run_id))
    await provider.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.get_run(run_id)["status"] == "interrupted"
    store.acquire_lease("after_cancel")
    store.release_lease("after_cancel")
    assert (await Pipeline(store, settings).execute(run_id))["status"] == "completed"


async def test_review_history_and_atomic_exports(store, settings, workspace):
    ids = add_text(store, settings, workspace)
    pipeline = Pipeline(store, settings)
    run_id = pipeline.plan(ids)
    await pipeline.execute(run_id)
    store.add_review(run_id, "test-reviewer", "needs_revision", "check units")
    store.add_review(run_id, "test-reviewer", "accepted", "units checked")
    exports = export_report(store, run_id, workspace / "exports")
    from pathlib import Path

    payload = json.loads(Path(exports["json"]).read_text(encoding="utf-8"))
    assert len(payload["reviews"]) == 2
    assert payload["review_status"] == "accepted"
    assert "units checked" in Path(exports["markdown"]).read_text(encoding="utf-8")


async def test_resume_rejects_changed_prompt_or_configuration(store, settings, workspace):
    ids = add_text(store, settings, workspace)
    run_id = Pipeline(store, settings).plan(ids)
    changed = settings.model_copy(update={"model": "different"})
    with pytest.raises(ValueError, match="configuration_mismatch"):
        await Pipeline(store, changed).execute(run_id)


async def test_empty_output_marked_for_coverage_review(store, settings, workspace):
    from research_agent.models import Extraction

    class Empty(RulesProvider):
        async def extract(self, chunk, feedback=None):
            return CallResult(extraction=Extraction(facts=[]))

    ids = add_text(store, settings, workspace)
    pipeline = Pipeline(store, settings, Empty())
    result = await pipeline.execute(pipeline.plan(ids))
    assert result["empty_chunks"]
    assert result["facts"] == []


async def test_partial_run_preserves_success_and_failed_document(store, settings, workspace):
    class Selective(RulesProvider):
        async def extract(self, chunk, feedback=None):
            if "失败材料" in chunk.segments[0].text:
                raise ProviderError("http_503")
            return await super().extract(chunk)

    file = workspace / "two.json"
    file.write_text(
        json.dumps(
            [
                {"title": "ok", "body": "营收12亿元。"},
                {"title": "bad", "body": "失败材料，营收13亿元。"},
            ]
        ),
        encoding="utf-8",
    )
    store.ingest(file, settings)
    pipeline = Pipeline(store, settings, Selective())
    result = await pipeline.execute(pipeline.plan())
    assert result["status"] == "partial"
    assert len(result["facts"]) == 1
    assert len(result["failures"]) == 1
    assert result["metrics"]["successful_chunks"] == 1
