from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, SecretStr


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class DatasourceType(str, Enum):
    POSTGRESQL = "postgresql"
    TIMESCALEDB = "timescaledb"


class DatasourceCredentials(BaseModel):
    host: str
    port: int = Field(ge=1, le=65535)
    database: str
    username: str
    password: SecretStr
    ssl_mode: str = "prefer"


class DatasourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    type: DatasourceType
    credentials: DatasourceCredentials


class DatasourceSummary(BaseModel):
    id: str
    name: str
    type: DatasourceType
    connected: bool = False
    table_count: int = 0
    schema_cached: bool = False
    discovered_at: datetime | None = None


class ColumnMetadata(BaseModel):
    name: str
    data_type: str
    nullable: bool
    primary_key: bool = False
    representative_values: list[Any] = Field(default_factory=list)


class TableMetadata(BaseModel):
    schema_name: str
    name: str
    columns: list[ColumnMetadata] = Field(default_factory=list)
    foreign_keys: list[dict[str, str]] = Field(default_factory=list)
    indexes: list[dict[str, Any]] = Field(default_factory=list)
    approximate_rows: int | None = None
    is_hypertable: bool = False
    time_column: str | None = None
    time_range: dict[str, Any] | None = None


class DatasourceMetadata(BaseModel):
    datasource_id: str
    type: DatasourceType
    structural_metadata: list[TableMetadata]
    semantic_metadata: dict[str, Any] = Field(default_factory=dict)
    fingerprint: str
    discovered_at: datetime = Field(default_factory=now_utc)


class CrossDatasourceRelation(BaseModel):
    source_datasource: str
    source_field: str
    target_datasource: str
    target_field: str
    confidence: float = Field(ge=0, le=1)
    origin: Literal["explicit", "name_type_match", "llm", "human"]
    confirmed: bool = False


class MetricDefinition(BaseModel):
    name: str
    expression: str
    unit: str | None = None
    confidence: float = Field(ge=0, le=1)


class HypothesisStatus(str, Enum):
    ACTIVE = "active"
    WEAKENED = "weakened"
    REJECTED = "rejected"
    SUPPORTED = "supported"
    CONFIRMED = "confirmed"
    NEEDS_MORE_EVIDENCE = "needs_more_evidence"


class Hypothesis(BaseModel):
    id: str = Field(default_factory=lambda: f"hyp_{uuid4().hex[:8]}")
    description: str
    category: str
    confidence: float = Field(ge=0, le=1)
    supporting_evidence: list[str] = Field(default_factory=list)
    contradicting_evidence: list[str] = Field(default_factory=list)
    status: HypothesisStatus = HypothesisStatus.ACTIVE


class Evidence(BaseModel):
    id: str = Field(default_factory=lambda: f"ev_{uuid4().hex[:8]}")
    description: str
    source: str
    relationship: Literal["direct", "supporting", "correlated", "contradicting"]
    confidence: float = Field(ge=0, le=1)
    data: dict[str, Any] = Field(default_factory=dict)


class Observation(BaseModel):
    description: str
    value: Any = None
    source: str


class InvestigationStep(BaseModel):
    iteration: int = 0
    hypothesis_id: str | None = None
    action: str
    datasource_id: str | None = None
    rationale: str
    expected_information_gain: float = Field(default=0.5, ge=0, le=1)
    query: str | None = None


class ExternalFinding(BaseModel):
    type: Literal["weather", "economy_fx", "news_event"]
    location: str | None = None
    period: str
    observation: str
    relationship: Literal["supporting", "correlated", "contradicting"] = "correlated"
    confidence: float = Field(ge=0, le=1)
    source_url: str
    source_title: str


class HumanFeedback(BaseModel):
    question: str
    response: str
    received_at: datetime = Field(default_factory=now_utc)


class InvestigationStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_HUMAN = "waiting_for_human"
    COMPLETED = "completed"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    FAILED = "failed"


class InvestigationLimits(BaseModel):
    max_iterations: int = Field(default=8, ge=1, le=30)
    max_sql_queries: int = Field(default=12, ge=1, le=50)
    max_external_calls: int = Field(default=2, ge=0, le=10)
    max_duration_seconds: int = Field(default=180, ge=10, le=1800)


class InvestigationCreate(BaseModel):
    question: str = Field(min_length=10, max_length=2000)
    datasource_ids: list[str] = Field(min_length=1)
    limits: InvestigationLimits = Field(default_factory=InvestigationLimits)


class InvestigationEvent(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    investigation_id: str
    type: str
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now_utc)


class FinalAnalysis(BaseModel):
    likely_root_cause: str | None
    confidence: float
    evidence: list[Evidence]
    rejected_hypotheses: list[Hypothesis]
    external_findings: list[ExternalFinding]
    caveats: list[str]
    summary: str


class InvestigationState(BaseModel):
    investigation_id: str
    question: str
    datasources: list[DatasourceSummary]
    limits: InvestigationLimits = Field(default_factory=InvestigationLimits)
    metric_definition: MetricDefinition | None = None
    observations: list[Observation] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    investigation_history: list[InvestigationStep] = Field(default_factory=list)
    external_findings: list[ExternalFinding] = Field(default_factory=list)
    current_focus: str | None = None
    next_action: str | None = None
    external_request: dict[str, str] | None = None
    pending_step: InvestigationStep | None = None
    pending_human_question: str | None = None
    human_feedback: list[HumanFeedback] = Field(default_factory=list)
    iteration: int = 0
    query_count: int = 0
    external_call_count: int = 0
    confidence: float | None = None
    status: InvestigationStatus = InvestigationStatus.QUEUED
    started_at: datetime = Field(default_factory=now_utc)
    final_analysis: FinalAnalysis | None = None
    error: str | None = None
