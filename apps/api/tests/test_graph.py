from datetime import UTC, datetime, timedelta

import pytest
from business_signals.config import settings
from business_signals.datasources.registry import DatasourceRegistry
from business_signals.datasources.safety import UnsafeQueryError
from business_signals.investigation import InvestigationEngine
from business_signals.llm import (
    CrossDatasourceLookup,
    DirectAnswerPlan,
    DirectAnswerQuery,
    EvidenceAssessment,
    ExternalAgentAssignments,
    ExternalResearchPlan,
    ExternalResearchPlans,
    HypothesisPlan,
    InvestigationDecision,
    InvestigationPlan,
    PremiseValidationPlan,
    QueryPlan,
    QuestionUnderstanding,
    Synthesis,
)
from business_signals.models import (
    ColumnMetadata,
    CrossDatasourceRelation,
    DatasourceMetadata,
    DatasourceSummary,
    DatasourceType,
    Evidence,
    EvidenceDataPoint,
    ExternalFinding,
    FinalAnalysis,
    HumanFeedback,
    Hypothesis,
    HypothesisStatus,
    InvestigationState,
    InvestigationStatus,
    Observation,
    QueryScope,
    ResolvedEntityReference,
    ScopeCoverage,
    TableMetadata,
)
from langgraph.types import Command
from sqlalchemy.exc import ProgrammingError


def test_graph_has_real_investigation_cycles() -> None:
    graph = InvestigationEngine(DatasourceRegistry()).graph.get_graph()
    edges = {(edge.source, edge.target) for edge in graph.edges}
    assert ("decide", "select_investigation") in edges
    assert ("select_investigation", "synthesize") in edges
    assert ("synthesize", "__end__") in edges
    assert ("understand", "request_human") in edges
    assert ("select_external_research", "research_external") in edges


def test_investigation_budget_excludes_time_waiting_for_a_clarification() -> None:
    engine = InvestigationEngine(DatasourceRegistry())
    now = datetime.now(UTC)
    state = InvestigationState(
        investigation_id="inv_paused_budget",
        question="Why did sales decline in 2024?",
        datasources=[],
        started_at=now - timedelta(minutes=5),
        paused_at=now - timedelta(minutes=4),
        limits={"max_duration_seconds": 180},
    )

    assert engine._budget_exhausted(state) is False


class UnexpectedLLM:
    async def structured(self, *args, **kwargs):
        raise AssertionError("The planner must not run when no hypothesis remains")


@pytest.mark.asyncio
async def test_exhausted_hypotheses_synthesize_instead_of_crashing() -> None:
    engine = InvestigationEngine(DatasourceRegistry(), llm=UnexpectedLLM())
    state = InvestigationState(
        investigation_id="inv_all_exhausted",
        question="Why did sales decline?",
        datasources=[],
        hypotheses=[
            {
                "id": "hyp_inventory",
                "name": "Inventory availability",
                "description": "Inventory could have constrained sales.",
                "category": "operations",
                "confidence": 0.4,
                "status": "needs_more_evidence",
            },
            {
                "id": "hyp_demand",
                "name": "Customer demand",
                "description": "Demand could have weakened.",
                "category": "demand",
                "confidence": 0.3,
                "status": "rejected",
            },
        ],
    )

    selected = await engine._select_investigation(state)
    routed_state = state.model_copy(update=selected)

    assert selected["pending_step"] is None
    assert selected["next_action"] == "finish"
    assert engine._route_after_selection(routed_state) == "synthesize"


