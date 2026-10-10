from pathlib import Path

import pytest
from business_signals.datasources.registry import DatasourceRegistry
from business_signals.models import (
    ColumnMetadata,
    CrossDatasourceRelation,
    DatasourceCreate,
    DatasourceMetadata,
    DatasourceSummary,
    TableMetadata,
)


class PersistedFixtureDatasource:
    def __init__(self, datasource_id: str, config: DatasourceCreate) -> None:
        self.summary = DatasourceSummary(id=datasource_id, name=config.name, type=config.type)

    async def test_connection(self) -> None:
        self.summary.connected = True

    async def lightweight_fingerprint(self) -> str:
        return "fixture-fingerprint"

    async def discover_structure(self) -> DatasourceMetadata:
        self.summary.schema_cached = True
        self.summary.table_count = 1
        return DatasourceMetadata(
            datasource_id=self.summary.id,
            type=self.summary.type,
            fingerprint="fixture-fingerprint",
            structural_metadata=[
                TableMetadata(
                    schema_name="public",
                    name="orders",
                    columns=[ColumnMetadata(name="id", data_type="integer", nullable=False, primary_key=True)],
                )
            ],
        )


class PersistedFixtureRegistry(DatasourceRegistry):
    def build(self, config: DatasourceCreate, datasource_id: str | None = None) -> PersistedFixtureDatasource:
        return PersistedFixtureDatasource(datasource_id or "ds_saved", config)


def fixture_config() -> DatasourceCreate:
    return DatasourceCreate.model_validate(
        {
            "name": "Saved analytics",
            "type": "timescaledb",
            "credentials": {
                "host": "localhost",
                "port": 5434,
                "database": "analytics",
                "username": "reader",
                "password": "local-only-secret",
            },
        }
    )


@pytest.mark.asyncio
async def test_datasource_and_cached_schema_survive_registry_restart(tmp_path: Path) -> None:
    store_path = tmp_path / "state" / "datasources.json"
    registry = PersistedFixtureRegistry(store_path)
    added = await registry.add(fixture_config())
    discovered = await registry.refresh(added.id)

    restored = PersistedFixtureRegistry(store_path)

    assert [source.id for source in restored.list()] == [added.id]
    assert restored.list()[0].connected is False
    assert restored.list()[0].schema_cached is True
    assert restored.cached_metadata(added.id) == discovered
    assert store_path.stat().st_mode & 0o777 == 0o600
    assert "local-only-secret" in store_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_delete_removes_persisted_connection_and_schema(tmp_path: Path) -> None:
    store_path = tmp_path / "datasources.json"
    registry = PersistedFixtureRegistry(store_path)
    added = await registry.add(fixture_config())
    await registry.refresh(added.id)

    await registry.delete(added.id)
    restored = PersistedFixtureRegistry(store_path)

    assert restored.list() == []
    with pytest.raises(KeyError):
        restored.cached_metadata(added.id)


@pytest.mark.asyncio
async def test_approved_cross_datasource_relation_survives_restart_and_is_removed_with_connection(
    tmp_path: Path,
) -> None:
    store_path = tmp_path / "datasources.json"
    registry = PersistedFixtureRegistry(store_path)
    first = await registry.add(fixture_config())
    second_config = fixture_config().model_copy(update={"name": "Product catalog"})
    second = await registry.add(second_config)
    relation = CrossDatasourceRelation(
        source_datasource=first.id,
        source_field="public.activity.item_reference",
        target_datasource=second.id,
        target_field="public.catalog_items.item_reference",
        label="Activity records belong to catalog items",
        confidence=1,
        sampled_values=10,
        matched_values=10,
        origin="human",
        confirmed=True,
    )
    registry._cross_relations[relation.id] = relation
    registry._save()

    restored = PersistedFixtureRegistry(store_path)
    assert restored.approved_cross_relations([first.id, second.id]) == [relation]

    await restored.delete(second.id)
    assert restored.approved_cross_relations([first.id]) == []
