from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import sqlglot
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from sqlalchemy.exc import DBAPIError, ProgrammingError, SQLAlchemyError
from sqlglot import expressions as exp

from business_signals.analytics import summarize_query_rows
from business_signals.config import settings
from business_signals.datasources.registry import DatasourceRegistry
from business_signals.datasources.safety import (
    CROSS_DATASOURCE_LOOKUP_KEY,
    UnsafeQueryError,
    query_references_table,
    validate_cross_datasource_lookup_projection,
    validate_query_tables,
    validate_read_query,
)
from business_signals.external import ExternalResearcher
from business_signals.investigation.direct_answers import DirectAnswerMixin
from business_signals.investigation.external_research import ExternalResearchCoordinator
from business_signals.llm import (
    LLM,
    CrossDatasourceLookup,
    DirectAnswerPlan,
    EvidenceAssessment,
    HypothesisPlan,
    InvestigationDecision,
    InvestigationPlan,
    QueryPlan,
    QuestionUnderstanding,
    Synthesis,
    create_llm,
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
    InvestigationScope,
    InvestigationState,
    InvestigationStatus,
    InvestigationStep,
    MetricDefinition,
    Observation,
    QueryScope,
    ResolvedEntityReference,
    ScopeCoverage,
)
from business_signals.prompt_catalog import prompts

EventSink = Callable[[InvestigationEvent], Awaitable[None]]
logger = logging.getLogger("uvicorn.error")


