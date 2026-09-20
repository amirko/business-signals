import pytest
from langgraph.types import Command
from sqlalchemy.exc import ProgrammingError
from business_signals.datasources.registry import DatasourceRegistry
from business_signals.engine import InvestigationEngine
from business_signals.llm import (
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
    DatasourceMetadata,
    DatasourceSummary,
    DatasourceType,
    InvestigationState,
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
                    columns=[ColumnMetadata(name="revenue", data_type="numeric", nullable=False)],
                )
            ],
        )

    async def metadata(self, datasource_id: str) -> DatasourceMetadata:
        assert datasource_id == "analytics"
        return self.metadata_value

    def get(self, datasource_id: str) -> FakeDatasource:
        assert datasource_id == "analytics"
        return self.source


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
