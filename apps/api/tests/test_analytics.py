import pytest

from business_signals.analytics import (
    before_after,
    correlation,
    duplicate_count,
    missing_data,
    percentage_change,
    period_over_period,
    segmentation,
    zscore_anomalies,
)


def test_percentage_and_period_change() -> None:
    assert percentage_change(100, 75) == -25
    assert percentage_change(0, 10) is None
    assert period_over_period([30, 45], [50, 50]) == {
        "current": 75.0,
        "previous": 100.0,
        "absolute_change": -25.0,
        "percentage_change": -25.0,
    }


def test_data_quality_helpers() -> None:
    rows = [{"id": 1, "region": "north", "value": 10}, {"id": 1, "region": "north", "value": None}]
    assert duplicate_count(rows, ["id"]) == 1
    assert missing_data(rows)["value"]["missing_pct"] == 50
    assert segmentation(rows, "region", "value")[0]["total"] == 10


def test_statistical_helpers() -> None:
    assert correlation([1, 2, 3], [2, 4, 6]) == pytest.approx(1)
    assert before_after([10, 10], [8, 8])["percentage_change"] == -20
    assert zscore_anomalies([10] * 20 + [100], threshold=3)[0]["index"] == 20
