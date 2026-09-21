from __future__ import annotations

import re
import unicodedata
from collections import defaultdict

from .models import Chunk, Evidence, Extraction, Fact, ValidationIssue, digest, stable_json


def number_tokens(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", text).replace("−", "-")
    # Signed values and percentages remain distinct; no inferred unit conversions.
    matches = re.findall(r"(?<![\d.])[+-]?\d+(?:,\d{3})*(?:\.\d+)?\s*%?", normalized)
    return {re.sub(r"[\s,]", "", item) for item in matches}


def validate(extraction: Extraction, chunk: Chunk) -> tuple[list[Fact], list[ValidationIssue]]:
    segments = {s.id: s for s in chunk.segments}
    valid, issues = [], []
    for index, candidate in enumerate(extraction.facts):
        segment = segments.get(candidate.citation.segment_id)
        quote = candidate.citation.quote
        reason = ""
        if not segment:
            reason = "unknown_segment_id"
        elif not quote.strip() or quote not in segment.text:
            reason = "quote_not_exact_substring"
        elif number_tokens(candidate.summary) - number_tokens(quote):
            reason = "summary_number_not_in_evidence"
        else:
            # Slots used for contradiction grouping must be literally grounded.
            for name in ("entity", "metric", "period", "value", "unit"):
                value = getattr(candidate, name)
                if value and value not in quote:
                    reason = f"{name}_not_in_evidence"
                    break
        if reason:
            issues.append(ValidationIssue(index=index, reason=reason))
            continue
        warnings = []
        if segment.text.count(quote) > 1:
            warnings.append("repeated_quote_first_occurrence_used")
        qualifiers = ("未经审计", "尚未", "预计", "可能", "传闻", "据称", "未经证实", "样本有限")
        missing = [word for word in qualifiers if word in quote and word not in candidate.summary]
        if missing:
            # Exact quotation alone does not protect against dropped qualifications.
            issues.append(
                ValidationIssue(index=index, reason="qualification_dropped:" + ",".join(missing))
            )
            continue
        start = segment.start + segment.text.index(quote)
        evidence = Evidence(
            document_id=chunk.document_id,
            segment_id=segment.id,
            quote=quote,
            start=start,
            end=start + len(quote),
            page=segment.page,
        )
        fields = candidate.model_dump(exclude={"citation"}, mode="json")
        fact_id = digest(stable_json(fields))[:24]
        valid.append(Fact(id=fact_id, **fields, evidence=[evidence], warnings=warnings))
    return valid, issues


def merge_facts(facts: list[Fact]) -> list[Fact]:
    """Merge identical structured claims; keep all distinct evidence locations."""
    merged = {}
    for fact in facts:
        if fact.id not in merged:
            merged[fact.id] = fact.model_copy(deep=True)
        else:
            target = merged[fact.id]
            keys = {(e.document_id, e.start, e.end) for e in target.evidence}
            target.evidence.extend(
                e for e in fact.evidence if (e.document_id, e.start, e.end) not in keys
            )
            target.warnings = sorted(set(target.warnings + fact.warnings))
    return list(merged.values())


def conflict_candidates(facts: list[Fact]) -> list[dict]:
    groups = defaultdict(list)
    for fact in facts:
        if all((fact.entity, fact.metric, fact.period, fact.value, fact.unit)):
            key = (fact.entity, fact.metric, fact.period, fact.unit, fact.nature)
            groups[key].append(fact)
    output = []
    for key, group in groups.items():
        if len({f.value for f in group}) > 1:
            output.append(
                {
                    "entity": key[0],
                    "metric": key[1],
                    "period": key[2],
                    "unit": key[3],
                    "nature": key[4],
                    "values": sorted({f.value for f in group}),
                    "fact_ids": [f.id for f in group],
                    "status": "needs_human_review",
                }
            )
    return output
