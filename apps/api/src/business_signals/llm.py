from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any, TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from business_signals.config import settings
from business_signals.models import Evidence, FinalAnalysis, Hypothesis, InvestigationStep, MetricDefinition

T = TypeVar("T", bound=BaseModel)


SYSTEM_PROMPT = """You are the planning component of a business root-cause investigation engine.
Use only supplied metadata and evidence. Never fabricate schema, values, or business semantics.
Keep competing hypotheses alive. Correlation is not causation. Prefer investigations with high
information gain. SQL must be one read-only PostgreSQL SELECT and must use only supplied schema.
Return valid JSON matching the requested schema, without markdown or private chain-of-thought."""


class QuestionUnderstanding(BaseModel):
    metric_definition: MetricDefinition | None = None
    observations: list[str] = Field(default_factory=list)
    ambiguity: str | None = None


class HypothesisPlan(BaseModel):
    hypotheses: list[Hypothesis]


class InvestigationPlan(BaseModel):
    step: InvestigationStep


class QueryPlan(BaseModel):
    sql: str
    purpose: str


class EvidenceAssessment(BaseModel):
    evidence: list[Evidence] = Field(default_factory=list)
    hypotheses: list[Hypothesis]
    confidence: float = Field(ge=0, le=1)
    ambiguity: str | None = None


class InvestigationDecision(BaseModel):
    action: str = Field(pattern="^(continue|finish)$")
    reason: str


class Synthesis(BaseModel):
    analysis: FinalAnalysis


class LLM(ABC):
    @abstractmethod
    async def structured(self, response_model: type[T], instruction: str, payload: dict[str, Any]) -> T: ...


class OpenAICompatibleLLM(LLM):
    def __init__(self) -> None:
        self.client: AsyncOpenAI | None = None

    async def structured(self, response_model: type[T], instruction: str, payload: dict[str, Any]) -> T:
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is required to run an investigation")
        if self.client is None:
            self.client = AsyncOpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
        response = await self.client.chat.completions.create(
            model=settings.openai_model,
            temperature=0,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"{instruction}\n\nInput:\n{json.dumps(payload, default=str)}"},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("The model returned an empty response")
        return response_model.model_validate_json(content)
