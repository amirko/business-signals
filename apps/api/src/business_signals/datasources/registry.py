from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from uuid import uuid4

from business_signals.config import settings
from business_signals.datasources.base import Datasource
from business_signals.datasources.postgres import PostgreSQLDatasource, TimescaleDatasource
from business_signals.models import (
    CrossDatasourceRelation,
    CrossDatasourceRelationCreate,
    DatasourceCreate,
    DatasourceMetadata,
    DatasourceSummary,
    DatasourceType,
)

logger = logging.getLogger("uvicorn.error")


class DatasourceRegistry:
    """Locally persisted datasource definitions with an on-disk schema cache."""

    def __init__(self, store_path: Path | None = None) -> None:
        self._sources: dict[str, Datasource] = {}
        self._metadata: dict[str, DatasourceMetadata] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._configs: dict[str, DatasourceCreate] = {}
        self._cross_relations: dict[str, CrossDatasourceRelation] = {}
        self._store_path = store_path or settings.datasource_store_path
        self._restore()

    def _restore(self) -> None:
        if not self._store_path.exists():
            return
        try:
            payload = json.loads(self._store_path.read_text(encoding="utf-8"))
            for item in payload.get("datasources", []):
                datasource_id = item["id"]
                config = DatasourceCreate.model_validate(item["config"])
                source = self.build(config, datasource_id)
                metadata_payload = item.get("metadata")
                if metadata_payload:
                    metadata = DatasourceMetadata.model_validate(metadata_payload)
                    self._metadata[datasource_id] = metadata
                    source.summary.table_count = len(metadata.structural_metadata)
                    source.summary.schema_cached = True
                    source.summary.discovered_at = metadata.discovered_at
                self._sources[datasource_id] = source
                self._configs[datasource_id] = config
                self._locks[datasource_id] = asyncio.Lock()
            for item in payload.get("cross_datasource_relations", []):
                relation = CrossDatasourceRelation.model_validate(item)
                if relation.confirmed and relation.source_datasource in self._sources and relation.target_datasource in self._sources:
                    self._cross_relations[relation.id] = relation
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Could not restore local datasource store %s: %s", self._store_path, exc)

    @staticmethod
    def _serializable_config(config: DatasourceCreate) -> dict[str, object]:
        """Keep the usable local password while ensuring it never reaches API output or LLM payloads."""
        return {
            "name": config.name,
            "type": config.type.value,
            "credentials": {
                "host": config.credentials.host,
                "port": config.credentials.port,
                "database": config.credentials.database,
                "username": config.credentials.username,
                "password": config.credentials.password.get_secret_value(),
                "ssl_mode": config.credentials.ssl_mode,
            },
        }

    def _save(self) -> None:
        payload = {
            "version": 2,
            "datasources": [
                {
                    "id": datasource_id,
                    "config": self._serializable_config(config),
                    "metadata": self._metadata[datasource_id].model_dump(mode="json")
                    if datasource_id in self._metadata
                    else None,
                }
                for datasource_id, config in self._configs.items()
            ],
            "cross_datasource_relations": [
                relation.model_dump(mode="json") for relation in self._cross_relations.values()
            ],
        }
        self._store_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_path = tempfile.mkstemp(
            prefix="datasources-", suffix=".json", dir=self._store_path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, self._store_path)
        finally:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)

    def build(self, config: DatasourceCreate, datasource_id: str | None = None) -> Datasource:
        source_id = datasource_id or f"ds_{uuid4().hex[:10]}"
        adapter = TimescaleDatasource if config.type == DatasourceType.TIMESCALEDB else PostgreSQLDatasource
        return adapter(source_id, config)

    async def test(self, config: DatasourceCreate) -> DatasourceSummary:
        source = self.build(config)
        await source.test_connection()
        return source.summary

    async def add(self, config: DatasourceCreate) -> DatasourceSummary:
        source = self.build(config)
        await source.test_connection()
        self._sources[source.summary.id] = source
        self._locks[source.summary.id] = asyncio.Lock()
        self._configs[source.summary.id] = config
        self._save()
        return source.summary

    def list(self) -> list[DatasourceSummary]:
        return [source.summary for source in self._sources.values()]

    def get(self, datasource_id: str) -> Datasource:
        try:
            return self._sources[datasource_id]
        except KeyError as exc:
            raise KeyError(f"Datasource {datasource_id!r} was not found") from exc

    async def metadata(self, datasource_id: str, force: bool = False) -> DatasourceMetadata:
        source = self.get(datasource_id)
        async with self._locks[datasource_id]:
            cached = self._metadata.get(datasource_id)
            if cached and not force:
                current = await source.lightweight_fingerprint()
                if current == cached.fingerprint:
                    return cached
            fresh = await source.discover_structure()
            self._metadata[datasource_id] = fresh
            self._save()
            return fresh

    async def refresh(self, datasource_id: str) -> DatasourceMetadata:
        return await self.metadata(datasource_id, force=True)

    def cached_metadata(self, datasource_id: str) -> DatasourceMetadata | None:
        self.get(datasource_id)
        return self._metadata.get(datasource_id)

    async def delete(self, datasource_id: str) -> None:
        source = self.get(datasource_id)
        self._sources.pop(datasource_id)
        self._metadata.pop(datasource_id, None)
        self._locks.pop(datasource_id, None)
        self._configs.pop(datasource_id, None)
        self._cross_relations = {
            relation_id: relation
            for relation_id, relation in self._cross_relations.items()
            if datasource_id not in {relation.source_datasource, relation.target_datasource}
        }
        close = getattr(source, "close", None)
        if close is not None:
            await close()
        self._save()

    def approved_cross_relations(self, datasource_ids: list[str]) -> list[CrossDatasourceRelation]:
        """Return only relationships an administrator has explicitly approved."""
        selected = set(datasource_ids)
        return [
            relation
            for relation in self._cross_relations.values()
            if relation.confirmed
            and relation.source_datasource in selected
            and relation.target_datasource in selected
        ]

    @staticmethod
    def _field_parts(field: str) -> tuple[str, str, str]:
        parts = field.split(".")
        if len(parts) != 3 or not all(parts):
            raise ValueError("A relationship field must be schema.table.column")
        return parts[0], parts[1], parts[2]

    @staticmethod
    def _quoted_identifier(identifier: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", identifier):
            raise ValueError("Relationship metadata contains an invalid identifier")
        return f'"{identifier}"'

    @classmethod
    def _quoted_field(cls, field: str) -> tuple[str, str]:
        schema, table, column = cls._field_parts(field)
        return f"{cls._quoted_identifier(schema)}.{cls._quoted_identifier(table)}", cls._quoted_identifier(column)

    @staticmethod
    def _compatible_types(source_type: str, target_type: str) -> bool:
        normalized_source, normalized_target = source_type.casefold(), target_type.casefold()
        # Cross-source identifiers must have the same discovered type. Numeric measures are
        # not interchangeable with numeric keys, even though PostgreSQL can sometimes coerce them.
        return normalized_source == normalized_target

    @staticmethod
    def _humanize_table_name(name: str) -> str:
        return name.replace("_", " ").strip().capitalize()

    async def _distinct_values(self, datasource_id: str, field: str, limit: int = 100) -> list[str]:
        table, column = self._quoted_field(field)
        rows = await self.get(datasource_id).execute_read_query(
            f"SELECT DISTINCT {column} AS value FROM {table} WHERE {column} IS NOT NULL LIMIT {limit}"
        )
        return [str(row["value"]) for row in rows if row.get("value") is not None]

    async def _matching_values(self, datasource_id: str, field: str, values: list[str]) -> set[str]:
        if not values:
            return set()
        table, column = self._quoted_field(field)
        values_sql = ", ".join("'" + value.replace("'", "''") + "'" for value in values)
        rows = await self.get(datasource_id).execute_read_query(
            f"SELECT DISTINCT {column} AS value FROM {table} WHERE {column} IN ({values_sql})"
        )
        return {str(row["value"]) for row in rows if row.get("value") is not None}

    async def validate_cross_relation(self, payload: CrossDatasourceRelationCreate) -> CrossDatasourceRelation:
        """Validate an administrator-selected relationship with read-only value coverage checks."""
        if payload.source_datasource == payload.target_datasource:
            raise ValueError("A cross-datasource relationship needs two different connections")
        source_metadata = await self.metadata(payload.source_datasource)
        target_metadata = await self.metadata(payload.target_datasource)
        source_schema, source_table_name, source_column_name = self._field_parts(payload.source_field)
        target_schema, target_table_name, target_column_name = self._field_parts(payload.target_field)
        source_table = next(
            (table for table in source_metadata.structural_metadata if (table.schema_name, table.name) == (source_schema, source_table_name)),
            None,
        )
        target_table = next(
            (table for table in target_metadata.structural_metadata if (table.schema_name, table.name) == (target_schema, target_table_name)),
            None,
        )
        source_column = next((column for column in source_table.columns if column.name == source_column_name), None) if source_table else None
        target_column = next((column for column in target_table.columns if column.name == target_column_name), None) if target_table else None
        if not source_column or not target_column:
            raise ValueError("The selected relationship field is no longer present in the discovered schema")
        if not target_column.primary_key:
            raise ValueError("The destination field must be a discovered primary key")
        if not self._compatible_types(source_column.data_type, target_column.data_type):
            raise ValueError("The selected fields have incompatible data types")
        sampled_values = await self._distinct_values(payload.source_datasource, payload.source_field)
        matched_values = await self._matching_values(payload.target_datasource, payload.target_field, sampled_values)
        matched_count = sum(value in matched_values for value in sampled_values)
        coverage = matched_count / len(sampled_values) if sampled_values else 0.0
        return CrossDatasourceRelation(
            source_datasource=payload.source_datasource,
            source_field=payload.source_field,
            target_datasource=payload.target_datasource,
            target_field=payload.target_field,
            label=payload.label,
            cardinality=payload.cardinality,
            confidence=coverage,
            sampled_values=len(sampled_values),
            matched_values=matched_count,
            origin="human",
            confirmed=True,
        )

    async def save_cross_relation(self, payload: CrossDatasourceRelationCreate) -> CrossDatasourceRelation:
        relation = await self.validate_cross_relation(payload)
        self._cross_relations[relation.id] = relation
        self._save()
        return relation

    async def update_cross_relation(
        self, relation_id: str, payload: CrossDatasourceRelationCreate
    ) -> CrossDatasourceRelation:
        if relation_id not in self._cross_relations:
            raise KeyError(f"Relationship {relation_id!r} was not found")
        relation = await self.validate_cross_relation(payload)
        relation = relation.model_copy(update={"id": relation_id})
        self._cross_relations[relation_id] = relation
        self._save()
        return relation

    def delete_cross_relation(self, relation_id: str) -> None:
        if relation_id not in self._cross_relations:
            raise KeyError(f"Relationship {relation_id!r} was not found")
        self._cross_relations.pop(relation_id)
        self._save()

    async def cross_relation_suggestions(self, datasource_ids: list[str] | None = None) -> list[CrossDatasourceRelation]:
        """Offer untrusted candidates based on key structure and sampled value coverage, never names."""
        source_ids = datasource_ids or list(self._sources)
        metadata = [await self.metadata(source_id) for source_id in source_ids]
        existing = {
            (relation.source_datasource, relation.source_field, relation.target_datasource, relation.target_field)
            for relation in self._cross_relations.values()
        }
        candidates: list[CrossDatasourceRelation] = []
        for source in metadata:
            for target in metadata:
                if source.datasource_id == target.datasource_id:
                    continue
                for source_table in source.structural_metadata:
                    for source_column in source_table.columns:
                        if source_column.primary_key:
                            continue
                        source_field = f"{source_table.schema_name}.{source_table.name}.{source_column.name}"
                        for target_table in target.structural_metadata:
                            for target_column in target_table.columns:
                                if not target_column.primary_key or not self._compatible_types(source_column.data_type, target_column.data_type):
                                    continue
                                target_field = f"{target_table.schema_name}.{target_table.name}.{target_column.name}"
                                relation_key = (source.datasource_id, source_field, target.datasource_id, target_field)
                                if relation_key in existing:
                                    continue
                                sampled_values = await self._distinct_values(source.datasource_id, source_field)
                                # Very low-cardinality fields (for example status flags) are too easy to
                                # match by coincidence. This is a structural/data-quality filter, not a name rule.
                                if len(sampled_values) < 5:
                                    continue
                                matched_values = await self._matching_values(target.datasource_id, target_field, sampled_values)
                                matched_count = sum(value in matched_values for value in sampled_values)
                                coverage = matched_count / len(sampled_values)
                                if coverage < 0.95:
                                    continue
                                candidates.append(
                                    CrossDatasourceRelation(
                                        source_datasource=source.datasource_id,
                                        source_field=source_field,
                                        target_datasource=target.datasource_id,
                                        target_field=target_field,
                                        label=(
                                            f"{self._humanize_table_name(source_table.name)} belong to "
                                            f"{self._humanize_table_name(target_table.name)}"
                                        ),
                                        confidence=coverage,
                                        sampled_values=len(sampled_values),
                                        matched_values=matched_count,
                                        origin="value_overlap",
                                        confirmed=False,
                                    )
                                )
        return candidates
