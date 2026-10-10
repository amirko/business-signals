from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, SecretStr

from business_signals.config import settings


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
    """A relationship approved by an administrator for two separate connections."""

    id: str = Field(default_factory=lambda: f"rel_{uuid4().hex[:10]}")
    source_datasource: str
    source_field: str
    target_datasource: str
    target_field: str
    label: str = Field(default="Approved relationship", min_length=1, max_length=160)
    cardinality: Literal["many_to_one", "one_to_one"] = "many_to_one"
    confidence: float = Field(ge=0, le=1)
    sampled_values: int = Field(default=0, ge=0)
    matched_values: int = Field(default=0, ge=0)
    origin: Literal["human", "value_overlap"] = "human"
    confirmed: bool = False


class CrossDatasourceRelationCreate(BaseModel):
    source_datasource: str
    source_field: str
    target_datasource: str
    target_field: str
    label: str = Field(min_length=1, max_length=160)
    cardinality: Literal["many_to_one", "one_to_one"] = "many_to_one"


class MetricDefinition(BaseModel):
    name: str
    expression: str
    unit: str | None = None
    confidence: float = Field(ge=0, le=1)


class InvestigationScope(BaseModel):
    """The business scope the investigation is expected to measure."""

    metric: str | None = None
    comparison: str | None = None
    dimensions: list[str] = Field(default_factory=list, max_length=12)
    filters: list[str] = Field(default_factory=list, max_length=20)


class ScopeCoverage(BaseModel):
    """A deterministic record of how much of a requested filter was matched."""

    description: str
    matched_records: int = Field(ge=0)
    limited: bool = False


class HypothesisStatus(str, Enum):
    ACTIVE = "active"
    WEAKENED = "weakened"
    REJECTED = "rejected"
    SUPPORTED = "supported"
    CONFIRMED = "confirmed"
    NEEDS_MORE_EVIDENCE = "needs_more_evidence"


class CausalClaim(BaseModel):
    """Schema-independent contract for one proposed explanation.

    The text is deliberately ordinary language.  It lets the workflow decide
    whether a proposed check actually bears on the explanation without
    teaching the application a fixed catalogue of business causes.
    """

    cause: str = Field(min_length=1, max_length=500)
    mechanism: str = Field(min_length=1, max_length=500)
    outcome: str = Field(min_length=1, max_length=500)
    required_evidence: list[str] = Field(default_factory=list, max_length=8)


class EvidenceRequirement(BaseModel):
    """One bounded fact needed to test one causal claim.

    A requirement is the unit of work for an investigation.  It is deliberately
    owned by the hypothesis rather than by an LLM response so a completed check
    cannot be re-planned under a different label.
    """

    id: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=500)
    status: Literal["pending", "completed", "unavailable", "duplicate"] = "pending"


class Hypothesis(BaseModel):
    id: str = Field(default_factory=lambda: f"hyp_{uuid4().hex[:8]}")
    name: str = Field(min_length=1, max_length=80)
    description: str
    category: str
    claim: CausalClaim | None = None
    requirements: list[EvidenceRequirement] = Field(default_factory=list, max_length=2)
    research_scope: Literal["internal", "external"] = "internal"
    # Configured agent IDs selected for this claim from their declared
    # purpose/input contract. This replaces universal topic-name matching.
    external_agent_ids: list[str] = Field(default_factory=list, max_length=8)
    # Retained only so existing archives and configured research agents can be
    # read. New investigation decisions use ``claim`` and agent purposes,
    # rather than a fixed taxonomy of topic strings.
    evidence_topics: list[str] = Field(default_factory=list, max_length=12)
    confidence: float = Field(ge=0, le=1)
    supporting_evidence: list[str] = Field(default_factory=list)
    contradicting_evidence: list[str] = Field(default_factory=list)
    status: HypothesisStatus = HypothesisStatus.ACTIVE


class EvidenceDataPoint(BaseModel):
    """A compact, schema-safe fact backing an evidence statement."""

    field: str
    value: str | int | float | bool | None = None


class Evidence(BaseModel):
    id: str = Field(default_factory=lambda: f"ev_{uuid4().hex[:8]}")
    description: str
    source: str
    relationship: Literal["direct", "supporting", "correlated", "contradicting"]
    confidence: float = Field(ge=0, le=1)
    # Premises establish the question's baseline; caveats establish a limitation.
    # Neither should be rendered as evidence for a causal hypothesis.
    scope: Literal["premise", "mechanism", "caveat"] = "mechanism"
    # Whether this evidence comes from the source that directly records the
    # claimed mechanism, or from an explicitly weaker substitute.
    basis: Literal["direct", "proxy", "context"] = "direct"
    # A causal finding has exactly one owner. Premises and caveats have none.
    hypothesis_id: str | None = None
    requirement_id: str | None = None
    # ``claim_id`` and ``hypothesis_ids`` are retained to read historical
    # archives. New live evidence uses the singular ownership fields above.
    claim_id: str | None = None
    evidence_topics: list[str] = Field(default_factory=list, max_length=12)
    hypothesis_ids: list[str] = Field(default_factory=list)
    # Strict Structured Outputs cannot accept an open-ended JSON object here.
    # Named values keep evidence inspectable while preserving a closed schema.
    data: list[EvidenceDataPoint] = Field(default_factory=list)


