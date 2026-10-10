from business_signals.analytics import summarize_query_rows


def test_query_summary_is_deterministic() -> None:
    summary = summarize_query_rows([{"revenue": 80, "units": 4}, {"revenue": 40, "units": 2}])
    assert summary["row_count"] == 2
    assert summary["numeric_columns"]["revenue"] == {
        "count": 2,
        "sum": 120.0,
        "mean": 60.0,
        "min": 40.0,
        "max": 80.0,
    }
