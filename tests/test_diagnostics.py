from research_agent.config import Settings
from research_agent.diagnostics import diagnose
from research_agent.models import CallResult, Extraction
from research_agent.providers import ProviderError, RulesProvider


async def test_baseline_is_not_model_ready():
    result, code = await diagnose(Settings(), live=True)
    assert code == 2
    assert result["network_test"] == "not_requested"
    assert not result["ready_for_model_extraction"]


async def test_missing_key_and_no_implicit_network(monkeypatch):
    monkeypatch.delenv("RESEARCH_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    settings = Settings(provider="openai", model="test-model")
    result, code = await diagnose(settings, live=True)
    assert code == 2
    assert result["action"] == "set_RESEARCH_API_KEY_in_local_environment"
    monkeypatch.setenv("RESEARCH_API_KEY", "private-key")
    result, code = await diagnose(settings)
    assert code == 2
    assert result["network_test"] == "not_requested"
    assert "private-key" not in str(result)


async def test_live_probe_only_sends_synthetic_text_and_closes(monkeypatch):
    monkeypatch.setenv("RESEARCH_API_KEY", "private-key")

    class Probe(RulesProvider):
        closed = False

        async def extract(self, chunk, feedback=None):
            assert chunk.document_id == "synthetic-diagnostic"
            return await super().extract(chunk)

        async def close(self):
            self.closed = True

    probe = Probe()
    monkeypatch.setattr("research_agent.diagnostics.build_provider", lambda _: probe)
    result, code = await diagnose(Settings(provider="openai", model="test-model"), live=True)
    assert code == 0
    assert result["business_validation"] == "not_performed"
    assert probe.closed


async def test_empty_output_and_provider_error_are_not_ready(monkeypatch):
    monkeypatch.setenv("RESEARCH_API_KEY", "private-key")

    class Probe(RulesProvider):
        fail = False

        async def extract(self, chunk, feedback=None):
            if self.fail:
                raise ProviderError("http_401")
            return CallResult(extraction=Extraction(facts=[]))

    probe = Probe()
    monkeypatch.setattr("research_agent.diagnostics.build_provider", lambda _: probe)
    settings = Settings(provider="openai", model="test-model")
    result, code = await diagnose(settings, live=True)
    assert code == 2
    assert not result["ready_for_model_extraction"]
    probe.fail = True
    result, code = await diagnose(settings, live=True)
    assert code == 2
    assert result["error"] == "http_401"