class Observation(BaseModel):
    description: str
    value: Any = None
    source: str


class InvestigationStep(BaseModel):
    iteration: int = 0
    hypothesis_id: str | None = None
    requirement_id: str | None = None
    action: str
    datasource_id: str | None = None
    rationale: str
    expected_information_gain: float = Field(default=0.5, ge=0, le=1)
    query: str | None = None


class ExternalFinding(BaseModel):
    # Agent identifiers are catalog-defined.  They are not a fixed enum so a
    # deployment can install an additional configured research agent.
    # Underscores remain accepted for archived findings created before the
    # catalog used hyphenated agent IDs.
    type: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,62}$")
    location: str | None = None
    period: str
    observation: str
    relationship: Literal["supporting", "correlated", "contradicting"] = "correlated"
    confidence: float = Field(ge=0, le=1)
    # A configured provider-quality estimate. It is deliberately distinct
    # from claim-specific ``confidence``, which is assigned after relevance
    # and evidence assessment.
    source_reliability: float | None = Field(default=None, ge=0, le=1)
    source_url: str
    source_title: str
    # A configured document search can require a literal match to its subject
    # before the language model ranks the provider's results.
    candidate_subject_match_required: bool = False
    significance: Literal["major", "minor", "none"] = "none"
    candidate_rankings: list["ExternalResearchCandidateRanking"] = Field(default_factory=list, max_length=50)
    measurements: list["ExternalMeasurement"] = Field(default_factory=list)
    coverage_method: str | None = None
    point_count: int | None = Field(default=None, ge=1)
    # Candidate documents are retained only for research agents configured to
    # use relevance selection. They show what the provider returned without
    # treating its first hit as evidence.
    candidates: list["ExternalResearchCandidate"] = Field(default_factory=list, max_length=50)


class ExternalResearchCandidate(BaseModel):
    """A provider-returned document considered for one external finding."""

    title: str = Field(min_length=1, max_length=500)
    url: str = Field(min_length=1, max_length=2_000)
    summary: str | None = Field(default=None, max_length=4_000)
    section: str | None = Field(default=None, max_length=200)
    published_at: str | None = Field(default=None, max_length=100)


class ExternalResearchCandidateRanking(BaseModel):
    """A grounded significance ranking for one provider-returned document."""

    candidate_index: int = Field(ge=0, le=49)
    significance: Literal["major", "minor", "none"]
    rationale: str = Field(min_length=1, max_length=500)


class ExternalResearchCheck(BaseModel):
    """A completed or unavailable external request, retained to prevent re-running it."""

    hypothesis_id: str
    agent_id: str
    subject: str
    start_date: str
    end_date: str
    comparison_start_date: str | None = None
    status: Literal["completed", "unavailable"]


class ExternalMeasurement(BaseModel):
    """A provider-neutral before/after measurement from external research."""

    name: str
    unit: str
    aggregation: Literal["mean", "sum"]
    baseline_period: str
    comparison_period: str
    baseline_value: float
    comparison_value: float
    percentage_change: float | None = None


class HumanFeedback(BaseModel):
    question: str
    response: str
    hypothesis_id: str | None = None
    received_at: datetime = Field(default_factory=now_utc)


class InvestigationStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_HUMAN = "waiting_for_human"
    COMPLETED = "completed"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    STOPPED = "stopped"
    FAILED = "failed"


class InvestigationLimits(BaseModel):
    max_iterations: int = Field(default_factory=lambda: settings.investigation_max_iterations, ge=1)
    max_sql_queries: int = Field(default_factory=lambda: settings.investigation_max_sql_queries, ge=1)
    max_external_calls: int = Field(default_factory=lambda: settings.investigation_max_external_calls, ge=0)
    max_duration_seconds: int = Field(default_factory=lambda: settings.investigation_max_duration_seconds, ge=10)


class InvestigationCreate(BaseModel):
    question: str = Field(min_length=10, max_length=2000)
    datasource_ids: list[str] = Field(min_length=1)


class InvestigationEvent(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    investigation_id: str
    type: str
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now_utc)


class FinalAnalysis(BaseModel):
    likely_root_cause: str | None
    conclusion_level: Literal["underlying_cause", "proximate_driver", "undetermined"] = "undetermined"
    confidence: float
    evidence: list[Evidence]
    rejected_hypotheses: list[Hypothesis]
    external_findings: list[ExternalFinding]
    caveats: list[str]
    summary: str
    follow_up_question: str | None = None


