"""Validated, declarative research-agent catalog.

Every agent is an HTTPS/JSON workflow. Catalog entries contain no executable code
or credentials: a secret is referenced only by an environment-variable name.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from string import Formatter
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from business_signals.config import settings

AgentId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{1,62}$")]
TemplateValue = Annotated[str, StringConstraints(min_length=1, max_length=1000)]
JsonPath = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.*{}-]*$", max_length=300)]
EvidenceTopic = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){0,4}$", min_length=2, max_length=80),
]


class ApiKeyAuthentication(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["api_key"]
    environment: Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{1,127}$")]
    location: Literal["header", "query"] = "header"
    name: Annotated[str, StringConstraints(min_length=1, max_length=100)] = "Authorization"
    prefix: str = ""


class NoAuthentication(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["none"] = "none"


Authentication = Annotated[ApiKeyAuthentication | NoAuthentication, Field(discriminator="type")]


class AgentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_label: Annotated[str, StringConstraints(min_length=2, max_length=80)] = "subject"
    subject_pattern: Annotated[str, StringConstraints(min_length=1, max_length=300)] = r"^[^\r\n\x00]{1,160}$"


class InputTransform(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
    operation: Literal["split"]
    source: Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
    separator: Annotated[str, StringConstraints(min_length=1, max_length=20)]
    index: int = Field(ge=0, le=20)
    uppercase: bool = False


class HttpStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,62}$")]
    url: TemplateValue
    method: Literal["GET", "POST"] = "GET"
    query: dict[str, TemplateValue] = Field(default_factory=dict)
    headers: dict[str, TemplateValue] = Field(default_factory=dict)
    json_body: dict[str, TemplateValue] | None = None
    extract: dict[Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")], JsonPath] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def validate_url_and_credentials(self) -> "HttpStep":
        raw_url = urlparse(self.url)
        placeholder_url = re.sub(r"\{[A-Za-z_][A-Za-z0-9_]*\}", "value", self.url)
        parsed = urlparse(placeholder_url)
        host = (parsed.hostname or "").casefold()
        if parsed.scheme != "https" or not host:
            raise ValueError("Research-agent URLs must use HTTPS")
        if "{" in raw_url.netloc or host == "localhost" or host.endswith(".local"):
            raise ValueError("Research-agent URLs must use a public host")
        if re.fullmatch(r"(?:127|10|192\.168)\..*", host):
            raise ValueError("Research-agent URLs must use a public host")
        sensitive_names = {"authorization", "api-key", "apikey", "x-api-key"}
        request_names = {name.casefold() for name in self.headers} | {name.casefold() for name in self.query}
        if request_names & sensitive_names:
            raise ValueError("Put API credentials in the authentication section, not request templates")
        return self


class ComputedValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
    operation: Literal["first", "last", "min", "max", "sum", "percent_change"]
    path: JsonPath | None = None
    from_field: Annotated[str | None, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")] = None
    to_field: Annotated[str | None, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")] = None
    precision: int | None = Field(default=None, ge=0, le=8)

    @model_validator(mode="after")
    def validate_operation_inputs(self) -> "ComputedValue":
        if self.operation == "percent_change":
            if not self.from_field or not self.to_field:
                raise ValueError("percent_change requires from_field and to_field")
        elif self.path is None:
            raise ValueError(f"{self.operation} requires a JSON path")
        return self


class RelevanceSelection(BaseModel):
    """How a generic document-search agent exposes all returned candidates."""

    title_path: JsonPath
    url_path: JsonPath
    summary_path: JsonPath | None = None
    section_path: JsonPath | None = None
    published_at_path: JsonPath | None = None
    max_candidates: int = Field(default=20, ge=1, le=50)


class JsonResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items_path: JsonPath = ""
    item: Literal["first", "last"] = "first"
    computed: list[ComputedValue] = Field(default_factory=list, max_length=20)
    observation_template: TemplateValue
    no_result_observation_template: TemplateValue | None = None
    source_title_template: TemplateValue = "{agent_name}"
    source_url_template: TemplateValue | None = None
    source_reliability: float = Field(
        default_factory=lambda: settings.source_reliability_default,
        ge=0,
        le=1,
        validation_alias=AliasChoices("source_reliability", "confidence"),
    )
    relationship: Literal["supporting", "correlated", "contradicting"] = "correlated"
    relevance_selection: RelevanceSelection | None = None


class HttpJsonRunner(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["http_json"]
    authentication: Authentication = NoAuthentication()
    input: AgentInput = Field(default_factory=AgentInput)
    input_transforms: list[InputTransform] = Field(default_factory=list, max_length=10)
    steps: list[HttpStep] = Field(min_length=1, max_length=8)
    response: JsonResponse

    @model_validator(mode="after")
    def unique_workflow_names(self) -> "HttpJsonRunner":
        names = [step.name for step in self.steps]
        names.extend(transform.name for transform in self.input_transforms)
        if len(names) != len(set(names)):
            raise ValueError("Research-agent step and input-transform names must be unique")
        return self


class SplitStringsStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,62}$")]
    type: Literal["split_strings"]
    source: Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
    target: Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
    separator: Annotated[str, StringConstraints(min_length=1, max_length=20)] = ";"


class PipelineHttpStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,62}$")]
    type: Literal["http_json"]
    request: HttpStep
    target: Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
    for_each: Annotated[str | None, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")] = None
    bindings: dict[Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")], JsonPath] = Field(
        default_factory=dict
    )
    when: Literal["always", "administrative_scope"] = "always"


class SelectGeographicLocationsStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,62}$")]
    type: Literal["select_geographic_locations"]
    responses: Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
    locations: Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
    candidates_path: JsonPath
    target: Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]


class SampleBoundaryStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,62}$")]
    type: Literal["sample_boundary"]
    source: Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
    geometry_path: JsonPath
    scope: Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]


class BatchCoordinatesStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,62}$")]
    type: Literal["batch_coordinates"]
    source: Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
    target: Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
    max_items_setting: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,62}$")]


class SummarizeDailySeriesStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,62}$")]
    type: Literal["summarize_daily_series"]
    source: Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
    daily_path: JsonPath
    minimum_field: Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
    maximum_field: Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
    precipitation_field: Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
    comparison_start_value: Annotated[str | None, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")] = None
    comparison_metrics: list["DailyComparisonMetric"] = Field(default_factory=list, max_length=12)


class DailyComparisonMetric(BaseModel):
    """A configured daily series that the generic pipeline may compare by period."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(min_length=2, max_length=120)]
    field: Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
    unit: Annotated[str, StringConstraints(min_length=1, max_length=30)]
    aggregation: Literal["mean", "sum"]


