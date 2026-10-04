from pathlib import Path

import pytest
from business_signals.datasources.registry import DatasourceRegistry
from business_signals.models import (
    Evidence,
    FinalAnalysis,
    HumanFeedback,
    Hypothesis,
    InvestigationState,
    InvestigationStatus,
)
from business_signals.service import InvestigationService


def build_state(investigation_id: str) -> InvestigationState:
    hypothesis = Hypothesis(
        id="hyp_inventory",
        name="Inventory constraint",
        description="Inventory constrained sales.",
        category="operations",
        confidence=0.82,
        status="supported",
    )

    evidence = Evidence(
        id="ev_inventory",
        description="Available units fell before orders.",
        source="analytics",
        relationship="direct",
        confidence=0.9,
        hypothesis_ids=[hypothesis.id],
    )
    return InvestigationState(
        investigation_id=investigation_id,
        question="Why did orders decline after July 14, 2024?",
        datasources=[],
        hypotheses=[hypothesis],
        evidence=[evidence],
        human_feedback=[HumanFeedback(question="Which region?", response="Northern Italy")],
        pending_human_question="Should discounts be included?",
        final_analysis=FinalAnalysis(
            likely_root_cause="Inventory constrained sales.",
            confidence=0.82,
            evidence=[evidence],
            rejected_hypotheses=[],
            external_findings=[],
            caveats=["Sales data is limited to the available period."],
            summary="Inventory is the strongest explanation.",
        ),
    )


def test_selected_follow_up_option_becomes_a_meaningful_next_request() -> None:
    question = (
        "Would you like me to (A) inspect transaction records, (B) look for a platform mapping, "
        "or (C) accept a best-effort estimate using session-level metrics?"
    )

    assert InvestigationService._follow_up_request(question, "C") == (
        "accept a best-effort estimate using session-level metrics"
    )
    assert InvestigationService._follow_up_request(question, "A.") == "inspect transaction records"
    assert InvestigationService._follow_up_request(question, "Ask about stores") == "Ask about stores"


def test_archive_restores_question_answers_hypotheses_evidence_and_conclusion(tmp_path: Path) -> None:
    archive_dir = tmp_path / "investigations"
    registry = DatasourceRegistry(store_path=tmp_path / "datasources.json")
    service = InvestigationService(registry, archive_dir=archive_dir)
    saved = build_state("inv_saved")
    service._records[saved.investigation_id] = saved
    service._save_archive(saved)

    restored = InvestigationService(registry, archive_dir=archive_dir).get(saved.investigation_id)

    assert restored.question == saved.question
    assert restored.human_feedback[0].response == "Northern Italy"
    assert restored.pending_human_question == "Should discounts be included?"
    assert restored.hypotheses[0].name == "Inventory constraint"
    assert restored.evidence[0].id == "ev_inventory"
    assert restored.final_analysis and restored.final_analysis.summary == "Inventory is the strongest explanation."
    assert (archive_dir / "inv_saved.json").stat().st_mode & 0o777 == 0o600
    assert "\n  \"question\":" in (archive_dir / "inv_saved.json").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_archive_can_delete_one_or_all_saved_investigations(tmp_path: Path) -> None:
    archive_dir = tmp_path / "investigations"
    registry = DatasourceRegistry(store_path=tmp_path / "datasources.json")
    service = InvestigationService(registry, archive_dir=archive_dir)
    first, second = build_state("inv_one"), build_state("inv_two")
    for state in (first, second):
        service._records[state.investigation_id] = state
        service._save_archive(state)

    await service.delete(first.investigation_id)
    assert [state.investigation_id for state in service.list()] == [second.investigation_id]
    await service.delete_all()

    assert service.list() == []
    assert list(archive_dir.glob("*.json")) == []


@pytest.mark.asyncio
async def test_deleting_a_conversation_deletes_its_persistent_checkpoint_first(tmp_path: Path) -> None:
    service = InvestigationService(DatasourceRegistry(store_path=tmp_path / "datasources.json"), archive_dir=tmp_path / "investigations")
    state = build_state("inv_checkpointed")
    service._records[state.investigation_id] = state
    service._save_archive(state)
    deleted_threads: list[str] = []

    async def record_checkpoint_deletion(investigation_id: str) -> None:
        deleted_threads.append(investigation_id)

    service._delete_persistent_checkpoints = record_checkpoint_deletion  # type: ignore[method-assign]
    await service.delete(state.investigation_id)

    assert deleted_threads == [state.investigation_id]
    assert state.investigation_id not in service._records
    assert not (tmp_path / "investigations" / f"{state.investigation_id}.json").exists()


