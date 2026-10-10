from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from business_signals.config import settings
from business_signals.datasources.registry import DatasourceRegistry
from business_signals.investigation import InvestigationEngine
from business_signals.models import (
    CrossDatasourceRelation,
    CrossDatasourceRelationCreate,
    DatasourceCreate,
    DatasourceMetadata,
    DatasourceSummary,
    InvestigationCreate,
    InvestigationState,
)
from business_signals.research_agents import ResearchAgent, ResearchAgentCatalog
from business_signals.service import InvestigationService

logger = logging.getLogger("uvicorn.error")


async def _configure_checkpoint_connection(connection: AsyncConnection) -> None:
    """Apply the checkpoint schema to every connection leased from the pool."""
    await connection.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(settings.checkpoint_schema)))


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Attach the graph to the project PostgreSQL instance for restart-safe checkpoints.

    The database is also used by the local Docker fixtures.  A fixture reload
    restarts PostgreSQL, so a single long-lived connection would remain broken
    for the lifetime of the API process.  A pool discards that connection and
    opens a fresh one for subsequent graph operations.
    """
    async with await AsyncConnection.connect(settings.checkpoint_database_url, autocommit=True) as connection:
        await connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(settings.checkpoint_schema)))
    async with AsyncConnectionPool(
        conninfo=settings.checkpoint_database_url,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        configure=_configure_checkpoint_connection,
        min_size=settings.checkpoint_pool_min_size,
        max_size=settings.checkpoint_pool_max_size,
        open=False,
    ) as checkpoint_pool:
        await checkpoint_pool.open(wait=True)
        checkpointer = AsyncPostgresSaver(checkpoint_pool, serde=InvestigationEngine.checkpoint_serde())
        await checkpointer.setup()
        investigations.set_checkpointer(checkpointer)
        yield


app = FastAPI(
    title="Business Signals API",
    version="0.1.0",
    description="Evidence-backed, multi-datasource root-cause investigations.",
    lifespan=lifespan,
)
local_web_origins = sorted({settings.web_origin, "http://localhost:3000", "http://localhost:5173"})
app.add_middleware(
    CORSMiddleware,
    allow_origins=local_web_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)

registry = DatasourceRegistry()
investigations = InvestigationService(registry)
research_agents = ResearchAgentCatalog.load(settings.research_agent_catalog_path)


class HumanResponse(BaseModel):
    response: str = Field(min_length=1, max_length=2000)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/research-agents", response_model=list[ResearchAgent])
async def list_research_agents() -> list[ResearchAgent]:
    """List configured agents. Authentication secret values are never part of this response."""
    return research_agents.list()


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


@app.get("/api/datasources/{datasource_id}/metadata", response_model=DatasourceMetadata)
async def get_cached_metadata(datasource_id: str) -> DatasourceMetadata:
    try:
        metadata = registry.cached_metadata(datasource_id)
        if metadata is None:
            raise HTTPException(status_code=404, detail="No cached schema is available for this datasource")
        return metadata
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/datasources/{datasource_id}/refresh", response_model=DatasourceMetadata)
async def refresh_datasource(datasource_id: str) -> DatasourceMetadata:
    try:
        return await registry.refresh(datasource_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Schema refresh failed: {exc}") from exc


@app.delete("/api/datasources/{datasource_id}", status_code=204)
async def delete_datasource(datasource_id: str) -> None:
    try:
        await registry.delete(datasource_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/relationships", response_model=list[CrossDatasourceRelation])
async def list_cross_datasource_relationships() -> list[CrossDatasourceRelation]:
    return registry.approved_cross_relations([source.id for source in registry.list()])


@app.get("/api/relationships/suggestions", response_model=list[CrossDatasourceRelation])
async def suggest_cross_datasource_relationships() -> list[CrossDatasourceRelation]:
    try:
        return await registry.cross_relation_suggestions()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not review relationship suggestions: {exc}") from exc


@app.post("/api/relationships/validate", response_model=CrossDatasourceRelation)
async def validate_cross_datasource_relationship(
    payload: CrossDatasourceRelationCreate,
) -> CrossDatasourceRelation:
    try:
        return await registry.validate_cross_relation(payload)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/relationships", response_model=CrossDatasourceRelation, status_code=201)
async def save_cross_datasource_relationship(
    payload: CrossDatasourceRelationCreate,
) -> CrossDatasourceRelation:
    try:
        return await registry.save_cross_relation(payload)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/relationships/{relationship_id}", response_model=CrossDatasourceRelation)
async def update_cross_datasource_relationship(relationship_id: str, payload: CrossDatasourceRelationCreate) -> CrossDatasourceRelation:
    try:
        return await registry.update_cross_relation(relationship_id, payload)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400 if isinstance(exc, ValueError) else 404, detail=str(exc)) from exc


@app.delete("/api/relationships/{relationship_id}", status_code=204)
async def delete_cross_datasource_relationship(relationship_id: str) -> None:
    try:
        registry.delete_cross_relation(relationship_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post(
    "/api/investigations",
    response_model=InvestigationState,
    response_model_exclude={
        "resolved_entities",
        "query_scopes",
        "query_result_cache",
        "cross_datasource_lookup_cache",
    },
    status_code=202,
)
async def create_investigation(payload: InvestigationCreate) -> InvestigationState:
    try:
        return await investigations.create(payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Could not start investigation because a selected datasource is unavailable")
        raise HTTPException(
            status_code=503,
            detail="A selected datasource is unavailable. Check its connection and try again.",
        ) from exc


@app.get(
    "/api/investigations",
    response_model=list[InvestigationState],
    response_model_exclude={
        "resolved_entities",
        "query_scopes",
        "query_result_cache",
        "cross_datasource_lookup_cache",
    },
)
async def list_investigations() -> list[InvestigationState]:
    return investigations.list()


@app.delete("/api/investigations", status_code=204)
async def delete_all_investigations() -> None:
    try:
        await investigations.delete_all()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get(
    "/api/investigations/{investigation_id}",
    response_model=InvestigationState,
    response_model_exclude={
        "resolved_entities",
        "query_scopes",
        "query_result_cache",
        "cross_datasource_lookup_cache",
    },
)
async def get_investigation(investigation_id: str) -> InvestigationState:
    try:
        return investigations.get(investigation_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/api/investigations/{investigation_id}", status_code=204)
async def delete_investigation(investigation_id: str) -> None:
    try:
        await investigations.delete(investigation_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post(
    "/api/investigations/{investigation_id}/stop",
    response_model=InvestigationState,
    response_model_exclude={
        "resolved_entities",
        "query_scopes",
        "query_result_cache",
        "cross_datasource_lookup_cache",
    },
)
async def stop_investigation(investigation_id: str) -> InvestigationState:
    try:
        return await investigations.stop(investigation_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/investigations/{investigation_id}/events")
async def stream_events(
    investigation_id: str,
    after: int = Query(default=0, ge=0),
    live_only: bool = Query(default=False),
) -> EventSourceResponse:
    try:
        investigations.get(investigation_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    async def generate():
        offset = after
        async for event in investigations.events(investigation_id, after, live_only=live_only):
            offset += 1
            yield {
                # The browser uses this monotonic offset to resume a live stream without replaying
                # completion events from an earlier turn in the same conversation.
                "id": str(offset),
                "event": event.type,
                "data": json.dumps(event.model_dump(mode="json")),
            }

    return EventSourceResponse(generate(), ping=15)


@app.post(
    "/api/investigations/{investigation_id}/responses",
    response_model=InvestigationState,
    response_model_exclude={
        "resolved_entities",
        "query_scopes",
        "query_result_cache",
        "cross_datasource_lookup_cache",
    },
    status_code=202,
)
async def respond_to_investigation(investigation_id: str, payload: HumanResponse) -> InvestigationState:
    try:
        return await investigations.respond(investigation_id, payload.response)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post(
    "/api/investigations/{investigation_id}/follow-up",
    response_model=InvestigationState,
    response_model_exclude={
        "resolved_entities",
        "query_scopes",
        "query_result_cache",
        "cross_datasource_lookup_cache",
    },
    status_code=202,
)
async def follow_up_on_investigation(investigation_id: str, payload: HumanResponse) -> InvestigationState:
    try:
        return await investigations.follow_up(investigation_id, payload.response)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post(
    "/api/investigations/{investigation_id}/skip-clarification",
    response_model=InvestigationState,
    response_model_exclude={
        "resolved_entities",
        "query_scopes",
        "query_result_cache",
        "cross_datasource_lookup_cache",
    },
    status_code=202,
)
async def skip_investigation_clarification(investigation_id: str, payload: HumanResponse) -> InvestigationState:
    try:
        return await investigations.skip_clarification(investigation_id, payload.response)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
