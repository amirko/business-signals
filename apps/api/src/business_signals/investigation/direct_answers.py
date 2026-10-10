"""Factual-answer retrieval, entity resolution, and presentation safeguards."""

from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any

import sqlglot
from sqlalchemy.exc import ProgrammingError
from sqlglot import expressions as exp

from business_signals.analytics import summarize_query_rows
from business_signals.datasources.safety import (
    UnsafeQueryError,
    query_references_table,
    validate_query_tables,
    validate_read_query,
)
from business_signals.llm import DirectAnswerPlan, DirectAnswerQuery
from business_signals.models import (
    Evidence,
    EvidenceDataPoint,
    FinalAnalysis,
    InvestigationState,
    InvestigationStatus,
    InvestigationStep,
    Observation,
    QueryScope,
    ResolvedEntityReference,
)
from business_signals.prompt_catalog import prompts

logger = logging.getLogger("uvicorn.error")


class DirectAnswerMixin:
    """Direct-answer path shared by the LangGraph investigation workflow."""

    @staticmethod
    def _redact_internal_identifier_columns(rows: list[dict[str, Any]], identifier_columns: set[str]) -> list[dict[str, Any]]:
        """Keep schema-declared keys inside execution, never in a direct-answer result."""
        return [{key: value for key, value in row.items() if key not in identifier_columns} for row in rows]

    @staticmethod
    def _customer_facing_name(name: Any, identifier: Any) -> str:
        """Drop a display-name suffix only when it duplicates a technical identifier suffix."""
        identifier_suffix = re.search(r"(\d+)$", str(identifier))
        if identifier_suffix:
            return re.sub(rf"\s+{re.escape(identifier_suffix.group(1))}$", "", str(name))
        return str(name)

    @classmethod
    def _remove_redundant_entity_labels(cls, rows: list[dict[str, Any]], entities: list[ResolvedEntityReference]) -> list[dict[str, Any]]:
        """Keep the safe display label, not an alternate catalog label derived from an internal key."""
        if not entities:
            return rows
        cleaned_rows: list[dict[str, Any]] = []
        for row in rows:
            cleaned_rows.append(
                {
                    key: value
                    for key, value in row.items()
                    if key == "display_name"
                    or not any(
                        isinstance(value, str) and cls._customer_facing_name(value, entity.identifier) == entity.display_name for entity in entities
                    )
                }
            )
        return cleaned_rows

    @classmethod
    def _redact_resolved_entity_references(cls, analysis: FinalAnalysis, entities: list[ResolvedEntityReference]) -> FinalAnalysis:
        """Guarantee that final text uses the resolved display name rather than a retained key."""
        if not entities:
            return analysis

        def clean_text(value: str) -> str:
            cleaned = value
            for entity in entities:
                suffix = re.search(r"(\d+)$", entity.identifier)
                if suffix:
                    # Handles a source label such as "Toiletry Bag 119 (Toiletry Bag)" as well
                    # as the simpler "Toiletry Bag 119", without removing meaningful numbers.
                    label = re.escape(entity.display_name)
                    number = re.escape(suffix.group(1))
                    cleaned = re.sub(
                        rf"\b{label}\s+{number}\s*\(\s*{label}\s*\)",
                        entity.display_name,
                        cleaned,
                        flags=re.IGNORECASE,
                    )
                    cleaned = re.sub(
                        rf"\b{label}\s+{number}\b",
                        entity.display_name,
                        cleaned,
                        flags=re.IGNORECASE,
                    )
                cleaned = re.sub(rf"\b{re.escape(entity.identifier)}\b", entity.display_name, cleaned)
            return cleaned

        evidence = [
            item.model_copy(
                update={
                    "description": clean_text(item.description),
                    "source": clean_text(item.source),
                    "data": [
                        point.model_copy(
                            update={
                                "field": clean_text(point.field),
                                "value": clean_text(point.value) if isinstance(point.value, str) else point.value,
                            }
                        )
                        for point in item.data
                    ],
                }
            )
            for item in analysis.evidence
        ]
        return analysis.model_copy(
            update={
                "likely_root_cause": clean_text(analysis.likely_root_cause) if analysis.likely_root_cause else None,
                "summary": clean_text(analysis.summary),
                "caveats": [clean_text(item) for item in analysis.caveats],
                "evidence": evidence,
                "follow_up_question": clean_text(analysis.follow_up_question) if analysis.follow_up_question else None,
            }
        )

    @staticmethod
    def _infer_display_column(table: dict[str, Any]) -> str | None:
        """Infer an entity label from schema roles and types, never from a hard-coded column name."""
        key_columns = {column["name"] for column in table["columns"] if column.get("pk")}
        key_columns.update(foreign_key["column_name"] for foreign_key in table.get("foreign_keys", []) if foreign_key.get("column_name"))
        textual_columns = [
            column
            for column in table["columns"]
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
    def _case_insensitive_text_filters(sql: str) -> str:
        """Make model-generated literal text filters resilient to stored value casing.

        Schema discovery describes types but not every permitted categorical value. A generated
        text literal must match the stored category regardless of capitalization rather than
        silently producing a convincing zero-row result. Only a column-to-string-literal equality
        is changed; joins, numeric comparisons, and non-literal expressions remain untouched.
        """
        try:
            statement = sqlglot.parse_one(sql, read="postgres")
        except sqlglot.errors.ParseError:
            return sql
        replacements: list[tuple[exp.EQ, exp.Column, exp.Literal]] = []
        for equality in statement.find_all(exp.EQ):
            left, right = equality.this, equality.expression
            if isinstance(left, exp.Column) and isinstance(right, exp.Literal) and right.is_string:
                replacements.append((equality, left, right))
            elif isinstance(right, exp.Column) and isinstance(left, exp.Literal) and left.is_string:
                replacements.append((equality, right, left))
        for equality, column, value in replacements:
            equality.replace(exp.ILike(this=column.copy(), expression=value.copy()))
        return statement.sql(dialect="postgres")

    @staticmethod
    def _normalize_before_after_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Calculate standard before/after rates outside SQL type semantics.

        Query planners may correctly return integer totals yet accidentally use
        PostgreSQL integer division for the derived rate. The two totals are
        the authoritative facts, so normalize only this documented result shape
        using decimal arithmetic before evidence or a user-facing answer sees it.
        """
        normalized: list[dict[str, Any]] = []
        for row in rows:
            if not {"earlier_value", "later_value"}.issubset(row):
                normalized.append(row)
                continue
            try:
                earlier = Decimal(str(row["earlier_value"]))
                later = Decimal(str(row["later_value"]))
            except (InvalidOperation, ValueError, TypeError):
                normalized.append(row)
                continue
            updated = dict(row)
            updated["rate_change_percent"] = None if earlier == 0 else float((later - earlier) * Decimal("100") / earlier)
            normalized.append(updated)
        return normalized

    @staticmethod
    def _validate_categorical_filter_values(sql: str, source_metadata: dict[str, Any]) -> None:
        """Reject made-up values for schema-discovered controlled vocabularies.

        Values are checked only for fields with a finite, discovered vocabulary.
        This remains independent of names such as ``channel`` or ``region``.
        """
        try:
            statement = sqlglot.parse_one(sql, read="postgres")
        except sqlglot.errors.ParseError as exc:
            raise UnsafeQueryError(f"Invalid SQL: {exc}") from exc

        known_by_table: dict[str, dict[str, set[str]]] = {}
        column_tables: dict[str, set[str]] = {}
        for table in source_metadata.get("tables", []):
            table_name = str(table.get("name", ""))
            known_columns: dict[str, set[str]] = {}
            for column in table.get("columns", []):
                values = {str(value).casefold() for value in column.get("representative_values", []) if isinstance(value, str)}
                if values:
                    column_name = str(column.get("name", ""))
                    known_columns[column_name] = values
                    column_tables.setdefault(column_name, set()).add(table_name)
            if known_columns:
                known_by_table[table_name] = known_columns
        if not known_by_table:
            return

        aliases: dict[str, str] = {}
        cte_names = {cte.alias_or_name for cte in statement.find_all(exp.CTE)}
        for table in statement.find_all(exp.Table):
            if table.name in cte_names:
                continue
            table_name = f"{table.db}.{table.name}" if table.db else table.name
            matching = next(
                (candidate for candidate in known_by_table if candidate == table_name or candidate.rsplit(".", 1)[-1] == table_name),
                None,
            )
            if matching:
                aliases[table.alias_or_name] = matching
                aliases[table.name] = matching

        def column_for(expression: exp.Expression) -> exp.Column | None:
            if isinstance(expression, exp.Column):
                return expression
            columns = list(expression.find_all(exp.Column))
            return columns[0] if len(columns) == 1 else None

        def known_values(column: exp.Column) -> tuple[str, set[str]] | None:
            table_name = aliases.get(column.table) if column.table else None
            if table_name is None:
                candidates = list(column_tables.get(column.name, set()))
                table_name = candidates[0] if len(candidates) == 1 else None
            if table_name is None:
                return None
            values = known_by_table.get(table_name, {}).get(column.name)
            return (f"{table_name}.{column.name}", values) if values else None

        def literal_values(expression: exp.Expression) -> list[str]:
            return [str(expression.this)] if isinstance(expression, exp.Literal) and expression.is_string else []

        checks: list[tuple[exp.Column, list[str]]] = []
        for expression in statement.walk():
            if isinstance(expression, (exp.EQ, exp.ILike)):
                left, right = expression.this, expression.expression
                left_column, right_column = column_for(left), column_for(right)
                if left_column is not None:
                    checks.append((left_column, literal_values(right)))
                elif right_column is not None:
                    checks.append((right_column, literal_values(left)))
            elif isinstance(expression, exp.In):
                column = column_for(expression.this)
                if column is not None:
                    checks.append(
                        (
                            column,
                            [str(value.this) for value in expression.expressions if isinstance(value, exp.Literal) and value.is_string],
                        )
                    )

        invalid: dict[str, tuple[list[str], set[str]]] = {}
        for column, values in checks:
            known = known_values(column)
            if known is None or not values:
                continue
            field, permitted = known
            unsupported = []
            for value in values:
                candidate = value.casefold()
                if candidate in permitted:
                    continue
                pattern = candidate.strip("%")
                if pattern and any(pattern in permitted_value for permitted_value in permitted):
                    continue
                unsupported.append(value)
            if unsupported:
                previous = invalid.get(field)
                invalid[field] = ([*(previous[0] if previous else []), *unsupported], permitted)
        if invalid:
            details = "; ".join(
                f"{field} used {sorted(set(values))}; known values are {sorted(permitted)}" for field, (values, permitted) in sorted(invalid.items())
            )
            raise UnsafeQueryError(f"Query uses values not present in the discovered controlled vocabulary: {details}")

    @staticmethod
    def _has_unavailable_aggregate_result(result: Any) -> bool:
        """Recognize a SQL aggregate over no attributable records without presenting it as zero."""
        if not isinstance(result, dict) or not isinstance(result.get("rows"), list):
            return False
        rows = [row for row in result["rows"] if isinstance(row, dict)]
        if not rows:
            return False
        values = [value for row in rows for value in row.values()]
        has_unavailable_value = any(value is None for value in values)
        has_numeric_value = any(isinstance(value, (int, float)) and not isinstance(value, bool) for value in values)
        return has_unavailable_value and not has_numeric_value

    @staticmethod
    def _named_entity_ranking_analysis(state: InvestigationState, draft: FinalAnalysis) -> FinalAnalysis | None:
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

        current_question = (state.current_conversation_question or state.question).casefold()
        ranking_terms = ("top", "most", "best", "highest", "popular", "least")
        # A grouped follow-up for one retained item has the same display name on every row;
        # it is a breakdown, never a ranking of that item repeated once per group.
        if not any(term in current_question for term in ranking_terms) or len({str(row["display_name"]) for row in rows}) < 2:
            return None

        metric_key = next(
            (key for key, value in rows[0].items() if isinstance(value, (int, float)) and not isinstance(value, bool)),
            None,
        )
        metric_label = state.metric_definition.name if state.metric_definition else "the requested measure"

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
            if "names were not available" not in caveat.casefold() and "totals (exact units sold" not in caveat.casefold()
        ]
        return draft.model_copy(
            update={
                "likely_root_cause": f"Requested ranking by {metric_label}",
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
        metadata = await self._metadata_payload(state)
        if result_observation is not None:
            rows = [row for row in result_observation.value["rows"] if isinstance(row, dict)]
            first_display_name = next((str(row["display_name"]) for row in rows if row.get("display_name")), None)
            source = next((item for item in metadata if item["id"] == result_observation.source), None)
            has_time_series = bool(source and any(table.get("roles", {}).get("time_fields", []) for table in source["tables"]))
            ranking_terms = ("top", "most", "best", "highest", "popular", "valuable")
            if first_display_name and has_time_series and any(term in state.question.casefold() for term in ranking_terms):
                metric = state.metric_definition.name if state.metric_definition else "the requested measure"
                return f"Would you like to see how {metric} changed over time for {first_display_name}?"
        if any(table.get("roles", {}).get("time_fields", []) for source in metadata for table in source["tables"]):
            return "Would you like to see how this result changed over time?"
        return "Would you like a more detailed breakdown of this result?"

    async def _answer_directly(self, state: InvestigationState) -> dict[str, Any]:
        """Answer factual requests with one or more isolated, guarded datasource queries."""
        metadata = await self._metadata_payload(state)
        plan = await self.llm.structured(
            DirectAnswerPlan,
            prompts.load("direct_answer_plan"),
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
                prompts.load("direct_answer_datasource_correction"),
                {
                    "question": state.question,
                    "datasources": metadata,
                    "previous_plan": plan.model_dump(mode="json"),
                },
            )
        if self._defers_datasource_selection(plan):
            raise UnsafeQueryError("Direct-answer planner attempted to defer datasource selection")
        selected_source_ids = {
            query.datasource_id
            for query in [
                DirectAnswerQuery(datasource_id=plan.datasource_id, sql=plan.sql, purpose=plan.purpose),
                *plan.supporting_queries,
            ]
            if (query.datasource_id in {source.id for source in state.datasources} and re.search(r"'(?:[^']|'')*'", query.sql) is not None)
        }
        if selected_source_ids:
            # Query planning is already complete, so enrich just the selected
            # sources before the execution guardrail (and any repair prompt).
            metadata = await self._metadata_payload(state, include_categorical_values_for=selected_source_ids)
        missing_lookup_relation = self._requested_relation_without_lookup(state, plan)
        if missing_lookup_relation is not None:
            logger.warning(
                "Direct-answer plan omitted a requested readable lookup: relation=%s",
                missing_lookup_relation.id,
            )
            plan = await self.llm.structured(
                DirectAnswerPlan,
                prompts.load("direct_answer_relationship_correction"),
                {
                    "question": state.question,
                    "previous_plan": plan.model_dump(mode="json"),
                    "required_relationship": missing_lookup_relation.model_dump(mode="json"),
                    "datasources": metadata,
                },
            )
            if self._requested_relation_without_lookup(state, plan) is not None:
                raise UnsafeQueryError("Direct-answer plan omitted the readable lookup required for the requested breakdown")
        query_plans = [
            DirectAnswerQuery(datasource_id=plan.datasource_id, sql=plan.sql, purpose=plan.purpose),
            *plan.supporting_queries,
        ]
        logger.info(
            "Direct-answer plan ready: primary_datasource=%s supporting_queries=%d combination=%s retained_entities=%d",
            plan.datasource_id,
            len(plan.supporting_queries),
            plan.combination.model_dump(mode="json") if plan.combination else None,
            len(state.resolved_entities),
        )
        query_plans[0] = self._apply_inherited_scope(state, query_plans[0])
        original_primary_sql = query_plans[0].sql
        query_plans[0] = self._constrain_query_to_referenced_entity(state, query_plans[0])
        primary_entity_filter_applied = query_plans[0].sql != original_primary_sql
        executed: list[tuple[list[dict[str, Any]], str, str, str] | None] = [None] * len(query_plans)

        # A lookup query can identify a small set of entities (for example, the
        # catalog record for "Lantern").  Apply an approved cross-datasource
        # relationship to the main query *before* it is grouped and limited.
        # Filtering the returned page afterwards can incorrectly discard a
        # matching entity that appears beyond the query's safety limit.
        if plan.combination is not None and plan.combination.operation == "intersection":
            combination = plan.combination
            if combination.supporting_query_index >= len(plan.supporting_queries):
                raise UnsafeQueryError("Direct-answer combination refers to a missing supporting query")
            primary_fields = self._combination_fields(combination.primary_key)
            supporting_fields = self._combination_fields(combination.supporting_key)
            supporting_index = combination.supporting_query_index + 1
            executed[supporting_index] = await self._execute_direct_query(state, query_plans[supporting_index], metadata)
            relation = (
                self._relation_for_primary_filter(
                    state,
                    query_plans[0],
                    query_plans[supporting_index],
                    primary_fields[0],
                    metadata,
                )
                if len(primary_fields) == len(supporting_fields) == 1
                else None
            )
            supporting_rows = executed[supporting_index][0]
            values = list(dict.fromkeys(str(row[supporting_fields[0]]) for row in supporting_rows if row.get(supporting_fields[0]) is not None))[:100]
            if relation is not None and values:
                query_plans[0] = query_plans[0].model_copy(
                    update={"sql": self._add_relation_filter(query_plans[0].sql, relation.source_field, values)}
                )
                logger.info(
                    "Applying approved cross-datasource relationship before primary query: relation=%s values=%d",
                    relation.id,
                    len(values),
                )

        for index, query in enumerate(query_plans):
            if executed[index] is None:
                executed[index] = await self._execute_direct_query(state, query, metadata)
        executed_results = [item for item in executed if item is not None]
        rows, datasource_id, sql, purpose = executed_results[0]
        if plan.combination is not None:
            combination = plan.combination
            if combination.supporting_query_index >= len(plan.supporting_queries):
                raise UnsafeQueryError("Direct-answer combination refers to a missing supporting query")
            if combination.operation == "aggregate":
                # The requested group label can arrive from a related source.
                # Merge it below before calculating from the complete result
                # set, rather than asking the model to add a truncated page.
                pass
            elif combination.operation == "intersection" and primary_entity_filter_applied:
                # The trusted relationship already narrowed the SQL result. A planner-produced
                # in-memory intersection can accidentally select an unrelated supporting query
                # (for example, store IDs) and turn valid rows into a false empty result.
                logger.info("Skipping redundant client-side intersection after entity filter was applied")
            else:
                supporting_rows = executed_results[combination.supporting_query_index + 1][0]
                primary_fields = self._combination_fields(combination.primary_key)
                supporting_fields = self._combination_fields(combination.supporting_key)
                if len(primary_fields) != len(supporting_fields):
                    raise UnsafeQueryError("Direct-answer combination uses a different number of primary and supporting fields")
                if any(any(field not in row for field in primary_fields) for row in rows) or any(
                    any(field not in row for field in supporting_fields) for row in supporting_rows
                ):
                    raise UnsafeQueryError("Direct-answer combination refers to fields not returned by its queries")
                primary_values = {self._combination_row_key(row, primary_fields) for row in rows}
                supporting_values = {self._combination_row_key(row, supporting_fields) for row in supporting_rows}
                if combination.operation == "set_difference":
                    rows = [row for row in rows if self._combination_row_key(row, primary_fields) not in supporting_values]
                elif combination.operation == "intersection":
                    rows = [row for row in rows if self._combination_row_key(row, primary_fields) in supporting_values]
                else:
                    rows = [
                        *rows,
                        *[row for row in supporting_rows if self._combination_row_key(row, supporting_fields) not in primary_values],
                    ]
        rows = self._merge_approved_supporting_rows(
            state,
            rows,
            datasource_id,
            query_plans[0].sql,
            [(item[0], query_plans[index]) for index, item in enumerate(executed_results[1:], start=1)],
        )
        if plan.combination is not None and plan.combination.operation == "aggregate":
            rows = self._aggregate_direct_answer_rows(rows, plan.combination)
        (
            enriched_rows,
            enrichment_steps,
            enrichment_queries,
            resolved_entities,
        ) = await self._resolve_display_names(state, metadata, datasource_id, sql, rows)
        all_resolved_entities = self._merge_resolved_entities(state.resolved_entities, resolved_entities)
        user_facing_rows = self._remove_redundant_entity_labels(enriched_rows, all_resolved_entities)
        steps = [
            InvestigationStep(
                iteration=1,
                action=item[3],
                datasource_id=item[1],
                rationale="Direct factual answer",
                query=item[2],
            )
            for item in executed_results
        ]
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
            "resolved_entities": all_resolved_entities,
            "query_scopes": self._merge_query_scope(state, datasource_id, sql),
            "investigation_history": [
                *state.investigation_history,
                *steps,
                *enrichment_steps,
            ],
            "query_count": state.query_count + len(executed_results) + enrichment_queries,
            "iteration": 1,
            "status": InvestigationStatus.RUNNING,
        }

    @staticmethod
    def _merge_resolved_entities(existing: list[ResolvedEntityReference], new: list[ResolvedEntityReference]) -> list[ResolvedEntityReference]:
        """Keep a small, de-duplicated backend lookup context across conversation turns."""
        by_identity = {(entity.datasource_id, entity.table, entity.identifier_field, entity.identifier): entity for entity in existing}
        for entity in new:
            by_identity[(entity.datasource_id, entity.table, entity.identifier_field, entity.identifier)] = entity
        return list(by_identity.values())[-100:]

    def _merge_query_scope(self, state: InvestigationState, datasource_id: str, sql: str) -> list[QueryScope]:
        """Persist reusable non-entity predicates such as time range and payment status."""
        try:
            statement = sqlglot.parse_one(sql, read="postgres")
        except sqlglot.errors.ParseError:
            return state.query_scopes
        select = statement if isinstance(statement, exp.Select) else statement.find(exp.Select)
        where = select.args.get("where") if select else None
        tables = list(statement.find_all(exp.Table))
        if where is None or len(tables) != 1:
            return state.query_scopes
        table = f"{tables[0].db}.{tables[0].name}" if tables[0].db else tables[0].name
        entity_columns = {
            relation.source_field.rsplit(".", 1)[-1]
            for relation in self.registry.approved_cross_relations([source.id for source in state.datasources])
            if relation.source_datasource == datasource_id
        }

        def leaves(node: exp.Expression) -> list[exp.Expression]:
            if isinstance(node, exp.And):
                return leaves(node.this) + leaves(node.expression)
            return [node]

        conditions = [
            condition for condition in leaves(where.this) if not any(column.name in entity_columns for column in condition.find_all(exp.Column))
        ]
        if not conditions:
            return [scope for scope in state.query_scopes if not (scope.datasource_id == datasource_id and scope.table == table)]
        predicate = conditions[0]
        for condition in conditions[1:]:
            predicate = exp.and_(predicate, condition)
        scope = QueryScope(
            datasource_id=datasource_id,
            table=table,
            predicate_sql=predicate.sql(dialect="postgres"),
        )
        return [
            *[item for item in state.query_scopes if not (item.datasource_id == datasource_id and item.table == table)],
            scope,
        ]

    @staticmethod
    def _question_overrides_scope(state: InvestigationState) -> bool:
        question = (state.current_conversation_question or state.question).casefold()
        return bool(
            re.search(
                r"\b(?:different|another|all[- ]time|last\s+\d+|between|from\s+\d|in\s+\d{4}|"
                r"across\s+all|all\s+(?:platforms|channels|stores|regions|products|items|customers)|"
                r"every\s+(?:platform|channel|store|region|product|item|customer))\b",
                question,
            )
        )

    def _apply_inherited_scope(self, state: InvestigationState, query: DirectAnswerQuery) -> DirectAnswerQuery:
        """Carry prior non-entity filters into a follow-up unless its wording changes the scope."""
        if self._question_overrides_scope(state):
            return query
        for scope in reversed(state.query_scopes):
            if scope.datasource_id != query.datasource_id or not query_references_table(query.sql, scope.table):
                continue
            try:
                statement = sqlglot.parse_one(query.sql, read="postgres")
                predicate = sqlglot.parse_one(f"SELECT * FROM scope_source WHERE {scope.predicate_sql}", read="postgres").args["where"].this
            except (sqlglot.errors.ParseError, KeyError):
                continue
            select = statement if isinstance(statement, exp.Select) else statement.find(exp.Select)
            if select is None:
                continue
            # A planner can deliberately put a prior dimension inside an aggregate FILTER in
            # order to compare it with the unfiltered population. Adding that dimension to the
            # outer WHERE would silently turn an all-population measure into the filtered subset.
            # Keep unrelated scope conditions, such as the date range, but leave an explicitly
            # compared dimension to the planner.
            filter_columns = {column.name for aggregate_filter in statement.find_all(exp.Filter) for column in aggregate_filter.find_all(exp.Column)}

            def leaves(node: exp.Expression) -> list[exp.Expression]:
                if isinstance(node, exp.And):
                    return leaves(node.this) + leaves(node.expression)
                return [node]

            inherited_conditions = [
                condition for condition in leaves(predicate) if not any(column.name in filter_columns for column in condition.find_all(exp.Column))
            ]
            if not inherited_conditions:
                logger.info(
                    "Skipped inherited scope dimension explicitly handled by aggregate filters: datasource=%s table=%s",
                    scope.datasource_id,
                    scope.table,
                )
                return query
            inherited_predicate = inherited_conditions[0]
            for condition in inherited_conditions[1:]:
                inherited_predicate = exp.and_(inherited_predicate, condition)
            where = select.args.get("where")
            select.set(
                "where",
                exp.Where(this=exp.and_(where.this, inherited_predicate) if where else inherited_predicate),
            )
            logger.info(
                "Applied inherited query scope: datasource=%s table=%s",
                scope.datasource_id,
                scope.table,
            )
            return query.model_copy(update={"sql": statement.sql(dialect="postgres")})
        return query

    @staticmethod
    def _question_refers_to_retained_entity(state: InvestigationState) -> bool:
        """Recognize an explicit reference to a previously resolved business entity."""
        question = (state.current_conversation_question or state.question).casefold()
        # A continuation normally inherits the single entity established by the prior answer.
        # Do not make the user repeat its name for a natural request such as "show by store".
        if (
            state.question.startswith("Follow-up request:")
            and state.resolved_entities
            and not re.search(r"\ball\s+(?:products|items|stores|suppliers)\b", question)
        ):
            return True
        if re.search(r"\b(?:this|that|the|these|those)\s+(?:item|product|store|supplier)\b", question):
            return True
        if re.search(r"\b(?:it|them)\b", question) and state.resolved_entities:
            return True
        return any(entity.display_name.casefold() in question for entity in state.resolved_entities)

    def _constrain_query_to_referenced_entity(self, state: InvestigationState, query: DirectAnswerQuery) -> DirectAnswerQuery:
        """Apply an approved key relationship when a follow-up explicitly names a retained entity."""
        if not state.resolved_entities or not self._question_refers_to_retained_entity(state):
            return query
        matching_relations: dict[str, tuple[Any, list[ResolvedEntityReference]]] = {}
        for relation in self.registry.approved_cross_relations([source.id for source in state.datasources]):
            if relation.source_datasource != query.datasource_id:
                continue
            source_table = relation.source_field.rsplit(".", 1)[0]
            if not query_references_table(query.sql, source_table):
                continue
            target_table, target_field = relation.target_field.rsplit(".", 1)
            for entity in state.resolved_entities:
                if entity.datasource_id == relation.target_datasource and entity.table == target_table and entity.identifier_field == target_field:
                    if relation.id not in matching_relations:
                        matching_relations[relation.id] = (relation, [])
                    matching_relations[relation.id][1].append(entity)
        if len(matching_relations) != 1:
            return query
        relation, entities = next(iter(matching_relations.values()))
        identifiers = list(dict.fromkeys(entity.identifier for entity in entities))
        logger.info(
            "Applying retained entity context before direct-answer query: relation=%s entities=%d",
            relation.id,
            len(identifiers),
        )
        return query.model_copy(
            update={
                # A planner occasionally compares the key column to a display label it saw in
                # the prior answer (for example, ``product_id = 'Toiletry Bag 119'``). Keeping
                # that predicate alongside the trusted key filter makes the query silently empty.
                # The stored entity reference is authoritative for a retained-entity follow-up.
                "sql": self._add_relation_filter(
                    self._remove_untrusted_relation_predicates(query.sql, relation.source_field, identifiers),
                    relation.source_field,
                    identifiers,
                )
            }
        )

    @staticmethod
    def _remove_untrusted_relation_predicates(sql: str, source_field: str, identifiers: list[str]) -> str:
        """Remove planner filters on a retained entity key before applying its trusted key.

        This is based on an administrator-approved relationship field, not a field-name convention.
        Other predicates, such as the inherited date range, remain unchanged.
        """
        column = source_field.rsplit(".", 1)[-1]
        try:
            statement = sqlglot.parse_one(sql, read="postgres")
        except sqlglot.errors.ParseError:
            return sql
        select = statement if isinstance(statement, exp.Select) else statement.find(exp.Select)
        if select is None or (where := select.args.get("where")) is None:
            return sql
        trusted = set(identifiers)

        def refers_to_relation_key(expression: exp.Expression) -> bool:
            return any(candidate.name == column for candidate in expression.find_all(exp.Column))

        def is_trusted_key_predicate(expression: exp.Expression) -> bool:
            if isinstance(expression, exp.EQ):
                left, right = expression.this, expression.expression
                return any(
                    isinstance(field, exp.Column)
                    and field.name == column
                    and isinstance(value, exp.Literal)
                    and value.is_string
                    and str(value.this) in trusted
                    for field, value in ((left, right), (right, left))
                )
            if isinstance(expression, exp.In) and isinstance(expression.this, exp.Column) and expression.this.name == column:
                values = list(expression.expressions)
                return bool(values) and all(isinstance(value, exp.Literal) and value.is_string and str(value.this) in trusted for value in values)
            return False

        def strip(expression: exp.Expression) -> exp.Expression | None:
            if isinstance(expression, exp.Paren):
                inner = strip(expression.this)
                return expression if inner is expression.this else (exp.Paren(this=inner) if inner is not None else None)
            if isinstance(expression, exp.And):
                left, right = strip(expression.this), strip(expression.expression)
                if left is None:
                    return right
                if right is None:
                    return left
                return exp.and_(left, right)
            if refers_to_relation_key(expression) and not is_trusted_key_predicate(expression):
                return None
            return expression

        cleaned = strip(where.this)
        if cleaned is where.this:
            return sql
        select.set("where", exp.Where(this=cleaned) if cleaned is not None else None)
        logger.info(
            "Replaced an untrusted retained-entity predicate with its stored reference: field=%s",
            source_field,
        )
        return statement.sql(dialect="postgres")

    def _requested_relation_without_lookup(self, state: InvestigationState, plan: DirectAnswerPlan) -> Any | None:
        """Require a readable lookup for an explicitly requested approved relationship dimension."""
        question = (state.current_conversation_question or state.question).casefold()
        selected_ids = [source.id for source in state.datasources]
        for relation in self.registry.approved_cross_relations(selected_ids):
            target_table = relation.target_field.rsplit(".", 1)[0]
            table_word = target_table.rsplit(".", 1)[-1].casefold().rstrip("s")
            if not table_word or not re.search(rf"\b{re.escape(table_word)}s?\b", question):
                continue
            if relation.source_datasource != plan.datasource_id or not query_references_table(plan.sql, relation.source_field.rsplit(".", 1)[0]):
                continue
            if not any(
                query.datasource_id == relation.target_datasource and query_references_table(query.sql, target_table)
                for query in plan.supporting_queries
            ):
                return relation
        return None

    def _merge_approved_supporting_rows(
        self,
        state: InvestigationState,
        primary_rows: list[dict[str, Any]],
        primary_datasource_id: str,
        primary_sql: str,
        supporting_results: list[tuple[list[dict[str, Any]], DirectAnswerQuery]],
    ) -> list[dict[str, Any]]:
        """Attach readable fields from independent, approved cross-source lookups.

        The merge key comes exclusively from an administrator-approved relationship. This lets a
        planner retrieve a store list or product catalog separately while preserving its readable
        fields in the factual result, without a cross-database SQL join or identifier exposure.
        """
        merged_rows = primary_rows
        relations = self.registry.approved_cross_relations([source.id for source in state.datasources])
        for supporting_rows, supporting_query in supporting_results:
            if not supporting_rows:
                continue
            for relation in relations:
                if (
                    relation.source_datasource != primary_datasource_id
                    or relation.target_datasource != supporting_query.datasource_id
                    or not query_references_table(primary_sql, relation.source_field.rsplit(".", 1)[0])
                    or not query_references_table(supporting_query.sql, relation.target_field.rsplit(".", 1)[0])
                ):
                    continue
                primary_key = relation.source_field.rsplit(".", 1)[-1]
                target_key = relation.target_field.rsplit(".", 1)[-1]
                supporting_key = next(
                    (key for key in (primary_key, target_key) if any(key in row for row in supporting_rows)),
                    None,
                )
                if supporting_key is None or not any(primary_key in row for row in merged_rows):
                    continue
                lookup = {str(row[supporting_key]): row for row in supporting_rows if row.get(supporting_key) is not None}
                if not lookup:
                    continue
                merged_rows = [
                    {
                        **row,
                        **{key: value for key, value in lookup[str(row[primary_key])].items() if key != supporting_key and key not in row},
                    }
                    if row.get(primary_key) is not None and str(row[primary_key]) in lookup
                    else row
                    for row in merged_rows
                ]
                logger.info(
                    "Merged approved supporting lookup into direct result: relation=%s matched_rows=%d",
                    relation.id,
                    sum(1 for row in merged_rows if row.get(primary_key) is not None and str(row[primary_key]) in lookup),
                )
        return merged_rows

    @staticmethod
    def _combination_fields(value: str) -> list[str]:
        """Parse a model-planned single or composite result key without trusting SQL-like text."""
        fields = [field.strip() for field in value.split(",") if field.strip()]
        if not fields or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", field) for field in fields):
            raise UnsafeQueryError("Direct-answer combination contains an invalid result field")
        return fields

    @staticmethod
    def _combination_row_key(row: dict[str, Any], fields: list[str]) -> tuple[str, ...]:
        """Use an ordered tuple so a composite group can be combined deterministically."""
        return tuple(str(row[field]) for field in fields)

    @classmethod
    def _aggregate_direct_answer_rows(cls, rows: list[dict[str, Any]], combination: Any) -> list[dict[str, Any]]:
        """Group complete, joined direct-answer rows without model arithmetic.

        Direct answers may need a readable dimension that comes from another
        selected datasource.  Raw query results remain capped only when they
        are finally displayed; this operation runs first over every returned
        row so a category, supplier, or similar breakdown is exact.
        """
        if not combination.group_by or not combination.measure or not combination.aggregation:
            raise UnsafeQueryError("Direct-answer aggregate requires group_by, measure, and aggregation fields")
        group_fields = cls._combination_fields(combination.group_by)
        measure_fields = cls._combination_fields(combination.measure)
        if len(measure_fields) != 1:
            raise UnsafeQueryError("Direct-answer aggregate accepts exactly one measure field")
        measure = measure_fields[0]
        required_fields = [*group_fields, measure]
        if any(any(field not in row for field in required_fields) for row in rows):
            raise UnsafeQueryError("Direct-answer aggregate refers to fields not returned by its queries")

        groups: dict[tuple[str, ...], tuple[dict[str, Any], list[Decimal]]] = {}
        for row in rows:
            key = cls._combination_row_key(row, group_fields)
            group, values = groups.setdefault(key, ({field: row[field] for field in group_fields}, []))
            value = cls._decimal_measure(row[measure])
            if value is not None:
                values.append(value)

        aggregate_rows: list[dict[str, Any]] = []
        for group, values in groups.values():
            if combination.aggregation == "sum":
                aggregate_value: Decimal | None = sum(values, Decimal("0")) if values else None
            elif combination.aggregation == "average":
                aggregate_value = sum(values, Decimal("0")) / len(values) if values else None
            elif combination.aggregation == "minimum":
                aggregate_value = min(values) if values else None
            else:
                aggregate_value = max(values) if values else None
            aggregate_rows.append({**group, measure: aggregate_value})
        logger.info(
            "Aggregated complete direct-answer result: operation=%s input_rows=%d output_groups=%d group_by=%s measure=%s",
            combination.aggregation,
            len(rows),
            len(aggregate_rows),
            ",".join(group_fields),
            measure,
        )
        return aggregate_rows

    @staticmethod
    def _decimal_measure(value: Any) -> Decimal | None:
        """Accept database numeric values without treating booleans or text labels as measures."""
        if value is None or isinstance(value, bool):
            return None
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise UnsafeQueryError("Direct-answer aggregate measure contains a non-numeric value") from None

    def _relation_for_primary_filter(
        self,
        state: InvestigationState,
        primary_query: DirectAnswerQuery,
        supporting_query: DirectAnswerQuery,
        primary_key: str,
        metadata: list[dict[str, Any]],
    ) -> Any | None:
        """Find an approved relation that can safely narrow a primary result set."""
        for relation in self.registry.approved_cross_relations([source.id for source in state.datasources]):
            if (
                relation.source_datasource != primary_query.datasource_id
                or relation.target_datasource != supporting_query.datasource_id
                or relation.source_field.rsplit(".", 1)[-1] != primary_key
            ):
                continue
            source_table = relation.source_field.rsplit(".", 1)[0]
            target_table = relation.target_field.rsplit(".", 1)[0]
            if query_references_table(primary_query.sql, source_table) and query_references_table(supporting_query.sql, target_table):
                return relation
        return None

    @staticmethod
    def _add_relation_filter(sql: str, source_field: str, values: list[str]) -> str:
        """Add a value filter where the approved source table is actually read."""
        source_table, column = source_field.rsplit(".", 1)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", column):
            raise UnsafeQueryError("Approved relationship contains an invalid source column")
        try:
            statement = sqlglot.parse_one(sql, read="postgres")
        except sqlglot.errors.ParseError as exc:
            raise UnsafeQueryError(f"Could not apply approved relationship filter: {exc}") from exc
        matching_tables = [
            table for table in statement.find_all(exp.Table) if (f"{table.db}.{table.name}" if table.db else table.name) == source_table
        ]
        if not matching_tables:
            raise UnsafeQueryError("Could not apply an approved relationship filter to this query")
        filtered_selects: set[int] = set()
        for table in matching_tables:
            select = table.find_ancestor(exp.Select)
            if select is None or id(select) in filtered_selects:
                continue
            table_reference = table.alias_or_name or None
            # An empty lookup must yield no primary rows. Omitting the predicate
            # would turn "no matching catalog records" into an unfiltered answer.
            condition = (
                exp.In(
                    this=exp.column(column, table=table_reference),
                    expressions=[exp.Literal.string(value) for value in values],
                )
                if values
                else exp.EQ(this=exp.Literal.number(1), expression=exp.Literal.number(0))
            )
            existing_where = select.args.get("where")
            select.set(
                "where",
                exp.Where(this=exp.and_(existing_where.this, condition) if existing_where else condition),
            )
            filtered_selects.add(id(select))
        return statement.sql(dialect="postgres")

    async def _execute_direct_query(
        self, state: InvestigationState, query: DirectAnswerQuery, metadata: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], str, str, str]:
        """Execute one direct-answer subquery with the same safety and correction policy as every other query."""
        source_ids = {source.id for source in state.datasources}
        if query.datasource_id not in source_ids:
            logger.warning("Direct-answer query selected an invalid datasource; requesting a corrected query")
            query = await self.llm.structured(
                DirectAnswerQuery,
                prompts.load("direct_query_datasource_correction"),
                {
                    "question": state.question,
                    "datasources": metadata,
                    "valid_datasource_ids": sorted(source_ids),
                },
            )
            if query.datasource_id not in source_ids:
                raise UnsafeQueryError("Direct-answer query selected a datasource outside this request")
        source_metadata = next(source for source in metadata if source["id"] == query.datasource_id)
        allowed_tables = {table["name"] for table in source_metadata["tables"]}
        for attempt in range(2):
            try:
                bound_sql = self._case_insensitive_text_filters(self._bind_trusted_entity_references(state, query))
                safe_sql = validate_query_tables(validate_read_query(bound_sql), allowed_tables)
                self._validate_categorical_filter_values(safe_sql, source_metadata)
                purpose = self._safe_direct_answer_text(query.purpose)
                if safe_sql != query.sql:
                    logger.info(
                        "Direct-answer query prepared: datasource=%s before=%s after=%s",
                        query.datasource_id,
                        query.sql,
                        safe_sql,
                    )
                else:
                    logger.info(
                        "Direct-answer query prepared: datasource=%s sql=%s",
                        query.datasource_id,
                        safe_sql,
                    )
                await self._emit(state, "QueryStarted", purpose, datasource_id=query.datasource_id)
                rows = await self.registry.get(query.datasource_id).execute_read_query(safe_sql)
                rows = self._normalize_before_after_rows(rows)
                logger.info(
                    "Direct-answer query completed: datasource=%s rows=%d purpose=%s",
                    query.datasource_id,
                    len(rows),
                    purpose,
                )
                await self._emit(
                    state,
                    "QueryCompleted",
                    "The data check is complete.",
                    datasource_id=query.datasource_id,
                    row_count=len(rows),
                )
                return rows, query.datasource_id, safe_sql, purpose
            except (ProgrammingError, UnsafeQueryError) as exc:
                if attempt:
                    raise
                logger.warning(
                    "Direct-answer query was rejected; requesting one corrected query: datasource=%s error=%s",
                    query.datasource_id,
                    exc,
                )
                await self._emit(
                    state,
                    "QueryRejected",
                    "The first check needed an adjustment, so the answer is trying again.",
                    datasource_id=query.datasource_id,
                )
                query = await self.llm.structured(
                    DirectAnswerQuery,
                    prompts.load("direct_query_guardrail_correction"),
                    {
                        "question": state.question,
                        "selected_datasource_id": source_metadata["id"],
                        "selected_datasource": source_metadata,
                        "previous_query": query.sql,
                        "rejection_reason": str(exc),
                    },
                )
                if query.datasource_id != source_metadata["id"]:
                    raise UnsafeQueryError("Corrected direct-answer query changed the selected datasource") from exc
        raise RuntimeError("A direct-answer query result was not produced")

    def _bind_trusted_entity_references(self, state: InvestigationState, query: DirectAnswerQuery) -> str:
        """Replace a planned entity placeholder with one unambiguous, stored backend reference.

        The planner may express a follow-up predicate as ``{{product_id}}`` rather than repeat an
        internal value. That value is never shown to the user, but it must be bound before SQL is
        validated and executed; PostgreSQL would otherwise compare against the literal placeholder
        and produce a convincing but false empty result.
        """
        placeholders = set(re.findall(r"\{\{\s*([A-Za-z_][A-Za-z0-9_$]*)\s*\}\}", query.sql))
        if re.search(r"\{\{.*?\}\}", query.sql) and not placeholders:
            raise UnsafeQueryError("Direct-answer query contains an invalid entity placeholder")

        values: dict[str, str] = {}
        approved_relations = self.registry.approved_cross_relations([source.id for source in state.datasources])
        for placeholder in placeholders:
            candidates: list[ResolvedEntityReference] = []
            for entity in state.resolved_entities:
                # Direct reuse on the same source/table, such as a catalog follow-up.
                if (
                    entity.datasource_id == query.datasource_id
                    and query_references_table(query.sql, entity.table)
                    and placeholder == entity.identifier_field
                ):
                    candidates.append(entity)
                    continue
                # Reuse through an administrator-approved cross-datasource relationship.
                for relation in approved_relations:
                    if relation.source_datasource != query.datasource_id or relation.target_datasource != entity.datasource_id:
                        continue
                    if relation.target_field != f"{entity.table}.{entity.identifier_field}":
                        continue
                    if relation.source_field.rsplit(".", 1)[-1] != placeholder:
                        continue
                    if query_references_table(query.sql, relation.source_field.rsplit(".", 1)[0]):
                        candidates.append(entity)
                        break
            unique = {
                (
                    candidate.datasource_id,
                    candidate.table,
                    candidate.identifier_field,
                    candidate.identifier,
                ): candidate
                for candidate in candidates
            }
            if len(unique) != 1:
                raise UnsafeQueryError(f"Direct-answer query placeholder {{{{{placeholder}}}}} does not identify one stored entity")
            values[placeholder] = next(iter(unique.values())).identifier

        bound_sql = query.sql
        if placeholders:
            bound_sql = re.sub(
                r"(?:'|\")?\{\{\s*([A-Za-z_][A-Za-z0-9_$]*)\s*\}\}(?:'|\")?",
                lambda match: "'" + values[match.group(1)].replace("'", "''") + "'",
                query.sql,
            )
        # Some planners repeat the human-facing name despite receiving the trusted reference.
        # Replace it only when it is compared to the source side of an approved relationship;
        # a name in any other column remains ordinary query text.
        try:
            statement = sqlglot.parse_one(bound_sql, read="postgres")
        except sqlglot.errors.ParseError:
            return bound_sql  # The normal SQL guardrail reports the parse error with a correction attempt.
        replacements: dict[tuple[str, str], str] = {}
        for relation in approved_relations:
            if relation.source_datasource != query.datasource_id:
                continue
            if not query_references_table(bound_sql, relation.source_field.rsplit(".", 1)[0]):
                continue
            source_column = relation.source_field.rsplit(".", 1)[-1]
            target_table, target_field = relation.target_field.rsplit(".", 1)
            for entity in state.resolved_entities:
                if entity.datasource_id == relation.target_datasource and entity.table == target_table and entity.identifier_field == target_field:
                    key = (source_column, entity.display_name)
                    # A duplicate display name is not safe to turn into a key automatically.
                    replacements[key] = entity.identifier if key not in replacements else ""

        def replacement_for(column: exp.Expression, value: exp.Expression) -> exp.Literal | None:
            if not isinstance(column, exp.Column) or not isinstance(value, exp.Literal) or not value.is_string:
                return None
            identifier = replacements.get((column.name, str(value.this)))
            return exp.Literal.string(identifier) if identifier else None

        changed = False
        for equality in statement.find_all(exp.EQ):
            replacement = replacement_for(equality.this, equality.expression)
            if replacement is not None:
                equality.set("expression", replacement)
                changed = True
        for membership in statement.find_all(exp.In):
            if not isinstance(membership.this, exp.Column):
                continue
            expressions = list(membership.expressions)
            replaced = [replacement_for(membership.this, expression) or expression for expression in expressions]
            if replaced != expressions:
                membership.set("expressions", replaced)
                changed = True
        if changed:
            logger.info("Bound a stored entity reference by its display name before executing a direct-answer query")
        return statement.sql(dialect="postgres")

    async def _resolve_display_names(
        self,
        state: InvestigationState,
        metadata: list[dict[str, Any]],
        source_id: str,
        sql: str,
        rows: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[InvestigationStep], int, list[ResolvedEntityReference]]:
        """Resolve arbitrary identifier fields to a display name using discovered schema metadata.

        This performs a separate, read-only lookup. It deliberately avoids cross-database SQL joins,
        and never exposes the identifier after the lookup has completed.
        """
        source_metadata = next((candidate for candidate in metadata if candidate["id"] == source_id), None)
        if source_metadata is None:
            return rows, [], 0, []
        queried_tables = [table for table in source_metadata["tables"] if query_references_table(sql, table["name"])]
        key_columns = {column["name"] for table in queried_tables for column in table["columns"] if column["pk"]}
        key_columns.update(
            foreign_key["column_name"] for table in queried_tables for foreign_key in table["foreign_keys"] if foreign_key.get("column_name")
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
                target_table = next(
                    (item for item in source_metadata["tables"] if item["name"] == target_name),
                    None,
                )
                if target_table and target_column:
                    target_columns = {column["name"] for column in target_table["columns"]}
                    target_display = self._infer_display_column(target_table)
                    if target_display:
                        lookup_candidates.append(
                            (
                                foreign_key["column_name"],
                                source_metadata,
                                target_table,
                                target_column,
                                target_display,
                            )
                        )

        relations = self.registry.approved_cross_relations([candidate["id"] for candidate in metadata])
        for relation in relations:
            if relation.source_datasource != source_id:
                continue
            source_identifier = relation.source_field.rsplit(".", 1)[-1]
            target_identifier = relation.target_field.rsplit(".", 1)[-1]
            target_table_name = relation.target_field.rsplit(".", 1)[0] if "." in relation.target_field else None
            key_columns.add(source_identifier)
            target_source = next(
                (candidate for candidate in metadata if candidate["id"] == relation.target_datasource),
                None,
            )
            if target_source is None:
                continue
            for target_table in target_source["tables"]:
                if target_table_name and target_table["name"] != target_table_name:
                    continue
                target_columns = {column["name"] for column in target_table["columns"]}
                target_display = self._infer_display_column(target_table)
                if target_identifier in target_columns and target_display:
                    lookup_candidates.append(
                        (
                            source_identifier,
                            target_source,
                            target_table,
                            target_identifier,
                            target_display,
                        )
                    )

        lookups: list[tuple[str, list[str], dict[str, Any], dict[str, Any], str, str]] = []
        seen_lookups: set[tuple[str, str, str, str]] = set()
        for (
            identifier_column,
            candidate,
            table,
            target_identifier,
            display_column,
        ) in lookup_candidates:
            identifiers = list(dict.fromkeys(str(row[identifier_column]) for row in rows if row.get(identifier_column) is not None))[:100]
            identity = (identifier_column, candidate["id"], table["name"], target_identifier)
            if identifiers and identity not in seen_lookups:
                seen_lookups.add(identity)
                lookups.append(
                    (
                        identifier_column,
                        identifiers,
                        candidate,
                        table,
                        target_identifier,
                        display_column,
                    )
                )
        if not lookups:
            return self._redact_internal_identifier_columns(rows, key_columns), [], 0, []

        enriched_rows = rows
        steps: list[InvestigationStep] = []
        resolved_entities: list[ResolvedEntityReference] = []
        for lookup_index, (
            identifier_column,
            identifiers,
            catalogue,
            lookup_table,
            target_identifier,
            display_column,
        ) in enumerate(lookups):
            quoted_identifiers = ", ".join("'" + value.replace("'", "''") + "'" for value in identifiers)
            name_query = validate_query_tables(
                validate_read_query(
                    f"SELECT {target_identifier}, {display_column} FROM {lookup_table['name']} WHERE {target_identifier} IN ({quoted_identifiers})"
                ),
                {table["name"] for table in catalogue["tables"]},
            )
            await self._emit(
                state,
                "QueryStarted",
                "Matching selected records to readable names.",
                datasource_id=catalogue["id"],
            )
            try:
                name_rows = await self.registry.get(catalogue["id"]).execute_read_query(name_query)
            except (ProgrammingError, UnsafeQueryError) as exc:
                logger.warning(
                    "Display-name lookup failed; redacting the internal reference: datasource=%s error=%s",
                    catalogue["id"],
                    exc,
                )
                await self._emit(
                    state,
                    "QueryRejected",
                    "A name lookup was unavailable, so its internal references were removed.",
                    datasource_id=catalogue["id"],
                )
                continue
            await self._emit(
                state,
                "QueryCompleted",
                "Readable names are ready.",
                datasource_id=catalogue["id"],
                row_count=len(name_rows),
            )
            names = {
                str(row[target_identifier]): row[display_column]
                for row in name_rows
                if row.get(target_identifier) is not None and row.get(display_column)
            }
            table_label = lookup_table["name"].rsplit(".", 1)[-1].rstrip("s") or "related"
            output_column = "display_name" if lookup_index == 0 else f"{table_label}_name"
            enriched_rows = [
                {
                    **row,
                    output_column: self._customer_facing_name(names[str(row[identifier_column])], row[identifier_column]),
                }
                if row.get(identifier_column) is not None and str(row[identifier_column]) in names
                else row
                for row in enriched_rows
            ]
            steps.append(
                InvestigationStep(
                    iteration=1,
                    action="Match selected records to readable names.",
                    datasource_id=catalogue["id"],
                    rationale="Make the result understandable without combining databases.",
                    query=name_query,
                )
            )
            # Retain only the primary resolved entity for a later "this item" follow-up.
            if lookup_index == 0:
                resolved_entities = [
                    ResolvedEntityReference(
                        datasource_id=catalogue["id"],
                        table=lookup_table["name"],
                        identifier_field=target_identifier,
                        identifier=identifier,
                        display_name=self._customer_facing_name(name, identifier),
                    )
                    for identifier, name in names.items()
                ]
        return (
            self._redact_internal_identifier_columns(enriched_rows, key_columns),
            steps,
            len(steps),
            resolved_entities,
        )
