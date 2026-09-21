from __future__ import annotations

import io
import json
import re
import unicodedata
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

from pypdf import PdfReader

from .config import Settings
from .models import Chunk, Segment, SourceInput, digest

SUPPORTED = {".txt", ".md", ".json", ".jsonl", ".eml", ".pdf"}
PARSER_VERSION = "text-v2"


def canonicalize(text: str) -> str:
    # Preserve interior whitespace and case: `1 2` must not merge into `12`.
    return unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n")).strip()


def iso_date(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = parsedate_to_datetime(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


class TextHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        elif tag in {"p", "div", "br", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def parse_bytes(name: str, raw: bytes, settings: Settings) -> list[SourceInput]:
    if len(raw) > settings.max_file_mb * 1024 * 1024:
        raise ValueError("file_size_limit")
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError("unsupported_format")
    if suffix == ".pdf":
        try:
            reader = PdfReader(io.BytesIO(raw), strict=False)
            if reader.is_encrypted:
                raise ValueError("encrypted_pdf")
            if len(reader.pages) > settings.max_pdf_pages:
                raise ValueError("pdf_page_limit")
            pages, sections, warnings, position = [], [], [], 0
            for number, page in enumerate(reader.pages, 1):
                stream = page.get_contents()
                if stream and len(stream.get_data()) > 10_000_000:
                    raise ValueError("pdf_page_stream_limit")
                text = canonicalize(page.extract_text() or "")
                if not text:
                    warnings.append(f"page_{number}_no_text_check_scan_or_blank")
                if sections:
                    position += 2
                pages.append((number, position, position + len(text)))
                sections.append(text)
                position += len(text)
                if position > settings.max_document_chars:
                    raise ValueError("document_size_limit")
            body = "\n\n".join(sections)
            if not body.strip():
                raise ValueError("pdf_has_no_extractable_text_ocr_required")
            # Preserve empty first pages, so page offsets stay correct.
            docs = [
                SourceInput(
                    title=Path(name).stem,
                    body=body,
                    source=Path(name).name,
                    pages=pages,
                    warnings=warnings,
                )
            ]
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("pdf_parse_failed") from exc
    elif suffix == ".eml":
        mail = BytesParser(policy=policy.default).parsebytes(raw)
        part = mail.get_body(preferencelist=("plain", "html"))
        if part is None:
            raise ValueError("email_has_no_text_body")
        body = part.get_content()
        if part.get_content_type() == "text/html":
            parser = TextHTML()
            parser.feed(body)
            body = "".join(parser.parts)
        warnings = ["email_attachment_not_parsed" for _ in mail.iter_attachments()]
        docs = [
            SourceInput(
                title=str(mail.get("subject", Path(name).stem)),
                body=canonicalize(body),
                source=str(mail.get("from", "")),
                published_at=str(mail.get("date", "")),
                warnings=sorted(set(warnings)),
            )
        ]
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("utf8_required") from exc
        if suffix in {".json", ".jsonl"}:
            items = (
                [json.loads(line) for line in text.splitlines() if line.strip()]
                if suffix == ".jsonl"
                else json.loads(text)
            )
            items = items if isinstance(items, list) else [items]
            if not items or len(items) > 10000:
                raise ValueError("json_record_count_limit")
            docs = [SourceInput.model_validate(item) for item in items]
            for doc in docs:
                if doc.pages:
                    raise ValueError("page_offsets_are_parser_owned")
                doc.body = canonicalize(doc.body)
        else:
            docs = [
                SourceInput(title=Path(name).stem, body=canonicalize(text), source=Path(name).name)
            ]
    for doc in docs:
        if not doc.title.strip() or not doc.body.strip():
            raise ValueError("empty_title_or_body")
        if len(doc.body) > settings.max_document_chars:
            raise ValueError("document_size_limit")
        if doc.url and not re.fullmatch(r"https?://[^\s]+", doc.url):
            raise ValueError("invalid_source_url")
        try:
            doc.published_at = iso_date(doc.published_at)
        except (ValueError, TypeError, OverflowError):
            doc.warnings.append("invalid_date_preserved_in_original_blob")
            doc.published_at = ""
    return docs


def segment_document(document_id: str, doc: SourceInput, max_chars: int) -> list[Segment]:
    result = []
    spans = doc.pages or [(None, 0, len(doc.body))]
    for page, page_start, page_end in spans:
        cursor = page_start
        while cursor < page_end:
            end = min(cursor + max_chars, page_end)
            if end < page_end:
                # Prefer a sentence/line break in the latter half, but never drop text.
                window = doc.body[cursor:end]
                boundaries = [m.end() for m in re.finditer(r"[。！？\n]|(?<=[.!?])\s", window)]
                eligible = [point for point in boundaries if point >= max_chars // 2]
                if eligible:
                    end = cursor + eligible[-1]
            text = doc.body[cursor:end]
            if text.strip():
                segment_id = digest(f"{document_id}:{cursor}:{end}")[:24]
                result.append(
                    Segment(
                        id=segment_id,
                        document_id=document_id,
                        start=cursor,
                        end=end,
                        text=text,
                        page=page,
                    )
                )
            cursor = end
    return result


def make_chunks(document_id: str, segments: list[Segment], settings: Settings) -> list[Chunk]:
    chunks, start = [], 0
    while start < len(segments):
        end, size = start, 0
        while end < len(segments) and size + len(segments[end].text) <= settings.chunk_chars:
            size += len(segments[end].text)
            end += 1
        if end == start:
            raise ValueError("segment_exceeds_chunk_budget")
        selected = segments[start:end]
        chunks.append(
            Chunk(
                id=digest(":".join(s.id for s in selected)),
                document_id=document_id,
                segments=selected,
            )
        )
        if end == len(segments):
            break
        start = max(start + 1, end - settings.overlap_segments)
    return chunks
