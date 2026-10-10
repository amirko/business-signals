from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from business_signals.config import settings
from business_signals.datasources.base import Datasource
from business_signals.datasources.safety import validate_read_query
from business_signals.models import (
    ColumnMetadata,
    DatasourceCreate,
    DatasourceMetadata,
    DatasourceSummary,
    TableMetadata,
    now_utc,
)
from business_signals.observability import monitor


class PostgreSQLDatasource(Datasource):
    def __init__(self, datasource_id: str, config: DatasourceCreate) -> None:
        self.config = config
        self.summary = DatasourceSummary(id=datasource_id, name=config.name, type=config.type)
        c = config.credentials
        url = URL.create(
            drivername="postgresql+asyncpg",
            username=c.username,
            password=c.password.get_secret_value(),
            host=c.host,
            port=c.port,
            database=c.database,
        )
        connect_args = {
            "server_settings": {
                "application_name": "business-signals",
                "default_transaction_read_only": "on",
            }
        }
        self.engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True, connect_args=connect_args)

    async def test_connection(self) -> None:
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        self.summary.connected = True

    async def close(self) -> None:
        await self.engine.dispose()

    async def _structure_rows(self) -> list[dict[str, Any]]:
        sql = """
        SELECT c.table_schema, c.table_name, c.column_name, c.data_type, c.is_nullable,
               COALESCE(tc.constraint_type = 'PRIMARY KEY', false) AS is_primary_key,
               COALESCE(s.n_live_tup, 0)::bigint AS approximate_rows
        FROM information_schema.columns c
        LEFT JOIN information_schema.key_column_usage kcu
          ON kcu.table_schema=c.table_schema AND kcu.table_name=c.table_name
         AND kcu.column_name=c.column_name
        LEFT JOIN information_schema.table_constraints tc
          ON tc.constraint_schema=kcu.constraint_schema AND tc.constraint_name=kcu.constraint_name
         AND tc.constraint_type='PRIMARY KEY'
        LEFT JOIN pg_stat_user_tables s
          ON s.schemaname=c.table_schema AND s.relname=c.table_name
        WHERE c.table_schema NOT IN (
          'pg_catalog', 'information_schema',
          '_timescaledb_internal', '_timescaledb_catalog', '_timescaledb_config', '_timescaledb_cache',
          'timescaledb_information', 'timescaledb_experimental'
        )
          AND c.table_schema <> :checkpoint_schema
        ORDER BY c.table_schema, c.table_name, c.ordinal_position
        """
        async with self.engine.connect() as connection:
            result = await connection.execute(text(sql), {"checkpoint_schema": settings.checkpoint_schema})
            return [dict(row) for row in result.mappings()]

    async def lightweight_fingerprint(self) -> str:
        rows = await self._structure_rows()
        normalized = [(r["table_schema"], r["table_name"], r["column_name"], r["data_type"], r["is_nullable"]) for r in rows]
        return hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()

    async def discover_structure(self) -> DatasourceMetadata:
        rows = await self._structure_rows()
        tables: dict[tuple[str, str], TableMetadata] = {}
        for row in rows:
            key = (row["table_schema"], row["table_name"])
            table = tables.setdefault(
                key,
                TableMetadata(
                    schema_name=row["table_schema"],
                    name=row["table_name"],
                    approximate_rows=row["approximate_rows"],
                ),
            )
            table.columns.append(
                ColumnMetadata(
                    name=row["column_name"],
                    data_type=row["data_type"],
                    nullable=row["is_nullable"] == "YES",
                    primary_key=bool(row["is_primary_key"]),
                )
            )

        await self._enrich_foreign_keys(tables)
        await self._enrich_indexes(tables)
        fingerprint = await self.lightweight_fingerprint()
        result = DatasourceMetadata(
            datasource_id=self.summary.id,
            type=self.summary.type,
            structural_metadata=list(tables.values()),
            fingerprint=fingerprint,
        )
        self.summary.connected = True
        self.summary.table_count = len(tables)
        self.summary.schema_cached = True
        self.summary.discovered_at = now_utc()
        return result

    async def _enrich_foreign_keys(self, tables: dict[tuple[str, str], TableMetadata]) -> None:
        sql = """
        SELECT tc.table_schema, tc.table_name, kcu.column_name,
               ccu.table_schema AS foreign_schema, ccu.table_name AS foreign_table,
               ccu.column_name AS foreign_column
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu USING (constraint_catalog, constraint_schema, constraint_name)
        JOIN information_schema.constraint_column_usage ccu USING (constraint_catalog, constraint_schema, constraint_name)
        WHERE tc.constraint_type = 'FOREIGN KEY'
        """
        async with self.engine.connect() as connection:
            rows = (await connection.execute(text(sql))).mappings()
            for row in rows:
                table = tables.get((row["table_schema"], row["table_name"]))
                if table:
                    table.foreign_keys.append(dict(row))

    async def _enrich_indexes(self, tables: dict[tuple[str, str], TableMetadata]) -> None:
        sql = "SELECT schemaname, tablename, indexname, indexdef FROM pg_indexes WHERE schemaname NOT LIKE 'pg_%'"
        async with self.engine.connect() as connection:
            rows = (await connection.execute(text(sql))).mappings()
            for row in rows:
                table = tables.get((row["schemaname"], row["tablename"]))
                if table:
                    table.indexes.append({"name": row["indexname"], "definition": row["indexdef"]})

    async def describe_entity(self, entity: str) -> dict[str, Any]:
        metadata = await self.discover_structure()
        table = next((t for t in metadata.structural_metadata if t.name == entity), None)
        return table.model_dump(mode="json") if table else {}

    async def sample_data(self, entity: str, limit: int = 5) -> list[dict[str, Any]]:
        safe_entity = entity.replace('"', '""')
        return await self.execute_read_query(f'SELECT * FROM "{safe_entity}" LIMIT {min(limit, 20)}')

    async def execute_read_query(self, query: str) -> list[dict[str, Any]]:
        safe_sql = validate_read_query(query, settings.max_query_rows)
        async with monitor.span(
            "SQL read",
            run_type="tool",
            inputs=monitor.content_or_fingerprint(safe_sql),
            metadata={"datasource_type": self.summary.type.value, "query_fingerprint": monitor.content_or_fingerprint(safe_sql)["fingerprint"]},
            tags=["business-signals", "sql"],
        ) as trace:
            async with self.engine.connect() as connection:
                await connection.execute(text(f"SET LOCAL statement_timeout = {settings.query_timeout_seconds * 1000}"))
                result = await connection.execute(text(safe_sql))
                rows = [dict(row) for row in result.mappings()]
            trace.set_output(row_count=len(rows))
            return rows

    async def get_statistics(self, entity: str) -> dict[str, Any]:
        safe_entity = entity.replace('"', '""')
        rows = await self.execute_read_query(f'SELECT COUNT(*) AS row_count FROM "{safe_entity}"')
        return rows[0] if rows else {"row_count": 0}

    def capabilities(self) -> set[str]:
        return {"sql", "schema_discovery", "sampling", "statistics", "transactions"}


