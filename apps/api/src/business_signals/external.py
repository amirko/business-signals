from __future__ import annotations

from datetime import date
from typing import Literal

import httpx

from business_signals.models import ExternalFinding


class ExternalResearcher:
    """Structured clients for real public data; invoked only when the graph routes here."""

    def __init__(self, timeout: float = 12.0) -> None:
        self.timeout = timeout

    async def weather(self, location: str, start_date: str, end_date: str, context: str) -> ExternalFinding:
        del context
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            geo = await client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": location, "count": 1, "language": "en", "format": "json"},
            )
            geo.raise_for_status()
            result = geo.json().get("results", [])[0]
            params = {
                "latitude": result["latitude"],
                "longitude": result["longitude"],
                "start_date": start_date,
                "end_date": end_date,
                "daily": "temperature_2m_max,precipitation_sum,wind_speed_10m_max",
                "timezone": "auto",
            }
            weather = await client.get("https://archive-api.open-meteo.com/v1/archive", params=params)
            weather.raise_for_status()
            daily = weather.json()["daily"]
        max_temp = max(daily["temperature_2m_max"])
        rain = sum(value or 0 for value in daily["precipitation_sum"])
        wind = max(daily["wind_speed_10m_max"])
        return ExternalFinding(
            type="weather",
            location=location,
            period=f"{start_date} to {end_date}",
            observation=f"Historical conditions reached {max_temp:.1f}°C, {rain:.1f} mm total precipitation, and {wind:.1f} km/h peak daily wind.",
            relationship="correlated",
            confidence=0.78,
            source_url=str(weather.url),
            source_title="Open-Meteo Historical Weather API",
        )

    async def economy_fx(self, currency_pair: str, start_date: str, end_date: str, context: str) -> ExternalFinding:
        del context
        base, quote = currency_pair.upper().split("/")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                f"https://api.frankfurter.app/{start_date}..{end_date}",
                params={"from": base, "to": quote},
            )
            response.raise_for_status()
            rates = [day[quote] for day in response.json()["rates"].values()]
        change = (rates[-1] - rates[0]) / rates[0] * 100 if rates else 0
        return ExternalFinding(
            type="economy_fx",
            period=f"{start_date} to {end_date}",
            observation=f"{base}/{quote} moved {change:+.2f}% across the requested period.",
            relationship="correlated",
            confidence=0.82,
            source_url=str(response.url),
            source_title="Frankfurter exchange-rate API (ECB reference rates)",
        )

    async def news_event(self, query: str, start_date: str, end_date: str, context: str) -> ExternalFinding:
        del context
        start = date.fromisoformat(start_date).strftime("%Y%m%d000000")
        end = date.fromisoformat(end_date).strftime("%Y%m%d235959")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                "https://api.gdeltproject.org/api/v2/doc/doc",
                params={"query": query, "mode": "artlist", "format": "json", "startdatetime": start, "enddatetime": end, "maxrecords": 10},
            )
            response.raise_for_status()
            articles = response.json().get("articles", [])
        if not articles:
            observation, url, title, confidence = "No matching major event was found.", str(response.url), "GDELT", 0.45
        else:
            top = articles[0]
            observation, url, title, confidence = top.get("title", "Relevant event coverage found"), top.get("url", str(response.url)), top.get("domain", "GDELT source"), 0.68
        return ExternalFinding(
            type="news_event",
            period=f"{start_date} to {end_date}",
            observation=observation,
            relationship="correlated",
            confidence=confidence,
            source_url=url,
            source_title=title,
        )

    async def research(
        self,
        category: Literal["weather", "economy_fx", "news_event"],
        subject: str,
        start_date: str,
        end_date: str,
        context: str,
    ) -> ExternalFinding:
        if category == "weather":
            return await self.weather(subject, start_date, end_date, context)
        if category == "economy_fx":
            return await self.economy_fx(subject, start_date, end_date, context)
        return await self.news_event(subject, start_date, end_date, context)
