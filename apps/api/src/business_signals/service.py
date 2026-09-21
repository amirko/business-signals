from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from collections import defaultdict
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

from langgraph.types import Command
from psycopg import AsyncConnection, sql

from business_signals.config import settings
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

logger = logging.getLogger(__name__)


class InvestigationService:
    def __init__(
        self, registry: DatasourceRegistry, llm: LLM | None = None, archive_dir: Path | None = None
    ) -> None:
        self.registry = registry
        self._records: dict[str, InvestigationState] = {}
        self._events: dict[str, list[InvestigationEvent]] = defaultdict(list)
        self._subscribers: dict[str, set[asyncio.Queue[InvestigationEvent]]] = defaultdict(set)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._archive_dir = archive_dir or settings.investigation_store_dir
        self._persistent_checkpoints_enabled = False
        self.engine = InvestigationEngine(registry, llm=llm, event_sink=self._publish)
        self._restore_archives()

    def set_checkpointer(self, checkpointer: Any) -> None:
        self.engine.set_checkpointer(checkpointer)
        self._persistent_checkpoints_enabled = True

    def _archive_path(self, investigation_id: str) -> Path:
        if not investigation_id.startswith("inv_") or not investigation_id.replace("_", "").isalnum():
            raise ValueError("Invalid investigation ID")
        return self._archive_dir / f"{investigation_id}.json"

    def _restore_archives(self) -> None:
        if not self._archive_dir.exists():
            return
        for path in self._archive_dir.glob("inv_*.json"):
            try:
                state = InvestigationState.model_validate_json(path.read_text(encoding="utf-8"))
                self._records[state.investigation_id] = state
            except (OSError, ValueError) as exc:
                logger.warning("Could not restore investigation archive %s: %s", path, exc)

    def _save_archive(self, state: InvestigationState) -> None:
        path = self._archive_path(state.investigation_id)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_path = tempfile.mkstemp(prefix="investigation-", suffix=".json", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(state.model_dump(mode="json"), handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, path)
        finally:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)

    def _delete_archive(self, investigation_id: str) -> None:
        self._archive_path(investigation_id).unlink(missing_ok=True)

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
            original_question=request.question,
            current_conversation_question=request.question,
            datasources=summaries,
            limits=request.limits,
        )
        self._records[investigation_id] = state
        self._save_archive(state)
        self._tasks[investigation_id] = asyncio.create_task(self._run(investigation_id, state))
        return state

    async def _run(
        self, investigation_id: str, input_value: InvestigationState | Command[Any], thread_id: str | None = None
    ) -> None:
        config = {"configurable": {"thread_id": thread_id or investigation_id}}
        try:
            async for chunk in self.engine.graph.astream(input_value, config=config, stream_mode="values"):
                state = chunk if isinstance(chunk, InvestigationState) else InvestigationState.model_validate(chunk)
                self._records[investigation_id] = state
                self._save_archive(state)
            snapshot = await self.engine.graph.aget_state(config)
            checkpointed = InvestigationState.model_validate(snapshot.values)
            self._records[investigation_id] = checkpointed
            self._save_archive(checkpointed)
            if checkpointed.status == InvestigationStatus.WAITING_FOR_HUMAN and checkpointed.pending_human_question:
                await self._publish(
                    InvestigationEvent(
                        investigation_id=investigation_id,
                        type="HumanInputRequested",
                        message=checkpointed.pending_human_question,
                        data={
                            "question": checkpointed.pending_human_question,
                            "hypothesis_id": checkpointed.current_focus,
                        },
                    )
                )
        except Exception as exc:
            logger.exception("Investigation %s failed", investigation_id)
            safe_message = "The investigation could not be completed. Check the backend logs and try again."
            current = self._records[investigation_id]
            failed = current.model_copy(update={"status": InvestigationStatus.FAILED, "error": safe_message})
            self._records[investigation_id] = failed
            self._save_archive(failed)
            await self._publish(
                InvestigationEvent(
                    investigation_id=investigation_id,
                    type="InvestigationFailed",
                    message=safe_message,
                    data={"error_code": "investigation_failed"},
                )
            )

    def get(self, investigation_id: str) -> InvestigationState:
        try:
            return self._records[investigation_id]
        except KeyError as exc:
            raise KeyError(f"Investigation {investigation_id!r} was not found") from exc

    def list(self) -> list[InvestigationState]:
        return sorted(self._records.values(), key=lambda state: state.started_at, reverse=True)

    async def _delete_persistent_checkpoints(self, investigation_id: str) -> None:
        """Delete the base graph thread and every continuation thread for one conversation."""
        if not self._persistent_checkpoints_enabled:
            return
        continuation_prefix = f"{investigation_id}:"
        tables = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")
        async with await AsyncConnection.connect(settings.checkpoint_database_url, autocommit=True) as connection:
            for table in tables:
                statement = sql.SQL(
                    "DELETE FROM {} WHERE thread_id = %s OR LEFT(thread_id, CHAR_LENGTH(%s)) = %s"
                ).format(sql.Identifier(settings.checkpoint_schema, table))
                await connection.execute(statement, (investigation_id, continuation_prefix, continuation_prefix))
        logger.info("Deleted persistent checkpoints for conversation %s", investigation_id)

    async def delete(self, investigation_id: str) -> None:
        self.get(investigation_id)
        task = self._tasks.get(investigation_id)
        if task and not task.done():
            raise ValueError("A running investigation cannot be deleted")
        # Delete the durable state first. If PostgreSQL is unavailable, retain the archive and
        # in-memory record rather than leaving a resumable graph behind after a claimed deletion.
        await self._delete_persistent_checkpoints(investigation_id)
        self._records.pop(investigation_id, None)
        self._events.pop(investigation_id, None)
        self._tasks.pop(investigation_id, None)
        self._delete_archive(investigation_id)

    async def delete_all(self) -> None:
        running = [investigation_id for investigation_id, task in self._tasks.items() if not task.done()]
        if running:
            raise ValueError("Running investigations cannot be deleted")
        for investigation_id in list(self._records):
            await self.delete(investigation_id)

    async def respond(self, investigation_id: str, response: str) -> InvestigationState:
        state = self.get(investigation_id)
        if state.status != InvestigationStatus.WAITING_FOR_HUMAN:
            raise ValueError("This investigation is not waiting for human input")
        running = self._tasks.get(investigation_id)
        if running and not running.done():
            raise ValueError("The investigation is still processing its previous step")
        resumed = state.model_copy(
            update={
                "status": InvestigationStatus.RUNNING,
                "human_feedback": [
                    *state.human_feedback,
                    HumanFeedback(
                        question=state.pending_human_question or "Clarification",
                        response=response,
                        hypothesis_id=state.current_focus,
                    ),
                ],
            }
        )
        self._records[investigation_id] = resumed
        self._save_archive(resumed)
        await self._publish(
            InvestigationEvent(
                investigation_id=investigation_id,
                type="HumanInputReceived",
                message="Clarification received; investigation resumed.",
            )
        )
        self._tasks[investigation_id] = asyncio.create_task(
            self._run(investigation_id, Command(resume=response))
        )
        return resumed

    @staticmethod
    def _is_negative_follow_up(response: str) -> bool:
        normalized = response.strip().casefold().rstrip(".!?")
        return normalized in {"no", "no thanks", "no thank you", "nothing else", "that is all", "that's all", "done"}

    @staticmethod
    def _original_question(state: InvestigationState) -> str:
        if state.original_question:
            return state.original_question
        marker = "\n\nEarlier answer context: "
        return state.question.rsplit(marker, 1)[-1]

    @staticmethod
    def _prior_conversation_turns(state: InvestigationState) -> list[Any]:
        """Represent a completed investigation as the first turn before adding a continuation."""
        if state.conversation_turns or state.final_analysis is None:
            return state.conversation_turns
        from business_signals.models import ConversationTurn

        return [ConversationTurn(question=InvestigationService._original_question(state), answer=state.final_analysis)]

    @staticmethod
    def _continuation_context(state: InvestigationState) -> str:
        """Give a fresh continuation enough prior outcome context without rerunning the old investigation."""
        if state.final_analysis is None:
            return "The previous attempt ended before an answer was produced."
        conclusion_context = (
            f"Previous conclusion: {state.final_analysis.summary}\n"
            f"Confidence: {state.final_analysis.confidence:.0%}\n"
            f"Main finding: {state.final_analysis.likely_root_cause or 'No single finding'}"
        )
        if not state.human_feedback:
            return conclusion_context
        prior_answers = "\n".join(
            f"- {item.question} Answer: {item.response}" for item in state.human_feedback
        )
        return f"{conclusion_context}\nEarlier confirmed preferences:\n{prior_answers}"

    async def follow_up(self, investigation_id: str, response: str) -> InvestigationState:
        """Continue any finished conversation without discarding its prior investigation."""
        running = self._tasks.get(investigation_id)
        if running and not running.done():
            await running
        state = self.get(investigation_id)
        resumable_statuses = {
            InvestigationStatus.COMPLETED,
            InvestigationStatus.INSUFFICIENT_EVIDENCE,
            InvestigationStatus.FAILED,
        }
        if state.status not in resumable_statuses:
            raise ValueError("Only a finished conversation can receive a follow-up")
        follow_up_question = state.final_analysis.follow_up_question if state.final_analysis else None
        feedback = HumanFeedback(question=follow_up_question or "Would you like more detail?", response=response)
        if self._is_negative_follow_up(response):
            final_analysis = state.final_analysis.model_copy(update={"follow_up_question": None}) if state.final_analysis else None
            completed = state.model_copy(
                update={"human_feedback": [*state.human_feedback, feedback], "final_analysis": final_analysis}
            )
            self._records[investigation_id] = completed
            self._save_archive(completed)
            return completed

        requested_detail = follow_up_question if response.strip().casefold() in {"yes", "yes please", "sure", "please"} else response.strip()
        updated_question = (
            f"Follow-up request: {requested_detail}\n\n"
            f"Original question: {self._original_question(state)}\n\n"
            f"{self._continuation_context(state)}\n\n"
            "Continue from the previous result. Do not repeat the original investigation; answer only the new request."
        )
        replacement = state.model_copy(
            update={
                "question": updated_question,
                "original_question": self._original_question(state),
                "current_conversation_question": requested_detail,
                "metric_definition": None,
                "current_focus": None,
                "next_action": None,
                "external_request": None,
                "pending_step": None,
                "request_type": "direct_answer",
                "pending_human_question": None,
                "human_resume_node": None,
                "human_feedback": [*state.human_feedback, feedback],
                "conversation_turns": self._prior_conversation_turns(state),
                "confidence": None,
                "status": InvestigationStatus.RUNNING,
                "final_analysis": None,
                "error": None,
            }
        )
        self._records[investigation_id] = replacement
        self._save_archive(replacement)
        self._tasks[investigation_id] = asyncio.create_task(
            self._run(investigation_id, replacement, thread_id=f"{investigation_id}:follow_up:{uuid4().hex}")
        )
        return replacement

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
