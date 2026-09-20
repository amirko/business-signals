from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from uuid import uuid4

from business_signals.config import settings
from business_signals.datasources.base import Datasource
from business_signals.datasources.postgres import PostgreSQLDatasource, TimescaleDatasource
from business_signals.models import (
    CrossDatasourceRelation,
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
            "version": 1,
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
        close = getattr(source, "close", None)
        if close is not None:
            await close()
        self._save()

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
