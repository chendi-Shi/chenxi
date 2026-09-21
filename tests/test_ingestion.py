import json

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from research_agent.models import SourceInput
from research_agent.parsing import canonicalize, make_chunks, parse_bytes, segment_document


def test_incremental_import_retains_duplicate_sources(store, settings, workspace):
    path = workspace / "a.txt"
    path.write_text("公司营收12亿元。", encoding="utf-8")
    first = store.ingest(path, settings)
    assert first["new_documents"] == 1
    assert store.ingest(path, settings)["files_unchanged"] == 1
    other = workspace / "forwarded.txt"
    other.write_text("公司营收12亿元。", encoding="utf-8")
    second = store.ingest(other, settings)
    assert second["duplicate_documents"] == 1
    assert second["new_sources"] == 1
    assert len(store.sources(store.select_documents()[0])) == 2


def test_updated_file_creates_revision_without_losing_old_source(store, settings, workspace):
    path = workspace / "a.txt"
    path.write_text("营收12亿元。", encoding="utf-8")
    store.ingest(path, settings)
    path.write_text("营收13亿元。", encoding="utf-8")
    store.ingest(path, settings)
    assert len(store.select_documents()) == 2


def test_numeric_whitespace_is_not_collapsed():
    assert canonicalize("1 2") != canonicalize("12")
    assert canonicalize("a\r\nb") == "a\nb"


def test_bad_file_does_not_block_good_file(store, settings, workspace):
    (workspace / "bad.json").write_text("{", encoding="utf-8")
    (workspace / "ok.txt").write_text("有效内容", encoding="utf-8")
    result = store.ingest(workspace, settings)
    assert result["new_documents"] == 1
    assert len(result["errors"]) == 1


def test_json_import_atomic_on_invalid_record(store, settings, workspace):
    path = workspace / "batch.json"
    path.write_text(
        json.dumps([{"title": "a", "body": "有效"}, {"title": "b", "body": ""}]), encoding="utf-8"
    )
    assert store.ingest(path, settings)["errors"]
    assert store.select_documents() == []


def test_unknown_date_is_explicit_and_date_filter_excludes_it(store, settings, workspace):
    path = workspace / "batch.json"
    path.write_text(
        json.dumps(
            [
                {"title": "a", "body": "有效A", "published_at": "2026-09-21"},
                {"title": "b", "body": "有效B", "published_at": "not-a-date"},
            ]
        ),
        encoding="utf-8",
    )
    store.ingest(path, settings)
    ids = store.select_documents("2026-09-21T00:00:00+00:00", "2026-09-22T00:00:00+00:00")
    assert len(ids) == 1
    assert len(store.select_documents()) == 2


def test_eml_quoted_headers_and_html(settings):
    raw = b"Subject: =?utf-8?b?5Lia57up?=\r\nFrom: research@example.test\r\nDate: Mon, 21 Sep 2026 08:00:00 +0800\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>Revenue 12</p><script>bad()</script><p>Unaudited</p>"
    doc = parse_bytes("x.eml", raw, settings)[0]
    assert doc.title == "业绩"
    assert "bad()" not in doc.body
    assert doc.published_at == "2026-09-21T00:00:00+00:00"


def test_email_attachment_is_flagged(settings):
    from email.message import EmailMessage

    message = EmailMessage()
    message["Subject"] = "Report"
    message.set_content("Revenue 12.")
    message.add_attachment(b"content", maintype="application", subtype="pdf", filename="a.pdf")
    doc = parse_bytes("x.eml", message.as_bytes(), settings)[0]
    assert "email_attachment_not_parsed" in doc.warnings


def make_pdf(path, texts):
    writer = PdfWriter()
    for text in texts:
        page = writer.add_blank_page(width=300, height=300)
        if text:
            font = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
            page[NameObject("/Resources")] = DictionaryObject(
                {
                    NameObject("/Font"): DictionaryObject(
                        {NameObject("/F1"): writer._add_object(font)}
                    )
                }
            )
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 12 Tf 20 250 Td ({text}) Tj ET".encode())
            page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(path)


def test_pdf_pages_offsets_and_blank_page_warning(settings, workspace):
    path = workspace / "test.pdf"
    make_pdf(path, ["Revenue 12 million", "", "Profit 3 million"])
    doc = parse_bytes(path.name, path.read_bytes(), settings)[0]
    segments = segment_document("doc", doc, settings.segment_chars)
    assert [s.page for s in segments] == [1, 3]
    for segment in segments:
        assert doc.body[segment.start : segment.end] == segment.text
    assert "page_2_no_text_check_scan_or_blank" in doc.warnings


def test_pdf_no_text_requires_ocr(settings, workspace):
    path = workspace / "blank.pdf"
    make_pdf(path, [""])
    with pytest.raises(ValueError, match="ocr_required"):
        parse_bytes(path.name, path.read_bytes(), settings)


def test_size_limit_rejects_not_truncates(settings):
    settings.max_document_chars = 1000
    with pytest.raises(ValueError, match="document_size_limit"):
        parse_bytes("x.txt", b"a" * 1001, settings)


def test_long_document_all_text_covered_with_overlap(settings):
    body = "📌测试材料：营收12亿元，仍需核对。\n" * 2000
    doc = SourceInput(title="long", body=body)
    segments = segment_document("doc", doc, settings.segment_chars)
    assert "".join(s.text for s in segments) == body
    chunks = make_chunks("doc", segments, settings)
    assert len(chunks) > 1
    assert {s.id for chunk in chunks for s in chunk.segments} == {s.id for s in segments}
    assert all(sum(len(s.text) for s in chunk.segments) <= settings.chunk_chars for chunk in chunks)
    assert chunks[0].segments[-1].id == chunks[1].segments[0].id


def test_cjk_and_english_search(store, settings, workspace):
    path = workspace / "docs.json"
    path.write_text(
        json.dumps(
            [
                {"title": "a", "body": "芯片订单增长，revenue improved."},
                {"title": "b", "body": "会议报名截止。"},
            ]
        ),
        encoding="utf-8",
    )
    store.ingest(path, settings)
    assert len(store.search("芯片订单")) == 1
    assert len(store.search("revenue")) == 1
    assert store.search("不存在") == []