class ConversationTurn(BaseModel):
    question: str
    answer: FinalAnalysis
    created_at: datetime = Field(default_factory=now_utc)


class ResolvedEntityReference(BaseModel):
    """A backend-only entity identity retained for safe, efficient follow-up retrieval."""

    datasource_id: str
    table: str
    identifier_field: str
    identifier: str
    display_name: str


class QueryScope(BaseModel):
    """Reusable, non-entity constraints from a successful direct query."""

    datasource_id: str
    table: str
    predicate_sql: str


class QueryResultCacheEntry(BaseModel):
    """A persisted, schema-versioned result of an already-executed query."""

    key: str
    datasource_id: str
    schema_fingerprint: str | None = None
    rows: list[dict[str, Any]] = Field(default_factory=list)
    display_rows: list[dict[str, Any]] = Field(default_factory=list)
    deterministic_summary: Any = None
    resolved_entities: list[ResolvedEntityReference] = Field(default_factory=list)


class CrossDatasourceLookupCacheEntry(BaseModel):
    """Persisted values from one approved related-source lookup.

    The entry belongs to one investigation state.  Its key includes the
    approved relationship, the lookup SQL, and the source schema fingerprint,
    so a value set is never reused after the related schema changes or in a
    different conversation.
    """

    key: str
    relation_id: str
    relation_signature: str
    datasource_id: str
    schema_fingerprint: str | None = None
    lookup_sql: str
    values: list[str] = Field(default_factory=list)
    source_row_count: int = Field(default=0, ge=0)


class InvestigationState(BaseModel):
    investigation_id: str
    # The graph checkpoint to resume. Follow-up turns use a distinct thread so they do not
    # replay the completed original investigation.
    checkpoint_thread_id: str | None = None
    question: str
    datasources: list[DatasourceSummary]
    original_question: str | None = None
    current_conversation_question: str | None = None
    limits: InvestigationLimits = Field(default_factory=InvestigationLimits)
    metric_definition: MetricDefinition | None = None
    analysis_scope: InvestigationScope | None = None
    # A causal question may assert a factual starting point (for example a
    # business result or change).  It is model-extracted in business language,
    # then independently checked before any causal hypothesis is executed.
    premise_to_validate: str | None = None
    premise_status: Literal["not_needed", "pending", "confirmed", "rejected", "inconclusive"] = "not_needed"
    scope_coverage: list[ScopeCoverage] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    hypothesis_name_index: dict[str, str] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)
    investigation_history: list[InvestigationStep] = Field(default_factory=list)
    # Recoverable failures are part of the investigation record. They explain
    # gaps in the conclusion without turning one unavailable check into a
    # failed conversation.
    failed_checks: list[str] = Field(default_factory=list)
    external_findings: list[ExternalFinding] = Field(default_factory=list)
    external_research_checks: list[ExternalResearchCheck] = Field(default_factory=list)
    current_focus: str | None = None
    next_action: str | None = None
    # Older archives contain one request; new investigations can dispatch several
    # independent configured agents concurrently.
    external_request: dict[str, str] | list[dict[str, str]] | None = None
    pending_step: InvestigationStep | None = None
    request_type: Literal["investigation", "direct_answer"] = "investigation"
    pending_human_question: str | None = None
    human_resume_node: Literal[
        "generate_hypotheses", "validate_premise", "answer_directly", "select_investigation"
    ] | None = None
    human_feedback: list[HumanFeedback] = Field(default_factory=list)
    # Time spent waiting for the user is not investigation runtime and must not consume its budget.
    paused_at: datetime | None = None
    paused_duration_seconds: float = Field(default=0, ge=0)
    conversation_turns: list[ConversationTurn] = Field(default_factory=list)
    # This is persisted with the conversation but deliberately excluded from every HTTP response.
    resolved_entities: list[ResolvedEntityReference] = Field(default_factory=list)
    query_scopes: list[QueryScope] = Field(default_factory=list)
    # This is per investigation, survives resume, and is never shared with a
    # different user's question or source schema.
    query_result_cache: list[QueryResultCacheEntry] = Field(default_factory=list)
    # Related-source keys are likewise private to this investigation. They are
    # retained so multiple hypotheses can reuse the same approved lookup.
    cross_datasource_lookup_cache: list[CrossDatasourceLookupCacheEntry] = Field(default_factory=list)
    iteration: int = 0
    query_count: int = 0
    external_call_count: int = 0
    confidence: float | None = None
    status: InvestigationStatus = InvestigationStatus.QUEUED
    started_at: datetime = Field(default_factory=now_utc)
    final_analysis: FinalAnalysis | None = None
    error: str | None = None
