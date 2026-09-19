from __future__ import annotations

import asyncio
from uuid import uuid4

from business_signals.datasources.base import Datasource
from business_signals.datasources.postgres import PostgreSQLDatasource, TimescaleDatasource
from business_signals.models import (
    CrossDatasourceRelation,
    DatasourceCreate,
    DatasourceMetadata,
    DatasourceSummary,
    DatasourceType,
)


class DatasourceRegistry:
    """In-memory by design: credentials never leave the backend process."""

    def __init__(self) -> None:
        self._sources: dict[str, Datasource] = {}
        self._metadata: dict[str, DatasourceMetadata] = {}
        self._locks: dict[str, asyncio.Lock] = {}

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
        await self.metadata(source.summary.id)
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
            return fresh

    async def refresh(self, datasource_id: str) -> DatasourceMetadata:
        return await self.metadata(datasource_id, force=True)

    async def infer_cross_relations(self, datasource_ids: list[str]) -> list[CrossDatasourceRelation]:
        """Propose conservative identifier links; callers must confirm before relying on them."""
        metadata = [await self.metadata(source_id) for source_id in datasource_ids]
        relations: list[CrossDatasourceRelation] = []
        for source in metadata:
            for table in source.structural_metadata:
                for column in table.columns:
                    if not column.name.endswith("_id"):
                        continue
                    entity = column.name.removesuffix("_id")
                    target_names = {entity, f"{entity}s", f"{entity}es"}
                    for target in metadata:
                        if target.datasource_id == source.datasource_id:
                            continue
                        for target_table in target.structural_metadata:
                            target_id = next((candidate for candidate in target_table.columns if candidate.name == "id"), None)
                            if target_table.name in target_names and target_id and target_id.data_type == column.data_type:
                                relations.append(
                                    CrossDatasourceRelation(
                                        source_datasource=source.datasource_id,
                                        source_field=f"{table.schema_name}.{table.name}.{column.name}",
                                        target_datasource=target.datasource_id,
                                        target_field=f"{target_table.schema_name}.{target_table.name}.id",
                                        confidence=0.78,
                                        origin="name_type_match",
                                        confirmed=False,
                                    )
                                )
        return relations
