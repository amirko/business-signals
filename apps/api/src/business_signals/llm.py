from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any, Literal, TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from business_signals.config import settings
from business_signals.models import Evidence, FinalAnalysis, Hypothesis, InvestigationStep, MetricDefinition

T = TypeVar("T", bound=BaseModel)

# Uvicorn configures this logger at INFO level, so these records appear in the
# terminal when the API is started with `make api`.
logger = logging.getLogger("uvicorn.error")


SYSTEM_PROMPT = """You are the planning component of a business root-cause investigation engine.
Use only supplied metadata and evidence. Never fabricate schema, values, or business semantics.
Keep competing hypotheses alive. Correlation is not causation. Prefer investigations with high
information gain. SQL must be one read-only PostgreSQL SELECT and must use only supplied schema.
All text that a person will read must use plain business language for a non-technical audience.
Do not expose SQL, function names, table names, column names, datasource IDs, or implementation
details in hypothesis names, descriptions, evidence, clarification questions, step purposes, or
conclusions. For example, say "the total amount customers spent" rather than "SUM(revenue)" and
say "the sales records may be incomplete" rather than a database-ingestion diagnosis. The sole
exception is QueryPlan.sql, which is backend-only. Ask one concise clarification at a time, using
ordinary words and useful choices rather than technical terminology.
Return only the requested structured response. Use its exact property names and do not substitute
similarly named fields. Do not include markdown or private chain-of-thought."""


class QuestionUnderstanding(BaseModel):
    metric_definition: MetricDefinition | None = None
    observations: list[str] = Field(default_factory=list)
    ambiguity: str | None = None
    request_type: Literal["investigation", "direct_answer"] = "investigation"


class HypothesisPlan(BaseModel):
    hypotheses: list[Hypothesis]


class PlannedInvestigationStep(BaseModel):
    """LLM-facing plan that selects a human-readable hypothesis name, not an internal ID."""

    iteration: int = 0
    hypothesis_name: str
    action: str
    datasource_id: str | None = None
    rationale: str
    expected_information_gain: float = Field(default=0.5, ge=0, le=1)


class InvestigationPlan(BaseModel):
    step: PlannedInvestigationStep


class QueryPlan(BaseModel):
    sql: str
    purpose: str


class DirectAnswerPlan(BaseModel):
    datasource_id: str
    sql: str
    purpose: str
    supporting_queries: list["DirectAnswerQuery"] = Field(default_factory=list, max_length=3)
    combination: "DirectAnswerCombination | None" = None


class DirectAnswerQuery(BaseModel):
    """One read-only query, always executed against exactly one datasource."""

    datasource_id: str
    sql: str
    purpose: str


class DirectAnswerCombination(BaseModel):
    """A small, deterministic cross-source operation over independently queried rows."""

    operation: Literal["set_difference", "intersection", "union"]
    primary_key: str
    supporting_query_index: int = Field(ge=0, le=2)
    supporting_key: str


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
        step_name = response_model.__name__
        logger.info("LLM step started: step=%s model=%s", step_name, settings.openai_model)
        try:
            # `parse` uses OpenAI Structured Outputs: the response is constrained to the
            # Pydantic schema, unlike JSON mode which guarantees valid JSON only.
            response = await self.client.chat.completions.parse(
                model=settings.openai_model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"{instruction}\n\nInput:\n{json.dumps(payload, default=str)}"},
                ],
                response_format=response_model,
            )
            message = response.choices[0].message
            if message.parsed is None:
                if message.refusal:
                    raise RuntimeError("The model declined to produce the requested structured response")
                raise RuntimeError("The model returned an empty structured response")
            result = message.parsed
        except Exception:
            logger.exception("LLM step failed: step=%s", step_name)
            raise

        logger.info("LLM step completed: step=%s output=%s", step_name, result.model_dump_json())
        return result
