from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Category(StrEnum):
    EARNINGS = "公司业绩"
    INDUSTRY = "产业动态"
    POLICY = "宏观政策"
    EVENT = "会议活动"
    OTHER = "其他"


class SourceInput(StrictModel):
    title: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1)
    source: str = ""
    published_at: str = ""
    url: str = ""
    pages: list[tuple[int, int, int]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class Segment(StrictModel):
    id: str
    document_id: str
    start: int
    end: int
    text: str
    page: int | None = None


class Chunk(StrictModel):
    id: str
    document_id: str
    segments: list[Segment]


class Citation(StrictModel):
    segment_id: str
    quote: str = Field(min_length=1, max_length=1600)


class Candidate(StrictModel):
    summary: str = Field(min_length=1, max_length=1600)
    category: Category
    entity: str = Field(max_length=200)
    metric: str = Field(max_length=100)
    period: str = Field(max_length=100)
    value: str = Field(max_length=100)
    unit: str = Field(max_length=100)
    nature: Literal["reported", "guidance", "opinion", "rumor"]
    importance: Literal["high", "medium", "low"]
    citation: Citation


class Extraction(StrictModel):
    facts: list[Candidate] = Field(max_length=30)


class Evidence(StrictModel):
    document_id: str
    segment_id: str
    quote: str
    start: int
    end: int
    page: int | None


class Fact(StrictModel):
    id: str
    summary: str
    category: Category
    entity: str
    metric: str
    period: str
    value: str
    unit: str
    nature: str
    importance: str
    evidence: list[Evidence]
    warnings: list[str]


class CallResult(StrictModel):
    extraction: Extraction
    input_tokens: int = 0
    output_tokens: int = 0
    attempts: int = 1


class ValidationIssue(StrictModel):
    index: int
    reason: str
