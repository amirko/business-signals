from business_signals.config import Settings, settings
from business_signals.investigation.workflow import InvestigationEngine
from business_signals.models import (
    CrossDatasourceLookupCacheEntry,
    Evidence,
    Hypothesis,
    InvestigationCreate,
    InvestigationLimits,
    InvestigationScope,
)
from business_signals.research_agents import JsonResponse, PipelineOutput


def test_confidence_is_bounded() -> None:
    evidence = Evidence(description="Stock fell first", source="analytics", relationship="direct", confidence=0.9)
    assert evidence.confidence == 0.9


def test_hypotheses_get_stable_prefixes() -> None:
    hypothesis = Hypothesis(
        name="Inventory constraint",
        description="Inventory constrained sales",
        category="inventory",
        confidence=0.3,
    )
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
        external_research_checks_per_batch=2,
        investigation_max_duration_seconds=900,
        ignore_investigation_limits=True,
        metric_definition_confidence=0.72,
        report_percent_decimal_places=2,
        source_reliability_default=0.61,
        target_cell_area_km2=1250,
        max_points_weather_api=42,
        external_http_timeout_seconds=17,
        external_research_max_period_days=730,
        anthropic_http_timeout_seconds=75,
        anthropic_max_tokens=2048,
        query_repair_max_attempts=3,
        entity_lookup_max_rows=75,
        cross_datasource_lookup_max_values=250,
        direct_answer_display_max_rows=400,
        retained_entity_reference_max_count=80,
        cross_datasource_relation_sample_size=90,
        cross_datasource_relation_min_sample_values=4,
        cross_datasource_relation_min_coverage=0.92,
        supported_hypothesis_confidence_threshold=0.81,
        insufficient_evidence_confidence_threshold=0.51,
        direct_evidence_conclusion_confidence_threshold=0.77,
        indirect_evidence_confidence_ceiling=0.76,
        unavailable_data_confidence=0.93,
    )

    assert configured.investigation_max_iterations == 21
    assert configured.investigation_max_sql_queries == 34
    assert configured.investigation_max_external_calls == 5
    assert configured.external_research_checks_per_batch == 2
    assert configured.investigation_max_duration_seconds == 900
    assert configured.ignore_investigation_limits is True
    assert configured.metric_definition_confidence == 0.72
    assert configured.report_percent_decimal_places == 2
    assert configured.source_reliability_default == 0.61
    assert configured.target_cell_area_km2 == 1250
    assert configured.max_points_weather_api == 42
    assert configured.external_http_timeout_seconds == 17
    assert configured.external_research_max_period_days == 730
    assert configured.anthropic_http_timeout_seconds == 75
    assert configured.anthropic_max_tokens == 2048
    assert configured.query_repair_max_attempts == 3
    assert configured.entity_lookup_max_rows == 75
    assert configured.cross_datasource_lookup_max_values == 250
    assert configured.direct_answer_display_max_rows == 400
    assert configured.retained_entity_reference_max_count == 80
    assert configured.cross_datasource_relation_sample_size == 90
    assert configured.cross_datasource_relation_min_sample_values == 4
    assert configured.cross_datasource_relation_min_coverage == 0.92
    assert configured.supported_hypothesis_confidence_threshold == 0.81
    assert configured.insufficient_evidence_confidence_threshold == 0.51
    assert configured.direct_evidence_conclusion_confidence_threshold == 0.77
    assert configured.indirect_evidence_confidence_ceiling == 0.76
    assert configured.unavailable_data_confidence == 0.93


def test_research_agent_reliability_uses_the_configured_default(monkeypatch) -> None:
    monkeypatch.setattr(settings, "source_reliability_default", 0.61)

    response = JsonResponse.model_validate({"observation_template": "A result was returned."})
    pipeline_output = PipelineOutput.model_validate({"observation_template": "A result was returned."})

    assert response.source_reliability == 0.61
    assert pipeline_output.source_reliability == 0.61


def test_checkpoint_serializer_allows_persisted_investigation_models() -> None:
    scope = InvestigationScope(metric="revenue", filters=["Dubai"])
    lookup_cache = CrossDatasourceLookupCacheEntry(
        key="cache-key",
        relation_id="rel_store",
        relation_signature="analytics\nstore_id\ncatalog\nid",
        datasource_id="catalog",
        schema_fingerprint="catalog-v1",
        lookup_sql="SELECT id AS relationship_key FROM public.stores",
        values=["store-1"],
        source_row_count=1,
    )
    serializer = InvestigationEngine.checkpoint_serde()

    assert serializer.loads_typed(serializer.dumps_typed(scope)) == scope
    assert serializer.loads_typed(serializer.dumps_typed(lookup_cache)) == lookup_cache


def test_cross_datasource_lookup_cache_identity_is_the_source_query_not_a_relation_id() -> None:
    sql = "SELECT id AS relationship_key FROM public.stores WHERE city = 'Dubai'"

    first_key = InvestigationEngine._cross_datasource_lookup_cache_key("catalog", "catalog-v1", sql)
    second_key = InvestigationEngine._cross_datasource_lookup_cache_key("catalog", "catalog-v1", sql)

    assert first_key == second_key
    assert first_key != InvestigationEngine._cross_datasource_lookup_cache_key("catalog", "catalog-v2", sql)
