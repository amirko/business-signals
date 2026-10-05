from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any, Literal, TypeVar

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from business_signals.config import settings
from business_signals.models import (
    Evidence,
    FinalAnalysis,
    Hypothesis,
    InvestigationScope,
    MetricDefinition,
)
from business_signals.prompt_catalog import prompts

T = TypeVar("T", bound=BaseModel)

# Uvicorn configures this logger at INFO level, so these records appear in the
# terminal when the API is started with `make api`.
logger = logging.getLogger("uvicorn.error")


SYSTEM_PROMPT = prompts.load("system")


class QuestionUnderstanding(BaseModel):
    metric_definition: MetricDefinition | None = None
    scope: InvestigationScope | None = None
    observations: list[str] = Field(default_factory=list)
    ambiguity: str | None = None
    # Only one question can pause a conversation at a time. This lets the
    # workflow preserve a business-measure clarification while resolving a
    # separate vocabulary term from discovered data.
    clarification_kind: Literal["measure", "scope", "period", "definition", "other"] = "other"
    request_type: Literal["investigation", "direct_answer"] = "investigation"
    # The factual starting point a causal question assumes.  This is extracted
    # semantically by the model, not by matching words such as "rise" or
    # "fall", and must be checked before causes are investigated.
    premise_to_validate: str | None = Field(default=None, max_length=500)


class HypothesisPlan(BaseModel):
    hypotheses: list[Hypothesis]


class PlannedInvestigationStep(BaseModel):
    """LLM-facing plan that selects a human-readable hypothesis name, not an internal ID."""

    iteration: int = 0
    hypothesis_name: str
    # Older saved conversations do not contain this field. The workflow then
    # selects the first still-pending requirement deterministically.
    requirement_id: str = ""
    action: str
    datasource_id: str | None = None
    rationale: str
    expected_information_gain: float = Field(default=0.5, ge=0, le=1)


class InvestigationPlan(BaseModel):
    step: PlannedInvestigationStep


class PremiseValidationPlan(BaseModel):
    """Choose the source for validating a user-reported business change."""

    datasource_id: str
    action: str
    rationale: str


class CrossDatasourceLookup(BaseModel):
    """A bounded lookup used to filter a query on another connected datasource."""

    datasource_id: str
    relation_id: str
    sql: str
    purpose: str


class EvidenceCheckContract(BaseModel):
    """A bounded statement of what a generated query is allowed to prove.

    SQL remains a model proposal, but this contract is independently checked by
    the workflow before a database is touched.  It is intentionally phrased in
    business terms rather than a fixed list of causes or field names.
    """

    claim_id: str | None = None
    requirement_id: str | None = None
    role: Literal["baseline", "causal", "limitation"] = "causal"
    basis: Literal["direct", "proxy"] = "direct"
    relationship_ids: list[str] = Field(default_factory=list, max_length=2)
    rationale: str = Field(default="", max_length=500)


class QueryPlan(BaseModel):
    sql: str
    purpose: str
    cross_datasource_lookups: list[CrossDatasourceLookup] = Field(default_factory=list, max_length=2)
    contract: EvidenceCheckContract = Field(default_factory=EvidenceCheckContract)


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
    # Only applicable to the unowned baseline check.  A causal investigation
    # may proceed only when its factual starting point is confirmed.
    premise_verdict: Literal["not_applicable", "confirmed", "rejected", "inconclusive"] = "not_applicable"


class InvestigationDecision(BaseModel):
    action: str = Field(pattern="^(continue|external|finish)$")
    reason: str


class ExternalResearchPlan(BaseModel):
    """A targeted, bounded lookup selected from the configured research-agent catalog."""

    hypothesis_name: str
    agent_id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,62}$")
    subject: str = Field(min_length=1, max_length=160)
    start_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    comparison_start_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    rationale: str = Field(min_length=10, max_length=500)


class ExternalResearchPlans(BaseModel):
    """A bounded set of independent catalog-defined external checks."""

    plans: list[ExternalResearchPlan] = Field(min_length=1, max_length=3)


class ExternalAgentAssignment(BaseModel):
    """Independent suitability verdict for configured research agents."""

    hypothesis_name: str
    agent_ids: list[str] = Field(default_factory=list, max_length=8)
    valid: bool
    rationale: str = Field(min_length=1, max_length=500)


