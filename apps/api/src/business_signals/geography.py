"""Deterministic geographic resolution and boundary sampling primitives.

This module intentionally contains no provider URLs or field names.  A
catalog-defined geographic-weather runner supplies those details; these helpers
only turn validated locations and GeoJSON boundaries into auditable points.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal


ScopeKind = Literal["city", "multiple_locations", "region", "country"]


@dataclass(frozen=True)
class GeoPoint:
    latitude: float
    longitude: float
    label: str
    timezone: str | None = None


@dataclass(frozen=True)
class GeographicScope:
    kind: ScopeKind
    label: str
    points: tuple[GeoPoint, ...]
    coverage_method: str
    area_km2: float | None = None


def select_geocoding_candidate(subject: str, candidates: list[dict[str, Any]]) -> GeoPoint:
    """Select an exact city/country match without blindly trusting API ordering."""
    requested_parts = [part.strip().casefold() for part in subject.split(",") if part.strip()]
    requested_name = requested_parts[0] if requested_parts else subject.casefold()
    requested_context = set(requested_parts[1:])
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for candidate in candidates:
        name = str(candidate.get("name", "")).strip().casefold()
        country = str(candidate.get("country", "")).strip().casefold()
        region = str(candidate.get("admin1", "")).strip().casefold()
        if name != requested_name:
            continue
        context = {value for value in (country, region) if value}
        if requested_context and not requested_context.issubset(context):
            continue
        try:
            latitude, longitude = float(candidate["latitude"]), float(candidate["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        exact_context_score = len(requested_context & context)
        population = int(candidate.get("population") or 0)
        scored.append((exact_context_score, population, {**candidate, "latitude": latitude, "longitude": longitude}))
    if not scored:
        raise ValueError(f"No geocoding result precisely matched {subject!r}")
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    highest_score = scored[0][0]
    equally_specific = [item for item in scored if item[0] == highest_score]
    if not requested_context:
        candidate_places = {
            (
                str(item[2].get("country", "")).casefold(),
                str(item[2].get("admin1", "")).casefold(),
            )
            for item in equally_specific
        }
        if len(candidate_places) > 1:
            raise ValueError(f"Location {subject!r} is ambiguous; include its country or region")
    # Population is a tie-breaker only among otherwise valid equivalents. This
    # makes “Milan, Italy” deterministic while never selecting a different
    # country or region because it happens to be more populous.
    selected = equally_specific[0][2]
    return GeoPoint(
        latitude=selected["latitude"],
        longitude=selected["longitude"],
        label=", ".join(part.strip() for part in subject.split(",") if part.strip()),
        timezone=str(selected["timezone"]) if selected.get("timezone") else None,
    )


def geojson_area_km2(geometry: dict[str, Any]) -> float:
    """Approximate polygon area using a local equal-distance projection."""
    polygons = _polygons(geometry)
    if not polygons:
        raise ValueError("Boundary response did not contain a Polygon or MultiPolygon")
    latitudes = [point[1] for polygon in polygons for ring in polygon for point in ring]
    reference_latitude = sum(latitudes) / len(latitudes)
    metres_per_degree_lon = 111_320 * math.cos(math.radians(reference_latitude))
    metres_per_degree_lat = 110_574

    def ring_area(ring: list[list[float]]) -> float:
        projected = [(point[0] * metres_per_degree_lon, point[1] * metres_per_degree_lat) for point in ring]
        return abs(sum(
            x1 * y2 - x2 * y1
            for (x1, y1), (x2, y2) in zip(projected, projected[1:] + projected[:1], strict=True)
        )) / 2

    area_m2 = 0.0
    for polygon in polygons:
        area_m2 += ring_area(polygon[0]) - sum(ring_area(hole) for hole in polygon[1:])
    return max(0.0, area_m2 / 1_000_000)


def sample_boundary(
    geometry: dict[str, Any],
    label: str,
    target_cell_area_km2: float,
) -> GeographicScope:
    """Create an area-scaled, boundary-clipped grid with at least one point."""
    polygons = _polygons(geometry)
    if not polygons:
        raise ValueError("Boundary response did not contain a Polygon or MultiPolygon")
    area_km2 = geojson_area_km2(geometry)
    point_budget = max(1, math.ceil(area_km2 / target_cell_area_km2))
    min_lon = min(point[0] for polygon in polygons for ring in polygon for point in ring)
    max_lon = max(point[0] for polygon in polygons for ring in polygon for point in ring)
    min_lat = min(point[1] for polygon in polygons for ring in polygon for point in ring)
    max_lat = max(point[1] for polygon in polygons for ring in polygon for point in ring)
    mid_lat = (min_lat + max_lat) / 2
    width_km = max(1.0, (max_lon - min_lon) * 111.32 * math.cos(math.radians(mid_lat)))
    height_km = max(1.0, (max_lat - min_lat) * 110.57)
    aspect = width_km / height_km
    columns = max(1, math.ceil(math.sqrt(point_budget * aspect)))
    rows = max(1, math.ceil(point_budget / columns))
    lon_step, lat_step = (max_lon - min_lon) / columns, (max_lat - min_lat) / rows
    points: list[GeoPoint] = []
    for row in range(rows):
        latitude = min_lat + (row + 0.5) * lat_step
        for column in range(columns):
            longitude = min_lon + (column + 0.5) * lon_step
            if _contains(polygons, longitude, latitude):
                points.append(GeoPoint(latitude=latitude, longitude=longitude, label=label))
    if not points:
        raise ValueError(f"Could not create an in-boundary geographic point for {label}")
    # Irregular boundaries can produce more valid cells than the nominal budget.
    # Deterministic striding keeps coverage spread across the full geometry;
    # provider request limits are handled later by batching, not by discarding
    # geographic coverage here.
    if len(points) > point_budget:
        stride = len(points) / point_budget
        points = [points[min(len(points) - 1, math.floor(index * stride))] for index in range(point_budget)]
    return GeographicScope(
        kind="country" if _looks_like_country(geometry) else "region",
        label=label,
        points=tuple(points),
        coverage_method="polygon_grid",
        area_km2=area_km2,
    )


def _looks_like_country(geometry: dict[str, Any]) -> bool:
    return bool(geometry.get("properties", {}).get("country_code"))


def _polygons(geometry: dict[str, Any]) -> list[list[list[list[float]]]]:
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if geometry_type == "Polygon" and isinstance(coordinates, list):
        return [coordinates]
    if geometry_type == "MultiPolygon" and isinstance(coordinates, list):
        return coordinates
    return []


def _contains(polygons: list[list[list[list[float]]]], longitude: float, latitude: float) -> bool:
    return any(_in_ring(polygon[0], longitude, latitude) and not any(
        _in_ring(hole, longitude, latitude) for hole in polygon[1:]
    ) for polygon in polygons)


def _in_ring(ring: list[list[float]], longitude: float, latitude: float) -> bool:
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1], strict=True):
        intersects = (y1 > latitude) != (y2 > latitude) and longitude < (x2 - x1) * (latitude - y1) / (y2 - y1) + x1
        if intersects:
            inside = not inside
    return inside
