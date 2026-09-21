from types import SimpleNamespace
import logging

import pytest

from business_signals.config import settings
from business_signals.llm import EvidenceAssessment, OpenAICompatibleLLM, QuestionUnderstanding


class FakeCompletions:
    def __init__(self) -> None:
        self.request: dict[str, object] = {}

    async def parse(self, **kwargs: object) -> object:
        self.request = kwargs
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(parsed=QuestionUnderstanding(observations=[]), refusal=None))]
        )


@pytest.mark.asyncio
async def test_gpt_request_uses_structured_output_without_temperature(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
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
    assert "LLM step started: step=QuestionUnderstanding" in caplog.text
    assert "LLM step completed: step=QuestionUnderstanding" in caplog.text


def test_evidence_assessment_has_a_closed_structured_output_schema() -> None:
    from openai.lib._pydantic import to_strict_json_schema

    schema = to_strict_json_schema(EvidenceAssessment)
    evidence = schema["$defs"]["Evidence"]
    data_point = schema["$defs"]["EvidenceDataPoint"]

    assert evidence["properties"]["data"]["type"] == "array"
    assert data_point["additionalProperties"] is False
