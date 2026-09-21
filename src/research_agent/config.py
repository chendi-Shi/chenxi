from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, model_validator

from .models import StrictModel, digest, stable_json


class Settings(StrictModel):
    data_dir: Path = Path("data")
    provider: Literal["rules", "openai"] = "rules"
    model: str = ""
    base_url: str = "https://api.openai.com/v1"
    concurrency: int = Field(default=3, ge=1, le=16)
    request_timeout: float = Field(default=60, gt=0, le=300)
    max_retries: int = Field(default=3, ge=0, le=5)
    repair_attempts: int = Field(default=1, ge=0, le=2)
    chunk_chars: int = Field(default=6000, ge=500, le=24000)
    segment_chars: int = Field(default=1400, ge=100, le=3000)
    overlap_segments: int = Field(default=1, ge=0, le=2)
    max_file_mb: int = Field(default=20, ge=1, le=100)
    max_document_chars: int = Field(default=1_000_000, ge=1000, le=5_000_000)
    max_pdf_pages: int = Field(default=300, ge=1, le=1000)
    watchlist: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def coherent(self):
        if self.chunk_chars < self.segment_chars * (self.overlap_segments + 2):
            raise ValueError("chunk_chars must fit overlap_segments plus two segments")
        endpoint = urlparse(self.base_url)
        if (
            endpoint.scheme != "https"
            or not endpoint.netloc
            or endpoint.username
            or endpoint.password
        ):
            raise ValueError("base_url must be HTTPS without embedded credentials")
        if endpoint.query or endpoint.fragment:
            raise ValueError("base_url cannot contain a query or fragment")
        if self.provider == "openai" and not self.model.strip():
            raise ValueError("model is required for provider=openai")
        return self

    def extraction_key(self, prompt_hash: str):
        # Include every setting capable of changing extracted/accepted facts.
        selected = self.model_dump(
            mode="json",
            exclude={
                "data_dir",
                "concurrency",
                "request_timeout",
                "max_retries",
                "watchlist",
                "max_file_mb",
                "max_document_chars",
                "max_pdf_pages",
            },
        )
        return digest(
            stable_json({"settings": selected, "prompt": prompt_hash, "workflow": "0.2.0"})
        )


def load_settings(path: Path | None = None, **overrides) -> Settings:
    values = {}
    if path:
        with path.open("rb") as handle:
            values = tomllib.load(handle)
    for env, key in [
        ("RESEARCH_DATA_DIR", "data_dir"),
        ("RESEARCH_PROVIDER", "provider"),
        ("RESEARCH_MODEL", "model"),
        ("RESEARCH_BASE_URL", "base_url"),
    ]:
        if os.environ.get(env):
            values[key] = os.environ[env]
    values.update({key: value for key, value in overrides.items() if value is not None})
    return Settings.model_validate(values)
