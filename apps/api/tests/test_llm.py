import logging
from importlib.resources import files
from types import SimpleNamespace

import pytest
from business_signals.config import settings
from business_signals.llm import (
    AnthropicLLM,
    EvidenceAssessment,
    OpenAICompatibleLLM,
    QuestionUnderstanding,
    create_llm,
)
from business_signals.prompt_catalog import prompts


class FakeCompletions:
    def __init__(self) -> None:
        self.request: dict[str, object] = {}

    async def parse(self, **kwargs: object) -> object:
        self.request = kwargs
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        parsed=QuestionUnderstanding(observations=[]),
                        refusal=None,
                    )
                )
            ]
        )


class FakeAnthropicResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {
            "content": [
                {
                    "type": "tool_use",
                    "name": "submit_structured_response",
                    "input": {"observations": ["Sales fell."], "request_type": "investigation"},
                }
            ]
        }


class FakeAnthropicClient:
    def __init__(self) -> None:
        self.request: dict[str, object] = {}

    async def __aenter__(self) -> "FakeAnthropicClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def post(self, url: str, **kwargs: object) -> FakeAnthropicResponse:
        self.request = {"url": url, **kwargs}
        return FakeAnthropicResponse()


@pytest.mark.asyncio
async def test_gpt_request_uses_structured_output_without_temperature(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(settings, "ai_api_key", "test-key")
    monkeypatch.setattr(settings, "ai_provider", "openai")
    completions = FakeCompletions()
    llm = OpenAICompatibleLLM()
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        await llm.structured(QuestionUnderstanding, "Understand the question", {"question": "Why?"})

    assert completions.request["response_format"] is QuestionUnderstanding
    assert "temperature" not in completions.request
    messages = completions.request["messages"]
    assert isinstance(messages, list)
    assert "plain business language" in messages[0]["content"]
    assert "LLM step started: provider=openai step=QuestionUnderstanding" in caplog.text
    assert "LLM step completed: provider=openai step=QuestionUnderstanding" in caplog.text


@pytest.mark.asyncio
async def test_anthropic_request_uses_a_forced_schema_tool(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    import business_signals.llm as llm_module

    monkeypatch.setattr(settings, "ai_api_key", "test-key")
    monkeypatch.setattr(settings, "ai_provider", "anthropic")
    monkeypatch.setattr(settings, "ai_model", "claude-test")
    client = FakeAnthropicClient()
    monkeypatch.setattr(llm_module.httpx, "AsyncClient", lambda **_kwargs: client)

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        result = await AnthropicLLM().structured(QuestionUnderstanding, "Understand", {"question": "Why?"})

    assert result.observations == ["Sales fell."]
    assert client.request["url"] == "https://api.anthropic.com/v1/messages"
    assert client.request["headers"] == {
        "x-api-key": "test-key",
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    request = client.request["json"]
    assert isinstance(request, dict)
    assert request["tool_choice"] == {"type": "tool", "name": "submit_structured_response"}
    assert request["tools"][0]["input_schema"]["title"] == "QuestionUnderstanding"
    assert "LLM step completed: provider=anthropic step=QuestionUnderstanding" in caplog.text


def test_provider_factory_selects_the_configured_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ai_provider", "anthropic")
    assert isinstance(create_llm(), AnthropicLLM)

    monkeypatch.setattr(settings, "ai_provider", "openai_compatible")
    assert isinstance(create_llm(), OpenAICompatibleLLM)


def test_evidence_assessment_has_a_closed_structured_output_schema() -> None:
    from openai.lib._pydantic import to_strict_json_schema

    schema = to_strict_json_schema(EvidenceAssessment)
    evidence = schema["$defs"]["Evidence"]
    data_point = schema["$defs"]["EvidenceDataPoint"]

    assert evidence["properties"]["data"]["type"] == "array"
    assert data_point["additionalProperties"] is False


def test_prompt_catalog_loads_packaged_versioned_instructions() -> None:
    prompt_names = [resource.stem for resource in files("business_signals.prompts").iterdir() if resource.suffix == ".txt"]
    assert prompt_names
    assert all(prompts.load(name) for name in prompt_names)
    assert "plain business language" in prompts.load("system")
    with pytest.raises(ValueError, match="Invalid prompt name"):
        prompts.load("../system")
    with pytest.raises(RuntimeError, match="Prompt file is missing"):
        prompts.load("does_not_exist")
