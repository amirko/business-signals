import pytest
from langgraph.types import Command
from sqlalchemy.exc import ProgrammingError
from business_signals.datasources.safety import UnsafeQueryError
from business_signals.datasources.registry import DatasourceRegistry
from business_signals.engine import InvestigationEngine
from business_signals.llm import (
    DirectAnswerPlan,
    DirectAnswerQuery,
    EvidenceAssessment,
    HypothesisPlan,
    InvestigationDecision,
    InvestigationPlan,
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
    InvestigationState,
    HumanFeedback,
    Observation,
    TableMetadata,
)


def test_graph_has_real_investigation_cycles() -> None:
    graph = InvestigationEngine(DatasourceRegistry()).graph.get_graph()
    edges = {(edge.source, edge.target) for edge in graph.edges}
    assert ("decide", "select_investigation") in edges
    assert ("synthesize", "__end__") in edges
    assert ("understand", "request_human") in edges


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

    async def infer_cross_relations(self, datasource_ids: list[str]) -> list[CrossDatasourceRelation]:
        assert datasource_ids == ["analytics", "catalog"]
        return [
            CrossDatasourceRelation(
                source_datasource="analytics",
                source_field="public.sales.item_code",
                target_datasource="catalog",
                target_field="public.items.item_code",
                confidence=1,
                origin="explicit",
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

    resolved, _, query_count = await engine._resolve_display_names(
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

    resolved, step, query_count = await engine._resolve_display_names(
        InvestigationState(investigation_id="inv_lookup_failure", question="Top items", datasources=[]),
        metadata,
        "analytics",
        "SELECT item_code, SUM(units) AS total_units_sold FROM public.sales GROUP BY item_code",
        [{"item_code": "IT-007", "total_units_sold": 717}],
    )

    assert resolved == [{"total_units_sold": 717}]
    assert step is None
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

    async def infer_cross_relations(self, datasource_ids: list[str]) -> list[CrossDatasourceRelation]:
        return []


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

    async def infer_cross_relations(self, datasource_ids: list[str]) -> list[CrossDatasourceRelation]:
        return []


class FixtureLLM:
    async def structured(self, response_model, instruction: str, payload: dict):
        responses = {
            QuestionUnderstanding: {"metric_definition": {"name": "revenue", "expression": "SUM(revenue)", "unit": "EUR", "confidence": 0.95}, "observations": ["Northern revenue declined"]},
            HypothesisPlan: {"hypotheses": [
                {"id": "hyp_stock", "name": "Inventory constraint", "description": "Stock availability constrained sales", "category": "inventory", "confidence": 0.45},
                {"id": "hyp_demand", "name": "Demand decline", "description": "Demand declined", "category": "demand", "confidence": 0.35},
            ]},
            InvestigationPlan: {"step": {"iteration": 0, "hypothesis_name": "Inventory constraint", "action": "compare revenue and stock", "datasource_id": "analytics", "rationale": "Tests supply before demand", "expected_information_gain": 0.9}},
            QueryPlan: {"sql": "select region, revenue, available_units from sales_events", "purpose": "Measure revenue beside stock availability"},
            EvidenceAssessment: {"evidence": [{"id": "ev_stock", "description": "Low available units coincide with lower revenue", "source": "analytics", "relationship": "direct", "confidence": 0.86, "hypothesis_ids": ["hyp_stock"]}], "hypotheses": [
                {"id": "hyp_stock", "name": "Inventory constraint", "description": "Stock availability constrained sales", "category": "inventory", "confidence": 0.86, "status": "supported"},
                {"id": "hyp_demand", "name": "Demand decline", "description": "Demand declined", "category": "demand", "confidence": 0.12, "status": "rejected"},
            ], "confidence": 0.86},
            InvestigationDecision: {"action": "finish", "reason": "The fixture scenario has sufficient direct evidence."},
            Synthesis: {"analysis": {"likely_root_cause": "Stock availability constrained sales", "confidence": 0.86, "evidence": [{"id": "ev_stock", "description": "Low available units coincide with lower revenue", "source": "analytics", "relationship": "direct", "confidence": 0.86}], "rejected_hypotheses": [], "external_findings": [], "caveats": [], "summary": "Inventory is the leading explanation."}},
        }
        return response_model.model_validate(responses[response_model])


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
                }
            )
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

    assert analysis.summary == "1. Charging Hub — 732 total units sold; 2. Lantern — 727 total units sold."
    assert "P003" not in analysis.summary
    assert all(point.value not in {"P003", "IT-007"} for point in analysis.evidence[0].data)
    assert update["status"].value == "completed"


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
        "Would you like to see how sales value changed over time for Charging Hub?"
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
    assert len(completed.investigation_history) == 2
    assert llm.evidence_calls == 2


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


@pytest.mark.asyncio
async def test_direct_evidence_rejecting_every_decline_hypothesis_finishes_with_no_decline() -> None:
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
    assert result["final_analysis"].likely_root_cause == "No overall decline found"
    assert "no decline to explain" in result["final_analysis"].summary


@pytest.mark.asyncio
async def test_negligible_measured_decline_finishes_without_more_clarifications() -> None:
    registry = FakeRegistry()
    engine = InvestigationEngine(registry, llm=FixtureLLM())
    state = InvestigationState.model_validate(
        {
            "investigation_id": "inv_no_material_decline",
            "question": "Did sales decline in the last three months of 2024?",
            "datasources": [registry.source.summary.model_dump()],
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
                "data": [{"field": "percentage_change", "value": -0.09}],
            }],
        }
    )

    decision = await engine._decide(state)
    result = await engine._synthesize(state)

    assert decision["next_action"] == "finish"
    assert result["final_analysis"].likely_root_cause == "No meaningful overall decrease found"
    assert "1.0% negligibility threshold" in result["final_analysis"].summary


class RetryingDatasource(FakeDatasource):
    async def execute_read_query(self, query: str) -> list[dict[str, object]]:
        self.queries.append(query)
        if len(self.queries) == 1:
            raise ProgrammingError(query, {}, Exception("undefined column"))
        return [{"region": "north", "revenue": 80}]


class RetryingRegistry(FakeRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.source = RetryingDatasource()


class RetryingLLM:
    def __init__(self) -> None:
        self.query_calls = 0

    async def structured(self, response_model, instruction: str, payload: dict):
        assert response_model is QueryPlan
        self.query_calls += 1
        sql = "SELECT unknown_column FROM sales_events" if self.query_calls == 1 else "SELECT revenue FROM sales_events"
        return QueryPlan(sql=sql, purpose="Measure revenue")


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