def test_ignore_investigation_limits_disables_budget_enforcement(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = InvestigationEngine(DatasourceRegistry())
    monkeypatch.setattr(settings, "ignore_investigation_limits", True)
    state = InvestigationState(
        investigation_id="inv_ignore_limits",
        question="Why did sales decline in 2024?",
        datasources=[],
        started_at=datetime.now(UTC) - timedelta(hours=1),
        iteration=99,
        query_count=99,
    )

    assert engine._budget_exhausted(state) is False


def test_single_related_record_adds_a_scope_caveat_to_the_final_analysis() -> None:
    engine = InvestigationEngine(DatasourceRegistry())
    state = InvestigationState(
        investigation_id="inv_single_scope_match",
        question="Why did sales fall in Milan?",
        datasources=[],
        scope_coverage=[ScopeCoverage(description="Find the requested stores.", matched_records=1)],
    )
    analysis = FinalAnalysis(
        likely_root_cause=None,
        confidence=0.4,
        evidence=[],
        rejected_hypotheses=[],
        external_findings=[],
        caveats=[],
        summary="The available data is inconclusive.",
    )

    scoped = engine._with_scope_caveats(state, analysis)

    assert scoped.caveats == [
        "At least one requested filter matched only one related record, so findings apply to that matched part of the requested scope rather than automatically to the whole area or group."
    ]


class ExternalResearchFixture:
    def available_agents(self) -> list[dict[str, object]]:
        return [{"id": "weather", "name": "Historical weather", "purpose": "Check conditions", "subjects": ["weather"], "evidence_topics": ["weather.conditions"], "subject_label": "location"}]

    async def research(self, agent_id: str, subject: str, start_date: str, end_date: str, context: str) -> ExternalFinding:
        assert (agent_id, subject, start_date, end_date) == ("weather", "Milan", "2024-07-01", "2024-07-31")
        assert context
        return ExternalFinding(
            type="weather",
            period="2024-07-01 to 2024-07-31",
            observation="Rainfall was unusually high during the period.",
            relationship="correlated",
            confidence=0.78,
            source_url="https://weather.example.test/",
            source_title="Historical weather",
        )


class NoResultExternalResearchFixture(ExternalResearchFixture):
    async def research(self, agent_id: str, subject: str, start_date: str, end_date: str, context: str) -> ExternalFinding:
        return ExternalFinding(
            type="weather",
            period="2024-07-01 to 2024-07-31",
            observation="No matching major weather disruption was found during the period.",
            relationship="contradicting",
            confidence=0.78,
            source_url="https://weather.example.test/",
            source_title="Historical weather",
        )


class ExternalResearchPlanLLM:
    async def structured(self, response_model, instruction: str, payload: dict):
        assert response_model is ExternalResearchPlans
        assert payload["available_research_agents"][0]["id"] == "weather"
        return ExternalResearchPlans(plans=[ExternalResearchPlan(
            hypothesis_name="Weather disruption", agent_id="weather", subject="Milan",
            start_date="2024-07-01", end_date="2024-07-31",
            rationale="Check whether weather could have affected visits to outdoor-product stores.",
        )])


class ContradictingExternalEvidenceLLM(ExternalResearchPlanLLM):
    async def structured(self, response_model, instruction: str, payload: dict):
        if response_model is EvidenceAssessment:
            return EvidenceAssessment.model_validate(
                {
                    "evidence": [
                        {
                            "id": "ev_weather_absent",
                            "description": "No matching major weather disruption was found during the period.",
                            "source": "historical weather",
                            "relationship": "contradicting",
                            "confidence": 0.78,
                            "evidence_topics": ["weather.conditions"],
                            "hypothesis_ids": ["hyp_weather"],
                        }
                    ],
                    "hypotheses": [
                        {
                            "id": "hyp_weather",
                            "name": "Weather disruption",
                            "description": "Weather reduced visits to stores selling outdoor products.",
                            "category": "external",
                            "confidence": 0.08,
                            "status": "rejected",
                        }
                    ],
                    "confidence": 0.78,
                }
            )
        return await super().structured(response_model, instruction, payload)


class PrematureFinishDecisionLLM:
    async def structured(self, response_model, instruction: str, payload: dict):
        assert response_model is InvestigationDecision
        assert payload["external_research_available"] is True
        return InvestigationDecision(action="finish", reason="The first query appears conclusive.")


class FinishWithUntestedCauseLLM:
    async def structured(self, response_model, instruction: str, payload: dict):
        assert response_model is InvestigationDecision
        return InvestigationDecision(action="finish", reason="The immediate mechanism is measured.")


@pytest.mark.asyncio
async def test_selected_external_agent_runs_as_evidence_and_never_as_a_direct_cause() -> None:
    emitted = []

    async def sink(event):
        emitted.append(event)

    state = InvestigationState(
        investigation_id="inv_external",
        question="Why did outdoor-product sales in Milan fall in July 2024?",
        datasources=[],
        hypotheses=[
            {
                "id": "hyp_weather",
                    "name": "Weather disruption",
                    "description": "Weather reduced visits to stores selling outdoor products.",
                    "category": "external",
                    "research_scope": "external",
                    "evidence_topics": ["weather.conditions"],
                    "confidence": 0.4,
            }
        ],
        hypothesis_name_index={"weather disruption": "hyp_weather"},
        query_count=1,
    )
    engine = InvestigationEngine(
        DatasourceRegistry(),
        llm=ExternalResearchPlanLLM(),
        event_sink=sink,
        researcher=ExternalResearchFixture(),
    )

    selected = await engine._select_external_research(state)
    researched = await engine._research_external(state.model_copy(update=selected))

    assert researched["external_call_count"] == 1
    assert researched["external_findings"][0].relationship == "correlated"
    assert researched["observations"][0].source == "external:weather"
    assert [event.type for event in emitted] == [
        "ExternalResearchSelected",
        "ExternalResearchStarted",
        "ExternalResearchCompleted",
    ]


@pytest.mark.asyncio
async def test_empty_external_result_is_retained_and_can_reject_its_hypothesis() -> None:
    state = InvestigationState(
        investigation_id="inv_external_empty",
        question="Why did outdoor-product sales in Milan fall in July 2024?",
        datasources=[],
        hypotheses=[
            {
                "id": "hyp_weather",
                    "name": "Weather disruption",
                    "description": "Weather reduced visits to stores selling outdoor products.",
                    "category": "external",
                    "research_scope": "external",
                    "evidence_topics": ["weather.conditions"],
                    "confidence": 0.4,
            }
        ],
        hypothesis_name_index={"weather disruption": "hyp_weather"},
        query_count=1,
    )
    engine = InvestigationEngine(
        DatasourceRegistry(),
        llm=ContradictingExternalEvidenceLLM(),
        researcher=NoResultExternalResearchFixture(),
    )

    selected = await engine._select_external_research(state)
    researched = await engine._research_external(state.model_copy(update=selected))
    interpreted = await engine._interpret(state.model_copy(update={**selected, **researched}))

    assert researched["external_findings"][0].observation.startswith("No matching")
    assert researched["next_action"] is None
    assert interpreted["hypotheses"][0].status == "rejected"
    assert interpreted["evidence"][0].relationship == "contradicting"


@pytest.mark.asyncio
async def test_decision_does_not_finish_before_an_untested_external_cause_is_discriminated() -> None:
    state = InvestigationState(
        investigation_id="inv_external_before_finish",
        question="Why did outdoor-product sales in Milan fall in July 2024 compared with June?",
        datasources=[],
        hypotheses=[
            {
                "id": "hyp_weather",
                "name": "Weather disruption",
                "description": "Weather may have reduced store visits.",
                "category": "external",
                "research_scope": "external",
                "evidence_topics": ["weather.conditions"],
                "confidence": 0.35,
            }
        ],
        query_count=1,
        evidence=[
            {
                "id": "ev_change",
                "description": "Recorded revenue was lower in July.",
                "source": "sales records",
                "relationship": "supporting",
                "confidence": 0.8,
                "hypothesis_ids": ["hyp_weather"],
            }
        ],
    )
    engine = InvestigationEngine(
        DatasourceRegistry(),
        llm=PrematureFinishDecisionLLM(),
        researcher=ExternalResearchFixture(),
    )

    result = await engine._decide(state)

    assert result["next_action"] == "external"


@pytest.mark.asyncio
async def test_decision_runs_an_eligible_external_check_before_accepting_an_internal_root_cause() -> None:
    state = InvestigationState(
        investigation_id="inv_external_before_internal_conclusion",
        question="Why did outdoor-product sales in Milan fall in July 2024 compared with June?",
        datasources=[],
        premise_status="confirmed",
        query_count=1,
        hypotheses=[
            {
                "id": "hyp_visits",
                "name": "Lower store visits",
                "description": "Fewer shoppers visited stores.",
                "category": "traffic",
                "confidence": 0.9,
                "status": "supported",
            },
            {
                "id": "hyp_weather",
                "name": "Weather disruption",
                "description": "Weather may have reduced store visits.",
                "category": "external",
                "research_scope": "external",
                "evidence_topics": ["weather.conditions"],
                "confidence": 0.4,
            },
        ],
        evidence=[
            {
                "id": "ev_visits",
                "description": "Store visits fell during the comparison period.",
                "source": "traffic records",
                "relationship": "direct",
                "basis": "direct",
                "confidence": 0.9,
                "hypothesis_ids": ["hyp_visits"],
            }
        ],
    )
    engine = InvestigationEngine(
        DatasourceRegistry(),
        llm=UnexpectedLLM(),
        researcher=ExternalResearchFixture(),
    )

    result = await engine._decide(state)

    assert result["next_action"] == "external"


@pytest.mark.asyncio
async def test_decision_does_not_finish_at_a_measured_driver_when_an_internal_cause_is_untested() -> None:
    state = InvestigationState(
        investigation_id="inv_proximate_not_final",
        question="Why did the measure fall?",
        datasources=[],
        premise_status="confirmed",
        query_count=2,
        hypotheses=[
            {
                "id": "h_driver",
                "name": "Fewer transactions",
                "description": "Fewer transactions directly reduced the measure.",
                "category": "mechanism",
                "confidence": 0.9,
                "status": "supported",
            },
            {
                "id": "h_cause",
                "name": "Reduced availability",
                "description": "Availability may have reduced transactions.",
                "category": "operations",
                "confidence": 0.4,
                "status": "active",
            },
        ],
        evidence=[
            {
                "id": "ev_driver",
                "description": "Transactions fell.",
                "source": "records",
                "relationship": "direct",
                "basis": "direct",
                "scope": "mechanism",
                "confidence": 0.9,
                "hypothesis_id": "h_driver",
                "claim_id": "h_driver",
                "hypothesis_ids": ["h_driver"],
            },
        ],
    )
    engine = InvestigationEngine(DatasourceRegistry(), llm=FinishWithUntestedCauseLLM())

    result = await engine._decide(state)

    assert result["next_action"] == "continue"


@pytest.mark.asyncio
async def test_relevant_external_research_runs_after_the_internal_runtime_budget_is_spent() -> None:
    state = InvestigationState(
        investigation_id="inv_external_after_budget",
        question="Why did outdoor-product sales in Milan fall in July 2024 compared with June?",
        datasources=[],
        started_at=datetime.now(UTC) - timedelta(minutes=5),
        query_count=1,
        hypotheses=[{
            "id": "hyp_weather",
            "name": "Weather disruption",
            "description": "Weather may have reduced store visits.",
            "category": "external",
            "research_scope": "external",
            "evidence_topics": ["weather.conditions"],
            "confidence": 0.35,
        }],
    )
    engine = InvestigationEngine(
        DatasourceRegistry(),
        llm=PrematureFinishDecisionLLM(),
        researcher=ExternalResearchFixture(),
    )

    result = await engine._decide(state)

    assert result["next_action"] == "external"


def test_hypothesis_name_resolves_to_the_graph_hypothesis_id() -> None:
    state = InvestigationState.model_validate(
        {
            "investigation_id": "inv_labels",
            "question": "Why did revenue fall?",
            "datasources": [],
            "hypotheses": [
                {"id": "hyp_inventory", "name": "Inventory availability", "description": "Inventory fell", "category": "inventory", "confidence": 0.5},
                {"id": "hyp_demand", "name": "Reduced demand", "description": "Demand fell", "category": "demand", "confidence": 0.4},
            ],
            "hypothesis_name_index": {"inventory availability": "hyp_inventory", "reduced demand": "hyp_demand"},
        }
    )

    engine = InvestigationEngine(DatasourceRegistry())
    assert engine._resolve_hypothesis_name("reduced demand", state) == "hyp_demand"
    assert engine._resolve_hypothesis_name("Inventory availability", state) == "hyp_inventory"


def test_direct_answer_guardrail_removes_internal_identifier_columns() -> None:
    rows = InvestigationEngine._redact_internal_identifier_columns(
        [{"item_code": "P003", "store_id": "S17", "external_key": "x", "sku": "hub-3", "display_name": "Charging Hub", "units": 732}],
        {"item_code", "store_id", "external_key", "sku"},
    )

    assert rows == [{"display_name": "Charging Hub", "units": 732}]
    assert InvestigationEngine._safe_direct_answer_text("Return the three product IDs with the highest sales.") == (
        "Return the three items with the highest sales."
    )
    assert InvestigationEngine._customer_facing_name("Charging Hub 003", "item-003") == "Charging Hub"
    assert InvestigationEngine._customer_facing_name("Formula 1", "item-001") == "Formula 1"


def test_generated_text_filters_are_case_insensitive_without_changing_joins_or_numbers() -> None:
    sql = InvestigationEngine._case_insensitive_text_filters(
        "SELECT * FROM public.conversion_metrics metric "
        "JOIN public.stores store ON metric.store_id = store.id "
        "WHERE metric.platform = 'iOS' AND metric.completed_orders = 0"
    )

    assert "metric.platform ILIKE 'iOS'" in sql
    assert "metric.store_id = store.id" in sql
    assert "metric.completed_orders = 0" in sql


def test_direct_answer_combination_supports_composite_group_keys() -> None:
    fields = InvestigationEngine._combination_fields("store_id, channel")

    assert fields == ["store_id", "channel"]
    assert InvestigationEngine._combination_row_key(
        {"store_id": "S01", "channel": "online"}, fields
    ) == ("S01", "online")


def test_direct_answer_guardrail_removes_redundant_catalog_labels_and_retained_keys() -> None:
    entity = ResolvedEntityReference(
        datasource_id="catalog", table="public.products", identifier_field="id",
        identifier="P119", display_name="Toiletry Bag",
    )
    rows = InvestigationEngine._remove_redundant_entity_labels(
        [{"name": "Toiletry Bag 119", "display_name": "Toiletry Bag", "units_sold": 88}], [entity]
    )
    analysis = InvestigationEngine._redact_resolved_entity_references(
        FinalAnalysis(
            likely_root_cause="Sales for Toiletry Bag 119 (Toiletry Bag)",
            confidence=0.9,
            evidence=[Evidence(
                description="P119 sold 88 units.", source="sales records", relationship="direct", confidence=0.9,
                data=[EvidenceDataPoint(field="Product", value="Toiletry Bag 119")],
            )],
            rejected_hypotheses=[], external_findings=[], caveats=["P119 is the retained key."],
            summary="Toiletry Bag 119 (Toiletry Bag) sold 88 units.",
        ),
        [entity],
    )

    assert rows == [{"display_name": "Toiletry Bag", "units_sold": 88}]
    assert analysis.likely_root_cause == "Sales for Toiletry Bag"
    assert analysis.summary == "Toiletry Bag sold 88 units."
    assert analysis.evidence[0].data[0].value == "Toiletry Bag"
    assert analysis.caveats == ["Toiletry Bag is the retained key."]


class GenericLookupDatasource:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.queries: list[str] = []

    async def execute_read_query(self, query: str) -> list[dict[str, object]]:
        self.queries.append(query)
        return self.rows


class GenericLookupRegistry:
    def __init__(self) -> None:
        self.sources = {
            "catalog": GenericLookupDatasource([{"item_code": "IT-007", "catalog_caption": "Trail Backpack 007"}]),
        }

    def get(self, datasource_id: str) -> GenericLookupDatasource:
        return self.sources[datasource_id]

    def approved_cross_relations(self, datasource_ids: list[str]) -> list[CrossDatasourceRelation]:
        assert datasource_ids == ["analytics", "catalog"]
        return [
            CrossDatasourceRelation(
                source_datasource="analytics",
                source_field="public.sales.item_code",
                target_datasource="catalog",
                target_field="public.items.item_code",
                confidence=1,
                origin="human",
                confirmed=True,
            )
        ]


@pytest.mark.asyncio
async def test_schema_driven_lookup_resolves_a_non_product_identifier_column() -> None:
    registry = GenericLookupRegistry()
    engine = InvestigationEngine(registry)  # type: ignore[arg-type]
    metadata = [
        {"id": "analytics", "tables": [{"name": "public.sales", "columns": [{"name": "item_code", "pk": False}], "foreign_keys": []}]},
        {
            "id": "catalog",
            "tables": [
                {
                    "name": "public.categories",
                    "columns": [{"name": "item_code", "pk": True}, {"name": "name", "pk": False}],
                    "foreign_keys": [],
                },
                {
                    "name": "public.items",
                    "columns": [{"name": "item_code", "pk": True}, {"name": "catalog_caption", "type": "text", "pk": False, "nullable": False}],
                    "foreign_keys": [],
                }
            ],
        },
    ]
    rows = [{"item_code": "IT-007", "total_units_sold": 717}]

    resolved, _, query_count, _ = await engine._resolve_display_names(
        InvestigationState(investigation_id="inv_generic_lookup", question="Top items", datasources=[]),
        metadata,
        "analytics",
        "SELECT item_code, SUM(units) AS total_units_sold FROM public.sales GROUP BY item_code",
        rows,
    )

    assert resolved == [{"total_units_sold": 717, "display_name": "Trail Backpack"}]
    assert query_count == 1
    assert "item_code" in registry.sources["catalog"].queries[0]
    assert "catalog_caption" in registry.sources["catalog"].queries[0]
    assert "FROM public.items" in registry.sources["catalog"].queries[0]


@pytest.mark.asyncio
async def test_failed_display_name_lookup_redacts_schema_declared_identifiers() -> None:
    registry = GenericLookupRegistry()
    registry.sources["catalog"] = GenericLookupDatasource([])

    async def reject_lookup(_query: str) -> list[dict[str, object]]:
        raise UnsafeQueryError("lookup source is unavailable")

    registry.sources["catalog"].execute_read_query = reject_lookup  # type: ignore[method-assign]
    engine = InvestigationEngine(registry)  # type: ignore[arg-type]
    metadata = [
        {"id": "analytics", "tables": [{"name": "public.sales", "columns": [{"name": "item_code", "pk": False}], "foreign_keys": []}]},
        {"id": "catalog", "tables": [{"name": "public.items", "columns": [{"name": "item_code", "pk": True}, {"name": "display_name", "pk": False}], "foreign_keys": []}]},
    ]

    resolved, step, query_count, _ = await engine._resolve_display_names(
        InvestigationState(investigation_id="inv_lookup_failure", question="Top items", datasources=[]),
        metadata,
        "analytics",
        "SELECT item_code, SUM(units) AS total_units_sold FROM public.sales GROUP BY item_code",
        [{"item_code": "IT-007", "total_units_sold": 717}],
    )

    assert resolved == [{"total_units_sold": 717}]
    assert step == []
    assert query_count == 0


class FakeDatasource:
    summary = DatasourceSummary(id="analytics", name="Sales analytics", type=DatasourceType.TIMESCALEDB, connected=True)

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def execute_read_query(self, query: str) -> list[dict[str, object]]:
        self.queries.append(query)
        return [{"region": "north", "revenue": 80, "available_units": 4}, {"region": "north", "revenue": 40, "available_units": 2}]


class FakeRegistry:
    def __init__(self) -> None:
        self.source = FakeDatasource()
        self.metadata_value = DatasourceMetadata(
            datasource_id="analytics",
            type=DatasourceType.TIMESCALEDB,
            fingerprint="fixture",
            structural_metadata=[
                TableMetadata(
                    schema_name="public",
                    name="sales_events",
                    columns=[
                        ColumnMetadata(name="revenue", data_type="numeric", nullable=False),
                        ColumnMetadata(name="occurred_at", data_type="timestamp", nullable=False),
                    ],
                )
            ],
        )

    async def metadata(self, datasource_id: str) -> DatasourceMetadata:
        assert datasource_id == "analytics"
        return self.metadata_value

    def get(self, datasource_id: str) -> FakeDatasource:
        assert datasource_id == "analytics"
        return self.source

    def approved_cross_relations(self, datasource_ids: list[str]) -> list[CrossDatasourceRelation]:
        return []


def test_schema_discovered_categorical_values_reject_an_invented_filter() -> None:
    source = {
        "tables": [
            {
                "name": "public.activity",
                "columns": [
                    {"name": "fulfilment_path", "representative_values": ["counter", "website"]},
                ],
            },
        ],
    }

    with pytest.raises(UnsafeQueryError, match="known values are .*counter.*website"):
        InvestigationEngine._validate_categorical_filter_values(
            "SELECT * FROM public.activity WHERE LOWER(fulfilment_path) IN ('in-person', 'physical')",
            source,
        )

    InvestigationEngine._validate_categorical_filter_values(
        "SELECT * FROM public.activity WHERE fulfilment_path ILIKE '%counter%'",
        source,
    )


class CachedSchemaRegistry(FakeRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.metadata_calls = 0

    def cached_metadata(self, datasource_id: str) -> DatasourceMetadata:
        assert datasource_id == "analytics"
        return self.metadata_value

    async def metadata(self, datasource_id: str) -> DatasourceMetadata:
        self.metadata_calls += 1
        raise AssertionError("A persisted schema must not be rediscovered during an investigation")


@pytest.mark.asyncio
async def test_workflow_reuses_persisted_schema_payload_without_rediscovery() -> None:
    registry = CachedSchemaRegistry()
    engine = InvestigationEngine(registry)  # type: ignore[arg-type]
    state = InvestigationState(
        investigation_id="inv_schema_cache",
        question="Why did revenue change?",
        datasources=[registry.source.summary],
    )

    first = await engine._metadata_payload(state)
    second = await engine._metadata_payload(state)

    assert first == second
    assert registry.metadata_calls == 0


class VocabularyDatasource:
    summary = DatasourceSummary(id="source", name="Source", type=DatasourceType.POSTGRESQL, connected=True)

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def execute_read_query(self, query: str) -> list[dict[str, object]]:
        self.queries.append(query)
        return [{"value": "Outdoor"}]


class CategoricalVocabularyDatasource(VocabularyDatasource):
    async def execute_read_query(self, query: str) -> list[dict[str, object]]:
        self.queries.append(query)
        if "ranked_values" in query:
            return [{"column_name": "descriptor", "value": "Outdoor"}]
        return [{"value": "Outdoor"}]


class VocabularyRegistry:
    def __init__(self) -> None:
        self.source = VocabularyDatasource()
        self.metadata_value = DatasourceMetadata(
            datasource_id="source",
            type=DatasourceType.POSTGRESQL,
            fingerprint="vocabulary",
            structural_metadata=[
                TableMetadata(
                    schema_name="public",
                    name="lookup_a",
                    columns=[ColumnMetadata(name="descriptor", data_type="text", nullable=False)],
                )
            ],
        )

    async def metadata(self, datasource_id: str) -> DatasourceMetadata:
        assert datasource_id == "source"
        return self.metadata_value

    def get(self, datasource_id: str) -> VocabularyDatasource:
        assert datasource_id == "source"
        return self.source

    def approved_cross_relations(self, datasource_ids: list[str]) -> list[CrossDatasourceRelation]:
        return []


class CategoricalVocabularyRegistry(VocabularyRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.source = CategoricalVocabularyDatasource()


class VocabularyClarificationLLM:
    async def structured(self, response_model, _instruction: str, _payload: dict):
        assert response_model is QuestionUnderstanding
        return QuestionUnderstanding.model_validate({
            "metric_definition": {"name": "Units sold", "expression": "SUM(units)", "unit": "units", "confidence": 0.9},
            "scope": {"filters": ["outdoor-product"]},
            "ambiguity": "When you say 'outdoor-product', do you mean a stored group or a custom list?",
        })


@pytest.mark.asyncio
async def test_question_understanding_resolves_a_quoted_term_from_any_text_dimension() -> None:
    registry = VocabularyRegistry()
    engine = InvestigationEngine(registry, llm=VocabularyClarificationLLM())  # type: ignore[arg-type]
    state = InvestigationState(
        investigation_id="inv_vocabulary",
        question="Why did outdoor-product sales fall?",
        datasources=[registry.source.summary],
    )

    update = await engine._understand(state)

    assert update["pending_human_question"] is None
    assert update["analysis_scope"].filters[-1] == "The term “outdoor-product” is represented by “Outdoor” in the connected data."
    assert registry.source.queries == [
        'SELECT DISTINCT "descriptor" AS value FROM "public"."lookup_a" WHERE "descriptor" IS NOT NULL LIMIT 51'
    ]


@pytest.mark.asyncio
async def test_categorical_vocabulary_is_loaded_once_per_schema_version() -> None:
    registry = CategoricalVocabularyRegistry()
    engine = InvestigationEngine(registry)  # type: ignore[arg-type]
    state = InvestigationState(
        investigation_id="inv_categorical_cache",
        question="Why did the measure change?",
        datasources=[registry.source.summary],
    )

    first = await engine._metadata_payload(state, include_categorical_values_for={"source"})
    second = await engine._metadata_payload(state, include_categorical_values_for={"source"})

    assert first[0]["tables"][0]["columns"][0]["representative_values"] == ["Outdoor"]
    assert second == first
    assert len(registry.source.queries) == 1
    assert "ranked_values" in registry.source.queries[0]


class MeasureFirstClarificationLLM:
    async def structured(self, response_model, _instruction: str, _payload: dict):
        assert response_model is QuestionUnderstanding
        return QuestionUnderstanding.model_validate({
            "metric_definition": {
                "name": "Sales",
                "expression": "The requested business measure",
                "unit": None,
                "confidence": 0.45,
            },
            "scope": {"filters": ["outdoor-product"]},
            "ambiguity": "When you say sales, do you mean the total amount customers spent, the number of items sold, or the number of orders?",
            "clarification_kind": "measure",
        })


@pytest.mark.asyncio
async def test_measure_clarification_is_not_discarded_when_a_catalog_term_is_inferable() -> None:
    registry = VocabularyRegistry()
    engine = InvestigationEngine(registry, llm=MeasureFirstClarificationLLM())  # type: ignore[arg-type]
    state = InvestigationState(
        investigation_id="inv_measure_before_scope",
        question="Why did outdoor-product sales fall?",
        datasources=[registry.source.summary],
    )

    update = await engine._understand(state)

    assert update["pending_human_question"] == (
        "When you say sales, do you mean the total amount customers spent, the number of items sold, or the number of orders?"
    )
    assert update["status"] == InvestigationStatus.WAITING_FOR_HUMAN
    assert registry.source.queries == []


class LowConfidenceMeasureWithoutQuestionLLM:
    async def structured(self, response_model, _instruction: str, _payload: dict):
        assert response_model is QuestionUnderstanding
        return QuestionUnderstanding.model_validate({
            "metric_definition": {
                "name": "Physical-store revenue",
                "expression": "Total amount paid in physical stores",
                "unit": "local currency",
                "confidence": 0.7,
            },
            "scope": {"filters": ["Dubai"]},
            "premise_to_validate": "Physical-store revenue declined in the later period.",
        })


@pytest.mark.asyncio
async def test_low_confidence_measure_without_model_question_requests_clarification_before_validation() -> None:
    registry = VocabularyRegistry()
    engine = InvestigationEngine(registry, llm=LowConfidenceMeasureWithoutQuestionLLM())  # type: ignore[arg-type]
    state = InvestigationState(
        investigation_id="inv_low_confidence_measure",
        question="Why did physical-store revenue in Dubai fall?",
        datasources=[registry.source.summary],
    )

    update = await engine._understand(state)

    assert update["pending_human_question"] == (
        "How should I define \u201cPhysical-store revenue\u201d for this analysis? "
        "Please say what it should include or exclude."
    )
    assert update["human_resume_node"] == "validate_premise"
    assert update["status"] == InvestigationStatus.WAITING_FOR_HUMAN


class SetComparisonDatasource:
    def __init__(self, summary: DatasourceSummary, rows: list[dict[str, object]]) -> None:
        self.summary, self.rows, self.queries = summary, rows, []

    async def execute_read_query(self, query: str) -> list[dict[str, object]]:
        self.queries.append(query)
        return self.rows


class SetComparisonRegistry:
    def __init__(self) -> None:
        self.catalog = SetComparisonDatasource(
            DatasourceSummary(id="catalog", name="Catalog", type=DatasourceType.POSTGRESQL, connected=True),
            [{"id": "item-1"}, {"id": "item-2"}],
        )
        self.analytics = SetComparisonDatasource(
            DatasourceSummary(id="analytics", name="Analytics", type=DatasourceType.POSTGRESQL, connected=True),
            [{"item_id": "item-1"}, {"item_id": "item-2"}],
        )
        self.metadata_values = {
            "catalog": DatasourceMetadata(
                datasource_id="catalog", type=DatasourceType.POSTGRESQL, fingerprint="catalog",
                structural_metadata=[TableMetadata(schema_name="public", name="items", columns=[ColumnMetadata(name="id", data_type="text", nullable=False, primary_key=True), ColumnMetadata(name="display_name", data_type="text", nullable=False)])],
            ),
            "analytics": DatasourceMetadata(
                datasource_id="analytics", type=DatasourceType.POSTGRESQL, fingerprint="analytics",
                structural_metadata=[TableMetadata(schema_name="public", name="sales", columns=[ColumnMetadata(name="item_id", data_type="text", nullable=False)])],
            ),
        }

    async def metadata(self, datasource_id: str) -> DatasourceMetadata:
        return self.metadata_values[datasource_id]

    def get(self, datasource_id: str) -> SetComparisonDatasource:
        return {"catalog": self.catalog, "analytics": self.analytics}[datasource_id]

    def approved_cross_relations(self, datasource_ids: list[str]) -> list[CrossDatasourceRelation]:
        return []


class RelationFilterDatasource:
    def __init__(self, summary: DatasourceSummary, kind: str) -> None:
        self.summary, self.kind, self.queries = summary, kind, []

    async def execute_read_query(self, query: str) -> list[dict[str, object]]:
        self.queries.append(query)
        if "AS relationship_key" in query:
            return [{"relationship_key": "P162"}]
        if self.kind == "catalog":
            if "SELECT id, name" in query:
                return [{"id": "P162", "name": "Lantern"}]
            if " AS product_id" in query:
                return [{"product_id": "P162"}]
            if "SELECT id" in query:
                return [{"id": "P162"}]
            return [{"product_id": "P162"}]
        return [{"item_id": "P162", "units_sold": 97}] if "'P162'" in query else []


class RelationFilterRegistry:
    def __init__(self) -> None:
        self.catalog = RelationFilterDatasource(
            DatasourceSummary(id="catalog", name="Catalog", type=DatasourceType.POSTGRESQL, connected=True), "catalog"
        )
        self.analytics = RelationFilterDatasource(
            DatasourceSummary(id="analytics", name="Analytics", type=DatasourceType.POSTGRESQL, connected=True), "analytics"
        )
        self.metadata_values = {
            "catalog": DatasourceMetadata(
                datasource_id="catalog", type=DatasourceType.POSTGRESQL, fingerprint="catalog",
                structural_metadata=[TableMetadata(schema_name="public", name="products", columns=[ColumnMetadata(name="id", data_type="text", nullable=False, primary_key=True), ColumnMetadata(name="name", data_type="text", nullable=False)])],
            ),
            "analytics": DatasourceMetadata(
                datasource_id="analytics", type=DatasourceType.POSTGRESQL, fingerprint="analytics",
                structural_metadata=[TableMetadata(schema_name="public", name="sales_events", columns=[ColumnMetadata(name="item_id", data_type="text", nullable=False), ColumnMetadata(name="units", data_type="integer", nullable=False)])],
            ),
        }

    async def metadata(self, datasource_id: str) -> DatasourceMetadata:
        return self.metadata_values[datasource_id]

    def get(self, datasource_id: str) -> RelationFilterDatasource:
        return {"catalog": self.catalog, "analytics": self.analytics}[datasource_id]

    def approved_cross_relations(self, datasource_ids: list[str]) -> list[CrossDatasourceRelation]:
        return [CrossDatasourceRelation(
            id="rel_item", source_datasource="analytics", source_field="public.sales_events.item_id",
            target_datasource="catalog", target_field="public.products.id", confidence=1,
            origin="human", confirmed=True,
        )]


class FixtureLLM:
    async def structured(self, response_model, instruction: str, payload: dict):
        responses = {
            QuestionUnderstanding: {"metric_definition": {"name": "revenue", "expression": "SUM(revenue)", "unit": "EUR", "confidence": 0.95}, "observations": ["Northern revenue declined"], "premise_to_validate": "Revenue was lower in the later period than in the earlier period."},
            HypothesisPlan: {"hypotheses": [
                {"id": "hyp_stock", "name": "Inventory constraint", "description": "Stock availability constrained sales", "category": "inventory", "confidence": 0.45},
                {"id": "hyp_demand", "name": "Demand decline", "description": "Demand declined", "category": "demand", "confidence": 0.35},
            ]},
            InvestigationPlan: {"step": {"iteration": 0, "hypothesis_name": "Inventory constraint", "action": "compare revenue and stock", "datasource_id": "analytics", "rationale": "Tests supply before demand", "expected_information_gain": 0.9}},
            PremiseValidationPlan: {"datasource_id": "analytics", "action": "Compare revenue before and after.", "rationale": "Verify the reported revenue change before explaining it."},
            QueryPlan: {"sql": "select region, revenue, available_units from sales_events", "purpose": "Measure revenue beside stock availability"},
            EvidenceAssessment: {"evidence": [{"id": "ev_stock", "description": "Low available units coincide with lower revenue", "source": "analytics", "relationship": "direct", "confidence": 0.86, "hypothesis_ids": ["hyp_stock"]}], "hypotheses": [
                {"id": "hyp_stock", "name": "Inventory constraint", "description": "Stock availability constrained sales", "category": "inventory", "confidence": 0.86, "status": "supported"},
                {"id": "hyp_demand", "name": "Demand decline", "description": "Demand declined", "category": "demand", "confidence": 0.12, "status": "rejected"},
            ], "confidence": 0.86, "premise_verdict": "confirmed"},
            InvestigationDecision: {"action": "finish", "reason": "The fixture scenario has sufficient direct evidence."},
            Synthesis: {"analysis": {"likely_root_cause": "Stock availability constrained sales", "confidence": 0.86, "evidence": [{"id": "ev_stock", "description": "Low available units coincide with lower revenue", "source": "analytics", "relationship": "direct", "confidence": 0.86}], "rejected_hypotheses": [], "external_findings": [], "caveats": [], "summary": "Inventory is the leading explanation."}},
        }
        return response_model.model_validate(responses[response_model])


class InternalOnlyHistoricalHypothesisLLM:
    async def structured(self, response_model, _instruction: str, payload: dict):
        assert response_model is HypothesisPlan
        assert payload["available_research_agents"] == [
            {
                "id": "weather",
                "name": "Historical weather",
                "purpose": "Check historical conditions.",
                "subjects": ["weather", "rain"],
                "evidence_topics": ["weather.conditions"],
                "subject_label": "location",
            }
        ]
        return HypothesisPlan.model_validate({"hypotheses": [
            {
                "id": "hyp_inventory",
                "name": "Inventory limitation",
                "description": "The store did not have enough items available.",
                "category": "supply",
                "research_scope": "internal",
                "confidence": 0.4,
            }
        ]})


class PrematurelyExhaustedHypothesisLLM:
    async def structured(self, response_model, _instruction: str, _payload: dict):
        assert response_model is HypothesisPlan
        return HypothesisPlan.model_validate({"hypotheses": [
            {
                "id": "hyp_inventory",
                "name": "Inventory limitation",
                "description": "The store may not have had enough items available.",
                "category": "supply",
                "confidence": 0.4,
                "status": "needs_more_evidence",
            }
        ]})


class HistoricalWeatherResearcher:
    def available_agents(self) -> list[dict[str, object]]:
        return [{
            "id": "weather",
            "name": "Historical weather",
            "purpose": "Check historical conditions.",
            "subjects": ["weather", "rain"],
            "evidence_topics": ["weather.conditions"],
            "subject_label": "location",
        }]


@pytest.mark.asyncio
async def test_historical_decline_does_not_attach_an_arbitrary_configured_agent() -> None:
    engine = InvestigationEngine(
        DatasourceRegistry(),
        llm=InternalOnlyHistoricalHypothesisLLM(),
        researcher=HistoricalWeatherResearcher(),  # type: ignore[arg-type]
    )
    state = InvestigationState(
        investigation_id="inv_external_hypothesis",
        question="Why did outdoor-product sales in Milan fall in July 2024 compared with June?",
        datasources=[],
    )

    update = await engine._generate_hypotheses(state)

    assert [hypothesis.name for hypothesis in update["hypotheses"]] == ["Inventory limitation"]
    assert all(not hypothesis.external_agent_ids for hypothesis in update["hypotheses"])


@pytest.mark.asyncio
async def test_new_hypotheses_are_active_even_if_the_planner_prejudges_them() -> None:
    engine = InvestigationEngine(DatasourceRegistry(), llm=PrematurelyExhaustedHypothesisLLM())

    update = await engine._generate_hypotheses(
        InvestigationState(investigation_id="inv_new_active", question="Why did sales fall?", datasources=[])
    )

    assert update["hypotheses"][0].status == HypothesisStatus.ACTIVE


class AmbiguousFixtureLLM(FixtureLLM):
    async def structured(self, response_model, instruction: str, payload: dict):
        if response_model is QuestionUnderstanding:
            return QuestionUnderstanding.model_validate(
                {
                    "metric_definition": {
                        "name": "revenue",
                        "expression": "SUM(revenue)",
                        "unit": "EUR",
                        "confidence": 0.95,
                    },
                    "observations": ["The question does not specify the comparison region."],
                    "ambiguity": "Which region should the investigation use for the comparison?",
                    "premise_to_validate": "Revenue was lower in the later period than in the earlier period.",
                }
            )
        return await super().structured(response_model, instruction, payload)


class TrackPremiseValidationLLM(AmbiguousFixtureLLM):
    """Records whether the baseline validator runs across a human pause."""

    def __init__(self) -> None:
        self.premise_validation_calls = 0

    async def structured(self, response_model, instruction: str, payload: dict):
        if response_model is PremiseValidationPlan:
            self.premise_validation_calls += 1
        return await super().structured(response_model, instruction, payload)


class TrackUnambiguousPremiseValidationLLM(FixtureLLM):
    """Records the normal path, which must not wait for unnecessary input."""

    def __init__(self) -> None:
        self.calls: list[type] = []

    async def structured(self, response_model, instruction: str, payload: dict):
        if response_model in (QuestionUnderstanding, PremiseValidationPlan):
            self.calls.append(response_model)
        return await super().structured(response_model, instruction, payload)


class RejectedPremiseLLM(FixtureLLM):
    """A semantic premise can be rejected without relying on wording heuristics."""

    def __init__(self) -> None:
        self.investigation_plan_calls = 0
        self.hypothesis_plan_calls = 0

    async def structured(self, response_model, instruction: str, payload: dict):
        if response_model is QuestionUnderstanding:
            return QuestionUnderstanding.model_validate({
                "metric_definition": {"name": "Units sold", "expression": "Total items sold", "unit": "units", "confidence": 0.95},
                "premise_to_validate": "Outdoor-product unit sales in Milan were higher in July 2024 than in June 2024.",
            })
        if response_model is EvidenceAssessment:
            return EvidenceAssessment.model_validate({
                "evidence": [{
                    "id": "baseline_rejected",
                    "description": "The baseline comparison shows fewer units sold in July than in June.",
                    "source": "analytics",
                    "relationship": "direct",
                    "confidence": 0.95,
                    "scope": "premise",
                }],
                "hypotheses": [],
                "confidence": 0.95,
                "premise_verdict": "rejected",
            })
        if response_model is HypothesisPlan:
            self.hypothesis_plan_calls += 1
            raise AssertionError("Hypotheses must not be generated before the premise is validated")
        if response_model is InvestigationPlan:
            self.investigation_plan_calls += 1
            raise AssertionError("Causal checks must not run after the premise is rejected")
        return await super().structured(response_model, instruction, payload)


class ConfirmedPremiseOrderingLLM(FixtureLLM):
    """Captures the required baseline-before-hypotheses execution order."""

    def __init__(self) -> None:
        self.calls: list[type] = []

    async def structured(self, response_model, instruction: str, payload: dict):
        if response_model in {
            QuestionUnderstanding,
            PremiseValidationPlan,
            QueryPlan,
            EvidenceAssessment,
            HypothesisPlan,
        }:
            self.calls.append(response_model)
        return await super().structured(response_model, instruction, payload)


class LateAmbiguousFixtureLLM(FixtureLLM):
    def __init__(self) -> None:
        self.evidence_calls = 0

    async def structured(self, response_model, instruction: str, payload: dict):
        result = await super().structured(response_model, instruction, payload)
        if response_model is EvidenceAssessment:
            self.evidence_calls += 1
            if self.evidence_calls == 1:
                return result.model_copy(
                    update={"ambiguity": "Which historical dates should be compared?"}
                )
        return result


class DirectAnswerFixtureLLM:
    async def structured(self, response_model, instruction: str, payload: dict):
        responses = {
            QuestionUnderstanding: {
                "metric_definition": {
                    "name": "Most popular items",
                    "expression": "Units sold by item in 2024",
                    "unit": "units",
                    "confidence": 0.95,
                },
                "request_type": "direct_answer",
            },
            DirectAnswerPlan: {
                "datasource_id": "analytics",
                "sql": "SELECT region, revenue, available_units FROM sales_events",
                "purpose": "Find the most popular items in 2024.",
            },
            Synthesis: {
                "analysis": {
                    "likely_root_cause": "Most popular items in 2024",
                    "confidence": 0.9,
                    "evidence": [],
                    "rejected_hypotheses": [],
                    "external_findings": [],
                    "caveats": [],
                    "summary": "The requested ranking is ready.",
                }
            },
        }
        return response_model.model_validate(responses[response_model])


class RepairingDirectAnswerLLM(DirectAnswerFixtureLLM):
    def __init__(self) -> None:
        self.direct_plan_calls = 0

    async def structured(self, response_model, instruction: str, payload: dict):
        if response_model is DirectAnswerPlan:
            self.direct_plan_calls += 1
            return DirectAnswerPlan(
                datasource_id="analytics",
                sql="SELECT COUNT(*) FROM public.products p JOIN public.sales_events s ON p.id = s.product_id",
                purpose="Count catalog products with recorded sales.",
            )
        if response_model is DirectAnswerQuery:
            self.direct_plan_calls += 1
            return DirectAnswerQuery(
                datasource_id="analytics",
                sql="SELECT COUNT(DISTINCT product_id) AS products_with_sales FROM public.sales_events",
                purpose="Count the products with recorded sales.",
            )
        return await super().structured(response_model, instruction, payload)


class InvalidDatasourceDirectAnswerLLM(DirectAnswerFixtureLLM):
    def __init__(self) -> None:
        self.direct_plan_calls = 0

    async def structured(self, response_model, instruction: str, payload: dict):
        if response_model is DirectAnswerPlan:
            self.direct_plan_calls += 1
            if self.direct_plan_calls == 1:
                return DirectAnswerPlan(
                    datasource_id="not-a-datasource",
                    sql="SELECT * FROM public.products",
                    purpose="List products.",
                )
        if response_model is DirectAnswerQuery:
            self.direct_plan_calls += 1
            return DirectAnswerQuery(
                datasource_id="analytics",
                sql="SELECT region, revenue, available_units FROM sales_events",
                purpose="Find the most popular items in 2024.",
            )
        return await super().structured(response_model, instruction, payload)


class SetDifferenceDirectAnswerLLM(DirectAnswerFixtureLLM):
    async def structured(self, response_model, instruction: str, payload: dict):
        if response_model is DirectAnswerPlan:
            return DirectAnswerPlan.model_validate({
                "datasource_id": "catalog",
                "sql": "SELECT id FROM public.items",
                "purpose": "List all catalog items.",
                "supporting_queries": [{"datasource_id": "analytics", "sql": "SELECT DISTINCT item_id FROM public.sales", "purpose": "List items with recorded sales."}],
                "combination": {"operation": "set_difference", "primary_key": "id", "supporting_query_index": 0, "supporting_key": "item_id"},
            })
        return await super().structured(response_model, instruction, payload)


class RelationFilterDirectAnswerLLM(DirectAnswerFixtureLLM):
    async def structured(self, response_model, instruction: str, payload: dict):
        if response_model is DirectAnswerPlan:
            return DirectAnswerPlan.model_validate({
                "datasource_id": "analytics",
                "sql": "SELECT item_id, SUM(units) AS units_sold FROM public.sales_events GROUP BY item_id ORDER BY units_sold DESC LIMIT 500",
                "purpose": "Show daily sales for the selected item.",
                "supporting_queries": [{"datasource_id": "catalog", "sql": "SELECT id AS product_id FROM public.products WHERE name = 'Lantern'", "purpose": "Find Lantern."}],
                "combination": {"operation": "intersection", "primary_key": "item_id", "supporting_query_index": 0, "supporting_key": "product_id"},
            })
        return await super().structured(response_model, instruction, payload)


class CrossDatasourceInvestigationLLM:
    async def structured(self, response_model, instruction: str, payload: dict):
        assert response_model is QueryPlan
        assert payload["selected_datasource"]["id"] == "analytics"
        return QueryPlan.model_validate({
            "sql": "SELECT item_id, SUM(units) AS units_sold FROM public.sales_events GROUP BY item_id",
            "purpose": "Measure sales for the selected catalog items.",
            "contract": {
                "claim_id": "hypothesis",
                "role": "causal",
                "basis": "direct",
                "relationship_ids": ["rel_item"],
                "rationale": "The approved relationship supplies the selected catalog items.",
            },
            "cross_datasource_lookups": [{
                "datasource_id": "catalog",
                "relation_id": "rel_item",
                "sql": "SELECT id AS relationship_key FROM public.products WHERE name = 'Lantern'",
                "purpose": "Find the selected catalog item.",
            }],
        })


def test_cross_datasource_lookup_aliases_are_rejected_in_primary_sql() -> None:
    sql = (
        "SELECT * FROM public.sales_events "
        "WHERE product_id IN (SELECT id FROM lookup_outdoor_products) "
        "AND store_id IN (SELECT id FROM lookup_milan_stores)"
    )
    with pytest.raises(UnsafeQueryError, match="outside the selected datasource"):
        from business_signals.datasources.safety import validate_query_tables

        validate_query_tables(sql, {"public.sales_events"})


def test_declared_cross_datasource_lookups_replace_only_neutral_planner_placeholders() -> None:
    from business_signals.datasources.safety import validate_query_tables

    engine = InvestigationEngine(DatasourceRegistry())
    rewritten = engine._remove_planner_lookup_placeholders(
        (
            "SELECT * FROM public.sales_events "
            "WHERE product_id IN (SELECT relationship_key FROM product_ids) "
            "AND store_id IN (SELECT relationship_key FROM milan_stores)"
        ),
        [
            CrossDatasourceLookup(
                datasource_id="catalog",
                relation_id="rel_product",
                sql="SELECT id AS relationship_key FROM public.products",
                purpose="Find selected products.",
            ),
            CrossDatasourceLookup(
                datasource_id="catalog",
                relation_id="rel_store",
                sql="SELECT id AS relationship_key FROM public.stores",
                purpose="Find selected stores.",
            ),
        ],
        {"public.sales_events"},
    )

    assert "product_ids" not in rewritten
    assert "milan_stores" not in rewritten
    assert validate_query_tables(rewritten, {"public.sales_events"}) == rewritten

    bare_placeholder = engine._remove_planner_lookup_placeholders(
        "SELECT * FROM public.sales_events WHERE store_id IN (SELECT relationship_key)",
        [
            CrossDatasourceLookup(
                datasource_id="catalog",
                relation_id="rel_store",
                sql="SELECT id AS relationship_key FROM public.stores",
                purpose="Find selected stores.",
            )
        ],
        {"public.sales_events"},
    )

    assert "relationship_key" not in bare_placeholder
    assert validate_query_tables(bare_placeholder, {"public.sales_events"}) == bare_placeholder


@pytest.mark.asyncio
async def test_cross_datasource_lookup_rejects_mixed_keys_before_querying_the_related_source() -> None:
    registry = RelationFilterRegistry()
    engine = InvestigationEngine(registry)  # type: ignore[arg-type]
    state = InvestigationState(
        investigation_id="inv_mixed_lookup",
        question="Why did sales fall?",
        datasources=[registry.analytics.summary, registry.catalog.summary],
    )
    metadata = [
        {
            "id": source_id,
            "tables": [
                {"name": f"public.{table_name}", "columns": [], "foreign_keys": []}
                for table_name in (
                    ["sales_events"] if source_id == "analytics" else ["products", "stores"]
                )
            ],
        }
        for source_id in ("analytics", "catalog")
    ]
    with pytest.raises(UnsafeQueryError, match="do not combine different keys with UNION"):
        await engine._apply_cross_datasource_lookups(
            state,
            "analytics",
            "SELECT item_id FROM public.sales_events",
            [
                CrossDatasourceLookup(
                    datasource_id="catalog",
                    relation_id="rel_item",
                    sql=(
                        "SELECT product.id, NULL::text AS store_id FROM public.products AS product "
                        "UNION ALL SELECT NULL::text, store.id FROM public.stores AS store"
                    ),
                    purpose="Find products and stores.",
                )
            ],
            metadata,
        )

    assert registry.catalog.queries == []


class AgentAssignmentReviewLLM:
    async def structured(self, response_model, _instruction: str, _payload: dict):
        if response_model is HypothesisPlan:
            return HypothesisPlan.model_validate({"hypotheses": [{
                "id": "hyp_external",
                "name": "An external explanation",
                "description": "An external factor could have changed the outcome.",
                "category": "generic",
                "research_scope": "external",
                "external_agent_ids": ["unrelated-agent"],
                "claim": {
                    "cause": "An external factor",
                    "mechanism": "could change the outcome",
                    "outcome": "the reported change",
                    "required_evidence": ["A matching external observation."],
                },
                "confidence": 0.3,
            }]})
        assert response_model is ExternalAgentAssignments
        return ExternalAgentAssignments.model_validate({"assignments": [{
            "hypothesis_name": "An external explanation",
            "agent_ids": [],
            "valid": False,
            "rationale": "The configured agent cannot test this claim with the available input.",
        }]})


class UnrelatedAgentResearcher:
    def available_agents(self) -> list[dict[str, object]]:
        return [{
            "id": "unrelated-agent",
            "name": "Unrelated research",
            "purpose": "Check an unrelated public signal.",
            "subjects": ["unrelated"],
            "subject_label": "subject",
            "subject_pattern": ".+",
        }]


@pytest.mark.asyncio
async def test_independent_agent_review_rejects_an_irrelevant_assignment() -> None:
    engine = InvestigationEngine(
        DatasourceRegistry(),
        llm=AgentAssignmentReviewLLM(),
        researcher=UnrelatedAgentResearcher(),  # type: ignore[arg-type]
    )

    update = await engine._generate_hypotheses(
        InvestigationState(investigation_id="inv_agent_review", question="Why did the measure change?", datasources=[])
    )

    hypothesis = update["hypotheses"][0]
    assert hypothesis.research_scope == "internal"
    assert hypothesis.external_agent_ids == []


class IncorrectIntersectionDirectAnswerLLM(DirectAnswerFixtureLLM):
    async def structured(self, response_model, instruction: str, payload: dict):
        if response_model is DirectAnswerPlan:
            return DirectAnswerPlan.model_validate({
                "datasource_id": "analytics",
                "sql": "SELECT item_id, SUM(units) AS units_sold FROM public.sales_events GROUP BY item_id ORDER BY units_sold DESC LIMIT 500",
                "purpose": "Show sales for the retained item.",
                "supporting_queries": [{"datasource_id": "catalog", "sql": "SELECT id AS unrelated_store_key FROM public.products", "purpose": "Unrelated supporting lookup."}],
                "combination": {"operation": "intersection", "primary_key": "item_id", "supporting_query_index": 0, "supporting_key": "unrelated_store_key"},
            })
        return await super().structured(response_model, instruction, payload)


class AmbiguousAllHistoryDirectAnswerLLM(DirectAnswerFixtureLLM):
    async def structured(self, response_model, instruction: str, payload: dict):
        if response_model is QuestionUnderstanding:
            return QuestionUnderstanding.model_validate({
                "request_type": "direct_answer",
                "ambiguity": "Do you mean all time or a particular period?",
            })
        return await super().structured(response_model, instruction, payload)


class DatasourceDeferringLLM(SetDifferenceDirectAnswerLLM):
    def __init__(self) -> None:
        self.plan_calls = 0

    async def structured(self, response_model, instruction: str, payload: dict):
        if response_model is DirectAnswerPlan:
            self.plan_calls += 1
            if self.plan_calls == 1:
                return DirectAnswerPlan(
                    datasource_id="catalog",
                    sql="SELECT 'Which datasource should I use?' AS clarification",
                    purpose="Ask which datasource to use.",
                )
        return await super().structured(response_model, instruction, payload)


@pytest.mark.asyncio
async def test_internal_investigation_runs_from_plan_to_evidence() -> None:
    registry = FakeRegistry()
    engine = InvestigationEngine(registry, llm=FixtureLLM())
    result = await engine.graph.ainvoke(
        InvestigationState(
            investigation_id="inv_fixture",
            question="Why did northern revenue fall?",
            datasources=[registry.source.summary],
        ),
        config={"configurable": {"thread_id": "inv_fixture"}},
    )
    state = InvestigationState.model_validate(result)

    assert state.status.value == "completed"
    assert state.final_analysis and state.final_analysis.likely_root_cause == "Stock availability constrained sales"
    assert state.hypotheses[0].status.value == "supported"
    assert state.observations[-1].value["deterministic_summary"]["numeric_columns"]["revenue"]["sum"] == 120.0
    assert registry.source.queries == ["SELECT region, revenue, available_units FROM sales_events LIMIT 500"]


@pytest.mark.asyncio
async def test_ambiguous_question_interrupts_then_resumes_from_checkpoint() -> None:
    registry = FakeRegistry()
    engine = InvestigationEngine(registry, llm=AmbiguousFixtureLLM())
    initial = InvestigationState(
        investigation_id="inv_ambiguous",
        question="Why did revenue fall?",
        datasources=[registry.source.summary],
    )
    config = {"configurable": {"thread_id": initial.investigation_id}}

    paused = InvestigationState.model_validate(await engine.graph.ainvoke(initial, config=config))
    assert paused.status.value == "waiting_for_human"
    assert paused.pending_human_question == "Which region should the investigation use for the comparison?"
    assert paused.human_feedback == []

    completed = InvestigationState.model_validate(
        await engine.graph.ainvoke(Command(resume="Use northern Italy."), config=config)
    )
    assert completed.status.value == "completed"
    assert completed.pending_human_question is None
    assert completed.human_feedback[0].response == "Use northern Italy."


@pytest.mark.asyncio
async def test_clarification_is_collected_before_decline_premise_validation() -> None:
    registry = FakeRegistry()
    llm = TrackPremiseValidationLLM()
    engine = InvestigationEngine(registry, llm=llm)
    initial = InvestigationState(
        investigation_id="inv_clarification_before_premise",
        question="Why did revenue fall?",
        datasources=[registry.source.summary],
    )
    config = {"configurable": {"thread_id": initial.investigation_id}}

    paused = InvestigationState.model_validate(await engine.graph.ainvoke(initial, config=config))

    assert paused.status is InvestigationStatus.WAITING_FOR_HUMAN
    assert llm.premise_validation_calls == 0
    assert registry.source.queries == []

    completed = InvestigationState.model_validate(
        await engine.graph.ainvoke(Command(resume="Use northern Italy."), config=config)
    )

    assert completed.status is InvestigationStatus.COMPLETED
    assert llm.premise_validation_calls == 1
    assert any(evidence.scope == "premise" for evidence in completed.evidence)
    assert all(evidence.hypothesis_id is None for evidence in completed.evidence if evidence.scope == "premise")


@pytest.mark.asyncio
async def test_unambiguous_decline_skips_clarification_and_validates_immediately() -> None:
    registry = FakeRegistry()
    llm = TrackUnambiguousPremiseValidationLLM()
    engine = InvestigationEngine(registry, llm=llm)

    result = InvestigationState.model_validate(
        await engine.graph.ainvoke(
            InvestigationState(
                investigation_id="inv_unambiguous_premise",
                question="Why did northern revenue fall?",
                datasources=[registry.source.summary],
            ),
            config={"configurable": {"thread_id": "inv_unambiguous_premise"}},
        )
    )

    assert result.status is InvestigationStatus.COMPLETED
    assert result.pending_human_question is None
    assert result.human_feedback == []
    assert llm.calls == [QuestionUnderstanding, PremiseValidationPlan]


@pytest.mark.asyncio
async def test_rejected_semantic_premise_finishes_before_any_causal_query() -> None:
    registry = FakeRegistry()
    llm = RejectedPremiseLLM()
    engine = InvestigationEngine(registry, llm=llm)

    result = InvestigationState.model_validate(
        await engine.graph.ainvoke(
            InvestigationState(
                investigation_id="inv_rejected_rise_premise",
                question="Why did outdoor-product sales in Milan rise in July 2024 compared with June?",
                datasources=[registry.source.summary],
            ),
            config={"configurable": {"thread_id": "inv_rejected_rise_premise"}},
        )
    )

    assert result.status is InvestigationStatus.COMPLETED
    assert result.premise_status == "rejected"
    assert result.final_analysis and result.final_analysis.likely_root_cause == "Reported starting point was not found"
    assert llm.hypothesis_plan_calls == 0
    assert llm.investigation_plan_calls == 0


@pytest.mark.asyncio
async def test_confirmed_premise_generates_hypotheses_only_after_baseline_assessment() -> None:
    registry = FakeRegistry()
    llm = ConfirmedPremiseOrderingLLM()
    engine = InvestigationEngine(registry, llm=llm)

    result = InvestigationState.model_validate(
        await engine.graph.ainvoke(
            InvestigationState(
                investigation_id="inv_confirmed_premise",
                question="Why did northern revenue fall?",
                datasources=[registry.source.summary],
            ),
            config={"configurable": {"thread_id": "inv_confirmed_premise"}},
        )
    )

    baseline_assessment = llm.calls.index(EvidenceAssessment)
    hypothesis_generation = llm.calls.index(HypothesisPlan)
    assert result.premise_status == "confirmed"
    assert llm.calls[:baseline_assessment + 1] == [
        QuestionUnderstanding,
        PremiseValidationPlan,
        QueryPlan,
        EvidenceAssessment,
    ]
    assert hypothesis_generation > baseline_assessment


@pytest.mark.asyncio
async def test_direct_question_uses_one_query_without_hypotheses() -> None:
    registry = FakeRegistry()
    events = []

    async def capture(event):
        events.append(event)

    engine = InvestigationEngine(registry, llm=DirectAnswerFixtureLLM(), event_sink=capture)
    result = InvestigationState.model_validate(
        await engine.graph.ainvoke(
            InvestigationState(
                investigation_id="inv_direct",
                question="What are the most popular items sold in 2024?",
                datasources=[registry.source.summary],
            ),
            config={"configurable": {"thread_id": "inv_direct"}},
        )
    )

    assert result.status.value == "completed"
    assert result.request_type == "direct_answer"
    assert result.hypotheses == []
    assert result.query_count == 1
    assert result.final_analysis and result.final_analysis.summary == "The requested ranking is ready."
    assert result.conversation_turns[0].question == "What are the most popular items sold in 2024?"
    assert result.conversation_turns[0].answer.summary == "The requested ranking is ready."
    completed = next(event for event in events if event.type == "InvestigationCompleted")
    assert completed.data["conversation_turns"][0]["question"] == "What are the most popular items sold in 2024?"


@pytest.mark.asyncio
async def test_follow_up_is_classified_from_its_actual_question_not_the_context_wrapper() -> None:
    engine = InvestigationEngine(DatasourceRegistry(), llm=FixtureLLM())
    state = InvestigationState(
        investigation_id="inv_follow_up_routing",
        question=(
            "Follow-up request: Why did sales fall in July?\n\n"
            "Previous conclusion: Sales were lower in July."
        ),
        current_conversation_question="Why did sales fall in July?",
        datasources=[],
        request_type="investigation",
    )

    update = await engine._understand(state)

    assert update["request_type"] == "investigation"


@pytest.mark.asyncio
async def test_direct_answer_repairs_a_cross_datasource_query_once() -> None:
    registry = FakeRegistry()
    events = []

    async def capture(event):
        events.append(event)

    llm = RepairingDirectAnswerLLM()
    engine = InvestigationEngine(registry, llm=llm, event_sink=capture)
    result = InvestigationState.model_validate(
        await engine.graph.ainvoke(
            InvestigationState(
                investigation_id="inv_direct_repair",
                question="How many products have recorded sales?",
                datasources=[registry.source.summary],
            ),
            config={"configurable": {"thread_id": "inv_direct_repair"}},
        )
    )

    assert result.status.value == "completed"
    assert llm.direct_plan_calls == 2
    assert any(event.type == "QueryRejected" for event in events)
    assert registry.source.queries == ["SELECT COUNT(DISTINCT product_id) AS products_with_sales FROM public.sales_events LIMIT 500"]


@pytest.mark.asyncio
async def test_direct_answer_repairs_an_invalid_datasource_once() -> None:
    registry = FakeRegistry()
    llm = InvalidDatasourceDirectAnswerLLM()
    engine = InvestigationEngine(registry, llm=llm)

    result = InvestigationState.model_validate(
        await engine.graph.ainvoke(
            InvestigationState(
                investigation_id="inv_direct_source_repair",
                question="What are the most popular items sold in 2024?",
                datasources=[registry.source.summary],
            ),
            config={"configurable": {"thread_id": "inv_direct_source_repair"}},
        )
    )

    assert result.status.value == "completed"
    assert llm.direct_plan_calls == 2
    assert registry.source.queries == ["SELECT region, revenue, available_units FROM sales_events LIMIT 500"]


@pytest.mark.asyncio
async def test_direct_answer_combines_isolated_datasource_results_with_a_set_operation() -> None:
    registry = SetComparisonRegistry()
    engine = InvestigationEngine(registry, llm=SetDifferenceDirectAnswerLLM())  # type: ignore[arg-type]

    result = InvestigationState.model_validate(
        await engine.graph.ainvoke(
            InvestigationState(
                investigation_id="inv_set_difference",
                question="Which catalog items have never been sold?",
                datasources=[registry.catalog.summary, registry.analytics.summary],
            ),
            config={"configurable": {"thread_id": "inv_set_difference"}},
        )
    )

    assert result.status.value == "completed"
    assert result.query_count == 2
    assert result.observations[-1].value["rows"] == []
    assert registry.catalog.queries == ["SELECT id FROM public.items LIMIT 500"]
    assert registry.analytics.queries == ["SELECT DISTINCT item_id FROM public.sales LIMIT 500"]


@pytest.mark.asyncio
async def test_direct_answer_applies_an_approved_cross_source_filter_before_limit() -> None:
    registry = RelationFilterRegistry()
    engine = InvestigationEngine(registry, llm=RelationFilterDirectAnswerLLM())  # type: ignore[arg-type]

    update = await engine._answer_directly(
        InvestigationState(
            investigation_id="inv_relation_filter",
            question="Show Lantern's daily sales in May.",
            datasources=[registry.analytics.summary, registry.catalog.summary],
        )
    )

    assert update["observations"][-1].value["rows"] == [{"units_sold": 97, "display_name": "Lantern"}]
    assert [(entity.display_name, entity.identifier) for entity in update["resolved_entities"]] == [("Lantern", "P162")]
    assert "IN ('P162')" in registry.analytics.queries[0]
    assert registry.analytics.queries[0].index("IN ('P162')") < registry.analytics.queries[0].index("GROUP BY")


def test_direct_answer_binds_a_stored_entity_reference_through_an_approved_relationship() -> None:
    registry = RelationFilterRegistry()
    engine = InvestigationEngine(registry)  # type: ignore[arg-type]
    state = InvestigationState(
        investigation_id="inv_entity_binding",
        question="Show Lantern sales by day.",
        datasources=[registry.analytics.summary, registry.catalog.summary],
        resolved_entities=[ResolvedEntityReference(
            datasource_id="catalog", table="public.products", identifier_field="id",
            identifier="P162", display_name="Lantern",
        )],
    )

    sql = engine._bind_trusted_entity_references(
        state,
        DirectAnswerQuery(
            datasource_id="analytics",
            sql="SELECT item_id, SUM(units) FROM public.sales_events WHERE item_id = '{{item_id}}' GROUP BY item_id",
            purpose="Show Lantern sales.",
        ),
    )

    assert "item_id = 'P162'" in sql
    assert "{{item_id}}" not in sql


def test_direct_answer_binds_a_stored_entity_name_through_an_approved_relationship() -> None:
    registry = RelationFilterRegistry()
    engine = InvestigationEngine(registry)  # type: ignore[arg-type]
    state = InvestigationState(
        investigation_id="inv_entity_name_binding",
        question="Show Lantern sales by store.",
        datasources=[registry.analytics.summary, registry.catalog.summary],
        resolved_entities=[ResolvedEntityReference(
            datasource_id="catalog", table="public.products", identifier_field="id",
            identifier="P162", display_name="Lantern",
        )],
    )

    sql = engine._bind_trusted_entity_references(
        state,
        DirectAnswerQuery(
            datasource_id="analytics",
            sql="SELECT item_id, SUM(units) FROM public.sales_events WHERE item_id = 'Lantern' GROUP BY item_id",
            purpose="Show Lantern sales by store.",
        ),
    )

    assert "item_id = 'P162'" in sql
    assert "Lantern" not in sql


def test_direct_answer_constrains_an_explicit_entity_follow_up_before_grouping() -> None:
    registry = RelationFilterRegistry()
    engine = InvestigationEngine(registry)  # type: ignore[arg-type]
    state = InvestigationState(
        investigation_id="inv_entity_follow_up",
        question="Follow-up request: Split by stores and channel.",
        current_conversation_question="Split by stores and channel.",
        datasources=[registry.analytics.summary, registry.catalog.summary],
        resolved_entities=[ResolvedEntityReference(
            datasource_id="catalog", table="public.products", identifier_field="id",
            identifier="P162", display_name="Lantern",
        )],
    )

    query = engine._constrain_query_to_referenced_entity(
        state,
        DirectAnswerQuery(
            datasource_id="analytics",
            sql="SELECT item_id, SUM(units) FROM public.sales_events GROUP BY item_id ORDER BY SUM(units) DESC LIMIT 500",
            purpose="Show sales by store.",
        ),
    )

    assert "item_id IN ('P162')" in query.sql
    assert query.sql.index("item_id IN ('P162')") < query.sql.index("GROUP BY")


def test_approved_relation_filter_is_added_inside_a_cte_source_read() -> None:
    engine = InvestigationEngine(RelationFilterRegistry())  # type: ignore[arg-type]

    sql = engine._add_relation_filter(
        "WITH monthly AS (SELECT item_id, SUM(units) AS units_sold FROM public.sales_events GROUP BY item_id) "
        "SELECT * FROM monthly",
        "public.sales_events.item_id",
        ["P162"],
    )

    assert "sales_events.item_id IN ('P162')" in sql
    assert sql.index("sales_events.item_id IN ('P162')") < sql.index("GROUP BY item_id")


def test_direct_answer_replaces_a_display_label_used_in_a_retained_entity_key_predicate() -> None:
    registry = RelationFilterRegistry()
    engine = InvestigationEngine(registry)  # type: ignore[arg-type]
    state = InvestigationState(
        investigation_id="inv_retained_label",
        question="Follow-up request: Show this item by store.",
        current_conversation_question="Show this item by store.",
        datasources=[registry.analytics.summary, registry.catalog.summary],
        resolved_entities=[ResolvedEntityReference(
            datasource_id="catalog", table="public.products", identifier_field="id",
            identifier="P162", display_name="Lantern",
        )],
    )

    query = engine._constrain_query_to_referenced_entity(
        state,
        DirectAnswerQuery(
            datasource_id="analytics",
            sql=("SELECT store_id, SUM(units) FROM public.sales_events "
                 "WHERE item_id = 'Lantern 162' AND occurred_at >= '2024-07-01' "
                 "GROUP BY store_id"),
            purpose="Show sales by store.",
        ),
    )

    assert "Lantern 162" not in query.sql
    assert "item_id IN ('P162')" in query.sql
    assert "occurred_at >= '2024-07-01'" in query.sql


def test_direct_answer_inherits_previous_non_entity_scope() -> None:
    registry = RelationFilterRegistry()
    engine = InvestigationEngine(registry)  # type: ignore[arg-type]
    state = InvestigationState(
        investigation_id="inv_scope_follow_up",
        question="Follow-up request: Split by stores and channel.",
        current_conversation_question="Split by stores and channel.",
        datasources=[registry.analytics.summary, registry.catalog.summary],
        query_scopes=[QueryScope(
            datasource_id="analytics", table="public.sales_events",
            predicate_sql="occurred_at >= CAST('2024-07-01' AS TIMESTAMPTZ) AND occurred_at < CAST('2024-08-01' AS TIMESTAMPTZ)",
        )],
    )

    query = engine._apply_inherited_scope(
        state,
        DirectAnswerQuery(
            datasource_id="analytics",
            sql="SELECT item_id, SUM(units) FROM public.sales_events GROUP BY item_id",
            purpose="Show sales by store.",
        ),
    )

    assert "2024-07-01" in query.sql
    assert query.sql.index("2024-07-01") < query.sql.index("GROUP BY")


def test_inherited_scope_does_not_narrow_an_explicit_all_population_aggregate() -> None:
    registry = RelationFilterRegistry()
    engine = InvestigationEngine(registry)  # type: ignore[arg-type]
    state = InvestigationState(
        investigation_id="inv_scope_comparison",
        question="Follow-up request: Estimate this using the previous metric.",
        current_conversation_question="Estimate this using the previous metric.",
        datasources=[registry.analytics.summary, registry.catalog.summary],
        query_scopes=[
            QueryScope(
                datasource_id="analytics",
                table="public.sales_events",
                predicate_sql=(
                    "platform = 'iOS' AND occurred_at >= CAST('2024-03-18' AS TIMESTAMPTZ) "
                    "AND occurred_at < CAST('2024-04-01' AS TIMESTAMPTZ)"
                ),
            )
        ],
    )

    query = engine._apply_inherited_scope(
        state,
        DirectAnswerQuery(
            datasource_id="analytics",
            sql=(
                "SELECT COUNT(*) FILTER(WHERE platform ILIKE 'iOS') AS ios_records, "
                "COUNT(*) AS all_records FROM public.sales_events "
                "WHERE occurred_at >= CAST('2024-03-18' AS TIMESTAMPTZ) "
                "AND occurred_at < CAST('2024-04-01' AS TIMESTAMPTZ)"
            ),
            purpose="Compare the selected platform with all recorded sales.",
        ),
    )

    assert "FILTER(WHERE platform ILIKE 'iOS')" in query.sql
    outer_query = query.sql.split("FROM public.sales_events", maxsplit=1)[1]
    assert "platform" not in outer_query.casefold()
    assert "occurred_at" in outer_query


def test_all_platforms_follow_up_overrides_a_retained_platform_scope() -> None:
    registry = RelationFilterRegistry()
    engine = InvestigationEngine(registry)  # type: ignore[arg-type]
    state = InvestigationState(
        investigation_id="inv_all_platforms",
        question="Follow-up request: Show completed sales across all platforms by channel.",
        current_conversation_question="Show completed sales across all platforms by channel.",
        datasources=[registry.analytics.summary, registry.catalog.summary],
        query_scopes=[
            QueryScope(
                datasource_id="analytics",
                table="public.sales_events",
                predicate_sql="platform = 'iOS'",
            )
        ],
    )
    original = DirectAnswerQuery(
        datasource_id="analytics",
        sql=(
            "SELECT channel, COUNT(*) AS completed_sales_records FROM public.sales_events "
            "WHERE payment_status = 'completed' GROUP BY channel"
        ),
        purpose="Show completed sales records by channel.",
    )

    assert engine._apply_inherited_scope(state, original) == original


@pytest.mark.asyncio
async def test_entity_filtered_query_ignores_an_invalid_planner_intersection() -> None:
    registry = RelationFilterRegistry()
    engine = InvestigationEngine(registry, llm=IncorrectIntersectionDirectAnswerLLM())  # type: ignore[arg-type]
    update = await engine._answer_directly(
        InvestigationState(
            investigation_id="inv_invalid_intersection",
            question="Follow-up request: Split by stores for this item.",
            current_conversation_question="Split by stores for this item.",
            datasources=[registry.analytics.summary, registry.catalog.summary],
            resolved_entities=[ResolvedEntityReference(
                datasource_id="catalog", table="public.products", identifier_field="id",
                identifier="P162", display_name="Lantern",
            )],
        )
    )

    assert update["observations"][-1].value["rows"] == [{"units_sold": 97, "display_name": "Lantern"}]


def test_direct_answer_rejects_an_unresolved_entity_placeholder() -> None:
    registry = RelationFilterRegistry()
    engine = InvestigationEngine(registry)  # type: ignore[arg-type]
    state = InvestigationState(
        investigation_id="inv_unresolved_entity",
        question="Show item sales.",
        datasources=[registry.analytics.summary, registry.catalog.summary],
    )

    with pytest.raises(UnsafeQueryError, match="does not identify one stored entity"):
        engine._bind_trusted_entity_references(
            state,
            DirectAnswerQuery(
                datasource_id="analytics",
                sql="SELECT * FROM public.sales_events WHERE item_id = '{{item_id}}'",
                purpose="Show item sales.",
            ),
        )


@pytest.mark.asyncio
async def test_direct_existence_question_with_never_does_not_ask_for_an_unneeded_period() -> None:
    registry = FakeRegistry()
    engine = InvestigationEngine(registry, llm=AmbiguousAllHistoryDirectAnswerLLM())

    update = await engine._understand(
        InvestigationState(
            investigation_id="inv_all_history",
            question="Which items were never sold?",
            datasources=[registry.source.summary],
        )
    )

    assert update["request_type"] == "direct_answer"
    assert update["pending_human_question"] is None
    assert update["status"].value == "running"


def test_direct_follow_up_does_not_reask_an_answered_sales_metric() -> None:
    state = InvestigationState(
        investigation_id="inv_metric_reuse",
        question="Show the sales by store.",
        datasources=[],
        human_feedback=[HumanFeedback(
            question="When you say sold, do you mean the number of units customers purchased, the number of orders, or total revenue?",
            response="Number of units customers purchased.",
        )],
    )
    engine = InvestigationEngine(DatasourceRegistry())

    assert engine._reasks_answered_sales_metric(
        state, "When you say sales, do you mean the number of units sold or the total amount customers spent (revenue)?"
    )


def test_only_essential_business_definitions_can_pause_an_investigation() -> None:
    engine = InvestigationEngine(DatasourceRegistry())
    state = InvestigationState(investigation_id="inv_clarification_policy", question="Why did conversion fall?", datasources=[])

    assert engine._clarification_to_request(
        state, "When you say conversion rate, do you mean completed purchases divided by sessions or checkout starts?"
    )
    assert engine._clarification_to_request(
        state, "Allow me to run a different query or use an alternate data extract."
    ) is None
    assert engine._clarification_to_request(
        state, "Would you like me to widen the date range and try again?"
    ) is None
    answered = state.model_copy(update={"human_feedback": [HumanFeedback(question="Which period?", response="March")]})
    assert engine._clarification_to_request(answered, "Which region should be compared?") is None


@pytest.mark.asyncio
async def test_direct_answer_repairs_a_plan_that_asks_the_user_to_choose_selected_sources() -> None:
    registry = SetComparisonRegistry()
    llm = DatasourceDeferringLLM()
    engine = InvestigationEngine(registry, llm=llm)  # type: ignore[arg-type]

    result = InvestigationState.model_validate(
        await engine.graph.ainvoke(
            InvestigationState(
                investigation_id="inv_source_deferral",
                question="List every item that was never sold.",
                datasources=[registry.catalog.summary, registry.analytics.summary],
            ),
            config={"configurable": {"thread_id": "inv_source_deferral"}},
        )
    )

    assert result.status.value == "completed"
    assert llm.plan_calls == 2
    assert registry.catalog.queries == ["SELECT id FROM public.items LIMIT 500"]
    assert registry.analytics.queries == ["SELECT DISTINCT item_id FROM public.sales LIMIT 500"]


@pytest.mark.asyncio
async def test_named_ranking_never_returns_internal_identifiers() -> None:
    engine = InvestigationEngine(DatasourceRegistry(), llm=DirectAnswerFixtureLLM())
    state = InvestigationState(
        investigation_id="inv_named_ranking",
        question="What are the top selling items?",
        datasources=[],
        request_type="direct_answer",
        observations=[
            Observation(
                description="Rank the best-selling items.",
                source="analytics",
                value={
                    "rows": [
                        {"display_name": "Charging Hub", "total_units_sold": 732},
                        {"display_name": "Lantern", "total_units_sold": 727},
                    ]
                },
            )
        ],
    )

    update = await engine._synthesize(state)
    analysis = update["final_analysis"]

    assert analysis.summary == "1. Charging Hub — 732 the requested measure; 2. Lantern — 727 the requested measure."
    assert "P003" not in analysis.summary
    assert all(point.value not in {"P003", "IT-007"} for point in analysis.evidence[0].data)
    assert update["status"].value == "completed"


@pytest.mark.asyncio
async def test_direct_aggregate_without_an_attributable_value_explains_the_data_limitation() -> None:
    engine = InvestigationEngine(DatasourceRegistry(), llm=DirectAnswerFixtureLLM())
    state = InvestigationState(
        investigation_id="inv_unavailable_total",
        question="What was the total revenue from iOS orders?",
        datasources=[],
        request_type="direct_answer",
        observations=[Observation(
            description="Retrieve the requested revenue total.",
            source="analytics",
            value={"rows": [{"total_revenue": None}]},
        )],
    )

    update = await engine._synthesize(state)

    assert update["final_analysis"].likely_root_cause == "Requested total is not available from the connected data"
    assert "does not mean the total was zero" in update["final_analysis"].caveats[0]
    assert update["final_analysis"].evidence[0].data[0].value == "Not available"


@pytest.mark.asyncio
async def test_product_value_ranking_offers_an_executable_time_series_follow_up() -> None:
    registry = FakeRegistry()
    engine = InvestigationEngine(registry, llm=DirectAnswerFixtureLLM())
    state = InvestigationState(
        investigation_id="inv_value_ranking",
        question="What is the most valuable item?",
        datasources=[registry.source.summary],
        request_type="direct_answer",
        observations=[
            Observation(
                description="Rank items by sales value.",
                source="analytics",
                value={"rows": [{"display_name": "Charging Hub", "total_revenue": 18400}]},
            )
        ],
    )

    assert await engine._contextual_follow_up_question(state) == (
        "Would you like to see how the requested measure changed over time for Charging Hub?"
    )


@pytest.mark.asyncio
async def test_evidence_ambiguity_pauses_then_resumes_with_a_new_investigation_step() -> None:
    registry = FakeRegistry()
    llm = LateAmbiguousFixtureLLM()
    engine = InvestigationEngine(registry, llm=llm)
    initial = InvestigationState(
        investigation_id="inv_late_ambiguity",
        question="Why did northern revenue fall?",
        datasources=[registry.source.summary],
    )
    config = {"configurable": {"thread_id": initial.investigation_id}}

    paused = InvestigationState.model_validate(await engine.graph.ainvoke(initial, config=config))
    assert paused.status.value == "waiting_for_human"
    assert paused.pending_human_question == "Which historical dates should be compared?"

    completed = InvestigationState.model_validate(
        await engine.graph.ainvoke(Command(resume="Compare July and August 2024."), config=config)
    )
    assert completed.status.value == "completed"
    # The premise runs once before causal work. A clarification may add
    # context, but it must not re-run that completed baseline.
    assert len(completed.investigation_history) == 3
    assert {step.requirement_id for step in completed.investigation_history} == {None, "hyp_stock:r1", "hyp_demand:r1"}
    assert llm.evidence_calls == 3


class GenericEvidenceLLM:
    async def structured(self, response_model, instruction: str, payload: dict):
        assert response_model is EvidenceAssessment
        return EvidenceAssessment.model_validate(
            {
                "evidence": [{
                    "id": "ev_coverage",
                    "description": "The requested period has no recorded events.",
                    "source": "analytics",
                    "relationship": "contradicting",
                    "confidence": 0.9,
                    "hypothesis_ids": ["hyp_stock", "hyp_demand"],
                }],
                "hypotheses": [
                    {"id": "hyp_stock", "name": "Inventory constraint", "description": "Inventory constrained sales", "category": "inventory", "confidence": 0.1, "status": "rejected"},
                    {"id": "hyp_demand", "name": "Demand decline", "description": "Demand declined", "category": "demand", "confidence": 0.1, "status": "rejected"},
                ],
                "confidence": 0.2,
                "ambiguity": "Which historical comparison period should be used?",
            }
        )


@pytest.mark.asyncio
async def test_investigation_level_evidence_does_not_change_or_attach_to_all_hypotheses() -> None:
    registry = FakeRegistry()
    engine = InvestigationEngine(registry, llm=GenericEvidenceLLM())
    state = InvestigationState.model_validate(
        {
            "investigation_id": "inv_coverage",
            "question": "Why did revenue fall?",
            "datasources": [registry.source.summary.model_dump()],
            "hypotheses": [
                {"id": "hyp_stock", "name": "Inventory constraint", "description": "Inventory constrained sales", "category": "inventory", "confidence": 0.5},
                {"id": "hyp_demand", "name": "Demand decline", "description": "Demand declined", "category": "demand", "confidence": 0.4},
            ],
        }
    )

    result = await engine._interpret(state)

    assert result["evidence"][0].hypothesis_ids == []
    assert [hypothesis.status.value for hypothesis in result["hypotheses"]] == ["active", "active"]
    assert result["status"].value == "waiting_for_human"


def test_only_mechanism_evidence_with_matching_topics_can_attach_to_a_hypothesis() -> None:
    state = InvestigationState.model_validate(
        {
            "investigation_id": "inv_semantic_evidence_scope",
            "question": "Why did sales fall?",
            "datasources": [],
            "hypotheses": [
                {
                    "id": "hyp_weather",
                    "name": "Weather disruption",
                    "description": "Rain reduced visits.",
                    "category": "external",
                    "research_scope": "external",
                    "evidence_topics": ["weather.conditions"],
                    "confidence": 0.3,
                },
                {
                    "id": "hyp_promotion",
                    "name": "Promotion change",
                    "description": "A discount ended.",
                    "category": "pricing",
                    "evidence_topics": ["promotion.discount"],
                    "confidence": 0.3,
                },
            ],
        }
    )
    assessment = EvidenceAssessment.model_validate(
        {
            "evidence": [
                {
                    "id": "baseline",
                    "description": "Sales were lower in July.",
                    "source": "sales records",
                    "relationship": "direct",
                    "scope": "premise",
                    "confidence": 0.9,
                    "hypothesis_ids": ["hyp_weather"],
                },
                {
                    "id": "wrong_mechanism",
                    "description": "The weather was warmer.",
                    "source": "weather records",
                    "relationship": "contradicting",
                    "scope": "mechanism",
                    "evidence_topics": ["weather.conditions"],
                    "confidence": 0.8,
                    "hypothesis_ids": ["hyp_promotion"],
                },
                {
                    "id": "matching_mechanism",
                    "description": "The promotion ended in July.",
                    "source": "promotion records",
                    "relationship": "supporting",
                    "scope": "mechanism",
                    "evidence_topics": ["promotion.discount"],
                    "confidence": 0.8,
                    "hypothesis_ids": ["hyp_promotion"],
                },
            ],
            "hypotheses": [],
            "confidence": 0.8,
        }
    )

    evidence, affected = InvestigationEngine._scope_evidence(state, assessment)

    assert [item.hypothesis_ids for item in evidence] == [[], [], ["hyp_promotion"]]
    assert affected == {"hyp_promotion"}


def test_new_claim_contract_requires_an_explicit_single_evidence_owner() -> None:
    state = InvestigationState.model_validate({
        "investigation_id": "inv_claim_owner",
        "question": "Why did the measure change?",
        "datasources": [],
        "hypotheses": [{
            "id": "claim_one",
            "name": "A proposed explanation",
            "description": "A proposed explanation could account for the outcome.",
            "category": "generic",
            "claim": {
                "cause": "A proposed change",
                "mechanism": "changes the requested outcome",
                "outcome": "the requested outcome",
                "required_evidence": ["A direct matching observation."],
            },
            "confidence": 0.3,
        }],
    })
    assessment = EvidenceAssessment.model_validate({
        "evidence": [{
            "id": "unowned",
            "description": "A result was returned.",
            "source": "connected records",
            "relationship": "supporting",
            "scope": "mechanism",
            "confidence": 0.8,
            "hypothesis_ids": ["claim_one"],
        }, {
            "id": "owned",
            "description": "A result directly tested the proposed explanation.",
            "source": "connected records",
            "relationship": "supporting",
            "scope": "mechanism",
            "basis": "direct",
            "claim_id": "claim_one",
            "hypothesis_ids": ["claim_one"],
            "confidence": 0.8,
        }],
        "hypotheses": [],
        "confidence": 0.8,
    })

    evidence, affected = InvestigationEngine._scope_evidence(state, assessment)

    assert evidence[0].hypothesis_ids == []
    assert evidence[0].claim_id is None
    assert evidence[1].hypothesis_ids == ["claim_one"]
    assert evidence[1].claim_id == "claim_one"
    assert affected == {"claim_one"}


def test_live_observation_owner_overrides_an_incorrect_llm_evidence_owner() -> None:
    """The evidence interpreter cannot move H1's result into H2's tree."""
    state = InvestigationState.model_validate({
        "investigation_id": "inv_fixed_evidence_owner",
        "question": "Why did the measure change?",
        "datasources": [],
        "hypotheses": [
            {"id": "h1", "name": "First cause", "description": "First explanation", "category": "one", "confidence": 0.4},
            {"id": "h2", "name": "Second cause", "description": "Second explanation", "category": "two", "confidence": 0.4},
        ],
        "observations": [{
            "description": "The selected H1 check completed.",
            "source": "analytics",
            "value": {"hypothesis_id": "h1", "requirement_id": "h1:r1", "evidence_contract": {"role": "causal"}},
        }],
    })
    assessment = EvidenceAssessment.model_validate({
        "evidence": [{
            "id": "mislabelled",
            "description": "A result from H1's selected check.",
            "source": "analytics",
            "relationship": "supporting",
            "scope": "mechanism",
            "confidence": 0.8,
            "claim_id": "h2",
            "hypothesis_ids": ["h2"],
        }],
        "hypotheses": [],
        "confidence": 0.8,
    })

    evidence, affected = InvestigationEngine._scope_evidence(state, assessment)

    assert evidence[0].hypothesis_id == "h1"
    assert evidence[0].requirement_id == "h1:r1"
    assert evidence[0].hypothesis_ids == ["h1"]
    assert affected == {"h1"}


def test_premise_contract_keeps_baseline_evidence_out_of_hypothesis_trees() -> None:
    state = InvestigationState.model_validate({
        "investigation_id": "inv_baseline_evidence",
        "question": "Why did sales fall?",
        "datasources": [],
        "hypotheses": [{"id": "h1", "name": "A cause", "description": "A cause", "category": "generic", "confidence": 0.4}],
        "observations": [{
            "description": "Validate the reported change.",
            "source": "analytics",
            "value": {"evidence_contract": {"role": "baseline", "basis": "direct"}},
        }],
    })
    assessment = EvidenceAssessment.model_validate({
        "evidence": [{
            "id": "baseline",
            "description": "Sales increased by 7.1%.",
            "source": "analytics",
            "relationship": "direct",
            "scope": "mechanism",
            "confidence": 0.95,
            "claim_id": "h1",
            "hypothesis_ids": ["h1"],
            "data": [{"field": "measured_metric_percentage_change", "value": 7.1}],
        }],
        "hypotheses": [],
        "confidence": 0.95,
    })

    evidence, affected = InvestigationEngine._scope_evidence(state, assessment)

    assert evidence[0].scope == "premise"
    assert evidence[0].hypothesis_id is None
    assert evidence[0].hypothesis_ids == []
    assert affected == set()


def test_hypothesis_baseline_evidence_stays_with_its_selected_hypothesis() -> None:
    state = InvestigationState.model_validate({
        "investigation_id": "inv_hypothesis_baseline_evidence",
        "question": "Why did sales fall?",
        "datasources": [],
        "hypotheses": [{"id": "h1", "name": "Store visits", "description": "Visits fell", "category": "generic", "confidence": 0.4}],
        "observations": [{
            "description": "Compare visits before and after.",
            "source": "analytics",
            "value": {
                "hypothesis_id": "h1",
                "requirement_id": "h1:r1",
                "evidence_contract": {"role": "baseline", "basis": "direct"},
            },
        }],
    })
    assessment = EvidenceAssessment.model_validate({
        "evidence": [{
            "id": "visits",
            "description": "Store visits were lower in the later period.",
            "source": "analytics",
            "relationship": "supporting",
            "scope": "premise",
            "confidence": 0.9,
        }],
        "hypotheses": [],
        "confidence": 0.9,
    })

    evidence, affected = InvestigationEngine._scope_evidence(state, assessment)

    assert evidence[0].scope == "mechanism"
    assert evidence[0].hypothesis_id == "h1"
    assert evidence[0].requirement_id == "h1:r1"
    assert evidence[0].hypothesis_ids == ["h1"]
    assert affected == {"h1"}


def test_before_after_rows_use_decimal_rate_calculation_for_integer_totals() -> None:
    rows = InvestigationEngine._normalize_before_after_rows([
        {"earlier_value": 60666, "later_value": 24611, "rate_change_percent": 0},
    ])

    assert rows[0]["rate_change_percent"] == pytest.approx(-59.43, abs=0.01)


def test_query_cache_is_scoped_to_the_final_sql_and_source_schema_version() -> None:
    engine = InvestigationEngine(DatasourceRegistry())
    sql = "SELECT revenue FROM sales_events WHERE occurred_at >= DATE '2024-07-01'"
    key = engine._query_cache_key("analytics", "schema-v1", sql)
    state = InvestigationState.model_validate({
        "investigation_id": "inv_query_cache",
        "question": "Why did revenue change?",
        "datasources": [],
        "query_result_cache": [{
            "key": key,
            "datasource_id": "analytics",
            "schema_fingerprint": "schema-v1",
            "rows": [{"revenue": 42}],
            "display_rows": [{"revenue": 42}],
            "deterministic_summary": {"row_count": 1},
        }],
    })

    assert engine._cached_query_result(state, "analytics", "schema-v1", sql) is not None
    assert engine._cached_query_result(state, "analytics", "schema-v2", sql) is None


@pytest.mark.asyncio
async def test_rejected_premise_finishes_without_causal_synthesis() -> None:
    registry = FakeRegistry()
    engine = InvestigationEngine(registry, llm=FixtureLLM())
    hypotheses = [
        {"id": "hyp_demand", "name": "Lower demand", "description": "Demand fell", "category": "demand", "confidence": 0.02, "status": "rejected"},
        {"id": "hyp_price", "name": "Lower prices", "description": "Prices fell", "category": "pricing", "confidence": 0.1, "status": "rejected"},
    ]
    state = InvestigationState.model_validate(
        {
            "investigation_id": "inv_no_decline",
            "question": "Did sales decline in the last three months of 2024?",
            "datasources": [registry.source.summary.model_dump()],
            "premise_to_validate": "Sales declined in the last three months of 2024.",
            "premise_status": "rejected",
            "hypotheses": hypotheses,
            "evidence": [{
                "id": "ev_totals",
                "description": "The total amount customers spent was slightly higher in the later period.",
                "source": "analytics",
                "relationship": "direct",
                "confidence": 0.9,
                "hypothesis_ids": ["hyp_demand", "hyp_price"],
            }],
        }
    )

    decision = await engine._decide(state)
    result = await engine._synthesize(state)

    assert decision["next_action"] == "finish"
    assert result["final_analysis"].likely_root_cause == "Reported starting point was not found"
    assert "no validated result to explain" in result["final_analysis"].summary
    assert result["conversation_turns"][-1].question == state.question
    assert result["conversation_turns"][-1].answer == result["final_analysis"]


@pytest.mark.asyncio
async def test_negligible_measured_decline_finishes_without_more_clarifications() -> None:
    registry = FakeRegistry()
    engine = InvestigationEngine(registry, llm=FixtureLLM())
    state = InvestigationState.model_validate(
        {
            "investigation_id": "inv_no_material_decline",
            "question": "Did sales decline in the last three months of 2024?",
            "datasources": [registry.source.summary.model_dump()],
            "premise_to_validate": "Sales changed in the last three months of 2024.",
            "premise_status": "confirmed",
            "hypotheses": [
                {
                    "id": "hyp_demand",
                    "name": "Lower demand",
                    "description": "Demand fell",
                    "category": "demand",
                    "confidence": 0.4,
                }
            ],
            "evidence": [{
                "id": "ev_units",
                "description": "Units sold changed by only −0.09% in the later period.",
                "source": "analytics",
                "relationship": "direct",
                "confidence": 0.9,
                "hypothesis_ids": ["hyp_demand"],
                "data": [{"field": "rate_change_percent", "value": -0.09}],
            }],
        }
    )

    decision = await engine._decide(state)
    result = await engine._synthesize(state)

    assert decision["next_action"] == "finish"
    assert result["final_analysis"].likely_root_cause == "No meaningful overall change found"
    assert "1.00% negligibility threshold" in result["final_analysis"].summary
    assert result["conversation_turns"][-1].question == state.question


def test_final_analysis_uses_relative_percentages_and_downgrades_an_incomplete_causal_claim() -> None:
    hypothesis = Hypothesis.model_validate({
        "id": "hyp_driver",
        "name": "An immediate driver",
        "description": "A measured immediate driver changed the outcome.",
        "category": "generic",
        "confidence": 0.9,
        "status": "supported",
        "requirements": [{"id": "hyp_driver:r1", "description": "Verify the upstream cause.", "status": "pending"}],
    })
    analysis = FinalAnalysis(
        likely_root_cause="The measured driver changed.",
        confidence=0.9,
        evidence=[],
        rejected_hypotheses=[],
        external_findings=[],
        caveats=[],
        summary="The rate fell by -25 percent from 20% to 15%.",
    )

    state = InvestigationState(
        investigation_id="inv_incomplete_driver",
        question="Why did the measure change?",
        datasources=[],
        hypotheses=[hypothesis],
    )
    bounded = InvestigationEngine._bound_conclusion_level(analysis, state, [hypothesis])
    normalized = InvestigationEngine._normalize_analysis_presentation(bounded)

    assert normalized.conclusion_level == "proximate_driver"
    assert "-25.00%" in normalized.summary
    assert "20.00%" in normalized.summary
    assert "percentage points" not in normalized.summary


def test_rejected_premise_does_not_request_a_second_baseline_check() -> None:
    engine = InvestigationEngine(DatasourceRegistry())
    state = InvestigationState(
        investigation_id="inv_premise_increase",
        question="Why did sales change in July?",
        datasources=[],
        premise_to_validate="Sales fell in July.",
        premise_status="rejected",
        evidence=[Evidence(
            description="Sales increased in July.",
            source="analytics",
            relationship="direct",
            confidence=0.95,
            scope="premise",
            data=[EvidenceDataPoint(field="measured_metric_percentage_change", value=7.1)],
        )],
    )

    assert not engine._requires_premise_validation(state)
    assert engine._route_after_hypothesis_generation(state) == "select_investigation"


def test_reported_premise_requires_a_check_before_hypothesis_work() -> None:
    engine = InvestigationEngine(DatasourceRegistry())
    state = InvestigationState(
        investigation_id="inv_premise_required",
        question="Why did sales improve in July?",
        datasources=[],
        premise_to_validate="Sales improved in July.",
        premise_status="pending",
    )

    assert engine._route_after_hypothesis_generation(state) == "validate_premise"


def test_possible_cause_percentage_change_cannot_trigger_the_negligible_decline_shortcut() -> None:
    engine = InvestigationEngine(DatasourceRegistry())
    state = InvestigationState(
        investigation_id="inv_driver_change",
        question="Why did conversion decline?",
        datasources=[],
        evidence=[
            Evidence(
                description="Payment failures increased after the release.",
                source="analytics",
                relationship="direct",
                confidence=0.9,
                hypothesis_ids=["hyp_payment"],
                data=[EvidenceDataPoint(field="percentage_change", value=12.54)],
            )
        ],
    )

    assert not engine._no_material_decline(state)


class RetryingDatasource(FakeDatasource):
    async def execute_read_query(self, query: str) -> list[dict[str, object]]:
        self.queries.append(query)
        if len(self.queries) == 1:
            raise ProgrammingError(query, {}, Exception("undefined column"))
        return [{"region": "north", "revenue": 80}]


class FailingDatasource(FakeDatasource):
    async def execute_read_query(self, query: str) -> list[dict[str, object]]:
        self.queries.append(query)
        raise ProgrammingError(query, {}, Exception("undefined column"))


class CompilerFailingDatasource(FakeDatasource):
    async def execute_read_query(self, query: str) -> list[dict[str, object]]:
        self.queries.append(query)
        raise KeyError("unexpected DB-API placeholder")


class RetryingRegistry(FakeRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.source = RetryingDatasource()


class FailingRegistry(FakeRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.source = FailingDatasource()


class CompilerFailingRegistry(FakeRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.source = CompilerFailingDatasource()


class RetryingLLM:
    def __init__(self) -> None:
        self.query_calls = 0

    async def structured(self, response_model, instruction: str, payload: dict):
        assert response_model is QueryPlan
        self.query_calls += 1
        sql = "SELECT unknown_column FROM sales_events" if self.query_calls == 1 else "SELECT revenue FROM sales_events"
        return QueryPlan(sql=sql, purpose="Measure revenue")


class CommentRepairingLLM:
    def __init__(self) -> None:
        self.query_calls = 0

    async def structured(self, response_model, instruction: str, payload: dict):
        assert response_model is QueryPlan
        self.query_calls += 1
        if self.query_calls == 1:
            return QueryPlan(sql="/* initial explanatory note */ SELECT revenue FROM sales_events", purpose="Measure revenue")
        assert payload["rejection_reason"] == "SQL comments are not allowed"
        assert "Do not include SQL comments" in instruction
        return QueryPlan(sql="SELECT revenue FROM sales_events", purpose="Measure revenue")


class SyntaxRepairingLLM:
    """The retry must happen before an invalid planner query touches a datasource."""

    def __init__(self) -> None:
        self.query_calls = 0

    async def structured(self, response_model, instruction: str, payload: dict):
        assert response_model is QueryPlan
        self.query_calls += 1
        if self.query_calls == 1:
            return QueryPlan(sql="SELECT CASE WHEN ( THEN revenue END FROM sales_events", purpose="Measure revenue")
        assert "Invalid SQL:" in payload["rejection_reason"]
        assert "Ensure every parenthesis is balanced" in instruction
        return QueryPlan(sql="SELECT revenue FROM sales_events", purpose="Measure revenue")


@pytest.mark.asyncio
async def test_execute_retries_one_postgres_rejected_query_with_a_repair() -> None:
    registry = RetryingRegistry()
    engine = InvestigationEngine(registry, llm=RetryingLLM())
    state = InvestigationState(
        investigation_id="inv_retry",
        question="Why did revenue fall?",
        datasources=[registry.source.summary],
        pending_step={
            "hypothesis_id": "hypothesis",
            "action": "compare revenue",
            "datasource_id": "analytics",
            "rationale": "test the leading hypothesis",
        },
    )

    result = await engine._execute(state)

    assert registry.source.queries == [
        "SELECT unknown_column FROM sales_events LIMIT 500",
        "SELECT revenue FROM sales_events LIMIT 500",
    ]
    assert result["query_count"] == 1
    assert result["pending_step"].query == "SELECT revenue FROM sales_events LIMIT 500"


@pytest.mark.asyncio
async def test_failed_query_is_contained_to_its_hypothesis() -> None:
    registry = FailingRegistry()
    events = []

    async def capture(event):
        events.append(event)

    engine = InvestigationEngine(registry, llm=RetryingLLM(), event_sink=capture)
    state = InvestigationState(
        investigation_id="inv_contained_query_failure",
        question="Why did revenue fall?",
        datasources=[registry.source.summary],
        hypotheses=[{
            "id": "hypothesis",
            "name": "Inventory availability",
            "description": "Inventory constrained sales.",
            "category": "inventory",
            "confidence": 0.5,
        }],
        pending_step={
            "hypothesis_id": "hypothesis",
            "action": "Compare revenue.",
            "datasource_id": "analytics",
            "rationale": "Test inventory.",
        },
    )

    result = await engine._execute(state)

    assert result["pending_step"] is None
    assert result["hypotheses"][0].status == HypothesisStatus.NEEDS_MORE_EVIDENCE
    assert result["failed_checks"]
    assert any(event.type == "QueryFailed" for event in events)
    failed_state = state.model_copy(update=result)
    assert engine._route_after_execution(failed_state) == "decide"


@pytest.mark.asyncio
async def test_failed_premise_check_ends_without_starting_causal_work() -> None:
    registry = FailingRegistry()
    events = []

    async def capture(event):
        events.append(event)

    engine = InvestigationEngine(registry, llm=RetryingLLM(), event_sink=capture)
    state = InvestigationState(
        investigation_id="inv_failed_premise_check",
        question="Why did revenue change?",
        datasources=[registry.source.summary],
        premise_to_validate="Revenue changed during the requested period.",
        premise_status="pending",
        pending_step={
            "hypothesis_id": None,
            "action": "Validate the reported business change.",
            "datasource_id": "analytics",
            "rationale": "Check the factual starting point first.",
        },
    )

    result = await engine._execute(state)

    assert result["pending_step"] is None
    assert result["premise_status"] == "inconclusive"
    assert result["next_action"] == "finish"
    assert any(event.type == "QueryFailed" for event in events)


@pytest.mark.asyncio
async def test_database_compiler_key_error_is_contained_to_its_hypothesis() -> None:
    registry = CompilerFailingRegistry()
    engine = InvestigationEngine(registry, llm=RetryingLLM())
    state = InvestigationState(
        investigation_id="inv_compiler_failure",
        question="Why did revenue fall?",
        datasources=[registry.source.summary],
        hypotheses=[{
            "id": "hypothesis", "name": "Inventory availability", "description": "Inventory constrained sales.",
            "category": "inventory", "confidence": 0.5,
        }],
        pending_step={
            "hypothesis_id": "hypothesis", "action": "Compare revenue.",
            "datasource_id": "analytics", "rationale": "Test inventory.",
        },
    )

    result = await engine._execute(state)

    assert len(registry.source.queries) == 2  # one repair attempt, then containment
    assert result["pending_step"] is None
    assert result["hypotheses"][0].status == HypothesisStatus.NEEDS_MORE_EVIDENCE


@pytest.mark.asyncio
async def test_repeated_completed_result_exhausts_the_current_hypothesis() -> None:
    engine = InvestigationEngine(DatasourceRegistry())
    state = InvestigationState(
        investigation_id="inv_repeated_result",
        question="Why did revenue fall?",
        datasources=[],
        observations=[Observation(
            description="First measurement.",
            source="analytics",
            value={"rows": [{"display_name": "Lantern", "units": 10}], "hypothesis_id": "hypothesis"},
        )],
        hypotheses=[{
            "id": "hypothesis", "name": "Inventory availability", "description": "Inventory constrained sales.",
            "category": "inventory", "confidence": 0.5,
        }],
        pending_step={
            "hypothesis_id": "hypothesis", "action": "Repeat revenue measurement.",
            "datasource_id": "analytics", "rationale": "Retry the same check.",
        },
    )

    assert engine._has_duplicate_completed_result(
        state, "analytics", "hypothesis", [{"display_name": "Lantern", "units": 10}]
    )
    result = await engine._record_duplicate_step(state, [], [], 0, [], 0, "SELECT units FROM sales_events")

    assert result["pending_step"] is None
    assert result["hypotheses"][0].status == HypothesisStatus.NEEDS_MORE_EVIDENCE
    assert "repeated an earlier result" in result["failed_checks"][0]


@pytest.mark.asyncio
async def test_execute_repairs_a_query_rejected_for_sql_comments() -> None:
    registry = FakeRegistry()
    engine = InvestigationEngine(registry, llm=CommentRepairingLLM())
    state = InvestigationState(
        investigation_id="inv_comment_retry",
        question="Why did revenue fall?",
        datasources=[registry.source.summary],
        pending_step={
            "hypothesis_id": "hypothesis",
            "action": "compare revenue",
            "datasource_id": "analytics",
            "rationale": "test the leading hypothesis",
        },
    )

    result = await engine._execute(state)

    assert engine.llm.query_calls == 2
    assert registry.source.queries == ["SELECT revenue FROM sales_events LIMIT 500"]
    assert result["pending_step"].query == "SELECT revenue FROM sales_events LIMIT 500"


@pytest.mark.asyncio
async def test_execute_repairs_invalid_planner_sql_before_querying_the_datasource() -> None:
    registry = FakeRegistry()
    engine = InvestigationEngine(registry, llm=SyntaxRepairingLLM())
    state = InvestigationState(
        investigation_id="inv_syntax_retry",
        question="Why did revenue fall?",
        datasources=[registry.source.summary],
        pending_step={
            "hypothesis_id": "hypothesis",
            "action": "compare revenue",
            "datasource_id": "analytics",
            "rationale": "test the leading hypothesis",
        },
    )

    result = await engine._execute(state)

    assert engine.llm.query_calls == 2
    assert registry.source.queries == ["SELECT revenue FROM sales_events LIMIT 500"]
    assert result["pending_step"].query == "SELECT revenue FROM sales_events LIMIT 500"


@pytest.mark.asyncio
async def test_investigation_filters_a_primary_source_with_an_approved_related_lookup() -> None:
    registry = RelationFilterRegistry()
    engine = InvestigationEngine(registry, llm=CrossDatasourceInvestigationLLM())  # type: ignore[arg-type]
    state = InvestigationState(
        investigation_id="inv_cross_source_lookup",
        question="Why did Lantern sales change?",
        datasources=[registry.analytics.summary, registry.catalog.summary],
        pending_step={
            "hypothesis_id": "hypothesis",
            "action": "Measure sales for the requested product.",
            "datasource_id": "analytics",
            "rationale": "Test the product-specific change.",
        },
    )

    result = await engine._execute(state)

    assert "item_id IN ('P162')" in registry.analytics.queries[0]
    assert "lookup_outdoor_products" not in registry.analytics.queries[0]
    assert registry.catalog.queries[0] == (
        "SELECT id AS relationship_key FROM public.products WHERE name ILIKE 'Lantern' LIMIT 500"
    )
    assert result["query_count"] == 3  # catalog lookup, primary query, display-name lookup
    assert result["observations"][-1].value["rows"] == [{"units_sold": 97, "display_name": "Lantern"}]


@pytest.mark.asyncio
async def test_investigation_filters_a_primary_target_source_with_a_reverse_approved_lookup() -> None:
    registry = RelationFilterRegistry()
    engine = InvestigationEngine(registry)  # type: ignore[arg-type]
    state = InvestigationState(
        investigation_id="inv_reverse_cross_source_lookup",
        question="Which catalog items had recorded sales?",
        datasources=[registry.catalog.summary, registry.analytics.summary],
        pending_step={
            "hypothesis_id": "hypothesis",
            "action": "Limit catalog items to those with recorded sales.",
            "datasource_id": "catalog",
            "rationale": "Use the approved sales-to-catalog relationship in reverse.",
        },
    )
    metadata = [
        {"id": "catalog", "tables": [{"name": "public.products", "columns": [], "foreign_keys": []}]},
        {"id": "analytics", "tables": [{"name": "public.sales_events", "columns": [], "foreign_keys": []}]},
    ]

    sql, steps, query_count, coverage, cache_entries = await engine._apply_cross_datasource_lookups(
        state,
        "catalog",
        "SELECT p.id FROM public.products AS p WHERE p.name = 'Lantern'",
        [CrossDatasourceLookup(
            datasource_id="analytics",
            relation_id="rel_item",
            sql="SELECT DISTINCT item_id AS relationship_key FROM public.sales_events",
            purpose="Find catalog items with recorded sales.",
        )],
        metadata,
    )

    assert "p.id IN ('P162')" in sql
    assert registry.analytics.queries == [
        "SELECT DISTINCT item_id AS relationship_key FROM public.sales_events LIMIT 500"
    ]
    assert steps[0].datasource_id == "analytics"
    assert query_count == 1
    assert coverage[0].matched_records == 1
    assert len(cache_entries) == 1

    resumed_state = state.model_copy(update={"cross_datasource_lookup_cache": cache_entries})
    _, _, reused_query_count, _, reused_cache_entries = await engine._apply_cross_datasource_lookups(
        resumed_state,
        "catalog",
        "SELECT p.id FROM public.products AS p WHERE p.name = 'Lantern'",
        [CrossDatasourceLookup(
            datasource_id="analytics",
            relation_id="rel_item",
            sql="SELECT DISTINCT item_id AS relationship_key FROM public.sales_events",
            purpose="Find catalog items with recorded sales.",
        )],
        metadata,
    )

    assert reused_query_count == 0
    assert reused_cache_entries == []
    assert registry.analytics.queries == [
        "SELECT DISTINCT item_id AS relationship_key FROM public.sales_events LIMIT 500"
    ]
