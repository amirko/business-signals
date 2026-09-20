from types import SimpleNamespace

import pytest

from business_signals.datasources.registry import DatasourceRegistry
from business_signals.models import InvestigationEvent, InvestigationState
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


class FailingGraph:
    async def astream(self, *args: object, **kwargs: object):
        raise RuntimeError("provider response contains sensitive diagnostic details")
        yield  # pragma: no cover


@pytest.mark.asyncio
async def test_investigation_failure_is_logged_but_not_sent_to_client(caplog: pytest.LogCaptureFixture) -> None:
    service = InvestigationService(DatasourceRegistry())
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
