from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from business_signals.external import ExternalResearcher
from business_signals.config import settings
from business_signals.research_agents import ResearchAgentCatalog, ResearchAgentCatalogModel
from business_signals.geography import sample_boundary, select_geocoding_candidate


def test_geocoding_uses_country_context_before_population() -> None:
    point = select_geocoding_candidate(
        "Milan, Italy",
        [
            {"name": "Milan", "country": "United States", "latitude": 41, "longitude": -91, "population": 9_000},
            {"name": "Milan", "country": "Italy", "latitude": 45.46427, "longitude": 9.18951, "population": 1_371_498, "timezone": "Europe/Rome"},
        ],
    )

    assert (point.latitude, point.longitude, point.timezone) == (45.46427, 9.18951, "Europe/Rome")


def test_geocoding_refuses_an_ambiguous_city_without_context() -> None:
    with pytest.raises(ValueError, match="ambiguous"):
        select_geocoding_candidate(
            "Milan",
            [
                {"name": "Milan", "country": "United States", "latitude": 41, "longitude": -91},
                {"name": "Milan", "country": "Italy", "latitude": 45, "longitude": 9},
            ],
        )


def test_polygon_sampling_scales_with_area_without_discarding_coverage() -> None:
    boundary = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
    }

    scope = sample_boundary(boundary, "Example region", target_cell_area_km2=2_500)

    assert scope.coverage_method == "polygon_grid"
    assert scope.area_km2 and scope.area_km2 > 10_000
    assert len(scope.points) == 5
    assert all(0 < point.latitude < 1 and 0 < point.longitude < 1 for point in scope.points)


class _Response:
    status_code = 200
    headers: dict[str, str] = {}

    def __init__(self, url: str, payload: Any) -> None:
        self.url = httpx.URL(url)
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


class _WeatherClient:
    requests: list[tuple[str, dict[str, str]]] = []

    async def __aenter__(self) -> "_WeatherClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def request(self, _method: str, url: str, **kwargs: Any) -> _Response:
        params = kwargs["params"]
        self.requests.append((url, params))
        if "geocoding-api" in url:
            return _Response(
                "https://geocoding-api.open-meteo.com/v1/search?name=Milan",
                {
                    "results": [
                        {
                            "name": "Milan",
                            "country": "Italy",
                            "latitude": 45.46427,
                            "longitude": 9.18951,
                            "feature_code": "PPLA",
                            "population": 1_371_498,
                            "timezone": "Europe/Rome",
                        }
                    ]
                },
            )
        assert url == "https://historical-forecast-api.open-meteo.com/v1/forecast"
        return _Response(
            "https://historical-forecast-api.open-meteo.com/v1/forecast?latitude=45.464270&longitude=9.189510",
            {
                "daily": {
                    "time": ["2024-07-01", "2024-07-02"],
                    "temperature_2m_min": [18.0, 19.0],
                    "temperature_2m_max": [28.0, 30.0],
                    "precipitation_sum": [1.2, 0.0],
                }
            },
        )


@pytest.mark.asyncio
async def test_weather_agent_resolves_a_city_then_uses_historical_forecast_coordinates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _WeatherClient.requests = []
    monkeypatch.setattr("business_signals.external.httpx.AsyncClient", lambda **_kwargs: _WeatherClient())

    finding = await ExternalResearcher().research(
        "weather", "Milan, Italy", "2024-07-01", "2024-07-02", "Why did sales fall?"
    )

    assert "single_city" in finding.observation
    assert finding.source_title.endswith("single_city, 1 point(s)")
    forecast_url, forecast_params = _WeatherClient.requests[1]
    assert forecast_url == "https://historical-forecast-api.open-meteo.com/v1/forecast"
    assert forecast_params["latitude"] == "45.464270"
    assert forecast_params["longitude"] == "9.189510"
    assert forecast_params["daily"] == "temperature_2m_min,temperature_2m_max,precipitation_sum"


@pytest.mark.asyncio
async def test_a_renamed_agent_runs_the_same_catalog_pipeline_without_python_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.loads(Path("config/research-agents.json").read_text())
    payload["agents"] = [payload["agents"][0]]
    payload["agents"][0]["id"] = "regional-climate"
    payload["agents"][0]["name"] = "Regional climate"
    catalog = ResearchAgentCatalog(ResearchAgentCatalogModel.model_validate(payload))
    _WeatherClient.requests = []
    monkeypatch.setattr("business_signals.external.httpx.AsyncClient", lambda **_kwargs: _WeatherClient())

    finding = await ExternalResearcher(catalog=catalog).research(
        "regional-climate", "Milan, Italy", "2024-07-01", "2024-07-02", "Why did sales fall?"
    )

    assert finding.type == "regional-climate"
    assert len(_WeatherClient.requests) == 2


