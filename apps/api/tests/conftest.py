"""Test-wide safeguards for local optional integrations."""

import os

import pytest

# LangGraph's LangSmith integration reads this environment variable as its
# modules load, before pytest fixtures can run. Prevent real trace ingestion
# from a developer's local .env during import and test execution.
os.environ["LANGSMITH_TRACING"] = "false"

from business_signals.config import settings


@pytest.fixture(autouse=True)
def disable_external_observability(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests must not create real LangSmith runs from a developer's .env."""
    monkeypatch.setattr(settings, "langsmith_tracing", False)
