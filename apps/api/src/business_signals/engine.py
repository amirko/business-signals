from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from business_signals.datasources.registry import DatasourceRegistry
from business_signals.external import ExternalResearcher
from business_signals.llm import (
    EvidenceAssessment,
    HypothesisPlan,
    InvestigationDecision,
    InvestigationPlan,
    LLM,
    OpenAICompatibleLLM,
    QueryPlan,
    QuestionUnderstanding,
    Synthesis,
)
from business_signals.models import (
    Evidence,
    FinalAnalysis,
    HumanFeedback,
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
        external: ExternalResearcher | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        self.registry = registry
        self.llm = llm or OpenAICompatibleLLM()
        self.external = external or ExternalResearcher()
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
        graph.add_node("external_research", self._external_research)
        graph.add_node("human_input", self._human_input)
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
                "external": "external_research",
                "hitl": "human_input",
                "finish": "synthesize",
            },
        )
        graph.add_edge("external_research", "decide")
        graph.add_edge("human_input", "select_investigation")
        graph.add_edge("synthesize", END)
        return graph.compile(checkpointer=MemorySaver())

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
                "unconfirmed_cross_datasource_relations": [
                    relation.model_dump(mode="json")
                    for relation in await self.registry.infer_cross_relations([source.id for source in state.datasources])
                ],
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
        allowed_ids = {source.id for source in state.datasources}
        if result.datasource_id not in allowed_ids:
            raise ValueError("The planner selected a datasource outside this investigation")
        await self._emit(state, "QueryStarted", result.purpose, datasource_id=result.datasource_id)
        rows = await self.registry.get(result.datasource_id).execute_read_query(result.sql)
        await self._emit(
            state,
            "QueryCompleted",
            f"Query returned {len(rows)} rows",
            datasource_id=result.datasource_id,
            row_count=len(rows),
        )
        step = state.pending_step.model_copy(update={"datasource_id": result.datasource_id, "query": result.sql})
        observation = Observation(
            description=result.purpose,
            value={"rows": rows[:100], "row_count": len(rows)},
            source=result.datasource_id,
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
            "pending_human_question": result.ambiguity or state.pending_human_question,
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
        if state.pending_human_question:
            return {"next_action": "hitl", "status": InvestigationStatus.WAITING_FOR_HUMAN}
        supported = [h for h in state.hypotheses if h.status in {HypothesisStatus.SUPPORTED, HypothesisStatus.CONFIRMED}]
        if supported and (state.confidence or 0) >= 0.78 and len(state.evidence) >= 2:
            return {"next_action": "finish"}

        result = await self.llm.structured(
            InvestigationDecision,
            "Decide whether to continue internal investigation, selectively call one external specialist, request human clarification, or finish. Prefer internal validation. External research requires a specific plausible mechanism and dates.",
            self._state_payload(state),
        )
        action = result.action
        if action.startswith("external_") and state.external_call_count >= state.limits.max_external_calls:
            action = "continue"
        external_request = None
        if action.startswith("external_"):
            category = action.removeprefix("external_")
            if not (result.external_start_date and result.external_end_date and result.external_location):
                action = "continue"
            else:
                external_request = {
                    "category": category,
                    "subject": result.external_location,
                    "start_date": result.external_start_date,
                    "end_date": result.external_end_date,
                    "context": result.reason,
                }
        return {
            "next_action": action,
            "pending_human_question": result.human_question if action == "hitl" else None,
            "external_request": external_request,
            "status": InvestigationStatus.WAITING_FOR_HUMAN if action == "hitl" else InvestigationStatus.RUNNING,
        }

    def _route(self, state: InvestigationState) -> str:
        if (state.next_action or "continue").startswith("external_"):
            return "external"
        return {"hitl": "hitl", "finish": "finish"}.get(state.next_action or "continue", "continue")

    async def _external_research(self, state: InvestigationState) -> dict[str, Any]:
        request = state.external_request
        if not request:
            return {"next_action": "continue"}
        await self._emit(
            state,
            "ExternalResearchStarted",
            request["context"],
            category=request["category"],
            subject=request["subject"],
        )
        finding = await self.external.research(**request)  # type: ignore[arg-type]
        await self._emit(
            state,
            "ExternalFindingReceived",
            finding.observation,
            finding=finding.model_dump(mode="json"),
        )
        evidence = Evidence(
            description=finding.observation,
            source=finding.source_title,
            relationship=finding.relationship,
            confidence=finding.confidence,
            data={"url": finding.source_url, "period": finding.period},
        )
        return {
            "external_findings": [*state.external_findings, finding],
            "evidence": [*state.evidence, evidence],
            "external_call_count": state.external_call_count + 1,
            "next_action": "continue",
            "external_request": None,
        }

    async def _human_input(self, state: InvestigationState) -> dict[str, Any]:
        question = state.pending_human_question or "What business context should I use?"
        await self._emit(state, "HumanInputRequired", question, question=question)
        response = interrupt({"question": question})
        feedback = HumanFeedback(question=question, response=str(response))
        return {
            "human_feedback": [*state.human_feedback, feedback],
            "pending_human_question": None,
            "next_action": "continue",
            "status": InvestigationStatus.RUNNING,
        }

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
