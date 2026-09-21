import pytest

from research_agent.models import Candidate, Category, Chunk, Citation, Extraction, Segment
from research_agent.validation import conflict_candidates, merge_facts, number_tokens, validate


def example(summary=None, quote="云岚科技2026年上半年营收12亿元。", segment_id="s", **updates):
    fields = dict(
        summary=summary or quote,
        category=Category.EARNINGS,
        entity="云岚科技",
        metric="营收",
        period="2026年上半年",
        value="12",
        unit="亿元",
        nature="reported",
        importance="high",
        citation=Citation(segment_id=segment_id, quote=quote),
    )
    fields.update(updates)
    return Candidate(**fields)


def chunk(text="云岚科技2026年上半年营收12亿元。", doc="doc"):
    return Chunk(
        id="c",
        document_id=doc,
        segments=[Segment(id="s", document_id=doc, start=7, end=7 + len(text), text=text, page=2)],
    )


@pytest.mark.parametrize(
    "candidate,reason",
    [
        (example(segment_id="forged"), "unknown_segment_id"),
        (example(quote="虚构引文"), "quote_not_exact_substring"),
        (example(summary="营收99亿元"), "summary_number_not_in_evidence"),
        (example(value="13"), "value_not_in_evidence"),
        (example(unit="万元"), "unit_not_in_evidence"),
        (example(entity="另一家公司"), "entity_not_in_evidence"),
    ],
)
def test_bad_evidence_is_rejected(candidate, reason):
    facts, issues = validate(Extraction(facts=[candidate]), chunk())
    assert not facts
    assert issues[0].reason == reason


def test_negative_numbers_and_percentages_not_equivalent():
    assert number_tokens("-3%") != number_tokens("3%")
    assert number_tokens("3") != number_tokens("3%")
    assert number_tokens("1,234.5") == {"1234.5"}


def test_dropped_qualification_rejected():
    text = "云岚科技2026年上半年营收12亿元，未经审计。"
    _, issues = validate(
        Extraction(facts=[example(summary="营收12亿元。", quote=text)]), chunk(text)
    )
    assert issues[0].reason.startswith("qualification_dropped")


def test_offsets_page_and_overlap_merge():
    facts, _ = validate(Extraction(facts=[example()]), chunk())
    assert facts[0].evidence[0].start == 7
    assert facts[0].evidence[0].page == 2
    assert len(merge_facts(facts + facts)[0].evidence) == 1
    other, _ = validate(Extraction(facts=[example()]), chunk(doc="second"))
    assert len(merge_facts(facts + other)[0].evidence) == 2


def test_conflicts_only_same_period_unit_and_nature():
    a, _ = validate(Extraction(facts=[example()]), chunk())
    quote = "云岚科技2026年上半年营收13亿元。"
    b, _ = validate(Extraction(facts=[example(quote=quote, value="13")]), chunk(quote, "second"))
    assert len(conflict_candidates(a + b)) == 1
    b[0].nature = "guidance"
    assert not conflict_candidates(a + b)
