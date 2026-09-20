import pytest
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
                {"id": "hyp_stock", "description": "Stock availability constrained sales", "category": "inventory", "confidence": 0.45},
                {"id": "hyp_demand", "description": "Demand declined", "category": "demand", "confidence": 0.35},
            ]},
            InvestigationPlan: {"step": {"iteration": 0, "hypothesis_id": "hyp_stock", "action": "compare revenue and stock", "datasource_id": "analytics", "rationale": "Tests supply before demand", "expected_information_gain": 0.9}},
            QueryPlan: {"sql": "select region, revenue, available_units from sales_events", "purpose": "Measure revenue beside stock availability"},
            EvidenceAssessment: {"evidence": [{"id": "ev_stock", "description": "Low available units coincide with lower revenue", "source": "analytics", "relationship": "direct", "confidence": 0.86}], "hypotheses": [
                {"id": "hyp_stock", "description": "Stock availability constrained sales", "category": "inventory", "confidence": 0.86, "status": "supported"},
                {"id": "hyp_demand", "description": "Demand declined", "category": "demand", "confidence": 0.12, "status": "rejected"},
            ], "confidence": 0.86},
            InvestigationDecision: {"action": "finish", "reason": "The fixture scenario has sufficient direct evidence."},
            Synthesis: {"analysis": {"likely_root_cause": "Stock availability constrained sales", "confidence": 0.86, "evidence": [{"id": "ev_stock", "description": "Low available units coincide with lower revenue", "source": "analytics", "relationship": "direct", "confidence": 0.86}], "rejected_hypotheses": [], "external_findings": [], "caveats": [], "summary": "Inventory is the leading explanation."}},
        }
        return response_model.model_validate(responses[response_model])


@pytest.mark.asyncio
async def test_internal_investigation_runs_from_plan_to_evidence() -> None:
    registry = FakeRegistry()
    engine = InvestigationEngine(registry, llm=FixtureLLM())
    result = await engine.graph.ainvoke(
        InvestigationState(
            investigation_id="inv_fixture",
            question="Why did northern revenue fall?",
            datasources=[registry.source.summary],
        )
    )
    state = InvestigationState.model_validate(result)

    assert state.status.value == "completed"
    assert state.final_analysis and state.final_analysis.likely_root_cause == "Stock availability constrained sales"
    assert state.hypotheses[0].status.value == "supported"
    assert state.observations[-1].value["deterministic_summary"]["numeric_columns"]["revenue"]["sum"] == 120.0
    assert registry.source.queries == ["SELECT region, revenue, available_units FROM sales_events LIMIT 500"]
