from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from langgraph.types import Command

from business_signals.datasources.registry import DatasourceRegistry
from business_signals.engine import InvestigationEngine
from business_signals.llm import LLM
from business_signals.models import (
    HumanFeedback,
    InvestigationCreate,
    InvestigationEvent,
    InvestigationState,
    InvestigationStatus,
)


class InvestigationService:
    def __init__(self, registry: DatasourceRegistry, llm: LLM | None = None) -> None:
        self.registry = registry
        self._records: dict[str, InvestigationState] = {}
        self._events: dict[str, list[InvestigationEvent]] = defaultdict(list)
        self._subscribers: dict[str, set[asyncio.Queue[InvestigationEvent]]] = defaultdict(set)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self.engine = InvestigationEngine(registry, llm=llm, event_sink=self._publish)

    async def _publish(self, event: InvestigationEvent) -> None:
        self._events[event.investigation_id].append(event)
        for queue in self._subscribers[event.investigation_id]:
            queue.put_nowait(event)

    async def create(self, request: InvestigationCreate) -> InvestigationState:
        summaries = []
        for source_id in request.datasource_ids:
            source = self.registry.get(source_id)
            await self.registry.metadata(source_id)
            summaries.append(source.summary)
        investigation_id = f"inv_{uuid4().hex[:12]}"
        state = InvestigationState(
            investigation_id=investigation_id,
            question=request.question,
            datasources=summaries,
            limits=request.limits,
        )
        self._records[investigation_id] = state
        self._tasks[investigation_id] = asyncio.create_task(self._run(investigation_id, state))
        return state

    async def _run(self, investigation_id: str, input_value: InvestigationState | Command[Any]) -> None:
        config = {"configurable": {"thread_id": investigation_id}}
        try:
            async for chunk in self.engine.graph.astream(input_value, config=config, stream_mode="values"):
                state = chunk if isinstance(chunk, InvestigationState) else InvestigationState.model_validate(chunk)
                self._records[investigation_id] = state
        except Exception as exc:
            current = self._records[investigation_id]
            failed = current.model_copy(update={"status": InvestigationStatus.FAILED, "error": str(exc)})
            self._records[investigation_id] = failed
            await self._publish(
                InvestigationEvent(
                    investigation_id=investigation_id,
                    type="InvestigationFailed",
                    message="The investigation stopped because an execution error occurred.",
                    data={"error": str(exc)},
                )
            )

    def get(self, investigation_id: str) -> InvestigationState:
        try:
            return self._records[investigation_id]
        except KeyError as exc:
            raise KeyError(f"Investigation {investigation_id!r} was not found") from exc

    async def respond(self, investigation_id: str, response: str) -> InvestigationState:
        state = self.get(investigation_id)
        if state.status != InvestigationStatus.WAITING_FOR_HUMAN:
            raise ValueError("This investigation is not waiting for human input")
        running = self._tasks.get(investigation_id)
        if running and not running.done():
            raise ValueError("The investigation is still processing its previous step")
        self._tasks[investigation_id] = asyncio.create_task(
            self._run(investigation_id, Command(resume=response))
        )
        resumed = state.model_copy(
            update={
                "status": InvestigationStatus.RUNNING,
                "human_feedback": [
                    *state.human_feedback,
                    HumanFeedback(question=state.pending_human_question or "Clarification", response=response),
                ],
            }
        )
        self._records[investigation_id] = resumed
        return resumed

    async def events(self, investigation_id: str, after: int = 0) -> AsyncIterator[InvestigationEvent]:
        self.get(investigation_id)
        for event in self._events[investigation_id][after:]:
            yield event
        queue: asyncio.Queue[InvestigationEvent] = asyncio.Queue()
        self._subscribers[investigation_id].add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers[investigation_id].discard(queue)
