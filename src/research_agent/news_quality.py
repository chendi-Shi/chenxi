"""Per-company delivery gates; counters contain no article text or credentials."""

COMPANIES = ("英伟达", "腾讯")


class QualityGateError(ValueError):
    def __init__(self, code, report):
        super().__init__(code)
        self.companies = metrics(report)
        self.model_error = report.get("model_error", "")


def metrics(report):
    verified = {s["article_id"] for s in report["summaries"]}
    result = {}
    for company in COMPANIES:
        ids = {a["id"] for a in report["articles"] if a["company"] == company}
        sources = [s for s in report["coverage"] if s["company"] == company]
        result[company] = {
            "articles": len(ids),
            "verified_summaries": len(ids & verified),
            "healthy_sources": sum(s["status"] == "ok" for s in sources),
            "source_count": len(sources),
        }
    return result


def quality_gate(report):
    ids = [a["id"] for a in report["articles"]]
    summaries = [s["article_id"] for s in report["summaries"]]
    if len(ids) != len(set(ids)) or any(a["company"] not in COMPANIES for a in report["articles"]):
        raise QualityGateError("quality_gate_invalid_article_ids", report)
    if len(summaries) != len(set(summaries)) or not set(summaries) <= set(ids):
        raise QualityGateError("quality_gate_invalid_summary_ids", report)
    for stats in metrics(report).values():
        if not stats["healthy_sources"]:
            raise QualityGateError("quality_gate_company_uncovered", report)
        if stats["articles"] and stats["verified_summaries"] * 2 < stats["articles"]:
            raise QualityGateError("quality_gate_insufficient_verified_summaries", report)
        if not stats["articles"] and stats["healthy_sources"] < stats["source_count"]:
            raise QualityGateError("quality_gate_empty_with_source_failure", report)
