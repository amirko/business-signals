from business_signals.investigation.direct_answers import DirectAnswerMixin


def test_direct_answer_mixin_infers_display_fields_from_schema_roles() -> None:
    table = {
        "columns": [
            {"name": "catalog_code", "pk": True, "type": "text"},
            {"name": "catalog_caption", "pk": False, "type": "text"},
            {"name": "units", "pk": False, "type": "integer"},
        ],
        "foreign_keys": [],
    }

    assert DirectAnswerMixin._infer_display_column(table) == "catalog_caption"
    assert DirectAnswerMixin._redact_internal_identifier_columns(
        [{"catalog_code": "C-7", "catalog_caption": "Lantern", "units": 42}],
        {"catalog_code"},
    ) == [{"catalog_caption": "Lantern", "units": 42}]


def test_direct_answer_mixin_keeps_text_filters_safe_and_readable() -> None:
    sql = DirectAnswerMixin._case_insensitive_text_filters(
        "SELECT * FROM public.conversion_metrics metric "
        "JOIN public.stores store ON metric.store_id = store.id "
        "WHERE metric.platform = 'iOS' AND metric.completed_orders = 0"
    )

    assert "metric.platform ILIKE 'iOS'" in sql
    assert "metric.store_id = store.id" in sql
    assert "metric.completed_orders = 0" in sql
    assert DirectAnswerMixin._safe_direct_answer_text(
        "Return the three product IDs with the highest sales."
    ) == "Return the three items with the highest sales."
