"""Deterministic gold matching: exact evidence and category, not semantic model grading."""

from __future__ import annotations

import json
from pathlib import Path

from .config import Settings
from .models import SourceInput
from .parsing import make_chunks, segment_document
from .providers import Provider
from .validation import validate


async def evaluate(path: Path, settings: Settings, provider: Provider):
    items = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    results, total_gold, matched_total, total_output, supported_output = [], 0, 0, 0, 0
    for item in items:
        doc = SourceInput(title=item["id"], body=item["body"])
        chunks = make_chunks(
            item["id"], segment_document(item["id"], doc, settings.segment_chars), settings
        )
        found, rejected = [], 0
        for chunk in chunks:
            output = await provider.extract(chunk)
            valid, issues = validate(output.extraction, chunk)
            found.extend(valid)
            rejected += len(issues)
        # Count unique facts after overlap; evidence quotation equality is the published metric.
        actual = {(f.category.value, e.quote) for f in found for e in f.evidence}
        gold = {(fact["category"], fact["quote"]) for fact in item["gold"]}
        matched = actual & gold
        total_gold += len(gold)
        matched_total += len(matched)
        total_output += len(actual) + rejected
        supported_output += len(actual)
        results.append(
            {
                "id": item["id"],
                "gold": len(gold),
                "matched": len(matched),
                "output": len(actual),
                "rejected": rejected,
                "missing": [list(entry) for entry in sorted(gold - actual)],
            }
        )
    return {
        "dataset": path.name,
        "synthetic": all(item.get("synthetic", False) for item in items),
        "provider": settings.provider,
        "cases": results,
        "gold_facts": total_gold,
        "exact_gold_recall": matched_total / total_gold if total_gold else None,
        "exact_gold_precision": matched_total / (total_output - sum(c["rejected"] for c in results))
        if total_output > sum(c["rejected"] for c in results)
        else None,
        "validation_acceptance_rate": supported_output / total_output if total_output else None,
        "warning": "Exact quotation matching on synthetic data; not semantic truth, real-world accuracy, or time saved.",
    }
