from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .config import Settings
from .models import Chunk, SourceInput, digest, stable_json
from .parsing import PARSER_VERSION, SUPPORTED, parse_bytes


def utcnow():
    return datetime.now(UTC).isoformat(timespec="seconds")


def search_tokens(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.casefold())
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        words.extend(run[i : i + 2] for i in range(len(run) - 1))
        words.extend(run)  # Single-character queries are supported too.
    return " ".join(words)


class Store:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.db = directory / "agent.sqlite3"
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError(f"unsupported_database_version:{version}")
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY, body TEXT NOT NULL, pages_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY, source_key TEXT NOT NULL, raw_hash TEXT NOT NULL,
                document_id TEXT NOT NULL REFERENCES documents(id), title TEXT NOT NULL,
                source TEXT NOT NULL, published_at TEXT NOT NULL, url TEXT NOT NULL,
                warnings_json TEXT NOT NULL, imported_at TEXT NOT NULL,
                UNIQUE(source_key,raw_hash,document_id)
            );
            CREATE TABLE IF NOT EXISTS files (
                source_key TEXT PRIMARY KEY, raw_hash TEXT NOT NULL, parser_key TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS document_search USING fts5(document_id UNINDEXED,tokens);
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, status TEXT NOT NULL,
                config_key TEXT NOT NULL, config_json TEXT NOT NULL,
                report_json TEXT, selection_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                run_id TEXT NOT NULL REFERENCES runs(id), chunk_id TEXT NOT NULL,
                document_id TEXT NOT NULL REFERENCES documents(id), chunk_json TEXT NOT NULL,
                state TEXT NOT NULL, result_json TEXT, error TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0, input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0, seconds REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(run_id,chunk_id)
            );
            CREATE TABLE IF NOT EXISTS cache (
                config_key TEXT NOT NULL, chunk_id TEXT NOT NULL, result_json TEXT NOT NULL,
                PRIMARY KEY(config_key,chunk_id)
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, chunk_id TEXT NOT NULL,
                event TEXT NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS leases (
                name TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                reviewer TEXT NOT NULL, status TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL
            );
            PRAGMA user_version=1;
            """)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.db, timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def ingest(self, path: Path, settings: Settings) -> dict:
        if not path.exists():
            raise ValueError("input_path_not_found")
        paths = (
            sorted(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED)
            if path.is_dir()
            else [path]
        )
        result = {
            "files_seen": len(paths),
            "files_unchanged": 0,
            "new_documents": 0,
            "new_sources": 0,
            "duplicate_documents": 0,
            "errors": [],
        }
        parser_key = digest(
            stable_json(
                {
                    "version": PARSER_VERSION,
                    "max_file_mb": settings.max_file_mb,
                    "max_document_chars": settings.max_document_chars,
                    "max_pdf_pages": settings.max_pdf_pages,
                }
            )
        )
        for file in paths:
            try:
                if file.stat().st_size > settings.max_file_mb * 1024 * 1024:
                    raise ValueError("file_size_limit")
                raw = file.read_bytes()
                raw_hash = hashlib.sha256(raw).hexdigest()
                source_key = str(file.resolve())
                with self.connection() as conn:
                    prior = conn.execute(
                        "SELECT raw_hash,parser_key FROM files WHERE source_key=?", (source_key,)
                    ).fetchone()
                if prior and prior["raw_hash"] == raw_hash and prior["parser_key"] == parser_key:
                    result["files_unchanged"] += 1
                    continue
                docs = parse_bytes(file.name, raw, settings)
                # The original bytes stay local, content-addressed and independent of input paths.
                blob = self.directory / "blobs" / raw_hash
                blob.parent.mkdir(exist_ok=True)
                if not blob.exists():
                    temp = blob.with_suffix("." + uuid.uuid4().hex + ".tmp")
                    temp.write_bytes(raw)
                    temp.replace(blob)
                counts = {"new_documents": 0, "new_sources": 0, "duplicate_documents": 0}
                with self.connection() as conn:
                    for index, doc in enumerate(docs):
                        doc_id = digest(
                            stable_json(
                                {"body": doc.body, "pages": doc.pages, "parser": PARSER_VERSION}
                            )
                        )
                        inserted = conn.execute(
                            "INSERT OR IGNORE INTO documents VALUES(?,?,?,?)",
                            (doc_id, doc.body, stable_json(doc.pages), utcnow()),
                        ).rowcount
                        counts["new_documents"] += inserted
                        counts["duplicate_documents"] += int(not inserted)
                        if inserted:
                            conn.execute(
                                "INSERT INTO document_search(document_id,tokens) VALUES(?,?)",
                                (doc_id, search_tokens(doc.body)),
                            )
                        counts["new_sources"] += conn.execute(
                            "INSERT OR IGNORE INTO sources(source_key,raw_hash,document_id,title,source,published_at,url,warnings_json,imported_at) VALUES(?,?,?,?,?,?,?,?,?)",
                            (
                                f"{source_key}#{index}",
                                raw_hash,
                                doc_id,
                                doc.title,
                                doc.source,
                                doc.published_at,
                                doc.url,
                                stable_json(doc.warnings),
                                utcnow(),
                            ),
                        ).rowcount
                    conn.execute(
                        "INSERT OR REPLACE INTO files VALUES(?,?,?)",
                        (source_key, raw_hash, parser_key),
                    )
                for key, count in counts.items():
                    result[key] += count
            except (ValueError, OSError, LookupError) as exc:
                # Avoid logging mail contents or decoder/model response payloads.
                code = (
                    str(exc)
                    if type(exc) is ValueError and len(str(exc)) < 150
                    else type(exc).__name__
                )
                result["errors"].append({"file": file.name, "error": code})
        return result

    def sources(self, document_id: str):
        with self.connection() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT id,title,source,published_at,url,warnings_json,raw_hash FROM sources WHERE document_id=? ORDER BY id",
                    (document_id,),
                )
            ]

    def get_document(self, document_id: str) -> SourceInput:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        if row is None:
            raise ValueError("document_not_found")
        first = self.sources(document_id)[0]
        return SourceInput(
            title=first["title"],
            body=row["body"],
            pages=json.loads(row["pages_json"]),
            source=first["source"],
            published_at=first["published_at"],
            url=first["url"],
            warnings=json.loads(first["warnings_json"]),
        )

    def select_documents(self, since: str = "", until: str = ""):
        clauses, params = [], []
        # Date filter applies to source publication timestamps, not file import times.
        if since:
            clauses.append("s.published_at>=?")
            params.append(since)
        if until:
            clauses.append("s.published_at<?")
            params.append(until)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connection() as conn:
            return [
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT d.id FROM documents d JOIN sources s ON d.id=s.document_id"
                    + where
                    + " ORDER BY d.id",
                    params,
                )
            ]

    def search(self, query: str, limit: int = 10):
        tokens = list(dict.fromkeys(search_tokens(query).split()))
        if not tokens:
            return []
        # All query tokens must occur; parameterization + quoted tokens block FTS operators.
        expression = " AND ".join('"' + token + '"' for token in tokens[:50])
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT document_id,bm25(document_search) AS score FROM document_search WHERE document_search MATCH ? ORDER BY score LIMIT ?",
                (expression, limit),
            ).fetchall()
        return [
            {
                "document_id": row["document_id"],
                "score": row["score"],
                "sources": self.sources(row["document_id"]),
            }
            for row in rows
        ]

    def create_run(self, config: Settings, config_key: str, chunks: list[Chunk], selection: dict):
        run_id = uuid.uuid4().hex[:16]
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?,?,?)",
                (
                    run_id,
                    utcnow(),
                    "pending",
                    config_key,
                    config.model_dump_json(),
                    None,
                    stable_json(selection),
                ),
            )
            conn.executemany(
                "INSERT INTO jobs(run_id,chunk_id,document_id,chunk_json,state) VALUES(?,?,?,?,'pending')",
                [(run_id, c.id, c.document_id, c.model_dump_json()) for c in chunks],
            )
        return run_id

    def get_run(self, run_id: str):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("run_not_found")
        return dict(row)

    def jobs(self, run_id: str):
        with self.connection() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM jobs WHERE run_id=? ORDER BY document_id,chunk_id", (run_id,)
                )
            ]

    def event(self, run_id, chunk_id, event, detail):
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO events(run_id,chunk_id,event,detail_json,created_at) VALUES(?,?,?,?,?)",
                (run_id, chunk_id, event, stable_json(detail), utcnow()),
            )

    def acquire_lease(self, owner: str, ttl: float = 120):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT * FROM leases WHERE name='pipeline'").fetchone()
            if current and current["expires"] > time.time():
                raise ValueError("another_pipeline_is_running_or_lease_not_expired")
            conn.execute(
                "INSERT OR REPLACE INTO leases VALUES('pipeline',?,?)", (owner, time.time() + ttl)
            )

    def renew_lease(self, owner: str, ttl: float = 120):
        with self.connection() as conn:
            if (
                conn.execute(
                    "UPDATE leases SET expires=? WHERE name='pipeline' AND owner=?",
                    (time.time() + ttl, owner),
                ).rowcount
                != 1
            ):
                raise ValueError("pipeline_lease_lost")

    def release_lease(self, owner: str):
        with self.connection() as conn:
            conn.execute("DELETE FROM leases WHERE name='pipeline' AND owner=?", (owner,))

    def add_review(self, run_id: str, reviewer: str, status: str, note: str):
        self.get_run(run_id)
        if status not in {"accepted", "needs_revision", "pending"} or not reviewer.strip():
            raise ValueError("reviewer_and_valid_status_required")
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO reviews(run_id,reviewer,status,note,created_at) VALUES(?,?,?,?,?)",
                (run_id, reviewer, status, note, utcnow()),
            )
