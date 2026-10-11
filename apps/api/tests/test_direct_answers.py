from types import SimpleNamespace

from business_signals.investigation.direct_answers import DirectAnswerMixin
from business_signals.llm import DirectAnswerCombination, DirectAnswerQuery
from business_signals.models import CrossDatasourceRelation


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
    assert (
        DirectAnswerMixin._safe_direct_answer_text("Return the three product IDs with the highest sales.")
        == "Return the three items with the highest sales."
    )


def test_direct_answer_aggregates_complete_joined_rows_before_display_capping() -> None:
    rows = [
        {
            "product_key": f"P{index:03d}",
            "category_label": "Outdoor" if index % 2 else "Home",
            "revenue": "10.25",
        }
        for index in range(206)
    ]
    combination = DirectAnswerCombination(
        operation="aggregate",
        primary_key="product_key",
        supporting_query_index=0,
        supporting_key="product_key",
        group_by="category_label",
        measure="revenue",
        aggregation="sum",
    )

    result = DirectAnswerMixin._aggregate_direct_answer_rows(rows, combination)

    assert [(row["category_label"], str(row["revenue"])) for row in result] == [
        ("Home", "1055.75"),
        ("Outdoor", "1055.75"),
    ]


def test_direct_answer_aggregate_excludes_primary_rows_outside_supporting_lookup() -> None:
    """A group label from a supporting source defines the requested scope."""
    combination = DirectAnswerCombination(
        operation="aggregate",
        primary_key="store_id",
        supporting_query_index=0,
        supporting_key="id",
        group_by="city",
        measure="revenue",
        aggregation="sum",
    )

    result = DirectAnswerMixin._aggregate_direct_answer_rows(
        [
            {"store_id": "S1", "city": "Dubai", "revenue": "125.50"},
            {"store_id": "S2", "city": "Dubai", "revenue": "74.50"},
            # This store was not returned by the Dubai lookup, so it has no
            # city label after the approved in-memory merge.
            {"store_id": "S3", "revenue": "900.00"},
        ],
        combination,
    )

    assert result[0]["city"] == "Dubai"
    assert str(result[0]["revenue"]) == "200.00"


def test_direct_answer_keeps_valid_rows_when_a_stale_aggregate_does_not_match() -> None:
    """A query repair may change fields without invalidating the retrieved answer."""
    combination = DirectAnswerCombination(
        operation="aggregate",
        primary_key="store_id",
        supporting_query_index=0,
        supporting_key="id",
        group_by="city",
        measure="revenue",
        aggregation="sum",
    )
    rows = [{"product_id": "P-1", "name": "Lantern", "total_revenue": "125.50"}]

    result = DirectAnswerMixin._aggregate_or_keep_direct_answer_rows(rows, combination)

    assert result == rows


def test_direct_answer_keeps_primary_rows_when_a_stale_set_operation_does_not_match() -> None:
    """A successful product lookup must not fail because it exposes `id`, not `product_id`."""
    combination = DirectAnswerCombination(
        operation="intersection",
        primary_key="product_id",
        supporting_query_index=0,
        supporting_key="product_id",
    )
    rows = [{"product_id": "P-1", "revenue": "125.50"}]

    result = DirectAnswerMixin._combine_or_keep_direct_answer_rows(
        rows,
        [{"id": "P-1", "name": "Lantern"}],
        combination,
    )

    assert result == rows


def test_direct_answer_keeps_product_name_when_store_and_product_lookups_both_return_name() -> None:
    """Readable fields from two related catalog tables must not overwrite each other."""

    product_relation = CrossDatasourceRelation(
        id="product-relation",
        source_datasource="analytics",
        source_field="public.sales_events.product_id",
        target_datasource="catalog",
        target_field="public.products.id",
        label="sales product",
        confidence=1,
        origin="human",
        confirmed=True,
    )
    store_relation = CrossDatasourceRelation(
        id="store-relation",
        source_datasource="analytics",
        source_field="public.sales_events.store_id",
        target_datasource="catalog",
        target_field="public.stores.id",
        label="sales store",
        confidence=1,
        origin="human",
        confirmed=True,
    )
    mixin = DirectAnswerMixin()
    mixin.registry = SimpleNamespace(approved_cross_relations=lambda _ids: [product_relation, store_relation])
    state = SimpleNamespace(datasources=[SimpleNamespace(id="analytics"), SimpleNamespace(id="catalog")])
    primary_rows = [{"product_id": "P1", "store_id": "S1", "revenue": "125.50"}]
    supporting_results = [
        (
            [{"id": "S1", "name": "Dubai Mall"}],
            DirectAnswerQuery(datasource_id="catalog", sql="SELECT id, name FROM public.stores", purpose="store names"),
        ),
        (
            [{"id": "P1", "name": "Lantern"}],
            DirectAnswerQuery(datasource_id="catalog", sql="SELECT id, name FROM public.products", purpose="product names"),
        ),
    ]

    result = mixin._merge_approved_supporting_rows(
        state,
        primary_rows,
        "analytics",
        "SELECT product_id, store_id, revenue FROM public.sales_events",
        supporting_results,
    )

    assert result == [{"product_id": "P1", "store_id": "S1", "revenue": "125.50", "name": "Dubai Mall", "product_name": "Lantern"}]


def test_direct_answer_does_not_treat_a_new_follow_up_as_an_entity_reference() -> None:
    state = SimpleNamespace(
        question="Follow-up request: What was the best-selling item in Dubai?",
        current_conversation_question="What was the best-selling item in Dubai?",
        resolved_entities=[
            SimpleNamespace(display_name="Lantern"),
            SimpleNamespace(display_name="Trekking Poles"),
        ],
    )

    assert DirectAnswerMixin._question_refers_to_retained_entity(state) is False
