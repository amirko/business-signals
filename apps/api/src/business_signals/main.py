from __future__ import annotations

import json

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from business_signals.config import settings
from business_signals.datasources.registry import DatasourceRegistry
from business_signals.models import (
    DatasourceCreate,
    DatasourceMetadata,
    DatasourceSummary,
    InvestigationCreate,
    InvestigationState,
)
from business_signals.service import InvestigationService

app = FastAPI(
    title="Business Signals API",
    version="0.1.0",
    description="Evidence-backed, multi-datasource root-cause investigations.",
)
local_web_origins = sorted({settings.web_origin, "http://localhost:3000", "http://localhost:5173"})
app.add_middleware(
    CORSMiddleware,
    allow_origins=local_web_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

registry = DatasourceRegistry()
investigations = InvestigationService(registry)


class HumanResponse(BaseModel):
    response: str = Field(min_length=1, max_length=2000)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/datasources/test", response_model=DatasourceSummary)
async def test_datasource(payload: DatasourceCreate) -> DatasourceSummary:
    try:
        return await registry.test(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Connection failed: {exc}") from exc


@app.post("/api/datasources", response_model=DatasourceSummary, status_code=201)
async def add_datasource(payload: DatasourceCreate) -> DatasourceSummary:
    try:
        return await registry.add(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not add datasource: {exc}") from exc


@app.get("/api/datasources", response_model=list[DatasourceSummary])
async def list_datasources() -> list[DatasourceSummary]:
    return registry.list()


@app.post("/api/datasources/{datasource_id}/refresh", response_model=DatasourceMetadata)
async def refresh_datasource(datasource_id: str) -> DatasourceMetadata:
    try:
        return await registry.refresh(datasource_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Schema refresh failed: {exc}") from exc


@app.post("/api/investigations", response_model=InvestigationState, status_code=202)
async def create_investigation(payload: InvestigationCreate) -> InvestigationState:
    try:
        return await investigations.create(payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/investigations/{investigation_id}", response_model=InvestigationState)
async def get_investigation(investigation_id: str) -> InvestigationState:
    try:
        return investigations.get(investigation_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/investigations/{investigation_id}/events")
async def stream_events(investigation_id: str, after: int = Query(default=0, ge=0)) -> EventSourceResponse:
    try:
        investigations.get(investigation_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    async def generate():
        async for event in investigations.events(investigation_id, after):
            yield {
                "id": event.id,
                "event": event.type,
                "data": json.dumps(event.model_dump(mode="json")),
            }

    return EventSourceResponse(generate(), ping=15)


@app.post("/api/investigations/{investigation_id}/responses", response_model=InvestigationState, status_code=202)
async def respond_to_investigation(investigation_id: str, payload: HumanResponse) -> InvestigationState:
    try:
        return await investigations.respond(investigation_id, payload.response)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
