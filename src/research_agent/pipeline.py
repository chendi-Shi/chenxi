from __future__ import annotations

import asyncio
import json
import time
import uuid

from .config import Settings
from .models import Chunk, Fact, stable_json
from .parsing import make_chunks, segment_document
from .providers import PROMPT_HASH, Provider, ProviderError, build_provider
from .store import Store, utcnow
from .validation import conflict_candidates, merge_facts, validate


class Pipeline:
    def __init__(self, store: Store, settings: Settings, provider: Provider | None = None):
        self.store, self.settings = store, settings
        self.provider = provider
        self.config_key = settings.extraction_key(PROMPT_HASH)

    def plan(self, ids: list[str] | None = None, since="", until="") -> str:
        ids = ids if ids is not None else self.store.select_documents(since, until)
        if not ids:
            raise ValueError("no_documents_selected")
        chunks = []
        for document_id in sorted(set(ids)):
            doc = self.store.get_document(document_id)
            segments = segment_document(document_id, doc, self.settings.segment_chars)
            chunks.extend(make_chunks(document_id, segments, self.settings))
        return self.store.create_run(
            self.settings,
            self.config_key,
            chunks,
            {"document_ids": sorted(set(ids)), "since": since, "until_exclusive": until},
        )

    async def _heartbeat(self, owner):
        while True:
            await asyncio.sleep(10)
            self.store.renew_lease(owner)

    async def execute(self, run_id: str, retry_failed=False):
        run = self.store.get_run(run_id)
        if run["config_key"] != self.config_key:
            raise ValueError("resume_configuration_mismatch")
        if run["status"] == "completed" and not retry_failed:
            return json.loads(run["report_json"])
        # Fail configuration before mutating job state.
        provider = self.provider or build_provider(self.settings)
        owner = uuid.uuid4().hex
        try:
            self.store.acquire_lease(owner)
        except Exception:
            await provider.close()
            raise
        heartbeat = asyncio.create_task(self._heartbeat(owner))
        tasks = []
        try:
            with self.store.connection() as conn:
                conn.execute("UPDATE runs SET status='running' WHERE id=?", (run_id,))
                conn.execute(
                    "UPDATE jobs SET state='pending' WHERE run_id=? AND state='running'", (run_id,)
                )
                if retry_failed:
                    conn.execute(
                        "UPDATE jobs SET state='pending',error='' WHERE run_id=? AND state='failed'",
                        (run_id,),
                    )
            semaphore = asyncio.Semaphore(self.settings.concurrency)
            for job in self.store.jobs(run_id):
                if job["state"] == "pending":
                    tasks.append(
                        asyncio.create_task(self._process(run_id, job, provider, semaphore))
                    )
            if tasks:
                batch = asyncio.gather(*tasks)
                done, _ = await asyncio.wait(
                    [batch, heartbeat], return_when=asyncio.FIRST_COMPLETED
                )
                if heartbeat in done:
                    await heartbeat  # Raise lease failure, never continue without ownership.
                await batch
            report = self._report(run_id)
            with self.store.connection() as conn:
                conn.execute(
                    "UPDATE runs SET status=?,report_json=? WHERE id=?",
                    (report["status"], stable_json(report), run_id),
                )
            return report
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with self.store.connection() as conn:
                conn.execute("UPDATE runs SET status='interrupted' WHERE id=?", (run_id,))
            raise
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            self.store.release_lease(owner)
            await provider.close()

    async def _process(self, run_id, job, provider, semaphore):
        async with semaphore:
            chunk = Chunk.model_validate_json(job["chunk_json"])
            with self.store.connection() as conn:
                cached = conn.execute(
                    "SELECT result_json FROM cache WHERE config_key=? AND chunk_id=?",
                    (self.config_key, chunk.id),
                ).fetchone()
                if cached:
                    conn.execute(
                        "UPDATE jobs SET state='cached',result_json=?,error='' WHERE run_id=? AND chunk_id=?",
                        (cached[0], run_id, chunk.id),
                    )
                    return
                conn.execute(
                    "UPDATE jobs SET state='running' WHERE run_id=? AND chunk_id=?",
                    (run_id, chunk.id),
                )
            self.store.event(run_id, chunk.id, "started", {"segments": len(chunk.segments)})
            start = time.monotonic()
            attempts = input_tokens = output_tokens = 0
            feedback, accepted, issues, error = [], [], [], ""
            try:
                for repair in range(self.settings.repair_attempts + 1):
                    try:
                        output = await provider.extract(chunk, feedback)
                    except ProviderError as exc:
                        attempts += exc.attempts
                        input_tokens += exc.input_tokens
                        output_tokens += exc.output_tokens
                        if (
                            exc.code == "invalid_extraction_schema"
                            and repair < self.settings.repair_attempts
                        ):
                            feedback = [{"reason": exc.code}]
                            self.store.event(
                                run_id,
                                chunk.id,
                                "repair",
                                {"round": repair + 1, "issues": feedback},
                            )
                            continue
                        raise
                    attempts += output.attempts
                    input_tokens += output.input_tokens
                    output_tokens += output.output_tokens
                    accepted, issues = validate(output.extraction, chunk)
                    if not issues:
                        break
                    feedback = [issue.model_dump() for issue in issues]
                    if repair < self.settings.repair_attempts:
                        self.store.event(
                            run_id, chunk.id, "repair", {"round": repair + 1, "issues": feedback}
                        )
                if issues:
                    error = "evidence_validation_failed"
                result = {
                    "facts": [f.model_dump(mode="json") for f in accepted],
                    "issues": [i.model_dump() for i in issues],
                    "empty": not accepted and not issues,
                    "saturated": len(accepted) == 30,
                }
            except ProviderError as exc:
                error = exc.code
                result = {"facts": [], "issues": [], "empty": False, "saturated": False}
            except Exception as exc:
                # Unexpected provider bug is visible, with no private text in error logs.
                error = "provider_error:" + type(exc).__name__
                result = {"facts": [], "issues": [], "empty": False, "saturated": False}
            elapsed = round(time.monotonic() - start, 3)
            state = "failed" if error else "succeeded"
            with self.store.connection() as conn:
                conn.execute(
                    "UPDATE jobs SET state=?,result_json=?,error=?,attempts=attempts+?,input_tokens=input_tokens+?,output_tokens=output_tokens+?,seconds=seconds+? WHERE run_id=? AND chunk_id=?",
                    (
                        state,
                        stable_json(result),
                        error,
                        attempts,
                        input_tokens,
                        output_tokens,
                        elapsed,
                        run_id,
                        chunk.id,
                    ),
                )
                if not error:
                    conn.execute(
                        "INSERT OR REPLACE INTO cache VALUES(?,?,?)",
                        (self.config_key, chunk.id, stable_json(result)),
                    )
            self.store.event(
                run_id,
                chunk.id,
                state,
                {
                    "error": error,
                    "attempts": attempts,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "seconds": elapsed,
                },
            )

    def _report(self, run_id):
        jobs = self.store.jobs(run_id)
        all_facts, failures, empty, saturated = [], [], [], []
        for job in jobs:
            result = json.loads(job["result_json"]) if job["result_json"] else {}
            all_facts.extend(Fact.model_validate(f) for f in result.get("facts", []))
            if job["state"] == "failed":
                failures.append(
                    {
                        "chunk_id": job["chunk_id"],
                        "document_id": job["document_id"],
                        "error": job["error"],
                        "issues": result.get("issues", []),
                    }
                )
            if result.get("empty"):
                empty.append(job["chunk_id"])
            if result.get("saturated"):
                saturated.append(job["chunk_id"])
        facts = merge_facts(all_facts)
        importance = {"high": 0, "medium": 1, "low": 2}
        facts.sort(
            key=lambda f: (
                not any(
                    term.casefold() in (f.entity + f.summary).casefold()
                    for term in self.settings.watchlist
                ),
                importance[f.importance],
                f.category.value,
                f.id,
            )
        )
        documents = []
        for document_id in sorted({job["document_id"] for job in jobs}):
            sources = self.store.sources(document_id)
            documents.append(
                {
                    "id": document_id,
                    "sources": sources,
                    "warnings": sorted(
                        {w for source in sources for w in json.loads(source["warnings_json"])}
                    ),
                }
            )
        success = sum(j["state"] in {"succeeded", "cached"} for j in jobs)
        return {
            "run_id": run_id,
            "created_at": utcnow(),
            "status": "completed" if not failures else ("partial" if success else "failed"),
            "provider": self.settings.provider,
            "model": self.settings.model,
            "config_key": self.config_key,
            "prompt_hash": PROMPT_HASH,
            "selection": json.loads(self.store.get_run(run_id)["selection_json"]),
            "documents": documents,
            "facts": [f.model_dump(mode="json") for f in facts],
            "conflicts": conflict_candidates(facts),
            "failures": failures,
            "empty_chunks": empty,
            "saturated_chunks": saturated,
            "metrics": {
                "documents": len(documents),
                "chunks": len(jobs),
                "successful_chunks": success,
                "cache_hits": sum(j["state"] == "cached" for j in jobs),
                "facts": len(facts),
                "http_attempts": sum(j["attempts"] for j in jobs),
                "input_tokens": sum(j["input_tokens"] for j in jobs),
                "output_tokens": sum(j["output_tokens"] for j in jobs),
                "summed_chunk_seconds": round(sum(j["seconds"] for j in jobs), 3),
            },
            "review_status": "pending",
            "limitations": [
                "引文与数值校验不是语义真实性证明。",
                "空结果和达到单块上限的输出需要复核覆盖率。",
                "疑似冲突基于字面槽位匹配，不能识别所有口径差异或单位换算。",
                "token 统计仅包含已返回 usage 的请求；未返回 usage 的失败请求用量未知。",
            ],
        }
