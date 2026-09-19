from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from business_signals.models import DatasourceMetadata, DatasourceSummary


class Datasource(ABC):
    """Adapter contract intentionally not coupled to relational databases."""

    summary: DatasourceSummary

    @abstractmethod
    async def test_connection(self) -> None: ...

    @abstractmethod
    async def discover_structure(self) -> DatasourceMetadata: ...

    @abstractmethod
    async def lightweight_fingerprint(self) -> str: ...

    @abstractmethod
    async def describe_entity(self, entity: str) -> dict[str, Any]: ...

    @abstractmethod
    async def sample_data(self, entity: str, limit: int = 5) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def execute_read_query(self, query: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def get_statistics(self, entity: str) -> dict[str, Any]: ...

    @abstractmethod
    def capabilities(self) -> set[str]: ...
