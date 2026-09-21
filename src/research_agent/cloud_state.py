"""Encrypted SQLite snapshots in a dedicated GitHub branch, using optimistic concurrency.

The encryption key only lives in GitHub Secrets. Missing/corrupt state fails closed.
This is a single-mailbox deployment adapter, not a replacement for a transactional DB.
"""

from __future__ import annotations

import base64
import gzip
import io
import re
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

import httpx
from cryptography.fernet import Fernet, InvalidToken


class StateError(Exception):
    pass


class GitHubState:
    def __init__(self, repository, token, key, directory, client=None):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise StateError("invalid_repository")
        if not token:
            raise StateError("missing_github_token")
        self.repository = repository
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            self.cipher = Fernet(key)
        except (ValueError, TypeError) as exc:
            raise StateError("invalid_state_encryption_key") from exc
        self.client = client or httpx.Client(
            base_url="https://api.github.com",
            headers={
                "Authorization": "Bearer " + token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=45,
            follow_redirects=False,
        )
        self.branch = "codex/daily-state"
        self.url = f"/repos/{repository}/contents/state.enc"
        self.sha = None
        self.prefix = ("chenxi-state-v1\n" + repository + "\n").encode()

    def request(self, method, path, **kwargs):
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise StateError("github_state_network_ambiguous") from exc
        if response.status_code == 409:
            raise StateError("github_state_concurrent_writer")
        if response.status_code not in {200, 201}:
            raise StateError(f"github_state_http_{response.status_code}")
        return response.json()

    def restore(self):
        result = self.request("GET", self.url, params={"ref": self.branch})
        try:
            encrypted = base64.b64decode(result["content"])
            if len(encrypted) > 950_000:
                raise StateError("state_size_limit")
            compressed = self.cipher.decrypt(encrypted)
            with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
                raw = stream.read(16_000_001)
            if len(raw) > 16_000_000 or not raw.startswith(self.prefix + b"SQLite format 3\0"):
                raise StateError("invalid_state_payload")
            target = self.directory / "daily.sqlite3"
            if target.exists():
                raise StateError("restore_requires_empty_working_directory")
            target.write_bytes(raw[len(self.prefix) :])
            with closing(sqlite3.connect(target)) as db:
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise StateError("state_integrity_failed")
            self.sha = result["sha"]
        except (InvalidToken, ValueError, KeyError, OSError, sqlite3.Error) as exc:
            raise StateError("state_decryption_or_integrity_failed") from exc

    def save(self, conn, *, initialize=False):
        if self.sha is None and not initialize:
            raise StateError("state_not_restored")
        # Backup API captures WAL consistently. DELETE journal makes the image self-contained.
        with tempfile.TemporaryDirectory(dir=self.directory) as folder:
            target = Path(folder) / "snapshot.sqlite3"
            with closing(sqlite3.connect(target)) as snapshot:
                conn.backup(snapshot)
                snapshot.execute("PRAGMA journal_mode=DELETE")
            encrypted = self.cipher.encrypt(gzip.compress(self.prefix + target.read_bytes()))
        if len(encrypted) > 750_000:
            raise StateError("encrypted_state_exceeds_750kb_archive_required")
        body = {
            "message": "Persist encrypted daily agent state",
            "branch": self.branch,
            "content": base64.b64encode(encrypted).decode(),
        }
        if self.sha:
            body["sha"] = self.sha
        result = self.request("PUT", self.url, json=body)
        self.sha = result["content"]["sha"]

    def close(self):
        self.client.close()