class ExternalAgentAssignments(BaseModel):
    assignments: list[ExternalAgentAssignment] = Field(default_factory=list, max_length=8)


class Synthesis(BaseModel):
    analysis: FinalAnalysis


class LLM(ABC):
    @abstractmethod
    async def structured(self, response_model: type[T], instruction: str, payload: dict[str, Any]) -> T: ...


class OpenAICompatibleLLM(LLM):
    """OpenAI's API, or a provider that implements its chat-completions contract."""

    def __init__(self) -> None:
        self.client: AsyncOpenAI | None = None

    async def structured(self, response_model: type[T], instruction: str, payload: dict[str, Any]) -> T:
        if not settings.ai_api_key:
            raise RuntimeError("AI_API_KEY is required when AI_PROVIDER is openai or openai_compatible")
        if self.client is None:
            self.client = AsyncOpenAI(api_key=settings.ai_api_key, base_url=settings.ai_base_url)
        step_name = response_model.__name__
        trusted_entities = payload.get("trusted_entity_references", [])
        entity_names = [item.get("display_name") for item in trusted_entities if isinstance(item, dict)]
        logger.info(
            "LLM step started: provider=%s step=%s model=%s question=%r datasources=%d retained_entity_names=%s",
            settings.ai_provider,
            step_name,
            settings.ai_model,
            payload.get("question"),
            len(payload.get("datasources", [])),
            entity_names,
        )
        try:
            # `parse` uses OpenAI Structured Outputs: the response is constrained to the
            # Pydantic schema, unlike JSON mode which guarantees valid JSON only.
            response = await self.client.chat.completions.parse(
                model=settings.ai_model,
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
            logger.exception("LLM step failed: provider=%s step=%s", settings.ai_provider, step_name)
            raise

        logger.info(
            "LLM step completed: provider=%s step=%s output=%s",
            settings.ai_provider,
            step_name,
            result.model_dump_json(),
        )
        return result


class AnthropicLLM(LLM):
    """Anthropic Messages API using a forced tool call for schema-validated output."""

    endpoint = "https://api.anthropic.com/v1/messages"

    async def structured(self, response_model: type[T], instruction: str, payload: dict[str, Any]) -> T:
        if not settings.ai_api_key:
            raise RuntimeError("AI_API_KEY is required when AI_PROVIDER is anthropic")

        step_name = response_model.__name__
        trusted_entities = payload.get("trusted_entity_references", [])
        entity_names = [item.get("display_name") for item in trusted_entities if isinstance(item, dict)]
        logger.info(
            "LLM step started: provider=anthropic step=%s model=%s question=%r datasources=%d retained_entity_names=%s",
            step_name,
            settings.ai_model,
            payload.get("question"),
            len(payload.get("datasources", [])),
            entity_names,
        )

        request = {
            "model": settings.ai_model,
            "max_tokens": 4096,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": f"{instruction}\n\nInput:\n{json.dumps(payload, default=str)}"}],
            "tools": [
                {
                    "name": "submit_structured_response",
                    "description": "Submit the requested response using the exact required schema.",
                    "input_schema": response_model.model_json_schema(),
                }
            ],
            "tool_choice": {"type": "tool", "name": "submit_structured_response"},
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    self.endpoint,
                    headers={
                        "x-api-key": settings.ai_api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json=request,
                )
                response.raise_for_status()
                content = response.json().get("content", [])
            tool_use = next(
                (
                    block
                    for block in content
                    if block.get("type") == "tool_use" and block.get("name") == "submit_structured_response"
                ),
                None,
            )
            if not tool_use or not isinstance(tool_use.get("input"), dict):
                raise RuntimeError("The model did not return the required structured response")
            result = response_model.model_validate(tool_use["input"])
        except Exception:
            logger.exception("LLM step failed: provider=anthropic step=%s", step_name)
            raise

        logger.info(
            "LLM step completed: provider=anthropic step=%s output=%s", step_name, result.model_dump_json()
        )
        return result


def create_llm() -> LLM:
    """Build the configured provider without leaking provider concerns into the engine."""
    if settings.ai_provider == "anthropic":
        return AnthropicLLM()
    return OpenAICompatibleLLM()
