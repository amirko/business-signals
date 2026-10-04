from business_signals.config import Settings
from business_signals.models import Evidence, Hypothesis, InvestigationCreate, InvestigationLimits


def test_confidence_is_bounded() -> None:
    evidence = Evidence(description="Stock fell first", source="analytics", relationship="direct", confidence=0.9)
    assert evidence.confidence == 0.9


def test_hypotheses_get_stable_prefixes() -> None:
    hypothesis = Hypothesis(name="Inventory constraint", description="Inventory constrained sales", category="inventory", confidence=0.3)
    assert hypothesis.id.startswith("hyp_")


def test_investigation_requires_datasource() -> None:
    request = InvestigationCreate(question="Why did revenue decline last week?", datasource_ids=["sales"])
    assert InvestigationLimits().max_iterations == 8
    assert request.datasource_ids == ["sales"]


def test_investigation_limits_are_configured_from_environment_settings() -> None:
    configured = Settings(
        _env_file=None,
        investigation_max_iterations=21,
        investigation_max_sql_queries=34,
        investigation_max_external_calls=5,
        investigation_max_duration_seconds=900,
        ignore_investigation_limits=True,
        target_cell_area_km2=1250,
        max_points_weather_api=42,
    )

    assert configured.investigation_max_iterations == 21
    assert configured.investigation_max_sql_queries == 34
    assert configured.investigation_max_external_calls == 5
    assert configured.investigation_max_duration_seconds == 900
    assert configured.ignore_investigation_limits is True
    assert configured.target_cell_area_km2 == 1250
    assert configured.max_points_weather_api == 42