class InvestigationEngine(DirectAnswerMixin):
    """Concrete LangGraph workflow, with a stable public compatibility import."""

    def __init__(
        self,
        registry: DatasourceRegistry,
        llm: LLM | None = None,
        event_sink: EventSink | None = None,
        checkpointer: Any | None = None,
        researcher: ExternalResearcher | None = None,
    ) -> None:
        self.registry = registry
        self.llm = llm or create_llm()
        self.event_sink = event_sink
        self._checkpointer = checkpointer
        self.researcher = researcher or ExternalResearcher()
        self.external_research = ExternalResearchCoordinator(
            self.researcher,
            self.llm,
            self._emit,
            self._state_payload,
            self._resolve_hypothesis_name,
        )
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
                QueryScope,
                ResolvedEntityReference,
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
        graph.add_node("select_external_research", self._select_external_research)
        graph.add_node("research_external", self._research_external)
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
        graph.add_conditional_edges(
            "select_investigation",
            self._route_after_selection,
            {"execute": "execute", "synthesize": "synthesize"},
        )
        graph.add_conditional_edges(
            "execute",
            self._route_after_execution,
            {"interpret": "interpret", "decide": "decide"},
        )
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
                "external": "select_external_research",
                "finish": "synthesize",
            },
        )
        graph.add_edge("select_external_research", "research_external")
        graph.add_conditional_edges(
            "research_external",
            self._route_after_external_research,
            {"interpret": "interpret", "continue": "select_investigation"},
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
        trusted_relationships = [
            relation.model_dump(mode="json")
            for relation in self.registry.approved_cross_relations([summary.id for summary in state.datasources])
        ]
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
                            "roles": self._schema_roles(table),
                        }
                        for table in metadata.structural_metadata
                    ],
                    "semantics": metadata.semantic_metadata,
                    "trusted_cross_datasource_relationships": trusted_relationships,
                }
            )
        return payload

    @classmethod
    def _schema_roles(cls, table: Any) -> dict[str, list[str]]:
        """Describe a table from its discovered structure, without relying on field names."""
        key_columns = {column.name for column in table.columns if column.primary_key}
        foreign_key_columns = {
            foreign_key["column_name"] for foreign_key in table.foreign_keys if foreign_key.get("column_name")
        }
        key_columns.update(foreign_key_columns)
        def has_type(column: Any, tokens: tuple[str, ...]) -> bool:
            value = column.data_type.casefold()
            return any(token in value for token in tokens)

        return {
            "primary_keys": [column.name for column in table.columns if column.primary_key],
            "foreign_keys": sorted(foreign_key_columns),
            "time_fields": list(dict.fromkeys([
                *([table.time_column] if table.time_column else []),
                *(column.name for column in table.columns if has_type(column, ("date", "time"))),
            ])),
            "numeric_measures": [
                column.name
                for column in table.columns
                if column.name not in key_columns
                and has_type(column, ("int", "numeric", "decimal", "real", "double", "money"))
            ],
            "text_dimensions": [
                column.name
                for column in table.columns
                if column.name not in key_columns
                and has_type(column, ("char", "text", "string", "citext"))
            ],
        }

    def _state_payload(self, state: InvestigationState) -> dict[str, Any]:
        history = [s.model_dump(mode="json") for s in state.investigation_history]
        if state.request_type == "direct_answer":
            for step in history:
                step.pop("query", None)
        return {
            "question": state.question,
            "metric": state.metric_definition.model_dump(mode="json") if state.metric_definition else None,
            "analysis_scope": state.analysis_scope.model_dump(mode="json") if state.analysis_scope else None,
            "scope_coverage": [coverage.model_dump(mode="json") for coverage in state.scope_coverage],
            "hypotheses": [h.model_dump(mode="json") for h in state.hypotheses],
            "evidence": [e.model_dump(mode="json") for e in state.evidence],
            "failed_checks": state.failed_checks,
            "external_findings": [f.model_dump(mode="json") for f in state.external_findings],
            "history": history,
            "human_feedback": [f.model_dump(mode="json") for f in state.human_feedback],
            # Trusted backend context for a continuation. It is never part of an HTTP response or
            # user-facing synthesis, but lets a follow-up query reuse an already resolved entity.
            "trusted_entity_references": [entity.model_dump(mode="json") for entity in state.resolved_entities],
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
        topics_by_hypothesis = {
            hypothesis.id: {topic.strip().casefold() for topic in hypothesis.evidence_topics if topic.strip()}
            for hypothesis in state.hypotheses
        }
        scoped_evidence = []
        affected_hypothesis_ids: set[str] = set()
        for evidence in assessment.evidence:
            hypothesis_ids = list(dict.fromkeys(
                hypothesis_id for hypothesis_id in evidence.hypothesis_ids if hypothesis_id in known_hypothesis_ids
            ))
            if evidence.scope != "mechanism":
                hypothesis_ids = []
            elif evidence.evidence_topics:
                evidence_topics = {topic.strip().casefold() for topic in evidence.evidence_topics if topic.strip()}
                hypothesis_ids = [
                    hypothesis_id
                    for hypothesis_id in hypothesis_ids
                    if evidence_topics.intersection(topics_by_hypothesis[hypothesis_id])
                ]
            elif any(topics_by_hypothesis[hypothesis_id] for hypothesis_id in hypothesis_ids):
                # New causal hypotheses declare their semantic mechanism. An
                # untagged result may be a baseline or coverage fact, but it
                # is never enough to attribute a cause to one of them.
                hypothesis_ids = []
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

    @classmethod
    def _requires_external_context(cls, state: InvestigationState, available_agents: list[dict[str, Any]]) -> bool:
        """Use configured research for historical causal questions, even if the first plan omitted it."""
        causal_openers = ("why", "what caused", "what explains", "what happened")
        has_historical_period = bool(re.search(r"\b(?:19|20)\d{2}\b", state.question))
        return bool(available_agents) and has_historical_period and cls._asks_about_decline(state.question) and state.question.strip().casefold().startswith(causal_openers)

    @staticmethod
    def _external_conditions_hypothesis(available_agents: list[dict[str, Any]]) -> Hypothesis:
        subjects = list(dict.fromkeys(
            str(subject)
            for agent in available_agents
            for subject in agent.get("subjects", [])
            if isinstance(subject, str)
        ))
        examples = ", ".join(subjects[:3]) or "outside conditions"
        return Hypothesis(
            name="External conditions affected demand",
            description=(
                "A factor outside the business, covered by the configured research sources "
                f"(for example {examples}), may have changed customer demand during this period."
            ),
            category="external",
            research_scope="external",
            evidence_topics=sorted({
                str(topic)
                for agent in available_agents
                for topic in agent.get("evidence_topics", [])
                if isinstance(topic, str)
            }),
            confidence=0.2,
        )

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
    def _is_essential_business_clarification(question: str | None) -> bool:
        """Only pause for a decision that changes what the user means, not how we execute."""
        if not question:
            return False
        candidate = question.strip().casefold()
        operational_markers = (
            "allow me", "next step", "run a different query", "try again", "retry", "alternate data",
            "data extract", "raw event", "data source", "datasource", "schema", "table", "column",
            "check another", "wider date range", "widen the date", "expand the date", "missing data", "proceed treating",
        )
        if any(marker in candidate for marker in operational_markers):
            return False
        meaning_markers = (
            "when you say", "do you mean", "which period", "what period", "which dates", "what dates", "which historical",
            "which region", "what region", "which store", "what does", "how should",
        )
        return any(marker in candidate for marker in meaning_markers)

    def _clarification_to_request(self, state: InvestigationState, ambiguity: str | None) -> str | None:
        """Keep human pauses rare and limited to one unresolved business definition per turn."""
        if not ambiguity:
            return None
        if state.human_feedback:
            logger.info("Suppressing additional clarification because this conversation already has user input")
            return None
        if self._is_essential_business_clarification(ambiguity):
            return ambiguity
        logger.info("Suppressing operational or non-essential clarification: %s", ambiguity)
        return None

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
    def _quoted_discovered_identifier(value: str) -> str:
        """Quote an identifier taken from schema discovery, never from user input."""
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", value):
            raise ValueError(f"Discovered metadata contains an unsafe identifier: {value!r}")
        return f'"{value}"'

    @staticmethod
    def _quoted_ambiguity_candidates(ambiguity: str) -> tuple[str, set[str]] | None:
        """Produce all meaningful word sequences from the model's quoted term.

        This deliberately has no vocabulary of business dimensions. For example,
        the term ``outdoor-product`` yields both ``outdoor product`` and
        ``outdoor``; either could be an exact controlled value in an arbitrary
        schema.
        """
        match = re.search(r"[‘'\"]([^’'\"]+)[’'\"]", ambiguity)
        if not match:
            return None
        term = match.group(1).strip()
        words = re.findall(r"[a-z0-9]+", term.casefold())
        candidates = {
            " ".join(words[start:end])
            for start in range(len(words))
            for end in range(start + 1, len(words) + 1)
            if len(" ".join(words[start:end])) >= 4
        }
        return (term, candidates) if candidates else None

    async def _infer_ambiguity_from_discovered_values(
        self, metadata: list[dict[str, Any]], ambiguity: str | None
    ) -> str | None:
        """Resolve a quoted term only when the connected data has one exact value.

        The resolver does not know what a category, location, channel, or product
        is. It looks only at the data types discovered for the selected sources:
        each non-key text dimension with at most 50 distinct values is a possible
        controlled vocabulary. If one normalized stored value exactly matches a
        word sequence in the quoted term, it is safe to retain that interpretation
        without asking the user. Zero or multiple matches leave the clarification
        in place.
        """
        if not ambiguity:
            return None
        extracted = self._quoted_ambiguity_candidates(ambiguity)
        if extracted is None:
            return None
        term, candidates = extracted
        matches: dict[str, str] = {}

        for source in metadata:
            datasource_id = str(source["id"])
            for table_metadata in source.get("tables", []):
                table_name = str(table_metadata.get("name", ""))
                if "." not in table_name:
                    continue
                schema_name, table_name = table_name.split(".", 1)
                text_dimensions = table_metadata.get("roles", {}).get("text_dimensions", [])
                for column_name in text_dimensions:
                    try:
                        table = ".".join((
                            self._quoted_discovered_identifier(schema_name),
                            self._quoted_discovered_identifier(table_name),
                        ))
                        column = self._quoted_discovered_identifier(str(column_name))
                        rows = await self.registry.get(datasource_id).execute_read_query(
                            f"SELECT DISTINCT {column} AS value FROM {table} WHERE {column} IS NOT NULL LIMIT 51"
                        )
                    except Exception:
                        # This probe is an optional ambiguity reducer. A failed
                        # probe is never permitted to hide the original question
                        # or stop the investigation, but remains visible in logs.
                        logger.warning(
                            "Automatic exact-value ambiguity lookup failed: datasource=%s table=%s column=%s",
                            datasource_id,
                            table_name,
                            column_name,
                            exc_info=True,
                        )
                        continue
                    if len(rows) > 50:
                        continue
                    for row in rows:
                        value = row.get("value")
                        if not isinstance(value, str):
                            continue
                        normalized = " ".join(re.findall(r"[a-z0-9]+", value.casefold()))
                        if normalized in candidates:
                            matches.setdefault(normalized, value)

        if len(matches) != 1:
            return None
        value = next(iter(matches.values()))
        logger.info("Resolved quoted business term from an exact discovered value: term=%r", term)
        return f"The term “{term}” is represented by “{value}” in the connected data."

    @staticmethod
    def _measured_metric_percentage_changes(evidence: list[Evidence]) -> list[float]:
        """Read only explicitly-labelled changes in the requested business metric.

        Evidence often contains percentage changes for possible causes, such as payment failures
        or inventory. Those must never trigger the negligible-decline shortcut for the metric the
        user asked about.
        """
        changes: list[float] = []
        metric_percentage_field = re.compile(
            r"(?:requested|measured)[ _-]*metric[ _-]*(?:percentage|percent|pct)[ _-]*(?:change|difference)"
        )
        for item in evidence:
            if item.relationship != "direct" or not item.hypothesis_ids:
                continue
            for point in item.data:
                if metric_percentage_field.fullmatch(point.field.casefold()) and isinstance(point.value, (int, float)):
                    changes.append(float(point.value))
        return changes

    def _no_material_decline(self, state: InvestigationState) -> bool:
        if not self._asks_about_decline(state.question):
            return False
        changes = self._measured_metric_percentage_changes(state.evidence)
        # A materiality shortcut is intentionally conservative. It applies only when every
        # verified requested-metric change is a small decline, never to an increase in a driver.
        return bool(changes) and all(-settings.negligiblity_threshold_percent <= change <= 0 for change in changes)

    @staticmethod
    def _scope_caveats(state: InvestigationState) -> list[str]:
        """State observed coverage without guessing what an unmatched scope contains."""
        caveats: list[str] = []
        if any(item.matched_records == 0 for item in state.scope_coverage):
            caveats.append(
                "At least one requested filter matched no related records, so the requested scope could not be fully measured."
            )
        if any(item.matched_records == 1 for item in state.scope_coverage):
            caveats.append(
                "At least one requested filter matched only one related record, so findings apply to that matched part of the requested scope rather than automatically to the whole area or group."
            )
        if any(item.limited for item in state.scope_coverage):
            caveats.append(
                "At least one related-record filter reached the investigation safety limit, so results may not cover every matching record."
            )
        return caveats

    @classmethod
    def _with_scope_caveats(cls, state: InvestigationState, analysis: FinalAnalysis) -> FinalAnalysis:
        additions = [item for item in cls._scope_caveats(state) if item not in analysis.caveats]
        return analysis.model_copy(update={"caveats": [*analysis.caveats, *additions]})

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
            prompts.load("question_understanding"),
            {
                "question": state.question,
                "datasources": metadata,
                "previously_answered_clarifications": self._answered_clarifications(state),
            },
        )
        observations = [Observation(description=item, source="question") for item in result.observations]
        current_question = state.current_conversation_question or state.question
        request_type = (
            "direct_answer"
            if state.request_type == "direct_answer" or self._is_direct_answer_request(current_question)
            else result.request_type
        )
        ambiguity = None if request_type == "direct_answer" and self._has_explicit_all_history_scope(state.question) else result.ambiguity
        inferred_filter = await self._infer_ambiguity_from_discovered_values(metadata, ambiguity)
        analysis_scope = result.scope
        if inferred_filter:
            ambiguity = None
            observations.append(Observation(description=inferred_filter, source="catalog"))
            if analysis_scope is None:
                analysis_scope = InvestigationScope(filters=[inferred_filter])
            elif inferred_filter not in analysis_scope.filters:
                analysis_scope = analysis_scope.model_copy(
                    update={"filters": [*analysis_scope.filters, inferred_filter]}
                )
        if request_type == "direct_answer" and self._reasks_answered_sales_metric(state, ambiguity):
            logger.info("Skipping duplicate sales-metric clarification because the conversation already contains an answer")
            ambiguity = None
        ambiguity = self._clarification_to_request(state, ambiguity)
        return {
            "metric_definition": result.metric_definition,
            "analysis_scope": analysis_scope,
            "observations": observations,
            "request_type": request_type,
            "pending_human_question": ambiguity,
            "human_resume_node": (
                "answer_directly" if ambiguity and request_type == "direct_answer"
                else "generate_hypotheses" if ambiguity else None
            ),
            "status": InvestigationStatus.WAITING_FOR_HUMAN if ambiguity else InvestigationStatus.RUNNING,
            "paused_at": datetime.now(UTC) if ambiguity else None,
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

    @staticmethod
    def _route_after_external_research(state: InvestigationState) -> str:
        return "continue" if state.next_action == "continue" else "interpret"

    async def _request_human(self, state: InvestigationState) -> dict[str, Any]:
        question = state.pending_human_question
        if not question:
            return {"status": InvestigationStatus.RUNNING}
        response = interrupt({"question": question})
        paused_seconds = state.paused_duration_seconds
        if state.paused_at is not None:
            paused_seconds += max(0, (datetime.now(UTC) - state.paused_at).total_seconds())
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
            "paused_at": None,
            "paused_duration_seconds": paused_seconds,
        }

    async def _generate_hypotheses(self, state: InvestigationState) -> dict[str, Any]:
        available_agents = self.external_research.available_agents()
        result = await self.llm.structured(
            HypothesisPlan,
            prompts.load("hypotheses"),
            {
                **self._state_payload(state),
                "datasources": await self._metadata_payload(state),
                "available_research_agents": available_agents,
            },
        )
        # A plan is a list of candidates, not an assessment.  The model may
        # reasonably describe missing evidence while planning, but only an
        # executed check is allowed to move a new hypothesis out of ACTIVE.
        hypotheses = [
            hypothesis.model_copy(update={"status": HypothesisStatus.ACTIVE})
            for hypothesis in result.hypotheses
        ]
        if self._requires_external_context(state, available_agents) and not any(
            hypothesis.research_scope == "external" for hypothesis in hypotheses
        ):
            logger.info("Adding an external-conditions hypothesis omitted by the initial plan")
            hypotheses.append(self._external_conditions_hypothesis(available_agents))
        for hypothesis in hypotheses:
            await self._emit(
                state,
                "HypothesisCreated",
                hypothesis.description,
                hypothesis=hypothesis.model_dump(mode="json"),
            )
        hypothesis_name_index = {hypothesis.name.casefold(): hypothesis.id for hypothesis in hypotheses}
        if len(hypothesis_name_index) != len(hypotheses):
            raise ValueError("Generated hypotheses must have unique names")
        return {"hypotheses": hypotheses, "hypothesis_name_index": hypothesis_name_index}

    async def _select_investigation(self, state: InvestigationState) -> dict[str, Any]:
        active_hypotheses = [
            hypothesis
            for hypothesis in state.hypotheses
            if hypothesis.status
            not in {
                HypothesisStatus.REJECTED,
                HypothesisStatus.CONFIRMED,
                HypothesisStatus.NEEDS_MORE_EVIDENCE,
            }
        ]
        if not active_hypotheses:
            # A resumed conversation can arrive here after every candidate has
            # already been tested or marked unavailable.  That is a normal
            # terminal state, not a graph error.
            logger.info("No remaining hypotheses to investigate; synthesizing available evidence")
            return {
                "pending_step": None,
                "current_focus": None,
                "next_action": "finish",
                "status": InvestigationStatus.RUNNING,
            }

        metadata = await self._metadata_payload(state)
        metric_table = self._metric_table(state, metadata)
        must_measure_metric = bool(
            state.investigation_history
            and metric_table
            and not self._has_measured_table(state, metric_table[1])
        )
        result = await self.llm.structured(
            InvestigationPlan,
            prompts.load("investigation_plan"),
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
                prompts.load("investigation_plan_correction"),
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
        exhausted_hypothesis_ids = {
            hypothesis.id
            for hypothesis in state.hypotheses
            if hypothesis.status
            in {
                HypothesisStatus.REJECTED,
                HypothesisStatus.CONFIRMED,
                HypothesisStatus.NEEDS_MORE_EVIDENCE,
            }
        }
        if resolved_hypothesis_id in exhausted_hypothesis_ids:
            resolved_hypothesis_id = max(active_hypotheses, key=lambda hypothesis: hypothesis.confidence).id
            logger.info("Planner selected an exhausted hypothesis; continuing with another active hypothesis")
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
        metadata = await self._metadata_payload(state)
        selected_datasource = [source for source in metadata if source["id"] == datasource_id]
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
            prompts.load("investigation_query")
            + (
                f"\n\nThis query must measure before/after change from {metric_table[1]}."
                if must_measure_metric and metric_table
                else ""
            ),
            {
                "step": state.pending_step.model_dump(mode="json"),
                "selected_datasource": selected_datasource[0],
                "datasources": metadata,
                "investigation_context": self._state_payload(state),
            },
        )
        sql = result.sql
        lookup_steps: list[InvestigationStep] = []
        lookup_coverage: list[ScopeCoverage] = []
        lookup_query_count = 0
        for attempt in range(2):
            try:
                # The planner sometimes models the independently-run lookup as
                # a SQL CTE/table name (for example ``store_lookup``). It is not
                # a physical table: the approved lookup below supplies the
                # values. Remove only this constrained pseudo-table predicate;
                # all other unknown tables remain guardrail failures.
                sql = self._remove_cross_datasource_lookup_placeholders(
                    result.sql,
                    result.cross_datasource_lookups,
                    allowed_tables,
                )
                sql = validate_query_tables(
                    validate_read_query(self._case_insensitive_text_filters(sql)),
                    allowed_tables,
                )
                if must_measure_metric and metric_table and not query_references_table(sql, metric_table[1]):
                    raise UnsafeQueryError(f"Query must measure the reported metric from {metric_table[1]}")
                sql, lookup_steps, lookup_query_count, lookup_coverage = await self._apply_cross_datasource_lookups(
                    state,
                    datasource_id,
                    sql,
                    result.cross_datasource_lookups,
                    metadata,
                )
                await self._emit(
                    state,
                    "QueryStarted",
                    result.purpose,
                    datasource_id=datasource_id,
                    hypothesis_id=state.pending_step.hypothesis_id,
                )
                rows = await self.registry.get(datasource_id).execute_read_query(sql)
                break
            except (ProgrammingError, DBAPIError, SQLAlchemyError, UnsafeQueryError, KeyError) as exc:
                if attempt:
                    return await self._record_step_failure(state, exc)
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
                    prompts.load("investigation_query_correction")
                    + (
                        f"\n\nThe correction must measure before/after change from {metric_table[1]}."
                        if must_measure_metric and metric_table
                        else ""
                    ),
                    {
                        "step": state.pending_step.model_dump(mode="json"),
                        "selected_datasource": selected_datasource[0],
                        "datasources": metadata,
                        "previous_query": sql,
                        "rejection_reason": str(exc),
                        "investigation_context": self._state_payload(state),
                    },
                )
        else:  # pragma: no cover - the loop either breaks or re-raises
            raise RuntimeError("A query result was not produced")
        # Resolve schema-approved related display names before an observation reaches the
        # evidence model. Internal keys remain available only in persisted context.
        display_rows, name_steps, name_query_count, resolved_entities = await self._resolve_display_names(
            state, metadata, datasource_id, sql, rows[:100]
        )
        if self._has_duplicate_completed_result(
            state, datasource_id, state.pending_step.hypothesis_id, display_rows
        ):
            return await self._record_duplicate_step(
                state,
                lookup_steps,
                lookup_coverage,
                lookup_query_count,
                name_steps,
                name_query_count,
                sql,
            )
        deterministic_summary = summarize_query_rows(display_rows)
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
            value={
                "rows": display_rows,
                "deterministic_summary": deterministic_summary,
                "hypothesis_id": state.pending_step.hypothesis_id,
            },
            source=datasource_id,
        )
        return {
            "pending_step": step,
            "observations": [*state.observations, observation],
            "investigation_history": [*state.investigation_history, *lookup_steps, step, *name_steps],
            "query_count": state.query_count + lookup_query_count + 1 + name_query_count,
            "scope_coverage": [*state.scope_coverage, *lookup_coverage],
            "resolved_entities": self._merge_resolved_entities(state.resolved_entities, resolved_entities),
        }

    async def _record_step_failure(self, state: InvestigationState, exc: Exception) -> dict[str, Any]:
        """Contain an expected datasource failure to the hypothesis being tested."""
        step = state.pending_step
        if step is None or step.hypothesis_id is None:
            raise exc
        logger.exception(
            "Investigation check failed; continuing with other hypotheses: investigation=%s hypothesis=%s datasource=%s",
            state.investigation_id,
            step.hypothesis_id,
            step.datasource_id,
        )
        message = "This data check could not be completed, so the investigation will continue with other explanations."
        failed_step = step.model_copy(
            update={
                "action": "A planned data check was unavailable.",
                "rationale": message,
            }
        )
        hypotheses = [
            hypothesis.model_copy(
                update={"status": HypothesisStatus.NEEDS_MORE_EVIDENCE}
            )
            if hypothesis.id == step.hypothesis_id
            else hypothesis
            for hypothesis in state.hypotheses
        ]
        updated = next(hypothesis for hypothesis in hypotheses if hypothesis.id == step.hypothesis_id)
        await self._emit(
            state,
            "QueryFailed",
            message,
            datasource_id=step.datasource_id,
            hypothesis_id=step.hypothesis_id,
            error_code=type(exc).__name__,
        )
        await self._emit(
            state,
            "HypothesisUpdated",
            updated.description,
            hypothesis=updated.model_dump(mode="json"),
        )
        return {
            "hypotheses": hypotheses,
            "investigation_history": [*state.investigation_history, failed_step],
            "failed_checks": [*state.failed_checks, f"{updated.name}: {message}"],
            "pending_step": None,
            "current_focus": None,
            "next_action": "continue",
            "status": InvestigationStatus.RUNNING,
        }

    @staticmethod
    def _result_fingerprint(rows: list[dict[str, Any]]) -> str:
        canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def _has_duplicate_completed_result(
        cls,
        state: InvestigationState,
        datasource_id: str,
        hypothesis_id: str | None,
        rows: list[dict[str, Any]],
    ) -> bool:
        """A repeated result cannot add information, even if its SQL was rephrased."""
        candidate = cls._result_fingerprint(rows)
        for observation in state.observations:
            if observation.source != datasource_id or not isinstance(observation.value, dict):
                continue
            if observation.value.get("hypothesis_id") != hypothesis_id:
                continue
            prior_rows = observation.value.get("rows")
            if isinstance(prior_rows, list) and all(isinstance(row, dict) for row in prior_rows):
                if cls._result_fingerprint(prior_rows) == candidate:
                    return True
        return False

    async def _record_duplicate_step(
        self,
        state: InvestigationState,
        lookup_steps: list[InvestigationStep],
        lookup_coverage: list[ScopeCoverage],
        lookup_query_count: int,
        name_steps: list[InvestigationStep],
        name_query_count: int,
        sql: str,
    ) -> dict[str, Any]:
        """Contain a repeated completed check and force the graph to seek new evidence."""
        step = state.pending_step
        if step is None or step.hypothesis_id is None:
            raise RuntimeError("A duplicate result was produced without an active investigation step")
        logger.warning(
            "Repeated investigation result; exhausting current hypothesis: investigation=%s hypothesis=%s datasource=%s",
            state.investigation_id,
            step.hypothesis_id,
            step.datasource_id,
        )
        message = "This check repeated an earlier result, so the investigation is moving to other explanations."
        exhausted_step = step.model_copy(
            update={
                "action": "A repeated data check was skipped after returning no new information.",
                "rationale": message,
                "query": sql,
            }
        )
        hypotheses = [
            hypothesis.model_copy(update={"status": HypothesisStatus.NEEDS_MORE_EVIDENCE})
            if hypothesis.id == step.hypothesis_id
            else hypothesis
            for hypothesis in state.hypotheses
        ]
        updated = next(hypothesis for hypothesis in hypotheses if hypothesis.id == step.hypothesis_id)
        await self._emit(
            state,
            "QuerySkipped",
            message,
            datasource_id=step.datasource_id,
            hypothesis_id=step.hypothesis_id,
            error_code="DuplicateResult",
        )
        await self._emit(
            state,
            "HypothesisUpdated",
            updated.description,
            hypothesis=updated.model_dump(mode="json"),
        )
        return {
            "hypotheses": hypotheses,
            "investigation_history": [
                *state.investigation_history,
                *lookup_steps,
                exhausted_step,
                *name_steps,
            ],
            "query_count": state.query_count + lookup_query_count + 1 + name_query_count,
            "scope_coverage": [*state.scope_coverage, *lookup_coverage],
            "failed_checks": [*state.failed_checks, f"{updated.name}: {message}"],
            "pending_step": None,
            "current_focus": None,
            "next_action": "continue",
            "status": InvestigationStatus.RUNNING,
        }

    @staticmethod
    def _route_after_selection(state: InvestigationState) -> str:
        return "synthesize" if state.next_action == "finish" or state.pending_step is None else "execute"

    @staticmethod
    def _route_after_execution(state: InvestigationState) -> str:
        return "decide" if state.pending_step is None else "interpret"

    async def _apply_cross_datasource_lookups(
        self,
        state: InvestigationState,
        primary_datasource_id: str,
        primary_sql: str,
        lookups: list[CrossDatasourceLookup],
        metadata: list[dict[str, Any]],
    ) -> tuple[str, list[InvestigationStep], int, list[ScopeCoverage]]:
        """Apply approved related-source lookups without ever joining database connections."""
        if not lookups:
            return primary_sql, [], 0, []

        relations = {
            relation.id: relation
            for relation in self.registry.approved_cross_relations([source.id for source in state.datasources])
        }
        source_metadata = {source["id"]: source for source in metadata}
        filtered_sql = primary_sql
        steps: list[InvestigationStep] = []
        coverage: list[ScopeCoverage] = []
        used_relations: set[str] = set()

        for lookup in lookups:
            if lookup.relation_id in used_relations:
                raise UnsafeQueryError("A cross-datasource relationship can be used only once per query")
            relation = relations.get(lookup.relation_id)
            if relation is None:
                # Some models use a descriptive alias (for example,
                # ``lookup_milan_stores``) instead of the approved relation ID.
                # Resolve it only when its independently planned lookup reads
                # the opposite endpoint of exactly one approved relationship.
                candidates = [
                    candidate
                    for candidate in relations.values()
                    if candidate.confirmed
                    and self._can_traverse_relationship(
                        candidate, primary_datasource_id, lookup.datasource_id, lookup.sql
                    )
                ]
                if len(candidates) == 1:
                    relation = candidates[0]
                    logger.warning(
                        "Resolved planner lookup alias to approved relationship: alias=%s relation=%s",
                        lookup.relation_id,
                        relation.id,
                    )
            if relation is None or not relation.confirmed:
                raise UnsafeQueryError("Cross-datasource lookup requires an approved relationship")
            primary_field, lookup_field = self._relationship_traversal(
                relation, primary_datasource_id, lookup.datasource_id
            )
            lookup_source = source_metadata.get(lookup.datasource_id)
            if lookup_source is None:
                raise UnsafeQueryError("Cross-datasource lookup selected an unavailable datasource")

            lookup_table = lookup_field.rsplit(".", 1)[0]
            if not query_references_table(lookup.sql, lookup_table):
                raise UnsafeQueryError("Cross-datasource lookup must read the approved related table")
            validate_cross_datasource_lookup_projection(
                lookup.sql, relationship_field=lookup_field
            )
            lookup_sql = validate_query_tables(
                validate_read_query(self._case_insensitive_text_filters(lookup.sql)),
                {table["name"] for table in lookup_source["tables"]},
            )
            await self._emit(
                state,
                "QueryStarted",
                lookup.purpose,
                datasource_id=lookup.datasource_id,
                hypothesis_id=state.pending_step.hypothesis_id if state.pending_step else None,
            )
            lookup_rows = await self.registry.get(lookup.datasource_id).execute_read_query(lookup_sql)
            await self._emit(
                state,
                "QueryCompleted",
                "The related records are ready.",
                datasource_id=lookup.datasource_id,
                hypothesis_id=state.pending_step.hypothesis_id if state.pending_step else None,
                row_count=len(lookup_rows),
            )
            if any(CROSS_DATASOURCE_LOOKUP_KEY not in row for row in lookup_rows):
                raise UnsafeQueryError(
                    "Cross-datasource lookup must return the approved relationship key"
                )
            values = list(
                dict.fromkeys(
                    str(row[CROSS_DATASOURCE_LOOKUP_KEY])
                    for row in lookup_rows
                    if row.get(CROSS_DATASOURCE_LOOKUP_KEY) is not None
                )
            )[:500]
            coverage.append(
                ScopeCoverage(
                    description=lookup.purpose,
                    matched_records=len(values),
                    limited=len(lookup_rows) > len(values) or len(lookup_rows) >= 500,
                )
            )
            # A lookup can safely constrain every primary field connected to
            # the same endpoint. This covers both normal and reverse traversal
            # while keeping every database query independent.
            applicable_relations = [
                candidate
                for candidate in relations.values()
                if self._is_applicable_relationship(
                    candidate,
                    primary_datasource_id,
                    lookup.datasource_id,
                    lookup_field,
                    filtered_sql,
                )
            ]
            if relation not in applicable_relations:
                raise UnsafeQueryError("Cross-datasource lookup does not apply to a table in the primary query")
            applied_fields: set[str] = set()
            for applicable_relation in applicable_relations:
                if applicable_relation.id in used_relations:
                    continue
                applicable_primary_field, _ = self._relationship_traversal(
                    applicable_relation, primary_datasource_id, lookup.datasource_id
                )
                if applicable_primary_field in applied_fields:
                    continue
                filtered_sql = self._add_relation_filter(filtered_sql, applicable_primary_field, values)
                used_relations.add(applicable_relation.id)
                applied_fields.add(applicable_primary_field)
            steps.append(
                InvestigationStep(
                    iteration=state.pending_step.iteration if state.pending_step else 0,
                    hypothesis_id=state.pending_step.hypothesis_id if state.pending_step else None,
                    action=lookup.purpose,
                    datasource_id=lookup.datasource_id,
                    rationale="Use an approved relationship without joining separate databases.",
                    query=lookup_sql,
                )
            )
            logger.info(
                "Applied approved cross-datasource lookup: relations=%s lookup_source=%s primary_source=%s values=%d",
                [item.id for item in applicable_relations],
                lookup.datasource_id,
                primary_datasource_id,
                len(values),
            )
        return filtered_sql, steps, len(steps), coverage

    @staticmethod
    def _relationship_traversal(
        relation: Any, primary_datasource_id: str, lookup_datasource_id: str
    ) -> tuple[str, str]:
        """Return the primary and lookup fields for either approved edge direction."""
        if (
            relation.source_datasource == primary_datasource_id
            and relation.target_datasource == lookup_datasource_id
        ):
            return relation.source_field, relation.target_field
        if (
            relation.target_datasource == primary_datasource_id
            and relation.source_datasource == lookup_datasource_id
        ):
            return relation.target_field, relation.source_field
        raise UnsafeQueryError("Cross-datasource lookup does not match the selected datasource")

    @classmethod
    def _can_traverse_relationship(
        cls, relation: Any, primary_datasource_id: str, lookup_datasource_id: str, lookup_sql: str
    ) -> bool:
        try:
            _, lookup_field = cls._relationship_traversal(
                relation, primary_datasource_id, lookup_datasource_id
            )
        except UnsafeQueryError:
            return False
        return query_references_table(lookup_sql, lookup_field.rsplit(".", 1)[0])

    @classmethod
    def _is_applicable_relationship(
        cls,
        relation: Any,
        primary_datasource_id: str,
        lookup_datasource_id: str,
        lookup_field: str,
        primary_sql: str,
    ) -> bool:
        try:
            primary_field, candidate_lookup_field = cls._relationship_traversal(
                relation, primary_datasource_id, lookup_datasource_id
            )
        except UnsafeQueryError:
            return False
        return (
            candidate_lookup_field == lookup_field
            and query_references_table(primary_sql, primary_field.rsplit(".", 1)[0])
        )

    @staticmethod
    def _remove_cross_datasource_lookup_placeholders(
        sql: str,
        lookups: list[CrossDatasourceLookup],
        allowed_tables: set[str],
    ) -> str:
        """Remove an LLM-only ``*_lookup`` IN predicate before real lookup binding.

        The replacement is deliberately narrow. A normal unknown table still
        reaches ``validate_query_tables`` and is rejected; only a pseudo table
        with a matching separately planned lookup can be replaced by TRUE.
        """
        if not lookups:
            return sql
        try:
            statement = sqlglot.parse_one(sql, read="postgres")
        except sqlglot.errors.ParseError:
            return sql
        allowed_names = {table.rsplit(".", 1)[-1] for table in allowed_tables}
        planned_placeholders = {lookup.relation_id for lookup in lookups}
        replaced = 0
        for predicate in list(statement.find_all(exp.In)):
            query = predicate.args.get("query")
            tables = list(query.find_all(exp.Table)) if isinstance(query, exp.Subquery) else []
            if len(tables) != 1:
                continue
            table_name = tables[0].name
            # Prefer the explicit alias declared beside the independently
            # executable lookup. Keep a narrow compatibility path for a
            # single legacy ``product_lookup``-style alias.
            is_legacy_single_lookup = len(lookups) == 1 and table_name.endswith("_lookup")
            if table_name in allowed_names or (
                table_name not in planned_placeholders and not is_legacy_single_lookup
            ):
                continue
            predicate.replace(exp.EQ(this=exp.Literal.number(1), expression=exp.Literal.number(1)))
            replaced += 1
        if replaced:
            logger.warning(
                "Removed %d planner-only cross-datasource lookup predicates; approved lookups will bind the filters",
                replaced,
            )
        return statement.sql(dialect="postgres")

    async def _interpret(self, state: InvestigationState) -> dict[str, Any]:
        latest = state.observations[-1] if state.observations else None
        result = await self.llm.structured(
            EvidenceAssessment,
            prompts.load("evidence_assessment"),
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
        ambiguity = self._clarification_to_request(state, result.ambiguity)
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
            "pending_human_question": None if premise_rejected or no_material_decline else ambiguity,
            "human_resume_node": (
                "select_investigation" if ambiguity and not premise_rejected and not no_material_decline else None
            ),
            "status": (
                InvestigationStatus.WAITING_FOR_HUMAN
                if ambiguity and not premise_rejected and not no_material_decline
                else InvestigationStatus.RUNNING
            ),
            "paused_at": datetime.now(UTC) if ambiguity and not premise_rejected and not no_material_decline else None,
        }

    def _budget_exhausted(self, state: InvestigationState) -> bool:
        if settings.ignore_investigation_limits:
            return False
        elapsed = (datetime.now(UTC) - state.started_at).total_seconds() - state.paused_duration_seconds
        if state.paused_at is not None:
            elapsed -= max(0, (datetime.now(UTC) - state.paused_at).total_seconds())
        return (
            state.iteration >= state.limits.max_iterations
            or state.query_count >= state.limits.max_sql_queries
            or elapsed >= state.limits.max_duration_seconds
        )

    async def _select_external_research(self, state: InvestigationState) -> dict[str, Any]:
        return await self.external_research.select(state)

    async def _research_external(self, state: InvestigationState) -> dict[str, Any]:
        result = await self.external_research.execute(state)
        failed_ids = result.pop("failed_external_hypothesis_ids", [])
        failure_diagnostics = {
            item["hypothesis_id"]: item["diagnostic"]
            for item in result.pop("failed_external_checks", [])
        }
        if not failed_ids:
            return result
        failed_set = set(failed_ids)
        hypotheses = [
            hypothesis.model_copy(update={"status": HypothesisStatus.NEEDS_MORE_EVIDENCE})
            if hypothesis.id in failed_set
            else hypothesis
            for hypothesis in state.hypotheses
        ]
        for hypothesis in hypotheses:
            if hypothesis.id not in failed_set:
                continue
            await self._emit(
                state,
                "HypothesisUpdated",
                hypothesis.description,
                hypothesis=hypothesis.model_dump(mode="json"),
            )
        return {
            **result,
            "hypotheses": hypotheses,
            "failed_checks": [
                *state.failed_checks,
                *[
                    f"{hypothesis.name}: External check unavailable ({failure_diagnostics.get(hypothesis.id, 'unknown failure')}); "
                    "other explanations were still investigated."
                    for hypothesis in hypotheses
                    if hypothesis.id in failed_set
                ],
            ],
        }

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

        available_agents = self.external_research.available_agents()
        can_research_externally = bool(
            state.query_count
            and available_agents
            and (
                settings.ignore_investigation_limits
                or state.external_call_count < state.limits.max_external_calls
            )
            and self.external_research.has_eligible_check(state, available_agents)
        )
        untested_external_hypotheses = [
            hypothesis
            for hypothesis in state.hypotheses
            if hypothesis.id in self.external_research.eligible_agents_by_hypothesis(state, available_agents)
        ]
        if can_research_externally and untested_external_hypotheses:
            logger.info(
                "Selecting external research before synthesis: candidates=%s",
                [hypothesis.name for hypothesis in untested_external_hypotheses],
            )
            return {"next_action": "external", "status": InvestigationStatus.RUNNING}

        # Let a relevant configured external check complete the investigation
        # after its internal baseline, even when that baseline consumed the
        # internal runtime allowance. The external-call budget remains bounded
        # independently; after this check, the next decision will synthesize.
        if self._budget_exhausted(state):
            return {"next_action": "finish"}

        testable_hypotheses = [
            hypothesis
            for hypothesis in state.hypotheses
            if hypothesis.status
            not in {
                HypothesisStatus.REJECTED,
                HypothesisStatus.CONFIRMED,
                HypothesisStatus.NEEDS_MORE_EVIDENCE,
            }
        ]
        if not testable_hypotheses:
            return {"next_action": "finish", "status": InvestigationStatus.RUNNING}

        result = await self.llm.structured(
            InvestigationDecision,
            prompts.load("investigation_decision"),
            {
                **self._state_payload(state),
                "external_research_available": can_research_externally,
                "available_research_agents": available_agents if can_research_externally else [],
            },
        )
        if result.action == "external" and not can_research_externally:
            logger.info("Continuing internal investigation because external research is not available")
            return {"next_action": "continue", "status": InvestigationStatus.RUNNING}
        if result.action == "finish" and not (has_high_confidence_root and has_direct_evidence):
            logger.info("Continuing investigation because finality criteria have not been met")
            return {"next_action": "continue", "status": InvestigationStatus.RUNNING}
        return {"next_action": result.action, "status": InvestigationStatus.RUNNING}

    def _route(self, state: InvestigationState) -> str:
        if state.next_action == "finish":
            return "finish"
        if state.next_action == "external":
            return "external"
        return "continue"

    async def _synthesize(self, state: InvestigationState) -> dict[str, Any]:
        def completed_conversation_turns(analysis: FinalAnalysis) -> list[ConversationTurn]:
            """Persist every completed turn, regardless of which investigation path produced it."""
            return [
                *state.conversation_turns,
                ConversationTurn(
                    question=state.current_conversation_question or state.question,
                    answer=analysis,
                ),
            ]

        if state.request_type == "direct_answer":
            direct_result = next(
                (
                    observation.value
                    for observation in reversed(state.observations)
                    if isinstance(observation.value, dict) and isinstance(observation.value.get("rows"), list)
                ),
                None,
            )
            if self._has_unavailable_aggregate_result(direct_result):
                analysis = FinalAnalysis(
                    likely_root_cause="Requested total is not available from the connected data",
                    confidence=0.95,
                    evidence=[
                        Evidence(
                            id="unavailable_requested_total",
                            description="The selected records do not contain a revenue value that can be matched to the requested category and period.",
                            source="selected connected records",
                            relationship="direct",
                            confidence=0.95,
                            data=[EvidenceDataPoint(field="Requested total", value="Not available")],
                        )
                    ],
                    rejected_hypotheses=[],
                    external_findings=[],
                    caveats=[
                        "This does not mean the total was zero. The connected revenue records cannot attribute a value to the requested category.",
                        "An exact total requires revenue-bearing order records to carry that category or to share an approved order-level relationship with the records that do.",
                    ],
                    summary=(
                        "The requested total cannot be calculated from the connected data because the revenue "
                        "records cannot be matched to the requested category for this period."
                    ),
                )
            else:
                result = await self.llm.structured(
                    Synthesis,
                    prompts.load("direct_synthesis"),
                    {**self._state_payload(state), "direct_result": direct_result},
                )
                analysis = result.analysis
            named_entity_ranking = self._named_entity_ranking_analysis(state, analysis)
            if named_entity_ranking is not None:
                analysis = named_entity_ranking
            if analysis.likely_root_cause is None:
                analysis = analysis.model_copy(update={"likely_root_cause": "Direct answer"})
            analysis = self._redact_resolved_entity_references(analysis, state.resolved_entities)
            analysis = analysis.model_copy(
                update={"follow_up_question": analysis.follow_up_question or await self._contextual_follow_up_question(state)}
            )
            analysis = self._with_scope_caveats(state, analysis)
            conversation_turns = completed_conversation_turns(analysis)
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
            changes = self._measured_metric_percentage_changes(direct_evidence)
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
            analysis = analysis.model_copy(update={"follow_up_question": await self._contextual_follow_up_question(state)})
            analysis = self._with_scope_caveats(state, analysis)
            conversation_turns = completed_conversation_turns(analysis)
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
            analysis = analysis.model_copy(update={"follow_up_question": await self._contextual_follow_up_question(state)})
            analysis = self._with_scope_caveats(state, analysis)
            conversation_turns = completed_conversation_turns(analysis)
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
        result = await self.llm.structured(
            Synthesis,
            prompts.load("investigation_synthesis"),
            self._state_payload(state),
        )
        analysis = result.analysis.model_copy(update={"external_findings": state.external_findings})
        if not analysis.follow_up_question:
            analysis = analysis.model_copy(update={"follow_up_question": await self._contextual_follow_up_question(state)})
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
        analysis = self._with_scope_caveats(state, analysis)
        status = InvestigationStatus.INSUFFICIENT_EVIDENCE if analysis.likely_root_cause is None else InvestigationStatus.COMPLETED
        conversation_turns = completed_conversation_turns(analysis)
        await self._emit(
            state,
            "InvestigationCompleted",
            analysis.summary,
            status=status,
            budget_exhausted=exhausted,
            final_analysis=analysis.model_dump(mode="json"),
            conversation_turns=[turn.model_dump(mode="json") for turn in conversation_turns],
        )
        return {
            "final_analysis": analysis,
            "confidence": analysis.confidence,
            "conversation_turns": conversation_turns,
            "status": status,
        }