@pytest.mark.asyncio
async def test_negative_direct_answer_follow_up_ends_and_persists_the_session(tmp_path: Path) -> None:
    archive_dir = tmp_path / "investigations"
    service = InvestigationService(DatasourceRegistry(store_path=tmp_path / "datasources.json"), archive_dir=archive_dir)
    state = build_state("inv_direct_done").model_copy(
        update={
            "request_type": "direct_answer",
            "status": InvestigationStatus.COMPLETED,
            "final_analysis": build_state("inv_direct_done").final_analysis.model_copy(
                update={"follow_up_question": "Would you like more detail?"}
            ),
        }
    )
    service._records[state.investigation_id] = state
    service._save_archive(state)

    completed = await service.follow_up(state.investigation_id, "No thanks")
    restored = InvestigationService(
        DatasourceRegistry(store_path=tmp_path / "datasources.json"), archive_dir=archive_dir
    ).get(state.investigation_id)

    assert completed.status == InvestigationStatus.COMPLETED
    assert completed.final_analysis and completed.final_analysis.follow_up_question is None
    assert restored.human_feedback[-1].response == "No thanks"


@pytest.mark.asyncio
async def test_completed_investigation_can_continue_without_losing_prior_context(tmp_path: Path) -> None:
    service = InvestigationService(DatasourceRegistry(store_path=tmp_path / "datasources.json"), archive_dir=tmp_path / "investigations")
    state = build_state("inv_root_done").model_copy(update={"status": InvestigationStatus.COMPLETED})
    service._records[state.investigation_id] = state
    service._save_archive(state)

    async def do_not_run_graph(*_args, **_kwargs) -> None:
        return None

    service._run = do_not_run_graph  # type: ignore[method-assign]
    resumed = await service.follow_up(state.investigation_id, "Break this down by store")

    assert resumed.status == InvestigationStatus.RUNNING
    # The engine will classify this new turn from its actual question. A follow-up is
    # not inherently a direct retrieval; it may also be a new "why" investigation.
    assert resumed.request_type == "investigation"
    assert resumed.hypotheses == state.hypotheses
    assert resumed.evidence == state.evidence
    assert resumed.conversation_turns[0].question == state.question
    assert resumed.conversation_turns[0].answer.summary == state.final_analysis.summary
    assert "Follow-up request: Break this down by store" in resumed.question
    assert "Previous conclusion: Inventory is the strongest explanation." in resumed.question
    assert "Earlier confirmed preferences:" in resumed.question
    assert "Which region? Answer: Northern Italy" in resumed.question
    await service._tasks[state.investigation_id]


@pytest.mark.asyncio
async def test_clarification_resumes_the_follow_up_checkpoint_thread(tmp_path: Path) -> None:
    service = InvestigationService(
        DatasourceRegistry(store_path=tmp_path / "datasources.json"), archive_dir=tmp_path / "investigations"
    )
    state = build_state("inv_follow_up_waiting").model_copy(
        update={
            "status": InvestigationStatus.WAITING_FOR_HUMAN,
            "pending_human_question": "Would you like daily or weekly totals?",
            "checkpoint_thread_id": "inv_follow_up_waiting:follow_up:checkpoint",
        }
    )
    service._records[state.investigation_id] = state
    calls: list[str | None] = []

    async def record_run(*_args, **kwargs) -> None:
        calls.append(kwargs.get("thread_id"))

    service._run = record_run  # type: ignore[method-assign]
    await service.respond(state.investigation_id, "weekly")
    await service._tasks[state.investigation_id]

    assert calls == ["inv_follow_up_waiting:follow_up:checkpoint"]


@pytest.mark.asyncio
async def test_skipping_a_clarification_starts_a_fresh_context_preserving_turn(tmp_path: Path) -> None:
    service = InvestigationService(
        DatasourceRegistry(store_path=tmp_path / "datasources.json"), archive_dir=tmp_path / "investigations"
    )
    state = build_state("inv_skip_clarification").model_copy(
        update={
            "status": InvestigationStatus.WAITING_FOR_HUMAN,
            "pending_human_question": "Would you like daily or weekly totals?",
            "checkpoint_thread_id": "inv_skip_clarification:follow_up:paused",
            "conversation_turns": [],
        }
    )
    service._records[state.investigation_id] = state
    calls: list[tuple[object, str | None]] = []

    async def record_run(*args, **kwargs) -> None:
        calls.append((args[1], kwargs.get("thread_id")))

    service._run = record_run  # type: ignore[method-assign]
    resumed = await service.skip_clarification(state.investigation_id, "Show the result by store instead")
    await service._tasks[state.investigation_id]

    assert resumed.status == InvestigationStatus.RUNNING
    assert resumed.request_type == "investigation"
    assert resumed.pending_human_question is None
    assert resumed.hypotheses == state.hypotheses
    assert resumed.evidence == state.evidence
    assert resumed.human_feedback[-1].response.startswith("Skipped")
    assert "New request: Show the result by store instead" in resumed.question
    assert "Previous conclusion: Inventory is the strongest explanation." in resumed.question
    assert calls[0][1] and calls[0][1] != state.checkpoint_thread_id


def test_legacy_follow_up_uses_the_original_question_as_its_conversation_name(tmp_path: Path) -> None:
    service = InvestigationService(DatasourceRegistry(store_path=tmp_path / "datasources.json"), archive_dir=tmp_path / "investigations")
    legacy = InvestigationState(
        investigation_id="inv_legacy",
        question="Follow-up request: Show sales by store\n\nEarlier answer context: What is the most valuable item?",
        datasources=[],
    )

    assert service._original_question(legacy) == "What is the most valuable item?"
