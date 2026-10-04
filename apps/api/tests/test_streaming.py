import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from business_signals.datasources.registry import DatasourceRegistry
from business_signals.models import InvestigationEvent, InvestigationState, InvestigationStatus
from business_signals.service import InvestigationService


@pytest.mark.asyncio
async def test_investigation_events_replay_to_new_stream_subscribers() -> None:
    service = InvestigationService(DatasourceRegistry())
    state = InvestigationState(
        investigation_id="inv_stream",
        question="Why did revenue decline?",
        datasources=[],
    )
    service._records[state.investigation_id] = state
    published = InvestigationEvent(
        investigation_id=state.investigation_id,
        type="EvidenceFound",
        message="Revenue fell after stock availability declined.",
        data={"confidence": 0.86},
    )
    await service._publish(published)

    stream = service.events(state.investigation_id)
    received = await anext(stream)
    await stream.aclose()

    assert received == published


@pytest.mark.asyncio
async def test_live_only_event_stream_does_not_replay_a_completed_turn() -> None:
    service = InvestigationService(DatasourceRegistry())
    state = InvestigationState(investigation_id="inv_live_stream", question="Why did revenue decline?", datasources=[])
    service._records[state.investigation_id] = state
    await service._publish(InvestigationEvent(investigation_id=state.investigation_id, type="InvestigationCompleted", message="Old turn"))

    stream = service.events(state.investigation_id, live_only=True)
    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    await service._publish(InvestigationEvent(investigation_id=state.investigation_id, type="InvestigationCompleted", message="New turn"))
    received = await pending
    await stream.aclose()

    assert received.message == "New turn"


@pytest.mark.asyncio
async def test_event_stream_replays_updates_emitted_while_a_clarification_is_resuming() -> None:
    service = InvestigationService(DatasourceRegistry())
    state = InvestigationState(
        investigation_id="inv_clarification_resume",
        question="Why did revenue decline?",
        datasources=[],
    )
    service._records[state.investigation_id] = state
    await service._publish(
        InvestigationEvent(
            investigation_id=state.investigation_id,
            type="HumanInputRequested",
            message="Which sales measure should I use?",
        )
    )
    await service._publish(
        InvestigationEvent(
            investigation_id=state.investigation_id,
            type="HumanInputReceived",
            message="Clarification received; investigation resumed.",
        )
    )
    await service._publish(
        InvestigationEvent(
            investigation_id=state.investigation_id,
            type="HypothesisCreated",
            message="Reduced store visits may explain the decline.",
        )
    )

    stream = service.events(state.investigation_id, after=1)
    resumed = await anext(stream)
    hypothesis = await anext(stream)
    await stream.aclose()

    assert resumed.type == "HumanInputReceived"
    assert hypothesis.type == "HypothesisCreated"


class FailingGraph:
    async def astream(self, *args: object, **kwargs: object):
        raise RuntimeError("provider response contains sensitive diagnostic details")
        yield  # pragma: no cover


@pytest.mark.asyncio
async def test_investigation_failure_is_logged_but_not_sent_to_client(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    service = InvestigationService(DatasourceRegistry(), archive_dir=tmp_path / "investigations")
    state = InvestigationState(
        investigation_id="inv_failure",
        question="Why did revenue decline?",
        datasources=[],
    )
    service._records[state.investigation_id] = state
    service.engine = SimpleNamespace(graph=FailingGraph())

    with caplog.at_level("ERROR"):
        await service._run(state.investigation_id, state)

    failed = service.get(state.investigation_id)
    event = service._events[state.investigation_id][-1]
    assert "sensitive diagnostic" in caplog.text
    assert failed.error == "The investigation could not be completed. Check the backend logs and try again."
    assert event.data == {"error_code": "investigation_failed"}


@pytest.mark.asyncio
async def test_stopping_an_active_investigation_cancels_work_and_preserves_the_archive(tmp_path: Path) -> None:
    service = InvestigationService(DatasourceRegistry(), archive_dir=tmp_path / "investigations")
    state = InvestigationState(
        investigation_id="inv_stop",
        question="Why did revenue decline?",
        datasources=[],
        status=InvestigationStatus.RUNNING,
    )
    service._records[state.investigation_id] = state
    service._save_archive(state)

    async def keep_running() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(keep_running())
    service._tasks[state.investigation_id] = task
    stopped = await service.stop(state.investigation_id)

    assert task.cancelled()
    assert stopped.status == InvestigationStatus.STOPPED
    assert stopped.pending_step is None
    assert service._archive_path(state.investigation_id).exists()
    assert service._events[state.investigation_id][-1].type == "InvestigationStopped"
