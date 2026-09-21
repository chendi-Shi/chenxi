import json

import httpx
import pytest

from research_agent.models import Chunk, Segment
from research_agent.providers import OpenAIProvider, ProviderError, retry_after


def sample_chunk():
    return Chunk(
        id="c",
        document_id="d",
        segments=[Segment(id="s", document_id="d", start=0, end=3, text="正文。")],
    )


def response_data(facts=None):
    return {
        "status": "completed",
        "usage": {"input_tokens": 100, "output_tokens": 20},
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": json.dumps({"facts": facts or []})}],
            }
        ],
    }


async def test_rate_limit_retry_then_success(settings):
    calls = []
    delays = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json=response_data())

    async def sleep(value):
        delays.append(value)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAIProvider(settings, client, api_key="test-key", sleep=sleep)
        result = await provider.extract(sample_chunk())
    assert result.attempts == 2
    assert result.input_tokens == 100
    assert delays == [0]
    payload = json.loads(calls[-1].content)
    assert payload["store"] is False
    assert payload["text"]["format"]["strict"] is True
    assert payload["text"]["format"]["schema"]["additionalProperties"] is False


async def test_auth_failure_not_retried_or_response_leaked(settings):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(401, text="secret echo must not leak")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAIProvider(settings, client, api_key="test-key")
        with pytest.raises(ProviderError, match="http_401") as failure:
            await provider.extract(sample_chunk())
    assert count == 1
    assert "secret" not in str(failure.value)


@pytest.mark.parametrize(
    "data,expected",
    [
        ({"status": "incomplete"}, "response_not_completed"),
        (
            {
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}],
            },
            "model_refusal",
        ),
        ({"status": "completed", "output": []}, "invalid_extraction_schema"),
    ],
)
async def test_unusable_model_output_explicit(settings, data, expected):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=data))
    ) as client:
        with pytest.raises(ProviderError, match=expected):
            await OpenAIProvider(settings, client, api_key="test").extract(sample_chunk())


async def test_timeout_retry_is_bounded(settings):
    count = 0
    settings.max_retries = 2

    async def no_sleep(value):
        pass

    def handler(request):
        nonlocal count
        count += 1
        raise httpx.ReadTimeout("private diagnostic", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError, match="network_or_timeout") as failure:
            await OpenAIProvider(settings, client, api_key="test", sleep=no_sleep).extract(
                sample_chunk()
            )
    assert count == 3
    assert failure.value.attempts == 3


def test_retry_after_clamped():
    assert retry_after("99999", 0) == 60
    assert retry_after("-1", 0) == 0
    assert 0 <= retry_after("nonsense", 0) <= 60