PipelineStep = Annotated[
    SplitStringsStep
    | PipelineHttpStep
    | SelectGeographicLocationsStep
    | SampleBoundaryStep
    | BatchCoordinatesStep
    | SummarizeDailySeriesStep,
    Field(discriminator="type"),
]


class PipelineOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation_template: TemplateValue
    source_title_template: TemplateValue = "{agent_name}"
    source_reliability: float = Field(
        default_factory=lambda: settings.source_reliability_default,
        ge=0,
        le=1,
        validation_alias=AliasChoices("source_reliability", "confidence"),
    )
    relationship: Literal["supporting", "correlated", "contradicting"] = "correlated"


class PipelineRunner(BaseModel):
    """Composes only allow-listed, reusable integration primitives."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["pipeline"]
    authentication: Authentication = NoAuthentication()
    input: AgentInput = Field(default_factory=AgentInput)
    input_transforms: list[InputTransform] = Field(default_factory=list, max_length=10)
    capabilities: list[Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,62}$")]] = Field(
        default_factory=list, max_length=20
    )
    steps: list[PipelineStep] = Field(min_length=1, max_length=30)
    output: PipelineOutput

    @model_validator(mode="after")
    def unique_step_ids(self) -> "PipelineRunner":
        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("Pipeline step IDs must be unique")
        return self


Runner = Annotated[HttpJsonRunner | PipelineRunner, Field(discriminator="kind")]


class ResearchAgent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: AgentId
    name: Annotated[str, StringConstraints(min_length=2, max_length=100)]
    enabled: bool = True
    purpose: Annotated[str, StringConstraints(min_length=10, max_length=600)]
    subjects: list[Annotated[str, StringConstraints(min_length=2, max_length=80)]] = Field(min_length=1, max_length=30)
    # Admin-defined semantic capabilities. The investigation engine intersects
    # these with a hypothesis' evidence topics before an agent is selectable.
    evidence_topics: list[EvidenceTopic] = Field(default_factory=list, max_length=20)
    runner: Runner
    source_url: TemplateValue | None = None


class ResearchAgentCatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    agents: list[ResearchAgent] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_agent_ids(self) -> "ResearchAgentCatalogModel":
        ids = [agent.id for agent in self.agents]
        if len(ids) != len(set(ids)):
            raise ValueError("Research-agent IDs must be unique")
        return self


class ResearchAgentUnavailable(ValueError):
    """Raised when a configured agent is disabled or lacks its required secret."""


class ResearchAgentCatalog:
    def __init__(self, catalog: ResearchAgentCatalogModel) -> None:
        self._catalog = catalog
        self._by_id = {agent.id: agent for agent in catalog.agents}

    @classmethod
    def load(cls, path: Path) -> "ResearchAgentCatalog":
        try:
            payload: Any = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RuntimeError(f"Research-agent catalog was not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Research-agent catalog contains invalid JSON: {path}") from exc
        return cls(ResearchAgentCatalogModel.model_validate(payload))

    def list(self) -> list[ResearchAgent]:
        return self._catalog.agents

    def get(self, agent_id: str) -> ResearchAgent:
        try:
            agent = self._by_id[agent_id]
        except KeyError as exc:
            raise ResearchAgentUnavailable(f"Unknown research agent: {agent_id}") from exc
        if not agent.enabled:
            raise ResearchAgentUnavailable(f"Research agent is disabled: {agent_id}")
        authentication = agent.runner.authentication
        if isinstance(authentication, ApiKeyAuthentication) and not os.getenv(authentication.environment):
            raise ResearchAgentUnavailable(
                f"Research agent requires {authentication.environment} to be configured"
            )
        return agent


def render_template(value: str, values: dict[str, Any]) -> str:
    """Render simple named placeholders without attribute or index access."""
    for _, field_name, _, _ in Formatter().parse(value):
        if field_name is None:
            continue
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field_name):
            raise ValueError("Research-agent templates may contain only simple named placeholders")
        if field_name not in values:
            raise ValueError(f"Research-agent template uses an unsupported placeholder: {field_name}")
    return value.format_map(values)


def json_values(payload: Any, path: str) -> list[Any]:
    """Select values using a small JSON path language: dots, list indexes, and `*`."""
    values = [payload]
    for segment in filter(None, path.split(".")):
        next_values: list[Any] = []
        for value in values:
            if segment == "*":
                next_values.extend(value.values() if isinstance(value, dict) else value if isinstance(value, list) else [])
            elif isinstance(value, dict) and segment in value:
                next_values.append(value[segment])
            elif isinstance(value, list) and segment.isdigit() and int(segment) < len(value):
                next_values.append(value[int(segment)])
        values = next_values
    return values