class _CountryWeatherClient(_WeatherClient):
    async def request(self, _method: str, url: str, **kwargs: Any) -> _Response:
        params = kwargs["params"]
        self.requests.append((url, params))
        if "geocoding-api" in url:
            return _Response(
                "https://geocoding-api.open-meteo.com/v1/search?name=Exampleland",
                {
                    "results": [
                        {
                            "name": "Exampleland",
                            "country": "Exampleland",
                            "latitude": 0.5,
                            "longitude": 0.5,
                            "feature_code": "PCLI",
                        }
                    ]
                },
            )
        if "nominatim" in url:
            return _Response(
                "https://nominatim.openstreetmap.org/search?q=Exampleland",
                [{"geojson": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}}],
            )
        locations = params["latitude"].split(",")
        return _Response(
            "https://historical-forecast-api.open-meteo.com/v1/forecast",
            [
                {
                    "daily": {
                        "time": ["2024-07-01"],
                        "temperature_2m_min": [18],
                        "temperature_2m_max": [28],
                        "precipitation_sum": [1],
                    }
                }
                for _ in locations
            ],
        )


@pytest.mark.asyncio
async def test_weather_agent_batches_polygon_points_without_reducing_geographic_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _CountryWeatherClient.requests = []
    monkeypatch.setattr("business_signals.external.httpx.AsyncClient", lambda **_kwargs: _CountryWeatherClient())
    monkeypatch.setattr(settings, "target_cell_area_km2", 2_500)
    monkeypatch.setattr(settings, "max_points_weather_api", 2)

    finding = await ExternalResearcher().research(
        "weather", "Exampleland", "2024-07-01", "2024-07-01", "Was it rainy?"
    )

    forecast_requests = [request for request in _CountryWeatherClient.requests if "historical-forecast" in request[0]]
    assert len(forecast_requests) == 3  # Five polygon points, sent as 2 + 2 + 1.
    assert "polygon_grid" in finding.observation
    assert "5 point(s)" in finding.source_title


class _ComparisonWeatherClient(_WeatherClient):
    async def request(self, _method: str, url: str, **kwargs: Any) -> _Response:
        params = kwargs["params"]
        self.requests.append((url, params))
        if "geocoding-api" in url:
            return _Response(
                "https://geocoding-api.open-meteo.com/v1/search?name=Milan",
                {
                    "results": [
                        {
                            "name": "Milan",
                            "country": "Italy",
                            "latitude": 45.46427,
                            "longitude": 9.18951,
                            "feature_code": "PPLA",
                            "population": 1_371_498,
                            "timezone": "Europe/Rome",
                        }
                    ]
                },
            )
        return _Response(
            "https://historical-forecast-api.open-meteo.com/v1/forecast",
            {
                "daily": {
                    "time": ["2024-06-29", "2024-06-30", "2024-07-01", "2024-07-02"],
                    "temperature_2m_min": [12, 14, 18, 20],
                    "temperature_2m_max": [22, 24, 30, 32],
                    "precipitation_sum": [1, 3, 8, 2],
                }
            },
        )


@pytest.mark.asyncio
async def test_weather_agent_returns_generic_before_after_measurements_when_given_a_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ComparisonWeatherClient.requests = []
    monkeypatch.setattr("business_signals.external.httpx.AsyncClient", lambda **_kwargs: _ComparisonWeatherClient())

    finding = await ExternalResearcher().research(
        "weather",
        "Milan, Italy",
        "2024-06-29",
        "2024-07-02",
        "Did weather change?",
        "2024-07-01",
    )

    by_name = {measurement.name: measurement for measurement in finding.measurements}
    assert by_name["Average daily high temperature"].baseline_value == 23
    assert by_name["Average daily high temperature"].comparison_value == 31
    assert by_name["Average daily high temperature"].percentage_change == 34.78
    assert by_name["Total precipitation"].baseline_value == 4
    assert by_name["Total precipitation"].comparison_value == 10
    assert finding.coverage_method == "single_city"
    assert finding.point_count == 1
