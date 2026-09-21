from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from sqlalchemy.exc import ProgrammingError
from langgraph.types import interrupt

from business_signals.analytics import summarize_query_rows
from business_signals.config import settings
from business_signals.datasources.registry import DatasourceRegistry
from business_signals.datasources.safety import (
    UnsafeQueryError,
    query_references_table,
    validate_query_tables,
    validate_read_query,
)
from business_signals.llm import (
    LLM,
    DirectAnswerPlan,
    DirectAnswerQuery,
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
    ConversationTurn,
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
        checkpointer: Any | None = None,
    ) -> None:
        self.registry = registry
        self.llm = llm or OpenAICompatibleLLM()
        self.event_sink = event_sink
        self._checkpointer = checkpointer
        self.graph = self._build_graph()

    def set_checkpointer(self, checkpointer: Any) -> None:
        """Rebuild the graph with a durable saver once application startup has connected to PostgreSQL."""
        self._checkpointer = checkpointer
        self.graph = self._build_graph()

    @staticmethod
    def checkpoint_serde() -> JsonPlusSerializer:
        return JsonPlusSerializer(
            allowed_msgpack_modules=[
                DatasourceType,
                DatasourceSummary,
                ConversationTurn,
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

    def _build_graph(self):
        graph = StateGraph(InvestigationState)
        graph.add_node("understand", self._understand)
        graph.add_node("request_human", self._request_human)
        graph.add_node("answer_directly", self._answer_directly)
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
            {
                "request_human": "request_human",
                "answer_directly": "answer_directly",
                "generate_hypotheses": "generate_hypotheses",
            },
        )
        graph.add_conditional_edges(
            "request_human",
            self._route_after_human,
            {
                "generate_hypotheses": "generate_hypotheses",
                "answer_directly": "answer_directly",
                "select_investigation": "select_investigation",
            },
        )
        graph.add_edge("generate_hypotheses", "select_investigation")
        graph.add_edge("answer_directly", "synthesize")
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
        checkpointer = self._checkpointer or InMemorySaver(serde=self.checkpoint_serde())
        return graph.compile(checkpointer=checkpointer)

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
                            "columns": [
                                {"name": c.name, "type": c.data_type, "pk": c.primary_key, "nullable": c.nullable}
                                for c in table.columns
                            ],
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
        history = [s.model_dump(mode="json") for s in state.investigation_history]
        if state.request_type == "direct_answer":
            for step in history:
                step.pop("query", None)
        return {
            "question": state.question,
            "metric": state.metric_definition.model_dump(mode="json") if state.metric_definition else None,
            "hypotheses": [h.model_dump(mode="json") for h in state.hypotheses],
            "evidence": [e.model_dump(mode="json") for e in state.evidence],
            "external_findings": [f.model_dump(mode="json") for f in state.external_findings],
            "history": history,
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

    @staticmethod
    def _premise_is_directly_rejected(
        question: str, hypotheses: list[Hypothesis], evidence: list[Evidence]
    ) -> bool:
        """End a yes/no decline investigation once direct evidence rejects every explanation."""
        asks_about_decline = InvestigationEngine._asks_about_decline(question)
        has_direct_measurement = any(
            item.relationship == "direct" and item.confidence >= 0.75 and item.hypothesis_ids
            for item in evidence
        )
        return bool(hypotheses) and asks_about_decline and has_direct_measurement and all(
            hypothesis.status == HypothesisStatus.REJECTED for hypothesis in hypotheses
        )

    @staticmethod
    def _asks_about_decline(question: str) -> bool:
        decline_terms = ("decline", "declined", "decrease", "decreased", "drop", "dropped", "fall", "fell")
        return any(term in question.casefold() for term in decline_terms)

    @staticmethod
    def _is_direct_answer_request(question: str) -> bool:
        normalized = question.strip().casefold()
        direct_openers = ("what", "which", "who", "when", "where", "how many", "list", "show", "give me")
        direct_terms = ("most popular", "top-selling", "top selling", "highest", "lowest", "total", "count")
        return normalized.startswith(direct_openers) or any(term in normalized for term in direct_terms)

    @staticmethod
    def _has_explicit_all_history_scope(question: str) -> bool:
        """Treat ordinary existence wording as a complete all-recorded-history scope."""
        normalized = question.casefold()
        return any(phrase in normalized for phrase in ("never", " ever ", "at all", "all time", "all history", "all recorded history"))

    @staticmethod
    def _defers_datasource_selection(plan: DirectAnswerPlan) -> bool:
        """A selected source set is an instruction to the planner, never a question for the user."""
        plan_text = " ".join(
            [plan.purpose, plan.sql, *(query.purpose + " " + query.sql for query in plan.supporting_queries)]
        ).casefold()
        return any(phrase in plan_text for phrase in (
            "which datasource", "which data source", "choose a datasource", "choose one:",
            "select a datasource", "datasource selection", "source should i use",
        ))

    @staticmethod
    def _answered_clarifications(state: InvestigationState) -> list[dict[str, str]]:
        """Prior human answers are conversation facts, not fresh ambiguity for each follow-up."""
        return [{"question": item.question, "answer": item.response} for item in state.human_feedback]

    @staticmethod
    def _reasks_answered_sales_metric(state: InvestigationState, ambiguity: str | None) -> bool:
        """Do not let a follow-up re-open an answered units/revenue/orders choice."""
        if not ambiguity:
            return False
        candidate = ambiguity.casefold()
        asks_for_meaning = "when you say" in candidate or "do you mean" in candidate
        mentions_metric_choice = any(word in candidate for word in ("units", "revenue", "orders", "amount spent"))
        if not (asks_for_meaning and mentions_metric_choice and any(word in candidate for word in ("sales", "sold"))):
            return False
        return any(
            any(word in feedback.question.casefold() for word in ("sales", "sold"))
            and any(word in feedback.question.casefold() for word in ("units", "revenue", "orders", "amount"))
            and bool(feedback.response.strip())
            for feedback in state.human_feedback
        )

    @staticmethod
    def _percentage_changes(evidence: list[Evidence]) -> list[float]:
        """Read measured percentage changes from evidence, preferring structured values."""
        changes: list[float] = []
        percentage_field = re.compile(
            r"(?:percentage|percent|pct).*(?:change|difference)|(?:change|difference).*(?:percentage|percent|pct)"
        )
        percentage_value = re.compile(r"([-−]?\d+(?:\.\d+)?)\s*%")
        for item in evidence:
            if item.relationship != "direct" or not item.hypothesis_ids:
                continue
            found_in_data = False
            for point in item.data:
                if percentage_field.search(point.field.casefold()) and isinstance(point.value, (int, float)):
                    changes.append(float(point.value))
                    found_in_data = True
            if not found_in_data:
                for value in percentage_value.findall(item.description):
                    changes.append(float(value.replace("−", "-")))
        return changes

    def _no_material_decline(self, state: InvestigationState) -> bool:
        if not self._asks_about_decline(state.question):
            return False
        changes = self._percentage_changes(state.evidence)
        return bool(changes) and all(change >= -settings.negligiblity_threshold_percent for change in changes)

    @staticmethod
    def _redact_internal_identifier_columns(
        rows: list[dict[str, Any]], identifier_columns: set[str]
    ) -> list[dict[str, Any]]:
        """Keep schema-declared keys inside execution, never in a direct-answer result."""
        return [
            {
                key: value
                for key, value in row.items()
                if key not in identifier_columns
            }
            for row in rows
        ]

    @staticmethod
    def _customer_facing_name(name: Any, identifier: Any) -> str:
        """Drop a display-name suffix only when it duplicates a technical identifier suffix."""
        identifier_suffix = re.search(r"(\d+)$", str(identifier))
        if identifier_suffix:
            return re.sub(rf"\s+{re.escape(identifier_suffix.group(1))}$", "", str(name))
        return str(name)

    @staticmethod
    def _infer_display_column(table: dict[str, Any]) -> str | None:
        """Infer an entity label from schema roles and types, never from a hard-coded column name."""
        key_columns = {
            column["name"] for column in table["columns"] if column.get("pk")
        }
        key_columns.update(
            foreign_key["column_name"] for foreign_key in table.get("foreign_keys", []) if foreign_key.get("column_name")
        )
        textual_columns = [
            column for column in table["columns"]
            if column["name"] not in key_columns
            and any(token in str(column.get("type", "")).casefold() for token in ("char", "text", "string", "citext"))
        ]
        if len(textual_columns) == 1:
            return textual_columns[0]["name"]
        required_textual_columns = [column for column in textual_columns if not column.get("nullable", True)]
        return required_textual_columns[0]["name"] if len(required_textual_columns) == 1 else None

    @staticmethod
    def _safe_direct_answer_text(text: str) -> str:
        """Avoid exposing implementation identifiers in user-visible status messages."""
        return re.sub(r"\b(?:product|item|customer|store)?\s*IDs?\b", "items", text, flags=re.IGNORECASE)

    @staticmethod
    def _named_entity_ranking_analysis(
        state: InvestigationState, draft: FinalAnalysis
    ) -> FinalAnalysis | None:
        """Make a ranking deterministic once the safe lookup has supplied a display name."""
        rows: list[dict[str, Any]] = []
        for observation in reversed(state.observations):
            value = observation.value
            if not isinstance(value, dict) or not isinstance(value.get("rows"), list):
                continue
            candidates = [item for item in value["rows"] if isinstance(item, dict)]
            if candidates and all(item.get("display_name") for item in candidates):
                rows = candidates
                break
        if not rows:
            return None

        metric_names = {
            "total_units_sold": "total units sold",
            "units_sold": "units sold",
            "units": "units sold",
            "total_revenue": "total sales value",
            "revenue": "sales value",
            "order_count": "orders",
            "orders": "orders",
        }
        metric_key = next((key for key in metric_names if any(key in row for row in rows)), None)
        metric_label = metric_names.get(metric_key, "the requested measure")

        def display(value: Any) -> str:
            if isinstance(value, float):
                return f"{value:,.2f}".rstrip("0").rstrip(".")
            if isinstance(value, int):
                return f"{value:,}"
            return str(value)

        ranked_items = []
        data: list[EvidenceDataPoint] = []
        for rank, row in enumerate(rows, start=1):
            name = str(row["display_name"])
            detail = f" — {display(row[metric_key])} {metric_label}" if metric_key and row.get(metric_key) is not None else ""
            ranked_items.append(f"{rank}. {name}{detail}")
            data.append(EvidenceDataPoint(field=f"rank_{rank}_item", value=name))
            if metric_key and row.get(metric_key) is not None:
                data.append(EvidenceDataPoint(field=f"rank_{rank}_{metric_key}", value=row[metric_key]))

        caveats = [
            caveat
            for caveat in draft.caveats
            if "names were not available" not in caveat.casefold()
            and "totals (exact units sold" not in caveat.casefold()
        ]
        return draft.model_copy(
            update={
                "likely_root_cause": f"Top {len(rows)} selling items by {metric_label}",
                "summary": f"{'; '.join(ranked_items)}.",
                "evidence": [
                    Evidence(
                        id="direct_entity_ranking",
                        description=f"The ranking is based on {metric_label} across the requested sales records.",
                        source="selected sales records",
                        relationship="direct",
                        confidence=draft.confidence,
                        data=data,
                    )
                ],
                "caveats": caveats,
            }
        )

    async def _contextual_follow_up_question(self, state: InvestigationState) -> str | None:
        """Offer only a next question that the selected sources can actually answer."""
        result_observation = next(
            (
                observation
                for observation in reversed(state.observations)
                if isinstance(observation.value, dict) and isinstance(observation.value.get("rows"), list)
            ),
            None,
        )
        if result_observation is None:
            return None
        rows = [row for row in result_observation.value["rows"] if isinstance(row, dict)]
        first_display_name = next((str(row["display_name"]) for row in rows if row.get("display_name")), None)
        if first_display_name is None:
            return None
        metadata = await self._metadata_payload(state)
        source = next((item for item in metadata if item["id"] == result_observation.source), None)
        if source is None:
            return None
        columns = {column["name"].casefold() for table in source["tables"] for column in table["columns"]}
        has_time_series = bool(columns & {"occurred_at", "recorded_at", "date", "event_date", "created_at"})
        ranking_terms = ("top", "most", "best", "highest", "popular", "valuable")
        if not has_time_series or not any(term in state.question.casefold() for term in ranking_terms):
            return None
        metric = "sales value" if any(term in state.question.casefold() for term in ("valuable", "value", "revenue")) else "units sold"
        return f"Would you like to see how {metric} changed over time for {first_display_name}?"

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
            (
                "Identify the metric, comparison period, dimensions, and any ambiguity. Do not resolve "
                "ambiguous business terms by guessing. If clarification is needed, ask one short, friendly "
                "business question. For example: 'When you say sales, do you mean the total amount customers "
                "spent, the number of items sold, or the number of orders?' Do not mention formulas, database "
                "fields, tables, or data sources. Set request_type to direct_answer for a factual request such "
                "as a ranking, list, count, total, lookup, or description. Set it to investigation only when "
                "the user asks why a business result happened. Earlier answered clarifications are authoritative: "
                "reuse them for a follow-up unless the user explicitly changes them, and never ask the same "
                "question again."
            ),
            {
                "question": state.question,
                "datasources": metadata,
                "previously_answered_clarifications": self._answered_clarifications(state),
            },
        )
        observations = [Observation(description=item, source="question") for item in result.observations]
        request_type = (
            "direct_answer"
            if state.request_type == "direct_answer" or self._is_direct_answer_request(state.question)
            else result.request_type
        )
        ambiguity = None if request_type == "direct_answer" and self._has_explicit_all_history_scope(state.question) else result.ambiguity
        if request_type == "direct_answer" and self._reasks_answered_sales_metric(state, ambiguity):
            logger.info("Skipping duplicate sales-metric clarification because the conversation already contains an answer")
            ambiguity = None
        return {
            "metric_definition": result.metric_definition,
            "observations": observations,
            "request_type": request_type,
            "pending_human_question": ambiguity,
            "human_resume_node": (
                "answer_directly" if ambiguity and request_type == "direct_answer"
                else "generate_hypotheses" if ambiguity else None
            ),
            "status": InvestigationStatus.WAITING_FOR_HUMAN if ambiguity else InvestigationStatus.RUNNING,
        }

    @staticmethod
    def _route_after_understanding(state: InvestigationState) -> str:
        if state.pending_human_question:
            return "request_human"
        return "answer_directly" if state.request_type == "direct_answer" else "generate_hypotheses"

    @staticmethod
    def _route_after_human(state: InvestigationState) -> str:
        if state.human_resume_node:
            return state.human_resume_node
        return "answer_directly" if state.request_type == "direct_answer" else "select_investigation"

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
                HumanFeedback(
                    question=question,
                    response=str(response),
                    hypothesis_id=state.current_focus,
                ),
            ],
            "pending_human_question": None,
            "status": InvestigationStatus.RUNNING,
        }

    async def _answer_directly(self, state: InvestigationState) -> dict[str, Any]:
        """Answer factual requests with one or more isolated, guarded datasource queries."""
        metadata = await self._metadata_payload(state)
        plan = await self.llm.structured(
            DirectAnswerPlan,
            (
                "Answer this factual business request with focused read-only queries. Each query must select "
                "one exact datasource ID and use only that datasource's schema. Never join databases in SQL. "
                "If the answer needs data from separate datasources, put the main query in the top-level fields, "
                "put up to three additional independent queries in supporting_queries, and declare a combination "
                "only when their result sets need a deterministic union, intersection, or set difference. For a "
                "set difference, the main query is the set to keep and supporting_query_index identifies the set "
                "to remove. For rankings, return the requested top results in a "
                "business-friendly order. If the result uses an internal identifier, include that identifier "
                "column unchanged; the service will resolve a customer-facing name from a selected lookup "
                "source and remove the identifier before presenting the result. Do not invent a comparison, "
                "prior period, or causal explanation. The purpose is user-visible: explain the answer in plain "
                "language without table names or SQL. Earlier answered clarifications are authoritative: reuse "
                "them unless the user explicitly changes them."
            ),
            {
                "question": state.question,
                "metric": state.metric_definition.model_dump(mode="json") if state.metric_definition else None,
                "datasources": metadata,
                "previously_answered_clarifications": self._answered_clarifications(state),
            },
        )
        if self._defers_datasource_selection(plan):
            logger.warning("Direct-answer planner deferred datasource selection despite an explicit selected source set")
            plan = await self.llm.structured(
                DirectAnswerPlan,
                (
                    "Correct the plan. The user has already selected every available datasource. Do not ask "
                    "them to select a datasource, an approach, a period, or a definition, and never write a "
                    "clarification message as SQL. Decide which selected sources are necessary, query each one "
                    "separately, and combine their results when needed. Return only an executable retrieval plan."
                ),
                {"question": state.question, "datasources": metadata, "previous_plan": plan.model_dump(mode="json")},
            )
        if self._defers_datasource_selection(plan):
            raise UnsafeQueryError("Direct-answer planner attempted to defer datasource selection")
        query_plans = [DirectAnswerQuery(datasource_id=plan.datasource_id, sql=plan.sql, purpose=plan.purpose), *plan.supporting_queries]
        executed = [await self._execute_direct_query(state, query, metadata) for query in query_plans]
        rows, datasource_id, sql, purpose = executed[0]
        if plan.combination is not None:
            combination = plan.combination
            if combination.supporting_query_index >= len(plan.supporting_queries):
                raise UnsafeQueryError("Direct-answer combination refers to a missing supporting query")
            supporting_rows = executed[combination.supporting_query_index + 1][0]
            if any(combination.primary_key not in row for row in rows) or any(combination.supporting_key not in row for row in supporting_rows):
                raise UnsafeQueryError("Direct-answer combination refers to fields not returned by its queries")
            primary_values = {str(row[combination.primary_key]) for row in rows}
            supporting_values = {str(row[combination.supporting_key]) for row in supporting_rows}
            if combination.operation == "set_difference":
                rows = [row for row in rows if str(row[combination.primary_key]) not in supporting_values]
            elif combination.operation == "intersection":
                rows = [row for row in rows if str(row[combination.primary_key]) in supporting_values]
            else:
                rows = [*rows, *[row for row in supporting_rows if str(row[combination.supporting_key]) not in primary_values]]
        enriched_rows, enrichment_step, enrichment_queries = await self._resolve_display_names(
            state, metadata, datasource_id, sql, rows
        )
        user_facing_rows = enriched_rows
        steps = [InvestigationStep(iteration=1, action=item[3], datasource_id=item[1], rationale="Direct factual answer", query=item[2]) for item in executed]
        observation = Observation(
            description=purpose,
            value={
                "rows": user_facing_rows[:100],
                "deterministic_summary": summarize_query_rows(user_facing_rows),
            },
            source=datasource_id,
        )
        return {
            "observations": [*state.observations, observation],
            "investigation_history": [
                *state.investigation_history,
                *steps,
                *([enrichment_step] if enrichment_step else []),
            ],
            "query_count": state.query_count + len(executed) + enrichment_queries,
            "iteration": 1,
            "status": InvestigationStatus.RUNNING,
        }

    async def _execute_direct_query(
        self, state: InvestigationState, query: DirectAnswerQuery, metadata: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], str, str, str]:
        """Execute one direct-answer subquery with the same safety and correction policy as every other query."""
        source_ids = {source.id for source in state.datasources}
        if query.datasource_id not in source_ids:
            logger.warning("Direct-answer query selected an invalid datasource; requesting a corrected query")
            query = await self.llm.structured(
                DirectAnswerQuery,
                "Correct the query. Select one exact datasource_id from the supplied list and use only that datasource's schema.",
                {"question": state.question, "datasources": metadata, "valid_datasource_ids": sorted(source_ids)},
            )
            if query.datasource_id not in source_ids:
                raise UnsafeQueryError("Direct-answer query selected a datasource outside this request")
        source_metadata = next(source for source in metadata if source["id"] == query.datasource_id)
        allowed_tables = {table["name"] for table in source_metadata["tables"]}
        for attempt in range(2):
            try:
                safe_sql = validate_query_tables(validate_read_query(query.sql), allowed_tables)
                purpose = self._safe_direct_answer_text(query.purpose)
                await self._emit(state, "QueryStarted", purpose, datasource_id=query.datasource_id)
                rows = await self.registry.get(query.datasource_id).execute_read_query(safe_sql)
                await self._emit(state, "QueryCompleted", "The data check is complete.", datasource_id=query.datasource_id, row_count=len(rows))
                return rows, query.datasource_id, safe_sql, purpose
            except (ProgrammingError, UnsafeQueryError) as exc:
                if attempt:
                    raise
                logger.warning("Direct-answer query was rejected; requesting one corrected query: datasource=%s error=%s", query.datasource_id, exc)
                await self._emit(state, "QueryRejected", "The first check needed an adjustment, so the answer is trying again.", datasource_id=query.datasource_id)
                query = await self.llm.structured(
                    DirectAnswerQuery,
                    "Correct the query using only the supplied datasource schema. Keep the same datasource_id and return one read-only PostgreSQL SELECT with a plain-language purpose.",
                    {"question": state.question, "selected_datasource_id": source_metadata["id"], "selected_datasource": source_metadata, "previous_query": query.sql, "rejection_reason": str(exc)},
                )
                if query.datasource_id != source_metadata["id"]:
                    raise UnsafeQueryError("Corrected direct-answer query changed the selected datasource")
        raise RuntimeError("A direct-answer query result was not produced")

    async def _resolve_display_names(
        self,
        state: InvestigationState,
        metadata: list[dict[str, Any]],
        source_id: str,
        sql: str,
        rows: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], InvestigationStep | None, int]:
        """Resolve arbitrary identifier fields to a display name using discovered schema metadata.

        This performs a separate, read-only lookup. It deliberately avoids cross-database SQL joins,
        and never exposes the identifier after the lookup has completed.
        """
        source_metadata = next((candidate for candidate in metadata if candidate["id"] == source_id), None)
        if source_metadata is None:
            return rows, None, 0
        queried_tables = [
            table for table in source_metadata["tables"] if query_references_table(sql, table["name"])
        ]
        key_columns = {
            column["name"]
            for table in queried_tables
            for column in table["columns"]
            if column["pk"]
        }
        key_columns.update(
            foreign_key["column_name"]
            for table in queried_tables
            for foreign_key in table["foreign_keys"]
            if foreign_key.get("column_name")
        )
        lookup_candidates: list[tuple[str, dict[str, Any], dict[str, Any], str, str]] = []
        for table in queried_tables:
            display_column = self._infer_display_column(table)
            for column in table["columns"]:
                if column["pk"] and display_column:
                    lookup_candidates.append((column["name"], source_metadata, table, column["name"], display_column))
            for foreign_key in table["foreign_keys"]:
                target_name = f"{foreign_key.get('foreign_schema', '')}.{foreign_key.get('foreign_table', '')}".strip(".")
                target_column = foreign_key.get("foreign_column")
                target_table = next((item for item in source_metadata["tables"] if item["name"] == target_name), None)
                if target_table and target_column:
                    target_columns = {column["name"] for column in target_table["columns"]}
                    target_display = self._infer_display_column(target_table)
                    if target_display:
                        lookup_candidates.append((foreign_key["column_name"], source_metadata, target_table, target_column, target_display))

        relations = await self.registry.infer_cross_relations([candidate["id"] for candidate in metadata])
        for relation in relations:
            if relation.source_datasource != source_id:
                continue
            source_identifier = relation.source_field.rsplit(".", 1)[-1]
            target_identifier = relation.target_field.rsplit(".", 1)[-1]
            target_table_name = relation.target_field.rsplit(".", 1)[0] if "." in relation.target_field else None
            key_columns.add(source_identifier)
            target_source = next((candidate for candidate in metadata if candidate["id"] == relation.target_datasource), None)
            if target_source is None:
                continue
            for target_table in target_source["tables"]:
                if target_table_name and target_table["name"] != target_table_name:
                    continue
                target_columns = {column["name"] for column in target_table["columns"]}
                target_display = self._infer_display_column(target_table)
                if target_identifier in target_columns and target_display:
                    lookup_candidates.append(
                        (source_identifier, target_source, target_table, target_identifier, target_display)
                    )

        lookup: tuple[str, list[str], dict[str, Any], dict[str, Any], str, str] | None = None
        for identifier_column, candidate, table, target_identifier, display_column in lookup_candidates:
            identifiers = list(
                dict.fromkeys(str(row[identifier_column]) for row in rows if row.get(identifier_column) is not None)
            )[:100]
            if identifiers:
                lookup = (identifier_column, identifiers, candidate, table, target_identifier, display_column)
                break
        if lookup is None:
            return self._redact_internal_identifier_columns(rows, key_columns), None, 0
        identifier_column, identifiers, catalogue, lookup_table, target_identifier, display_column = lookup
        quoted_identifiers = ", ".join("'" + value.replace("'", "''") + "'" for value in identifiers)
        name_query = validate_query_tables(
            validate_read_query(
                f"SELECT {target_identifier}, {display_column} FROM {lookup_table['name']} "
                f"WHERE {target_identifier} IN ({quoted_identifiers})"
            ),
            {table["name"] for table in catalogue["tables"]},
        )
        await self._emit(
            state,
            "QueryStarted",
            "Matching the selected items to their names.",
            datasource_id=catalogue["id"],
        )
        try:
            name_rows = await self.registry.get(catalogue["id"]).execute_read_query(name_query)
        except (ProgrammingError, UnsafeQueryError) as exc:
            # A stale schema or inaccessible lookup must never leak the original key values.
            logger.warning(
                "Display-name lookup failed; returning a redacted result instead: datasource=%s error=%s",
                catalogue["id"],
                exc,
            )
            await self._emit(
                state,
                "QueryRejected",
                "The item-name check was unavailable, so internal references were removed from the result.",
                datasource_id=catalogue["id"],
            )
            return self._redact_internal_identifier_columns(rows, key_columns), None, 0
        await self._emit(
            state,
            "QueryCompleted",
            "The item names are ready.",
            datasource_id=catalogue["id"],
            row_count=len(name_rows),
        )
        names = {
            str(row[target_identifier]): row[display_column]
            for row in name_rows
            if row.get(target_identifier) is not None and row.get(display_column)
        }
        enriched_rows = [
            {
                **row,
                "display_name": self._customer_facing_name(names[str(row[identifier_column])], row[identifier_column]),
            }
            if row.get(identifier_column) is not None and str(row[identifier_column]) in names
            else row
            for row in rows
        ]
        return (
            self._redact_internal_identifier_columns(enriched_rows, key_columns),
            InvestigationStep(
                iteration=1,
                action="Match the selected items to their names.",
                datasource_id=catalogue["id"],
                rationale="Make the result understandable without combining databases.",
                query=name_query,
            ),
            1,
        )

    async def _generate_hypotheses(self, state: InvestigationState) -> dict[str, Any]:
        result = await self.llm.structured(
            HypothesisPlan,
            (
                "Generate 3 to 5 distinct, testable competing hypotheses. Identify them sequentially as "
                "H1, H2, and so on. Give each a concise, unique name suitable for a UI label. Include "
                "internal and external possibilities only when plausible. Write every name and description "
                "as a plain-language business explanation; do not expose implementation or database details."
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
                "exact datasource ID. The action and rationale will be shown to a non-technical person, so "
                "describe the business check in everyday language without naming data tables or fields."
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
                "The purpose is shown in the UI: describe what the check will answer in everyday business "
                "language, without SQL, table names, or field names. "
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
            except (ProgrammingError, UnsafeQueryError) as exc:
                if attempt:
                    raise
                logger.warning(
                    "Generated query was rejected; requesting one corrected query: datasource=%s error=%s",
                    datasource_id,
                    exc,
                )
                await self._emit(
                    state,
                    "QueryRejected",
                    "The first check needed an adjustment, so the investigation is trying again.",
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
                        "rejection_reason": str(exc),
                        "investigation_context": self._state_payload(state),
                    },
                )
        else:  # pragma: no cover - the loop either breaks or re-raises
            raise RuntimeError("A query result was not produced")
        deterministic_summary = summarize_query_rows(rows)
        await self._emit(
            state,
            "QueryCompleted",
            "The data check is complete.",
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
                "context, not direct causal evidence. Write evidence and any clarification in plain business "
                "language. Do not expose SQL, table names, field names, data-source IDs, or ingestion jargon. "
                "For a before/after comparison, include the measured relative change as a numeric evidence "
                "data point named percentage_change (for example, -0.09 for a 0.09% decrease)."
            ),
            {**self._state_payload(state), "latest_observation": latest.model_dump(mode="json") if latest else None},
        )
        scoped_evidence, affected_hypothesis_ids = self._scope_evidence(state, result)
        hypotheses = self._merge_hypothesis_updates(state, result, affected_hypothesis_ids)
        known_evidence = {item.id for item in state.evidence}
        new_evidence = [item for item in scoped_evidence if item.id not in known_evidence]
        evidence = [*state.evidence, *new_evidence]
        premise_rejected = self._premise_is_directly_rejected(state.question, hypotheses, evidence)
        no_material_decline = self._no_material_decline(
            state.model_copy(update={"hypotheses": hypotheses, "evidence": evidence})
        )
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
            "evidence": evidence,
            "hypotheses": hypotheses,
            "confidence": result.confidence,
            "pending_human_question": None if premise_rejected or no_material_decline else result.ambiguity,
            "human_resume_node": (
                "select_investigation" if result.ambiguity and not premise_rejected and not no_material_decline else None
            ),
            "status": (
                InvestigationStatus.WAITING_FOR_HUMAN
                if result.ambiguity and not premise_rejected and not no_material_decline
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
        if self._no_material_decline(state):
            logger.info(
                "Finishing investigation because the measured change is within the %.2f%% materiality threshold",
                settings.negligiblity_threshold_percent,
            )
            return {"next_action": "finish", "status": InvestigationStatus.RUNNING}
        if self._premise_is_directly_rejected(state.question, state.hypotheses, state.evidence):
            logger.info("Finishing investigation because direct evidence rejects the reported decline")
            return {"next_action": "finish", "status": InvestigationStatus.RUNNING}
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
        if state.request_type == "direct_answer":
            direct_result = next(
                (
                    observation.value
                    for observation in reversed(state.observations)
                    if isinstance(observation.value, dict) and isinstance(observation.value.get("rows"), list)
                ),
                None,
            )
            result = await self.llm.structured(
                Synthesis,
                (
                    "Answer the user's factual request directly from the query result. Do not discuss root "
                "causes, hypotheses, or missing comparisons. Use likely_root_cause as a short answer "
                "title and put the requested ranking, list, total, or lookup result in summary. Name the "
                "actual results clearly in plain business language. Never return database identifiers, "
                "internal IDs, or technical field names; use the customer-facing name supplied in the result."
                ),
                {**self._state_payload(state), "direct_result": direct_result},
            )
            analysis = result.analysis
            named_entity_ranking = self._named_entity_ranking_analysis(state, analysis)
            if named_entity_ranking is not None:
                analysis = named_entity_ranking
            if analysis.likely_root_cause is None:
                analysis = analysis.model_copy(update={"likely_root_cause": "Direct answer"})
            analysis = analysis.model_copy(update={"follow_up_question": await self._contextual_follow_up_question(state)})
            conversation_turns = [
                *state.conversation_turns,
                ConversationTurn(
                    question=state.current_conversation_question or state.question,
                    answer=analysis,
                ),
            ]
            await self._emit(
                state,
                "InvestigationCompleted",
                analysis.summary,
                status=InvestigationStatus.COMPLETED,
                budget_exhausted=False,
                final_analysis=analysis.model_dump(mode="json"),
                conversation_turns=[turn.model_dump(mode="json") for turn in conversation_turns],
            )
            return {
                "final_analysis": analysis,
                "confidence": analysis.confidence,
                "conversation_turns": conversation_turns,
                "status": InvestigationStatus.COMPLETED,
            }
        if self._no_material_decline(state):
            direct_evidence = [
                item for item in state.evidence if item.relationship == "direct" and item.hypothesis_ids
            ]
            changes = self._percentage_changes(direct_evidence)
            smallest_change = min(changes, key=abs)
            strongest_evidence = max(direct_evidence, key=lambda item: item.confidence)
            analysis = FinalAnalysis(
                likely_root_cause="No meaningful overall decrease found",
                confidence=strongest_evidence.confidence,
                evidence=direct_evidence,
                rejected_hypotheses=[
                    hypothesis for hypothesis in state.hypotheses if hypothesis.status == HypothesisStatus.REJECTED
                ],
                external_findings=state.external_findings,
                caveats=[],
                summary=(
                    f"The measured change ({smallest_change:+.2f}%) is within the "
                    f"{settings.negligiblity_threshold_percent:.1f}% negligibility threshold, so it is too "
                    "small to treat as a meaningful overall decrease."
                ),
            )
            await self._emit(
                state,
                "InvestigationCompleted",
                analysis.summary,
                status=InvestigationStatus.COMPLETED,
                budget_exhausted=False,
                final_analysis=analysis.model_dump(mode="json"),
            )
            return {
                "final_analysis": analysis,
                "confidence": analysis.confidence,
                "status": InvestigationStatus.COMPLETED,
            }
        if self._premise_is_directly_rejected(state.question, state.hypotheses, state.evidence):
            direct_evidence = [
                item
                for item in state.evidence
                if item.relationship == "direct" and item.confidence >= 0.75 and item.hypothesis_ids
            ]
            strongest_evidence = max(direct_evidence, key=lambda item: item.confidence)
            analysis = FinalAnalysis(
                likely_root_cause="No overall decline found",
                confidence=strongest_evidence.confidence,
                evidence=direct_evidence,
                rejected_hypotheses=state.hypotheses,
                external_findings=state.external_findings,
                caveats=[],
                summary=(
                    "The evidence does not show an overall decline for the period you asked about, so there "
                    "is no decline to explain. " + strongest_evidence.description
                ),
            )
            await self._emit(
                state,
                "InvestigationCompleted",
                analysis.summary,
                status=InvestigationStatus.COMPLETED,
                budget_exhausted=False,
                final_analysis=analysis.model_dump(mode="json"),
            )
            return {
                "final_analysis": analysis,
                "confidence": analysis.confidence,
                "status": InvestigationStatus.COMPLETED,
            }
        result = await self.llm.structured(
            Synthesis,
            (
                "Produce a concise evidence-backed final analysis. Do not claim causation from correlation. "
                "When a high-confidence supported hypothesis has direct evidence, identify it as the likely "
                "root cause and state its caveats. If evidence is insufficient, likely_root_cause must be null "
                "and explain the leading possibilities and missing evidence. Evidence with an empty "
                "hypothesis_ids list is an investigation-level data-quality or coverage caveat: include it in "
                "caveats, never treat it as support or contradiction for a root-cause hypothesis. Write for a "
                "non-technical business reader; avoid formulas, SQL, table names, field names, and system jargon."
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