class TimescaleDatasource(PostgreSQLDatasource):
    async def discover_structure(self) -> DatasourceMetadata:
        metadata = await super().discover_structure()
        sql = """
        SELECT hypertable_schema, hypertable_name, num_dimensions
        FROM timescaledb_information.hypertables
        """
        try:
            async with self.engine.connect() as connection:
                hypertables = [dict(r) for r in (await connection.execute(text(sql))).mappings()]
                dimensions = [
                    dict(r)
                    for r in (
                        await connection.execute(
                            text("SELECT hypertable_schema, hypertable_name, column_name, dimension_type FROM timescaledb_information.dimensions")
                        )
                    ).mappings()
                ]
        except Exception:
            hypertables, dimensions = [], []

        by_key = {(t.schema_name, t.name): t for t in metadata.structural_metadata}
        for hypertable in hypertables:
            key = (hypertable["hypertable_schema"], hypertable["hypertable_name"])
            if key in by_key:
                by_key[key].is_hypertable = True
                dimension = next(
                    (d for d in dimensions if (d["hypertable_schema"], d["hypertable_name"]) == key and d["dimension_type"] == "Time"),
                    None,
                )
                by_key[key].time_column = dimension["column_name"] if dimension else None
        return metadata

    def capabilities(self) -> set[str]:
        return super().capabilities() | {"time_series", "hypertables", "time_buckets"}
