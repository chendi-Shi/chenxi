"""Readiness checks; a synthetic live probe never establishes business accuracy."""

from __future__ import annotations

import os

from .config import Settings
from .models import Chunk, Segment
from .providers import ProviderError, build_provider
from .validation import validate


async def diagnose(settings: Settings, live: bool = False) -> tuple[dict, int]:
    key_present = bool(os.environ.get("RESEARCH_API_KEY") or os.environ.get("OPENAI_API_KEY"))
    result = {
        "provider": settings.provider,
        "model": settings.model,
        "credential_present": key_present,
        "network_test": "not_requested",
        "business_validation": "not_performed",
        "ready_for_model_extraction": False,
    }
    if settings.provider == "rules":
        result["action"] = "select_model_provider_and_model; rules_is_only_a_baseline"
        return result, 2
    if not key_present:
        result["action"] = "set_RESEARCH_API_KEY_in_local_environment"
        return result, 2
    if not live:
        result["action"] = "run_doctor_with_live_to_verify_model_access"
        return result, 2

    # This text is invented and contains no local documents or company information.
    text = "虚构测试公司2025年营收12亿元，未经审计。"
    chunk = Chunk(
        id="diagnostic",
        document_id="synthetic-diagnostic",
        segments=[
            Segment(
                id="probe-1", document_id="synthetic-diagnostic", start=0, end=len(text), text=text
            )
        ],
    )
    provider = build_provider(settings)
    try:
        response = await provider.extract(chunk)
        facts, issues = validate(response.extraction, chunk)
        result["network_test"] = "responded"
        result["validated_facts"] = len(facts)
        result["validation_issues"] = [issue.reason for issue in issues]
        result["usage"] = {
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "attempts": response.attempts,
        }
        result["ready_for_model_extraction"] = bool(facts) and not issues
        result["action"] = (
            "validate_on_authorized_real_materials"
            if result["ready_for_model_extraction"]
            else "inspect_model_schema_and_evidence_support"
        )
    except ProviderError as exc:
        result["network_test"] = "failed"
        result["error"] = exc.code
        result["action"] = "check_model_access_endpoint_and_credentials"
    finally:
        await provider.close()
    return result, 0 if result["ready_for_model_extraction"] else 2
