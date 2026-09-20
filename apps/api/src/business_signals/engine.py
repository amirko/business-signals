from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from sqlalchemy.exc import ProgrammingError
from langgraph.types import interrupt

from business_signals.analytics import summarize_query_rows
from business_signals.datasources.registry import DatasourceRegistry
from business_signals.datasources.safety import (
    UnsafeQueryError,
    query_references_table,
    validate_query_tables,
    validate_read_query,
)
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
    DatasourceSummary,
    DatasourceType,
    Evidence,
    EvidenceDataPoint,
    ExternalFinding,
    FinalAnalysis,
    HumanFeedback,
    Hypothesis,
    HypothesisStatus,
    InvestigationEvent,
    InvestigationLimits,
    InvestigationState,
    InvestigationStatus,
    InvestigationStep,
    MetricDefinition,
    Observation,
)

EventSink = Callable[[InvestigationEvent], Awaitable[None]]
logger = logging.getLogger("uvicorn.error")


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
        graph.add_node("request_human", self._request_human)
        graph.add_node("generate_hypotheses", self._generate_hypotheses)
        graph.add_node("select_investigation", self._select_investigation)
        graph.add_node("execute", self._execute)
        graph.add_node("interpret", self._interpret)
        graph.add_node("decide", self._decide)
        graph.add_node("synthesize", self._synthesize)

        graph.add_edge(START, "understand")
        graph.add_conditional_edges(
            "understand",
            self._route_after_understanding,
            {"request_human": "request_human", "generate_hypotheses": "generate_hypotheses"},
        )
        graph.add_conditional_edges(
            "request_human",
            self._route_after_human,
            {
                "generate_hypotheses": "generate_hypotheses",
                "select_investigation": "select_investigation",
            },
        )
        graph.add_edge("generate_hypotheses", "select_investigation")
        graph.add_edge("select_investigation", "execute")
        graph.add_edge("execute", "interpret")
        graph.add_conditional_edges(
            "interpret",
            self._route_after_interpretation,
            {"request_human": "request_human", "decide": "decide"},
        )
        graph.add_conditional_edges(
            "decide",
            self._route,
            {
                "continue": "select_investigation",
                "finish": "synthesize",
            },
        )
        graph.add_edge("synthesize", END)
        checkpoint_serde = JsonPlusSerializer(
            allowed_msgpack_modules=[
                DatasourceType,
                DatasourceSummary,
                Evidence,
                EvidenceDataPoint,
                ExternalFinding,
                FinalAnalysis,
                HumanFeedback,
                Hypothesis,
                HypothesisStatus,
                InvestigationLimits,
                InvestigationState,
                InvestigationStatus,
                InvestigationStep,
                MetricDefinition,
                Observation,
            ]
        )
        return graph.compile(checkpointer=InMemorySaver(serde=checkpoint_serde))

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

    def _metric_table(self, state: InvestigationState, metadata: list[dict[str, Any]]) -> tuple[str, str] | None:
        """Find the discovered table named in the LLM's metric expression, if any."""
        if state.metric_definition is None:
            return None
        expression = state.metric_definition.expression.lower()
        for source in metadata:
            for table in source["tables"]:
                table_name = table["name"]
                bare_name = table_name.rsplit(".", 1)[-1]
                if f"{bare_name}." in expression or table_name.lower() in expression:
                    return source["id"], table_name
        return None

    @staticmethod
    def _has_measured_table(state: InvestigationState, table_name: str) -> bool:
        bare_name = table_name.rsplit(".", 1)[-1].lower()
        return any(bare_name in (step.query or "").lower() for step in state.investigation_history)

    def _resolve_hypothesis_name(self, candidate: str, state: InvestigationState) -> str | None:
        """Resolve a name through the current investigation's isolated name index."""
        return state.hypothesis_name_index.get(candidate.strip().casefold())

    @staticmethod
    def _scope_evidence(state: InvestigationState, assessment: EvidenceAssessment) -> tuple[list[Any], set[str]]:
        """Keep investigation-level caveats out of every hypothesis' evidence tree."""
        known_hypothesis_ids = {hypothesis.id for hypothesis in state.hypotheses}
        scoped_evidence = []
        affected_hypothesis_ids: set[str] = set()
        for evidence in assessment.evidence:
            hypothesis_ids = list(dict.fromkeys(
                hypothesis_id for hypothesis_id in evidence.hypothesis_ids if hypothesis_id in known_hypothesis_ids
            ))
            if len(known_hypothesis_ids) > 1 and set(hypothesis_ids) == known_hypothesis_ids:
                logger.warning(
                    "Evidence %s was linked to every hypothesis; treating it as an investigation-level caveat",
                    evidence.id,
                )
                hypothesis_ids = []
            scoped = evidence.model_copy(update={"hypothesis_ids": hypothesis_ids})
            scoped_evidence.append(scoped)
            affected_hypothesis_ids.update(hypothesis_ids)
        return scoped_evidence, affected_hypothesis_ids

    @staticmethod
    def _merge_hypothesis_updates(
        state: InvestigationState, assessment: EvidenceAssessment, affected_hypothesis_ids: set[str]
    ) -> list[Any]:
        """Only let evidence change the hypotheses it explicitly evaluates."""
        updates_by_id = {hypothesis.id: hypothesis for hypothesis in assessment.hypotheses}
        return [
            updates_by_id[hypothesis.id] if hypothesis.id in affected_hypothesis_ids and hypothesis.id in updates_by_id else hypothesis
            for hypothesis in state.hypotheses
        ]

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
            "human_resume_node": "generate_hypotheses" if result.ambiguity else None,
            "status": InvestigationStatus.WAITING_FOR_HUMAN if result.ambiguity else InvestigationStatus.RUNNING,
        }

    @staticmethod
    def _route_after_understanding(state: InvestigationState) -> str:
        return "request_human" if state.pending_human_question else "generate_hypotheses"

    @staticmethod
    def _route_after_human(state: InvestigationState) -> str:
        return state.human_resume_node or "select_investigation"

    @staticmethod
    def _route_after_interpretation(state: InvestigationState) -> str:
        return "request_human" if state.pending_human_question else "decide"

    async def _request_human(self, state: InvestigationState) -> dict[str, Any]:
        question = state.pending_human_question
        if not question:
            return {"status": InvestigationStatus.RUNNING}
        response = interrupt({"question": question})
        return {
            "human_feedback": [
                *state.human_feedback,
                HumanFeedback(question=question, response=str(response)),
            ],
            "pending_human_question": None,
            "status": InvestigationStatus.RUNNING,
        }

    async def _generate_hypotheses(self, state: InvestigationState) -> dict[str, Any]:
        result = await self.llm.structured(
            HypothesisPlan,
            (
                "Generate 3 to 5 distinct, testable competing hypotheses. Identify them sequentially as "
                "H1, H2, and so on. Give each a concise, unique name suitable for a UI label. Include "
                "internal and external possibilities only when plausible."
            ),
            {**self._state_payload(state), "datasources": await self._metadata_payload(state)},
        )
        for hypothesis in result.hypotheses:
            await self._emit(
                state,
                "HypothesisCreated",
                hypothesis.description,
                hypothesis=hypothesis.model_dump(mode="json"),
            )
        hypothesis_name_index = {hypothesis.name.casefold(): hypothesis.id for hypothesis in result.hypotheses}
        if len(hypothesis_name_index) != len(result.hypotheses):
            raise ValueError("Generated hypotheses must have unique names")
        return {"hypotheses": result.hypotheses, "hypothesis_name_index": hypothesis_name_index}

    async def _select_investigation(self, state: InvestigationState) -> dict[str, Any]:
        metadata = await self._metadata_payload(state)
        metric_table = self._metric_table(state, metadata)
        must_measure_metric = bool(
            state.investigation_history
            and metric_table
            and not self._has_measured_table(state, metric_table[1])
        )
        result = await self.llm.structured(
            InvestigationPlan,
            (
                "Choose the single highest-information-gain internal investigation. Do not just pick "
                "the next hypothesis in list order. Route it to the most suitable datasource. If a "
                "needed filter or lookup (such as resolving product names to IDs) exists only in one "
                "datasource, investigate that datasource first; never plan a cross-database SQL join. "
                "Before testing secondary causes, confirm the reported business metric with a before/after "
                "comparison in its metric table. Select a hypothesis by its exact supplied name and use an "
                "exact datasource ID."
            ),
            {
                **self._state_payload(state),
                "datasources": metadata,
                "required_metric_measurement": metric_table[1] if must_measure_metric and metric_table else None,
            },
        )
        planned_step = result.step
        resolved_hypothesis_id = self._resolve_hypothesis_name(planned_step.hypothesis_name, state)
        valid_datasource_ids = {source.id for source in state.datasources}
        if resolved_hypothesis_id is None or planned_step.datasource_id not in valid_datasource_ids:
            logger.warning("Planner returned an invalid hypothesis name or datasource ID; requesting a corrected plan")
            correction = await self.llm.structured(
                InvestigationPlan,
                "Correct the plan. Select one exact hypothesis name and one exact datasource ID from the supplied lists.",
                {
                    **self._state_payload(state),
                    "datasources": metadata,
                    "valid_hypothesis_names": list(state.hypothesis_name_index),
                    "valid_datasource_ids": [source.id for source in state.datasources],
                },
            )
            planned_step = correction.step
            resolved_hypothesis_id = self._resolve_hypothesis_name(planned_step.hypothesis_name, state)
        if resolved_hypothesis_id is None:
            resolved_hypothesis_id = max(state.hypotheses, key=lambda hypothesis: hypothesis.confidence).id
            logger.error("Planner correction still had an invalid hypothesis name; using the leading active hypothesis")
        datasource_id = planned_step.datasource_id
        if datasource_id not in valid_datasource_ids:
            fallback_datasource_id = metric_table[0] if metric_table else state.datasources[0].id
            datasource_id = fallback_datasource_id
            logger.error("Planner correction still had an invalid datasource ID; using a valid datasource")
        step = InvestigationStep(
            iteration=state.iteration + 1,
            hypothesis_id=resolved_hypothesis_id,
            action=planned_step.action,
            datasource_id=datasource_id,
            rationale=planned_step.rationale,
            expected_information_gain=planned_step.expected_information_gain,
        )
        if must_measure_metric and metric_table:
            step = step.model_copy(
                update={
                    "datasource_id": metric_table[0],
                    "action": f"Measure the reported metric in {metric_table[1]} before and after the stated change.",
                }
            )
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
        datasource_id = state.pending_step.datasource_id
        if datasource_id is None:
            raise RuntimeError("The selected investigation has no datasource")
        selected_datasource = [
            source for source in await self._metadata_payload(state) if source["id"] == datasource_id
        ]
        if not selected_datasource:
            raise ValueError("The selected investigation datasource has no discovered schema")
        allowed_tables = {table["name"] for table in selected_datasource[0]["tables"]}
        metric_table = self._metric_table(state, selected_datasource)
        must_measure_metric = bool(
            state.investigation_history
            and metric_table
            and not self._has_measured_table(state, metric_table[1])
        )
        result = await self.llm.structured(
            QueryPlan,
            (
                "Write one focused, executable read-only PostgreSQL query for this investigation. "
                "Always qualify ambiguous columns and keep the result compact. At any query level, "
                "never use a SELECT-list alias inside another expression in that same SELECT's ORDER "
                "BY, WHERE, HAVING, or SELECT list; use the original expression or an outer query instead. "
                "The supplied schema is for the selected datasource only: use no other database or table. "
                + (
                    f"This query must measure before/after change from {metric_table[1]}."
                    if must_measure_metric and metric_table
                    else ""
                )
            ),
            {
                "step": state.pending_step.model_dump(mode="json"),
                "datasources": selected_datasource,
                "investigation_context": self._state_payload(state),
            },
        )
        sql = result.sql
        for attempt in range(2):
            try:
                sql = validate_query_tables(
                    validate_read_query(result.sql),
                    allowed_tables,
                )
                if must_measure_metric and metric_table and not query_references_table(sql, metric_table[1]):
                    raise UnsafeQueryError(f"Query must measure the reported metric from {metric_table[1]}")
                await self._emit(
                    state,
                    "QueryStarted",
                    result.purpose,
                    datasource_id=datasource_id,
                    hypothesis_id=state.pending_step.hypothesis_id,
                )
                rows = await self.registry.get(datasource_id).execute_read_query(sql)
                break
            except (ProgrammingError, UnsafeQueryError):
                if attempt:
                    raise
                logger.warning(
                    "Generated query was rejected; requesting one corrected query: datasource=%s",
                    datasource_id,
                )
                await self._emit(
                    state,
                    "QueryRejected",
                    "The generated query was rejected; revising it once before continuing.",
                    datasource_id=datasource_id,
                    hypothesis_id=state.pending_step.hypothesis_id,
                )
                result = await self.llm.structured(
                    QueryPlan,
                    (
                        "Correct the previous query. It was rejected because it has an undefined identifier "
                        "or invalid SQL structure. Re-check every table and column against the supplied "
                        "schema. Do not use a SELECT-list alias inside another expression at the same query "
                        "level. Return one executable read-only PostgreSQL SELECT. "
                        + (
                            f"The correction must measure before/after change from {metric_table[1]}."
                            if must_measure_metric and metric_table
                            else ""
                        )
                    ),
                    {
                        "step": state.pending_step.model_dump(mode="json"),
                        "datasources": selected_datasource,
                        "previous_query": sql,
                        "investigation_context": self._state_payload(state),
                    },
                )
        else:  # pragma: no cover - the loop either breaks or re-raises
            raise RuntimeError("A query result was not produced")
        deterministic_summary = summarize_query_rows(rows)
        await self._emit(
            state,
            "QueryCompleted",
            f"Query returned {len(rows)} rows",
            datasource_id=datasource_id,
            hypothesis_id=state.pending_step.hypothesis_id,
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
            (
                "Interpret the latest deterministic query result. Add only evidence visible in the supplied "
                "rows. Link an evidence item only to the exact hypothesis IDs it actually evaluates; most "
                "evidence should link to one hypothesis, never copy it across all hypotheses. For a data "
                "coverage, ingestion, or other investigation-wide limitation, use an empty hypothesis_ids "
                "list so it remains a caveat rather than hypothesis evidence. Update confidence or status "
                "only for hypotheses linked to returned evidence, and distinguish direct, supporting, "
                "correlated, and contradicting evidence. A lookup that resolves names to IDs is supporting "
                "context, not direct causal evidence."
            ),
            {**self._state_payload(state), "latest_observation": latest.model_dump(mode="json") if latest else None},
        )
        scoped_evidence, affected_hypothesis_ids = self._scope_evidence(state, result)
        hypotheses = self._merge_hypothesis_updates(state, result, affected_hypothesis_ids)
        known_evidence = {item.id for item in state.evidence}
        new_evidence = [item for item in scoped_evidence if item.id not in known_evidence]
        for item in new_evidence:
            await self._emit(state, "EvidenceFound", item.description, evidence=item.model_dump(mode="json"))
        for hypothesis in hypotheses:
            if hypothesis.id not in affected_hypothesis_ids:
                continue
            await self._emit(
                state,
                "HypothesisUpdated",
                hypothesis.description,
                hypothesis=hypothesis.model_dump(mode="json"),
            )
        return {
            "evidence": [*state.evidence, *new_evidence],
            "hypotheses": hypotheses,
            "confidence": result.confidence,
            "pending_human_question": result.ambiguity,
            "human_resume_node": "select_investigation" if result.ambiguity else None,
            "status": (
                InvestigationStatus.WAITING_FOR_HUMAN
                if result.ambiguity
                else InvestigationStatus.RUNNING
            ),
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
        supported = [
            h for h in state.hypotheses if h.status in {HypothesisStatus.SUPPORTED, HypothesisStatus.CONFIRMED}
        ]
        has_high_confidence_root = any(hypothesis.confidence >= 0.78 for hypothesis in supported)
        has_direct_evidence = any(
            evidence.relationship == "direct"
            and any(hypothesis.id in evidence.hypothesis_ids for hypothesis in supported)
            for evidence in state.evidence
        )
        if has_high_confidence_root and has_direct_evidence:
            return {"next_action": "finish"}

        result = await self.llm.structured(
            InvestigationDecision,
            "Decide whether another internal investigation is needed or whether the available evidence is sufficient to finish.",
            self._state_payload(state),
        )
        if result.action == "finish" and not (has_high_confidence_root and has_direct_evidence):
            logger.info("Continuing investigation because finality criteria have not been met")
            return {"next_action": "continue", "status": InvestigationStatus.RUNNING}
        return {"next_action": result.action, "status": InvestigationStatus.RUNNING}

    def _route(self, state: InvestigationState) -> str:
        return "finish" if state.next_action == "finish" else "continue"

    async def _synthesize(self, state: InvestigationState) -> dict[str, Any]:
        result = await self.llm.structured(
            Synthesis,
            (
                "Produce a concise evidence-backed final analysis. Do not claim causation from correlation. "
                "When a high-confidence supported hypothesis has direct evidence, identify it as the likely "
                "root cause and state its caveats. If evidence is insufficient, likely_root_cause must be null "
                "and explain the leading possibilities and missing evidence. Evidence with an empty "
                "hypothesis_ids list is an investigation-level data-quality or coverage caveat: include it in "
                "caveats, never treat it as support or contradiction for a root-cause hypothesis."
            ),
            self._state_payload(state),
        )
        analysis = result.analysis
        exhausted = self._budget_exhausted(state)
        supported_with_direct_evidence = [
            hypothesis
            for hypothesis in state.hypotheses
            if hypothesis.status in {HypothesisStatus.SUPPORTED, HypothesisStatus.CONFIRMED}
            and hypothesis.confidence >= 0.78
            and any(
                evidence.relationship == "direct" and hypothesis.id in evidence.hypothesis_ids
                for evidence in state.evidence
            )
        ]
        if analysis.likely_root_cause is None and supported_with_direct_evidence:
            leading_hypothesis = max(supported_with_direct_evidence, key=lambda hypothesis: hypothesis.confidence)
            analysis = analysis.model_copy(update={"likely_root_cause": leading_hypothesis.description})
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
