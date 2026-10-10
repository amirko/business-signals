from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Sequence
from typing import Any


def summarize_query_rows(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Produce reproducible numeric facts from SQL output without an LLM."""
    numeric: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        for key, value in row.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            numeric[key].append(float(value))
    return {
        "row_count": len(rows),
        "numeric_columns": {
            key: {
                "count": len(values),
                "sum": sum(values),
                "mean": statistics.fmean(values),
                "min": min(values),
                "max": max(values),
            }
            for key, values in sorted(numeric.items())
            if values
        },
    }
