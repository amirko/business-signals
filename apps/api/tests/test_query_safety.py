import pytest

from business_signals.datasources.safety import UnsafeQueryError, validate_read_query


@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM sales_events",
        "WITH gone AS (DELETE FROM events RETURNING *) SELECT * FROM gone",
        "DROP TABLE products",
        "SELECT * INTO backup FROM products",
        "SELECT 1; SELECT 2",
        "SELECT * FROM products -- bypass",
    ],
)
def test_rejects_unsafe_queries(query: str) -> None:
    with pytest.raises(UnsafeQueryError):
        validate_read_query(query)


def test_adds_and_caps_limit() -> None:
    assert validate_read_query("SELECT * FROM products", 100).endswith("LIMIT 100")
    assert validate_read_query("WITH p AS (SELECT * FROM products) SELECT * FROM p LIMIT 999", 50).endswith("LIMIT 50")


def test_allows_read_only_cte() -> None:
    result = validate_read_query("WITH totals AS (SELECT sum(revenue) AS total FROM sales) SELECT * FROM totals")
    assert result.startswith("WITH totals AS")
