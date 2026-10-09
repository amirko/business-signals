"""Generic executor for declarative research agents."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, timedelta
from time import perf_counter
from typing import Any

import httpx

from business_signals.config import settings
from business_signals.models import ExternalFinding, ExternalMeasurement, ExternalResearchCandidate
from business_signals.research_agents import (
    ApiKeyAuthentication,
    BatchCoordinatesStep,
    ComputedValue,
    DailyComparisonMetric,
    HttpStep,
    PipelineHttpStep,
    PipelineRunner,
    SampleBoundaryStep,
    SelectGeographicLocationsStep,
    SplitStringsStep,
    SummarizeDailySeriesStep,
    ResearchAgent,
    ResearchAgentCatalog,
    ResearchAgentUnavailable,
    json_values,
    render_template,
)
from business_signals.geography import GeographicScope, GeoPoint, sample_boundary, select_geocoding_candidate

logger = logging.getLogger("uvicorn.error")


class ExternalResearcher:
    """Execute catalog-defined public and authenticated HTTPS/JSON research workflows."""

    def __init__(self, timeout: float = 12.0, catalog: ResearchAgentCatalog | None = None) -> None:
        self.timeout = timeout
        self.catalog = catalog or ResearchAgentCatalog.load(settings.research_agent_catalog_path)

    def available_agents(self) -> list[dict[str, Any]]:
        """Return safe planning metadata for enabled agents whose credentials are configured."""
        available = []
        for agent in self.catalog.list():
            try:
                self.catalog.get(agent.id)
            except ResearchAgentUnavailable:
                continue
            available.append(
                {
                    "id": agent.id,
                    "name": agent.name,
                    "purpose": agent.purpose,
                    "subjects": agent.subjects,
                    "evidence_topics": agent.evidence_topics,
                    "subject_label": agent.runner.input.subject_label,
                    "subject_pattern": agent.runner.input.subject_pattern,
                    "geographic_scope": (
                        {
                            "supported_kinds": ["city", "multiple_locations", "region", "country"],
                            "multiple_location_delimiter": ";",
                            "city_requirement": "Include country, and region when known, for an ambiguous city name.",
                        }
                        if isinstance(agent.runner, PipelineRunner)
                        and "geographic_scope" in agent.runner.capabilities
                        else None
                    ),
                }
            )
        return available

    @staticmethod
    def _safe_response_preview(payload: Any, limit: int = 4_000) -> str:
        """Keep provider responses inspectable without logging secrets or unbounded content."""
        sensitive_names = {"api_key", "apikey", "authorization", "token", "password", "secret"}

        def redact(value: Any) -> Any:
            if isinstance(value, dict):
                return {
                    key: "[redacted]" if key.casefold() in sensitive_names else redact(item)
                    for key, item in value.items()
                }
            if isinstance(value, list):
                return [redact(item) for item in value]
            return value

        encoded = json.dumps(redact(payload), ensure_ascii=False, default=str)
        return encoded if len(encoded) <= limit else f"{encoded[:limit]}… [truncated]"

    def validate_request(
        self, category: str, subject: str, start_date: str, end_date: str, comparison_start_date: str | None = None
    ) -> tuple[ResearchAgent, str]:
        """Validate a planned agent call before it is allowed to reach a provider."""
        agent = self.catalog.get(category)
        start, end = self._validated_period(start_date, end_date)
        if comparison_start_date:
            try:
                boundary = date.fromisoformat(comparison_start_date)
            except ValueError as exc:
                raise ValueError("External research comparison date must use YYYY-MM-DD") from exc
            if not start < boundary <= end:
                raise ValueError("External research comparison date must be within the requested period")
        validated_subject = self._validated_subject(
            subject, agent.runner.input.subject_label, agent.runner.input.subject_pattern
        )
        self._input_values(agent, validated_subject, start_date, end_date, "")
        return agent, validated_subject

    @staticmethod
    def _validated_period(start_date: str, end_date: str) -> tuple[date, date]:
        try:
            start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        except ValueError as exc:
            raise ValueError("External research dates must use YYYY-MM-DD") from exc
        if end < start:
            raise ValueError("External research end date must not be before its start date")
        if (end - start).days > 3660:
            raise ValueError("External research periods are limited to ten years")
        return start, end

    @staticmethod
    def _validated_subject(
        subject: str, label: str, pattern: str = r"^[^\r\n\x00]{1,160}$"
    ) -> str:
        value = subject.strip()
        if not value or len(value) > 160 or any(character in value for character in "\r\n\x00"):
            raise ValueError(f"External research {label} must be between 1 and 160 ordinary characters")
        if not re.fullmatch(pattern, value):
            raise ValueError(f"External research {label} does not match this agent's required format")
        return value

    @staticmethod
    def _input_values(
        agent: ResearchAgent,
        subject: str,
        start_date: str,
        end_date: str,
        context: str,
        comparison_start_date: str | None = None,
    ) -> dict[str, Any]:
        values: dict[str, Any] = {
            "agent_name": agent.name,
            "subject": subject,
            "start_date": start_date,
            "end_date": end_date,
            "context": context,
            "comparison_start_date": comparison_start_date or "",
        }
        for transform in agent.runner.input_transforms:
            source = str(values.get(transform.source, ""))
            parts = source.split(transform.separator)
            if transform.index >= len(parts) or not parts[transform.index]:
                raise ValueError(f"External research input cannot derive {transform.name}")
            transformed = parts[transform.index]
            values[transform.name] = transformed.upper() if transform.uppercase else transformed
        return values

    @staticmethod
    def _computed_value(spec: ComputedValue, payload: Any, values: dict[str, Any]) -> int | float:
        if spec.operation == "percent_change":
            before, after = float(values[spec.from_field or ""]), float(values[spec.to_field or ""])
            if before == 0:
                raise ValueError("Cannot calculate percentage change from zero")
            result: int | float = (after - before) / before * 100
        else:
            assert spec.path is not None
            selected = json_values(payload, render_template(spec.path, values))
            if not selected:
                raise ValueError(f"Research-agent response did not contain {spec.path}")
            if spec.operation == "first":
                result = selected[0]
            elif spec.operation == "last":
                result = selected[-1]
            else:
                numeric = [float(value) for value in selected if isinstance(value, int | float) and not isinstance(value, bool)]
                if not numeric:
                    raise ValueError(f"Research-agent response contained no numeric values at {spec.path}")
                result = {"min": min, "max": max, "sum": sum}[spec.operation](numeric)
        return round(result, spec.precision) if spec.precision is not None else result

    @staticmethod
    def _safe_source_url(url: str, authentication: ApiKeyAuthentication | object) -> str:
        """Never preserve a query-string credential in an evidence citation or archive."""
        parsed = httpx.URL(url)
        if isinstance(authentication, ApiKeyAuthentication) and authentication.location == "query":
            parsed = parsed.copy_remove_param(authentication.name)
        return str(parsed)

    @staticmethod
    def _first_text_at_path(item: dict[str, Any], path: str | None) -> str | None:
        if not path:
            return None
        values = json_values(item, path)
        return str(values[0]) if values and values[0] is not None else None

    @classmethod
    def _research_candidates(cls, rows: list[dict[str, Any]], runner: Any) -> list[ExternalResearchCandidate]:
        selection = runner.response.relevance_selection
        if selection is None:
            return []
        candidates: list[ExternalResearchCandidate] = []
        for item in rows[:selection.max_candidates]:
            title = cls._first_text_at_path(item, selection.title_path)
            url = cls._first_text_at_path(item, selection.url_path)
            if not title or not url:
                continue
            candidates.append(
                ExternalResearchCandidate(
                    title=title,
                    url=url,
                    summary=cls._first_text_at_path(item, selection.summary_path),
                    section=cls._first_text_at_path(item, selection.section_path),
                    published_at=cls._first_text_at_path(item, selection.published_at_path),
                )
            )
        return candidates

    async def research(
        self,
        category: str,
        subject: str,
        start_date: str,
        end_date: str,
        context: str,
        comparison_start_date: str | None = None,
    ) -> ExternalFinding:
        agent, subject = self.validate_request(category, subject, start_date, end_date, comparison_start_date)
        runner = agent.runner
        if isinstance(runner, PipelineRunner):
            return await self._run_pipeline(agent, subject, start_date, end_date, context, comparison_start_date)
        values = self._input_values(agent, subject, start_date, end_date, context, comparison_start_date)
        last_payload: Any = None
        last_url = ""

        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
            for step in runner.steps:
                headers = {key: render_template(value, values) for key, value in step.headers.items()}
                params = {key: render_template(value, values) for key, value in step.query.items()}
                if isinstance(runner.authentication, ApiKeyAuthentication):
                    destination = headers if runner.authentication.location == "header" else params
                    destination[runner.authentication.name] = f"{runner.authentication.prefix}{os.environ[runner.authentication.environment]}"
                body = (
                    {key: render_template(value, values) for key, value in step.json_body.items()} if step.json_body else None
                )
                request_url = str(httpx.URL(render_template(step.url, values), params=params))
                safe_request_url = self._safe_source_url(request_url, runner.authentication)
                logger.info(
                    "External agent HTTP request: agent=%s step=%s method=%s url=%s",
                    agent.id,
                    step.name,
                    step.method,
                    safe_request_url,
                )
                started_at = perf_counter()
                try:
                    response = await client.request(
                        step.method,
                        render_template(step.url, values),
                        params=params,
                        headers=headers,
                        json=body,
                    )
                    response.raise_for_status()
                except Exception as exc:
                    failed_response = getattr(exc, "response", None)
                    status_code = getattr(failed_response, "status_code", "unavailable")
                    logger.warning(
                        "External agent HTTP request failed: agent=%s step=%s method=%s url=%s status=%s duration_ms=%.1f error=%s",
                        agent.id,
                        step.name,
                        step.method,
                        safe_request_url,
                        status_code,
                        (perf_counter() - started_at) * 1000,
                        exc,
                    )
                    raise
                content_length = getattr(response, "headers", {}).get("content-length", "unknown")
                logger.info(
                    "External agent HTTP response: agent=%s step=%s status=%s duration_ms=%.1f content_length=%s",
                    agent.id,
                    step.name,
                    response.status_code,
                    (perf_counter() - started_at) * 1000,
                    content_length,
                )
                last_payload = response.json()
                logger.info(
                    "External agent JSON response: agent=%s step=%s payload=%s",
                    agent.id,
                    step.name,
                    self._safe_response_preview(last_payload),
                )
                last_url = self._safe_source_url(str(response.url), runner.authentication)
                for name, path in step.extract.items():
                    selected = json_values(last_payload, render_template(path, values))
                    if not selected:
                        raise ValueError(f"Research-agent step {step.name} did not return {path}")
                    values[name] = selected[0]

        assert last_payload is not None
        for computed in runner.response.computed:
            values[computed.name] = self._computed_value(computed, last_payload, values)
        rows = [
            item
            for value in json_values(last_payload, runner.response.items_path)
            for item in (value if isinstance(value, list) else [value])
            if isinstance(item, dict)
        ]
        if rows:
            item = rows[0] if runner.response.item == "first" else rows[-1]
            values.update({key: value for key, value in item.items() if value is not None})
            observation = render_template(runner.response.observation_template, values)
            source_url = render_template(runner.response.source_url_template, values) if runner.response.source_url_template else last_url
            source_title = render_template(runner.response.source_title_template, values)
        else:
            observation = render_template(
                runner.response.no_result_observation_template or "No matching records were returned by {agent_name}", values
            )
            source_url, source_title = last_url, agent.name
        candidates = self._research_candidates(rows, runner)
        finding = ExternalFinding(
            type=agent.id,
            period=f"{start_date} to {end_date}",
            observation=observation,
            relationship=runner.response.relationship,
            confidence=runner.response.confidence,
            source_url=source_url,
            source_title=source_title,
            candidates=candidates,
        )
        logger.info(
            "External agent finding: agent=%s period=%s observation=%r source_url=%s",
            agent.id,
            finding.period,
            finding.observation,
            finding.source_url,
        )
        return finding

    async def _request_json(
        self,
        client: httpx.AsyncClient,
        agent: ResearchAgent,
        step: HttpStep,
        values: dict[str, Any],
    ) -> tuple[Any, str]:
        """Run one configured step with the same safety and diagnostics as http_json agents."""
        runner = agent.runner
        headers = {key: render_template(value, values) for key, value in step.headers.items()}
        params = {key: render_template(value, values) for key, value in step.query.items()}
        if isinstance(runner.authentication, ApiKeyAuthentication):
            destination = headers if runner.authentication.location == "header" else params
            destination[runner.authentication.name] = f"{runner.authentication.prefix}{os.environ[runner.authentication.environment]}"
        body = {key: render_template(value, values) for key, value in step.json_body.items()} if step.json_body else None
        request_url = str(httpx.URL(render_template(step.url, values), params=params))
        safe_request_url = self._safe_source_url(request_url, runner.authentication)
        logger.info("External agent HTTP request: agent=%s step=%s method=%s url=%s", agent.id, step.name, step.method, safe_request_url)
        started_at = perf_counter()
        try:
            response = await client.request(step.method, render_template(step.url, values), params=params, headers=headers, json=body)
            response.raise_for_status()
        except Exception as exc:
            failed_response = getattr(exc, "response", None)
            status_code = getattr(failed_response, "status_code", "unavailable")
            logger.warning(
                "External agent HTTP request failed: agent=%s step=%s method=%s url=%s status=%s duration_ms=%.1f error=%s",
                agent.id, step.name, step.method, safe_request_url, status_code, (perf_counter() - started_at) * 1000, exc,
            )
            raise
        logger.info(
            "External agent HTTP response: agent=%s step=%s status=%s duration_ms=%.1f content_length=%s",
            agent.id, step.name, response.status_code, (perf_counter() - started_at) * 1000,
            response.headers.get("content-length", "unknown"),
        )
        payload = response.json()
        logger.info("External agent JSON response: agent=%s step=%s payload=%s", agent.id, step.name, self._safe_response_preview(payload))
        return payload, self._safe_source_url(str(response.url), runner.authentication)

    @staticmethod
    def _administrative_scope(candidates: list[dict[str, Any]]) -> bool:
        """Open-Meteo/GeoNames marks country and admin areas with these feature codes."""
        feature_code = str(candidates[0].get("feature_code", "")).upper() if candidates else ""
        return feature_code.startswith(("PCL", "ADM"))

    @staticmethod
    def _daily_records(payload: Any, daily_path: str) -> list[dict[str, Any]]:
        """Accept one Open-Meteo result or its documented multi-location list shape."""
        payloads = payload if isinstance(payload, list) else [payload]
        return [
            selected[0]
            for item in payloads
            if (selected := json_values(item, daily_path)) and isinstance(selected[0], dict)
        ]

    @staticmethod
    def _summarize_daily_series(
        scope: GeographicScope,
        daily_records: list[dict[str, Any]],
        minimum_field: str,
        maximum_field: str,
        precipitation_field: str,
        comparison_metrics: list[DailyComparisonMetric],
        comparison_start_date: str | None,
        start_date: str,
        end_date: str,
    ) -> dict[str, Any]:
        maximums: list[float] = []
        minimums: list[float] = []
        precipitation_totals: list[float] = []
        for daily in daily_records:
            maximums.extend(float(value) for value in daily.get(maximum_field, []) if value is not None)
            minimums.extend(float(value) for value in daily.get(minimum_field, []) if value is not None)
            precipitation = [float(value) for value in daily.get(precipitation_field, []) if value is not None]
            if precipitation:
                precipitation_totals.append(sum(precipitation))
        if not maximums or not minimums:
            raise ValueError("Pipeline response returned no daily minimum or maximum records")
        result: dict[str, Any] = {
            "point_count": len(scope.points),
            "coverage_method": scope.coverage_method,
            "minimum_value": round(min(minimums), 1),
            "maximum_value": round(max(maximums), 1),
            "average_precipitation": round(
                sum(precipitation_totals) / len(precipitation_totals), 1
            ) if precipitation_totals else 0.0,
        }
        if not comparison_start_date or not comparison_metrics:
            return result
        boundary = date.fromisoformat(comparison_start_date)
        before_values: dict[str, list[float]] = {metric.field: [] for metric in comparison_metrics}
        after_values: dict[str, list[float]] = {metric.field: [] for metric in comparison_metrics}
        for daily in daily_records:
            days = daily.get("time", [])
            if not isinstance(days, list):
                continue
            for metric in comparison_metrics:
                values = daily.get(metric.field, [])
                if not isinstance(values, list):
                    continue
                for day, value in zip(days, values, strict=False):
                    if value is None:
                        continue
                    try:
                        bucket = before_values if date.fromisoformat(str(day)) < boundary else after_values
                        bucket[metric.field].append(float(value))
                    except (TypeError, ValueError):
                        continue
        measurements: list[ExternalMeasurement] = []
        baseline_period = f"{start_date} to {(boundary - timedelta(days=1)).isoformat()}"
        comparison_period = f"{comparison_start_date} to {end_date}"
        for metric in comparison_metrics:
            before, after = before_values[metric.field], after_values[metric.field]
            if not before or not after:
                continue
            baseline = sum(before) / len(before) if metric.aggregation == "mean" else sum(before)
            comparison = sum(after) / len(after) if metric.aggregation == "mean" else sum(after)
            measurements.append(
                ExternalMeasurement(
                    name=metric.name,
                    unit=metric.unit,
                    aggregation=metric.aggregation,
                    baseline_period=baseline_period,
                    comparison_period=comparison_period,
                    baseline_value=round(baseline, 2),
                    comparison_value=round(comparison, 2),
                    percentage_change=(round((comparison - baseline) / abs(baseline) * 100, 2) if baseline else None),
                )
            )
        result["external_measurements"] = measurements
        return result

    @staticmethod
    def _bound_values(
        context: dict[str, Any], item: Any, bindings: dict[str, str]
    ) -> dict[str, Any]:
        values = dict(context)
        for name, path in bindings.items():
            if path == "":
                values[name] = item
                continue
            selected = json_values(item, path)
            if not selected:
                raise ValueError(f"Pipeline item did not contain binding path {path}")
            values[name] = selected[0]
        return values

    @staticmethod
    def _select_geographic_scope(
        locations: list[str], responses: list[Any], candidates_path: str
    ) -> tuple[GeographicScope, bool, str | None]:
        if len(locations) != len(responses):
            raise ValueError("Geographic selection needs exactly one geocoder response per location")
        points: list[GeoPoint] = []
        administrative = False
        administrative_label: str | None = None
        administrative_kind = "region"
        for location, payload in zip(locations, responses, strict=True):
            candidates = [
                item
                for value in json_values(payload, candidates_path)
                for item in (value if isinstance(value, list) else [value])
                if isinstance(item, dict)
            ]
            points.append(select_geocoding_candidate(location, candidates))
            if len(locations) == 1 and ExternalResearcher._administrative_scope(candidates):
                administrative = True
                administrative_label = location
                feature_code = str(candidates[0].get("feature_code", "")).upper()
                administrative_kind = "country" if feature_code.startswith("PCL") else "region"
        return (
            GeographicScope(
                kind=administrative_kind if administrative else "city" if len(points) == 1 else "multiple_locations",
                label="; ".join(locations),
                points=tuple(points),
                coverage_method="administrative_boundary" if administrative else "single_city" if len(points) == 1 else "multiple_named_locations",
            ),
            administrative,
            administrative_label,
        )

    async def _run_pipeline(
        self,
        agent: ResearchAgent,
        subject: str,
        start_date: str,
        end_date: str,
        context: str,
        comparison_start_date: str | None = None,
    ) -> ExternalFinding:
        """Interpret a catalog-defined sequence of allow-listed integration primitives."""
        runner = agent.runner
        assert isinstance(runner, PipelineRunner)
        values = self._input_values(agent, subject, start_date, end_date, context, comparison_start_date)
        last_url = ""
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
            for step in runner.steps:
                if isinstance(step, SplitStringsStep):
                    values[step.target] = [part.strip() for part in str(values[step.source]).split(step.separator) if part.strip()]
                    if not values[step.target]:
                        raise ValueError(f"Pipeline step {step.id} produced no values")
                elif isinstance(step, PipelineHttpStep):
                    if step.when == "administrative_scope" and not values.get("administrative_scope"):
                        values[step.target] = None
                        continue
                    items = values[step.for_each] if step.for_each else [None]
                    if not isinstance(items, list):
                        raise ValueError(f"Pipeline step {step.id} for_each must reference a list")
                    payloads = []
                    for item in items:
                        payload, last_url = await self._request_json(
                            client, agent, step.request, self._bound_values(values, item, step.bindings)
                        )
                        payloads.append(payload)
                    values[step.target] = payloads if step.for_each else payloads[0]
                elif isinstance(step, SelectGeographicLocationsStep):
                    locations = values.get(step.locations)
                    responses = values.get(step.responses)
                    if not isinstance(locations, list) or not isinstance(responses, list):
                        raise ValueError(f"Pipeline step {step.id} needs location and response lists")
                    scope, administrative, label = self._select_geographic_scope(locations, responses, step.candidates_path)
                    values[step.target] = scope
                    values["administrative_scope"] = administrative
                    values["administrative_label"] = label or ""
                elif isinstance(step, SampleBoundaryStep):
                    if not values.get("administrative_scope"):
                        continue
                    payload = values.get(step.source)
                    geometry_values = json_values(payload, step.geometry_path)
                    if not geometry_values or not isinstance(geometry_values[0], dict):
                        raise ValueError(f"Pipeline step {step.id} received no usable boundary geometry")
                    prior_scope = values.get(step.scope)
                    if not isinstance(prior_scope, GeographicScope):
                        raise ValueError(f"Pipeline step {step.id} needs a geographic scope")
                    sampled = sample_boundary(geometry_values[0], prior_scope.label, settings.target_cell_area_km2)
                    values[step.scope] = GeographicScope(
                        kind=prior_scope.kind,
                        label=prior_scope.label,
                        points=sampled.points,
                        coverage_method=sampled.coverage_method,
                        area_km2=sampled.area_km2,
                    )
                elif isinstance(step, BatchCoordinatesStep):
                    scope = values.get(step.source)
                    if not isinstance(scope, GeographicScope):
                        raise ValueError(f"Pipeline step {step.id} needs a geographic scope")
                    maximum = getattr(settings, step.max_items_setting, None)
                    if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < 1:
                        raise ValueError(
                            f"Pipeline step {step.id} references an invalid batch-limit setting "
                            f"{step.max_items_setting!r}"
                        )
                    values[step.target] = [
                        {
                            "latitudes": ",".join(f"{point.latitude:.6f}" for point in scope.points[offset : offset + maximum]),
                            "longitudes": ",".join(f"{point.longitude:.6f}" for point in scope.points[offset : offset + maximum]),
                        }
                        for offset in range(0, len(scope.points), maximum)
                    ]
                elif isinstance(step, SummarizeDailySeriesStep):
                    scope = values.get("geographic_scope")
                    payloads = values.get(step.source)
                    if not isinstance(scope, GeographicScope) or not isinstance(payloads, list):
                        raise ValueError(f"Pipeline step {step.id} needs geographic scope and response list")
                    daily_records = [record for payload in payloads for record in self._daily_records(payload, step.daily_path)]
                    values.update(self._summarize_daily_series(
                        scope,
                        daily_records,
                        step.minimum_field,
                        step.maximum_field,
                        step.precipitation_field,
                        step.comparison_metrics,
                        str(values.get(step.comparison_start_value, "")) or None if step.comparison_start_value else None,
                        start_date,
                        end_date,
                    ))
                else:  # pragma: no cover - Pydantic's discriminated union makes this unreachable.
                    raise ValueError(f"Unsupported pipeline step: {step.type}")
        scope = values.get("geographic_scope")
        if isinstance(scope, GeographicScope):
            logger.info(
                "Geographic pipeline scope resolved: agent=%s kind=%s label=%r points=%d coverage=%s area_km2=%s",
                agent.id, scope.kind, scope.label, len(scope.points), scope.coverage_method, scope.area_km2,
            )
        observation = render_template(runner.output.observation_template, values)
        finding = ExternalFinding(
            type=agent.id,
            location=subject,
            period=f"{start_date} to {end_date}",
            observation=observation,
            relationship=runner.output.relationship,
            confidence=runner.output.confidence,
            source_url=last_url or agent.source_url or "",
            source_title=render_template(runner.output.source_title_template, values),
            measurements=values.get("external_measurements", []),
            coverage_method=scope.coverage_method if isinstance(scope, GeographicScope) else None,
            point_count=len(scope.points) if isinstance(scope, GeographicScope) else None,
        )
        logger.info(
            "Pipeline agent finding: agent=%s observation=%r source_url=%s",
            agent.id, finding.observation, finding.source_url,
        )
        return finding
