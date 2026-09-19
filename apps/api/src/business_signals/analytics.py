from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from typing import Any, Hashable, Iterable, Sequence


def percentage_change(previous: float, current: float) -> float | None:
    if previous == 0:
        return None
    return (current - previous) / abs(previous) * 100


def period_over_period(current: Sequence[float], previous: Sequence[float]) -> dict[str, float | None]:
    current_total, previous_total = float(sum(current)), float(sum(previous))
    return {
        "current": current_total,
        "previous": previous_total,
        "absolute_change": current_total - previous_total,
        "percentage_change": percentage_change(previous_total, current_total),
    }


def cohort_comparison(values: Iterable[tuple[Hashable, float]]) -> dict[Hashable, dict[str, float]]:
    groups: dict[Hashable, list[float]] = defaultdict(list)
    for cohort, value in values:
        groups[cohort].append(float(value))
    return {
        cohort: {"count": float(len(items)), "mean": statistics.fmean(items), "total": sum(items)}
        for cohort, items in groups.items()
    }


def segmentation(rows: Iterable[dict[str, Any]], dimension: str, metric: str) -> list[dict[str, Any]]:
    groups: dict[Any, list[float]] = defaultdict(list)
    for row in rows:
        if row.get(metric) is not None:
            groups[row.get(dimension)].append(float(row[metric]))
    return sorted(
        ({"segment": key, "count": len(values), "total": sum(values), "mean": statistics.fmean(values)} for key, values in groups.items()),
        key=lambda item: item["total"],
        reverse=True,
    )


def missing_data(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, float]]:
    if not rows:
        return {}
    columns = {key for row in rows for key in row}
    total = len(rows)
    return {
        column: {
            "missing": float(sum(row.get(column) is None for row in rows)),
            "missing_pct": sum(row.get(column) is None for row in rows) / total * 100,
        }
        for column in sorted(columns)
    }


def duplicate_count(rows: Sequence[dict[str, Any]], keys: Sequence[str]) -> int:
    fingerprints = [tuple(row.get(key) for key in keys) for row in rows]
    return sum(count - 1 for count in Counter(fingerprints).values() if count > 1)


def distribution_comparison(baseline: Sequence[float], observed: Sequence[float]) -> dict[str, float]:
    if not baseline or not observed:
        raise ValueError("Both distributions require observations")
    return {
        "baseline_mean": statistics.fmean(baseline),
        "observed_mean": statistics.fmean(observed),
        "mean_shift": statistics.fmean(observed) - statistics.fmean(baseline),
        "baseline_stddev": statistics.pstdev(baseline),
        "observed_stddev": statistics.pstdev(observed),
    }


def correlation(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    x_mean, y_mean = statistics.fmean(xs), statistics.fmean(ys)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys, strict=True))
    denominator = math.sqrt(sum((x - x_mean) ** 2 for x in xs) * sum((y - y_mean) ** 2 for y in ys))
    return numerator / denominator if denominator else None


def zscore_anomalies(values: Sequence[float], threshold: float = 2.5) -> list[dict[str, float | int]]:
    if len(values) < 3 or statistics.pstdev(values) == 0:
        return []
    mean, stddev = statistics.fmean(values), statistics.pstdev(values)
    return [
        {"index": index, "value": value, "zscore": (value - mean) / stddev}
        for index, value in enumerate(values)
        if abs((value - mean) / stddev) >= threshold
    ]


def trend_change(values: Sequence[float]) -> dict[str, float | None]:
    if len(values) < 4:
        return {"before_slope": None, "after_slope": None, "slope_change": None}
    midpoint = len(values) // 2

    def slope(part: Sequence[float]) -> float:
        xs = list(range(len(part)))
        x_mean, y_mean = statistics.fmean(xs), statistics.fmean(part)
        denominator = sum((x - x_mean) ** 2 for x in xs)
        return sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, part, strict=True)) / denominator

    before, after = slope(values[:midpoint]), slope(values[midpoint:])
    return {"before_slope": before, "after_slope": after, "slope_change": after - before}


def before_after(values_before: Sequence[float], values_after: Sequence[float]) -> dict[str, float | None]:
    if not values_before or not values_after:
        raise ValueError("Before and after windows must not be empty")
    before, after = statistics.fmean(values_before), statistics.fmean(values_after)
    return {"before_mean": before, "after_mean": after, "percentage_change": percentage_change(before, after)}
