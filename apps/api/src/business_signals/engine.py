from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from langgraph.graph import END, START, StateGraph

from business_signals.analytics import summarize_query_rows
from business_signals.datasources.registry import DatasourceRegistry
from business_signals.datasources.safety import validate_read_query
from business_signals.llm import (
    LLM,
    EvidenceAssessment,
    HypothesisPlan,
    InvestigationDecision,
    InvestigationPlan,
    OpenAICompatibleLLM,
    QueryPlan,
    QuestionUnderstanding,
    Synthesis,
)
from business_signals.models import (
    HypothesisStatus,
    InvestigationEvent,
    InvestigationState,
    InvestigationStatus,
    Observation,
)

EventSink = Callable[[InvestigationEvent], Awaitable[None]]


class InvestigationEngine:
    """Reusable cyclic LangGraph engine, independent of HTTP and frontend concerns."""

    def __init__(
        self,
        registry: DatasourceRegistry,
        llm: LLM | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        self.registry = registry
        self.llm = llm or OpenAICompatibleLLM()
        self.event_sink = event_sink
        self.graph = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(InvestigationState)
        graph.add_node("understand", self._understand)
        graph.add_node("generate_hypotheses", self._generate_hypotheses)
        graph.add_node("select_investigation", self._select_investigation)
        graph.add_node("execute", self._execute)
        graph.add_node("interpret", self._interpret)
        graph.add_node("decide", self._decide)
        graph.add_node("synthesize", self._synthesize)

        graph.add_edge(START, "understand")
        graph.add_edge("understand", "generate_hypotheses")
        graph.add_edge("generate_hypotheses", "select_investigation")
        graph.add_edge("select_investigation", "execute")
        graph.add_edge("execute", "interpret")
        graph.add_edge("interpret", "decide")
        graph.add_conditional_edges(
            "decide",
            self._route,
            {
                "continue": "select_investigation",
                "finish": "synthesize",
            },
        )
        graph.add_edge("synthesize", END)
        return graph.compile()

    async def _emit(self, state: InvestigationState, event_type: str, message: str, **data: Any) -> None:
        if self.event_sink:
            await self.event_sink(
                InvestigationEvent(
                    investigation_id=state.investigation_id,
                    type=event_type,
                    message=message,
                    data=data,
                )
            )

    async def _metadata_payload(self, state: InvestigationState) -> list[dict[str, Any]]:
        payload = []
        for summary in state.datasources:
            metadata = await self.registry.metadata(summary.id)
            payload.append(
                {
                    "id": summary.id,
                    "name": summary.name,
                    "type": summary.type,
                    "tables": [
                        {
                            "name": f"{table.schema_name}.{table.name}",
                            "columns": [{"name": c.name, "type": c.data_type, "pk": c.primary_key} for c in table.columns],
                            "foreign_keys": table.foreign_keys,
                            "rows": table.approximate_rows,
                            "hypertable": table.is_hypertable,
                            "time_column": table.time_column,
                        }
                        for table in metadata.structural_metadata
                    ],
                    "semantics": metadata.semantic_metadata,
                }
            )
        return payload

    def _state_payload(self, state: InvestigationState) -> dict[str, Any]:
        return {
            "question": state.question,
            "metric": state.metric_definition.model_dump(mode="json") if state.metric_definition else None,
            "hypotheses": [h.model_dump(mode="json") for h in state.hypotheses],
            "evidence": [e.model_dump(mode="json") for e in state.evidence],
            "external_findings": [f.model_dump(mode="json") for f in state.external_findings],
            "history": [s.model_dump(mode="json") for s in state.investigation_history],
            "human_feedback": [f.model_dump(mode="json") for f in state.human_feedback],
            "iteration": state.iteration,
        }

    async def _understand(self, state: InvestigationState) -> dict[str, Any]:
        await self._emit(state, "InvestigationStarted", "Investigation started")
        metadata = await self._metadata_payload(state)
        for source in metadata:
            await self._emit(
                state,
                "SchemaDiscovered",
                f"Loaded cached structure for {source['name']}",
                datasource_id=source["id"],
                table_count=len(source["tables"]),
            )
        result = await self.llm.structured(
            QuestionUnderstanding,
            "Identify the metric, comparison period, dimensions, and any ambiguity. Do not resolve ambiguous business terms by guessing.",
            {
                "question": state.question,
                "datasources": metadata,
            },
        )
        observations = [Observation(description=item, source="question") for item in result.observations]
        return {
            "metric_definition": result.metric_definition,
            "observations": observations,
            "pending_human_question": result.ambiguity,
            "status": InvestigationStatus.RUNNING,
        }

    async def _generate_hypotheses(self, state: InvestigationState) -> dict[str, Any]:
        result = await self.llm.structured(
            HypothesisPlan,
            "Generate 3 to 5 distinct, testable competing hypotheses. Include internal and external possibilities only when plausible.",
            {**self._state_payload(state), "datasources": await self._metadata_payload(state)},
        )
        for hypothesis in result.hypotheses:
            await self._emit(
                state,
                "HypothesisCreated",
                hypothesis.description,
                hypothesis=hypothesis.model_dump(mode="json"),
            )
        return {"hypotheses": result.hypotheses}

    async def _select_investigation(self, state: InvestigationState) -> dict[str, Any]:
        result = await self.llm.structured(
            InvestigationPlan,
            "Choose the single highest-information-gain internal investigation. Do not just pick the next hypothesis in list order. Route it to the most suitable datasource.",
            {**self._state_payload(state), "datasources": await self._metadata_payload(state)},
        )
        step = result.step.model_copy(update={"iteration": state.iteration + 1})
        if step.datasource_id not in {source.id for source in state.datasources}:
            raise ValueError("The planner selected a datasource outside this investigation")
        if step.hypothesis_id not in {hypothesis.id for hypothesis in state.hypotheses}:
            raise ValueError("The planner selected an unknown hypothesis")
        await self._emit(
            state,
            "DatasourceSelected",
            step.rationale,
            datasource_id=step.datasource_id,
            hypothesis_id=step.hypothesis_id,
            expected_information_gain=step.expected_information_gain,
        )
        return {"pending_step": step, "current_focus": step.hypothesis_id, "iteration": state.iteration + 1}

    async def _execute(self, state: InvestigationState) -> dict[str, Any]:
        if state.pending_step is None:
            raise RuntimeError("No investigation step was selected")
        result = await self.llm.structured(
            QueryPlan,
            "Write one focused read-only PostgreSQL query for this investigation. Always qualify ambiguous columns and keep the result compact.",
            {"step": state.pending_step.model_dump(mode="json"), "datasources": await self._metadata_payload(state)},
        )
        datasource_id = state.pending_step.datasource_id
        if datasource_id is None:
            raise RuntimeError("The selected investigation has no datasource")
        sql = validate_read_query(result.sql)
        await self._emit(state, "QueryStarted", result.purpose, datasource_id=datasource_id)
        rows = await self.registry.get(datasource_id).execute_read_query(sql)
        deterministic_summary = summarize_query_rows(rows)
        await self._emit(
            state,
            "QueryCompleted",
            f"Query returned {len(rows)} rows",
            datasource_id=datasource_id,
            row_count=len(rows),
        )
        step = state.pending_step.model_copy(update={"query": sql})
        observation = Observation(
            description=result.purpose,
            value={"rows": rows[:100], "deterministic_summary": deterministic_summary},
            source=datasource_id,
        )
        return {
            "pending_step": step,
            "observations": [*state.observations, observation],
            "investigation_history": [*state.investigation_history, step],
            "query_count": state.query_count + 1,
        }

    async def _interpret(self, state: InvestigationState) -> dict[str, Any]:
        latest = state.observations[-1] if state.observations else None
        result = await self.llm.structured(
            EvidenceAssessment,
            "Interpret the latest deterministic query result. Add only evidence visible in the supplied rows. Update every hypothesis affected and distinguish direct, supporting, correlated, and contradicting evidence.",
            {**self._state_payload(state), "latest_observation": latest.model_dump(mode="json") if latest else None},
        )
        known_evidence = {item.id for item in state.evidence}
        new_evidence = [item for item in result.evidence if item.id not in known_evidence]
        for item in new_evidence:
            await self._emit(state, "EvidenceFound", item.description, evidence=item.model_dump(mode="json"))
        for hypothesis in result.hypotheses:
            await self._emit(
                state,
                "HypothesisUpdated",
                hypothesis.description,
                hypothesis=hypothesis.model_dump(mode="json"),
            )
        return {
            "evidence": [*state.evidence, *new_evidence],
            "hypotheses": result.hypotheses,
            "confidence": result.confidence,
            "pending_human_question": result.ambiguity,
        }

    def _budget_exhausted(self, state: InvestigationState) -> bool:
        elapsed = (datetime.now(timezone.utc) - state.started_at).total_seconds()
        return (
            state.iteration >= state.limits.max_iterations
            or state.query_count >= state.limits.max_sql_queries
            or elapsed >= state.limits.max_duration_seconds
        )

    async def _decide(self, state: InvestigationState) -> dict[str, Any]:
        if self._budget_exhausted(state):
            return {"next_action": "finish"}
        supported = [h for h in state.hypotheses if h.status in {HypothesisStatus.SUPPORTED, HypothesisStatus.CONFIRMED}]
        if supported and (state.confidence or 0) >= 0.78 and len(state.evidence) >= 2:
            return {"next_action": "finish"}

        result = await self.llm.structured(
            InvestigationDecision,
            "Decide whether another internal investigation is needed or whether the available evidence is sufficient to finish.",
            self._state_payload(state),
        )
        return {"next_action": result.action, "status": InvestigationStatus.RUNNING}

    def _route(self, state: InvestigationState) -> str:
        return "finish" if state.next_action == "finish" else "continue"

    async def _synthesize(self, state: InvestigationState) -> dict[str, Any]:
        result = await self.llm.structured(
            Synthesis,
            "Produce a concise evidence-backed final analysis. Do not claim causation from correlation. If evidence is insufficient, likely_root_cause must be null and explain the leading possibilities and missing evidence.",
            self._state_payload(state),
        )
        analysis = result.analysis
        exhausted = self._budget_exhausted(state)
        if analysis.confidence < 0.55 or not state.evidence:
            analysis = analysis.model_copy(
                update={
                    "likely_root_cause": None,
                    "caveats": [*analysis.caveats, "A definitive root cause could not be established within the investigation budget."],
                }
            )
        status = InvestigationStatus.INSUFFICIENT_EVIDENCE if analysis.likely_root_cause is None else InvestigationStatus.COMPLETED
        await self._emit(
            state,
            "InvestigationCompleted",
            analysis.summary,
            status=status,
            budget_exhausted=exhausted,
            final_analysis=analysis.model_dump(mode="json"),
        )
        return {"final_analysis": analysis, "confidence": analysis.confidence, "status": status}
