import asyncio

import pytest
from business_signals.investigation.external_research import ExternalResearchCoordinator
from business_signals.llm import ExternalResearchPlan, ExternalResearchPlans, ExternalResearchRelevance
from business_signals.models import (
    ExternalFinding,
    ExternalResearchCandidate,
    HypothesisStatus,
    InvestigationState,
    InvestigationStep,
)


class ExternalResearchFixture:
    def available_agents(self) -> list[dict[str, object]]:
        return [{"id": "weather", "name": "Historical weather", "evidence_topics": ["weather.conditions"]}]

    async def research(
        self, agent_id: str, subject: str, start_date: str, end_date: str, context: str
    ) -> ExternalFinding:
        assert (agent_id, subject, start_date, end_date, context) == (
            "weather",
            "Milan",
            "2024-07-01",
            "2024-07-31",
            "Why did outdoor sales fall?",
        )
        return ExternalFinding(
            type="weather",
            period="2024-07-01 to 2024-07-31",
            observation="Rainfall was unusually high during the period.",
            relationship="correlated",
            confidence=0.78,
            source_url="https://weather.example.test/",
            source_title="Historical weather",
        )


class ExternalResearchPlanningLLM:
    async def structured(
        self, response_model: type[ExternalResearchPlans], instruction: str, payload: dict[str, object]
    ) -> ExternalResearchPlans:
        assert response_model is ExternalResearchPlans
        assert payload["available_research_agents"] == [
            {"id": "weather", "name": "Historical weather", "evidence_topics": ["weather.conditions"]}
        ]
        return ExternalResearchPlans(plans=[ExternalResearchPlan(
            hypothesis_name="Weather disruption", agent_id="weather", subject="Milan",
            start_date="2024-07-01", end_date="2024-07-31",
            rationale="Check whether weather could have affected visits to outdoor-product stores.",
        )])


@pytest.mark.asyncio
async def test_external_research_coordinator_selects_and_records_a_bounded_check() -> None:
    emitted: list[tuple[str, str]] = []

    async def emit(_state: InvestigationState, event_type: str, message: str, **_data: object) -> None:
        emitted.append((event_type, message))

    coordinator = ExternalResearchCoordinator(
        researcher=ExternalResearchFixture(),  # type: ignore[arg-type]
        llm=ExternalResearchPlanningLLM(),  # type: ignore[arg-type]
        emit=emit,
        state_payload=lambda state: {"question": state.question},
        resolve_hypothesis_name=lambda name, state: state.hypothesis_name_index.get(name.casefold()),
    )
    state = InvestigationState(
        investigation_id="inv_external_component",
        question="Why did outdoor sales fall?",
        datasources=[],
        hypotheses=[
            {
                "id": "hyp_weather",
                "name": "Weather disruption",
                "description": "Weather reduced visits.",
                "category": "external",
                "research_scope": "external",
                "evidence_topics": ["weather.conditions"],
                "confidence": 0.4,
            }
        ],
        hypothesis_name_index={"weather disruption": "hyp_weather"},
    )

    selected = await coordinator.select(state)
    completed = await coordinator.execute(state.model_copy(update=selected))

    assert completed["external_call_count"] == 1
    assert completed["external_research_checks"][0].status == "completed"
    assert completed["external_findings"][0].relationship == "correlated"
    assert [event_type for event_type, _message in emitted] == [
        "ExternalResearchSelected",
        "ExternalResearchStarted",
        "ExternalResearchCompleted",
    ]


class UnavailableResearchFixture(ExternalResearchFixture):
    async def research(
        self, agent_id: str, subject: str, start_date: str, end_date: str, context: str
    ) -> ExternalFinding:
        raise RuntimeError("provider timeout")


class NewsCandidatesFixture:
    def available_agents(self) -> list[dict[str, object]]:
        return [{"id": "guardian-news", "name": "Historic news", "evidence_topics": ["public.events"]}]

    async def research(
        self, _agent_id: str, _subject: str, start_date: str, end_date: str, _context: str
    ) -> ExternalFinding:
        return ExternalFinding(
            type="guardian-news",
            period=f"{start_date} to {end_date}",
            observation="Unrelated tennis article",
            relationship="correlated",
            confidence=0.68,
            source_url="https://www.theguardian.com/sport/example",
            source_title="The Guardian: Sport",
            candidates=[
                ExternalResearchCandidate(
                    title="Unrelated tennis article",
                    url="https://www.theguardian.com/sport/example",
                    section="Sport",
                ),
                ExternalResearchCandidate(
                    title="General Dubai business feature",
                    url="https://www.theguardian.com/business/example",
                    section="Business",
                ),
            ],
        )


class NoRelevantNewsLLM:
    async def structured(self, response_model, _instruction: str, payload: dict[str, object]):
        assert response_model is ExternalResearchRelevance
        assert len(payload["provider_candidates"]) == 2
        return ExternalResearchRelevance(
            candidate_index=None,
            relevance="none",
            confidence=0.98,
            rationale="Neither returned article documents a disruption or closure relevant to the claim.",
        )


@pytest.mark.asyncio
async def test_external_research_omits_provider_candidates_that_do_not_bear_on_the_hypothesis() -> None:
    emitted: list[str] = []

    async def emit(_state: InvestigationState, event_type: str, _message: str, **_data: object) -> None:
        emitted.append(event_type)

    coordinator = ExternalResearchCoordinator(
        researcher=NewsCandidatesFixture(),  # type: ignore[arg-type]
        llm=NoRelevantNewsLLM(),  # type: ignore[arg-type]
        emit=emit,
        state_payload=lambda _state: {},
        resolve_hypothesis_name=lambda _name, _state: "hyp_events",
    )
    state = InvestigationState(
        investigation_id="inv_irrelevant_news",
        question="Why did store revenue fall?",
        datasources=[],
        hypotheses=[{
            "id": "hyp_events",
            "name": "Local closures",
            "description": "A local closure reduced access to stores.",
            "category": "external",
            "research_scope": "external",
            "evidence_topics": ["public.events"],
            "confidence": 0.2,
        }],
        external_request={
            "agent_id": "guardian-news",
            "hypothesis_id": "hyp_events",
            "subject": "Dubai, United Arab Emirates",
            "start_date": "2026-02-01",
            "end_date": "2026-03-31",
            "rationale": "Check reports for closures or disruptions affecting shopper access.",
        },
    )

    completed = await coordinator.execute(state)

    assert completed["external_findings"] == []
    assert completed["observations"] == []
    assert completed["external_research_checks"][0].status == "completed"
    assert emitted == ["ExternalResearchStarted", "ExternalResearchNoRelevantResult"]


class ParallelResearchFixture:
    def __init__(self) -> None:
        self.active = 0
        self.maximum_active = 0

    def available_agents(self) -> list[dict[str, object]]:
        return [
            {"id": "weather", "name": "Historical weather", "evidence_topics": ["weather.conditions"]},
            {"id": "guardian-news", "name": "Historic news and events (Guardian)", "evidence_topics": ["public.events"]},
        ]

    async def research(
        self, agent_id: str, subject: str, start_date: str, end_date: str, context: str
    ) -> ExternalFinding:
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        await asyncio.sleep(0)
        self.active -= 1
        return ExternalFinding(
            type=agent_id,
            period=f"{start_date} to {end_date}",
            observation=f"{agent_id} found relevant context for {subject}.",
            relationship="correlated",
            confidence=0.7,
            source_url=f"https://{agent_id}.example.test/",
            source_title=agent_id,
        )


class SequentialResearchPlanningLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def structured(self, response_model, _instruction: str, payload: dict[str, object]) -> ExternalResearchPlans:
        assert response_model is ExternalResearchPlans
        assert payload["maximum_checks"] == 1
        agent_id = "weather" if self.calls == 0 else "guardian-news"
        self.calls += 1
        return ExternalResearchPlans(plans=[
            ExternalResearchPlan(
                hypothesis_name="External conditions", agent_id=agent_id, subject="Milan",
                start_date="2024-07-01", end_date="2024-07-31", rationale="Check local weather.",
            ),
        ])


@pytest.mark.asyncio
async def test_external_research_runs_one_check_per_selection_so_each_result_can_be_interpreted() -> None:
    researcher = ParallelResearchFixture()

    async def emit(_state: InvestigationState, _event_type: str, _message: str, **_data: object) -> None:
        return None

    coordinator = ExternalResearchCoordinator(
        researcher=researcher,  # type: ignore[arg-type]
        llm=SequentialResearchPlanningLLM(),  # type: ignore[arg-type]
        emit=emit,
        state_payload=lambda _state: {},
        resolve_hypothesis_name=lambda _name, _state: "hyp_external",
    )
    state = InvestigationState(
        investigation_id="inv_parallel_external",
        question="Why did sales fall?",
        datasources=[],
        hypotheses=[{
            "id": "hyp_external", "name": "External conditions", "description": "Outside factors affected demand.",
            "category": "external", "research_scope": "external", "evidence_topics": ["weather.conditions", "public.events"], "confidence": 0.2,
        }],
        hypothesis_name_index={"external conditions": "hyp_external"},
    )

    first_selected = await coordinator.select(state)
    first_completed = await coordinator.execute(state.model_copy(update=first_selected))
    second_selected = await coordinator.select(state.model_copy(update=first_completed))
    completed = await coordinator.execute(state.model_copy(update={**first_completed, **second_selected}))

    assert completed["external_call_count"] == 2
    assert [finding.type for finding in completed["external_findings"]] == ["weather", "guardian-news"]
    assert researcher.maximum_active == 1


def test_external_agents_are_selected_by_semantic_evidence_topics() -> None:
    """A weather mechanism cannot accidentally invoke the FX connector."""
    coordinator = ExternalResearchCoordinator(
        researcher=type("Researcher", (), {
            "available_agents": lambda _self: [
                {"id": "weather", "name": "Weather", "evidence_topics": ["weather.conditions"]},
                {"id": "economy-fx", "name": "FX", "evidence_topics": ["currency.exchange"]},
            ]
        })(),  # type: ignore[arg-type]
        llm=ExternalResearchPlanningLLM(),  # type: ignore[arg-type]
        emit=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        state_payload=lambda _state: {},
        resolve_hypothesis_name=lambda _name, _state: None,
    )
    state = InvestigationState(
        investigation_id="inv_semantic_agent_routing",
        question="Why did sales fall?",
        datasources=[],
        hypotheses=[
            {
                "id": "weather_cause",
                "name": "Bad weather",
                "description": "Rain discouraged shoppers.",
                "category": "external",
                "research_scope": "external",
                "evidence_topics": ["weather.conditions"],
                "confidence": 0.3,
            },
            {
                "id": "currency_cause",
                "name": "Currency movement",
                "description": "A rate movement affected demand.",
                "category": "external",
                "research_scope": "external",
                "evidence_topics": ["currency.exchange"],
                "confidence": 0.3,
            },
        ],
    )

    eligible = coordinator.eligible_agents_by_hypothesis(state)

    assert [agent["id"] for agent in eligible["weather_cause"]] == ["weather"]
    assert [agent["id"] for agent in eligible["currency_cause"]] == ["economy-fx"]


@pytest.mark.asyncio
async def test_external_research_coordinator_logs_and_contains_provider_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    emitted: list[str] = []

    async def emit(_state: InvestigationState, event_type: str, _message: str, **_data: object) -> None:
        emitted.append(event_type)

    coordinator = ExternalResearchCoordinator(
        researcher=UnavailableResearchFixture(),  # type: ignore[arg-type]
        llm=ExternalResearchPlanningLLM(),  # type: ignore[arg-type]
        emit=emit,
        state_payload=lambda _state: {},
        resolve_hypothesis_name=lambda _name, _state: "hyp_weather",
    )
    state = InvestigationState(
        investigation_id="inv_external_unavailable",
        question="Why did outdoor sales fall?",
        datasources=[],
        external_request={
            "agent_id": "weather",
            "hypothesis_id": "hyp_weather",
            "subject": "Milan",
            "start_date": "2024-07-01",
            "end_date": "2024-07-31",
            "rationale": "Check weather.",
        },
    )

    with caplog.at_level("ERROR", logger="uvicorn.error"):
        completed = await coordinator.execute(state)

    assert completed["next_action"] == "continue"
    assert completed["failed_external_hypothesis_ids"] == ["hyp_weather"]
    assert completed["failed_external_checks"] == [
        {"hypothesis_id": "hyp_weather", "diagnostic": "weather: RuntimeError"}
    ]
    assert "External research check failed; continuing with other hypotheses" in caplog.text
    assert "provider timeout" in caplog.text
    assert emitted == ["ExternalResearchStarted", "ExternalResearchUnavailable"]
    assert completed["external_research_checks"][0].status == "unavailable"


@pytest.mark.asyncio
async def test_external_research_does_not_repeat_an_identical_completed_request() -> None:
    async def emit(_state: InvestigationState, _event_type: str, _message: str, **_data: object) -> None:
        return None

    coordinator = ExternalResearchCoordinator(
        researcher=ExternalResearchFixture(),  # type: ignore[arg-type]
        llm=ExternalResearchPlanningLLM(),  # type: ignore[arg-type]
        emit=emit,
        state_payload=lambda state: {"question": state.question},
        resolve_hypothesis_name=lambda name, state: state.hypothesis_name_index.get(name.casefold()),
    )
    state = InvestigationState(
        investigation_id="inv_no_duplicate_external_request",
        question="Why did outdoor sales fall?",
        datasources=[],
        hypotheses=[{
            "id": "hyp_weather", "name": "Weather disruption", "description": "Weather reduced visits.",
            "category": "external", "research_scope": "external", "evidence_topics": ["weather.conditions"], "confidence": 0.4,
        }],
        hypothesis_name_index={"weather disruption": "hyp_weather"},
    )

    first_selection = await coordinator.select(state)
    completed = await coordinator.execute(state.model_copy(update=first_selection))
    repeated_selection = await coordinator.select(state.model_copy(update=completed))

    assert repeated_selection["external_request"] is None
    assert repeated_selection["next_action"] == "continue"
    # A completed agent/hypothesis pair is no longer eligible, so it cannot
    # re-enter the planning loop with a slightly different request.
    assert "hypotheses" not in repeated_selection


@pytest.mark.asyncio
async def test_external_research_does_not_repeat_a_legacy_archived_agent_step() -> None:
    async def emit(_state: InvestigationState, _event_type: str, _message: str, **_data: object) -> None:
        return None

    coordinator = ExternalResearchCoordinator(
        researcher=ExternalResearchFixture(),  # type: ignore[arg-type]
        llm=ExternalResearchPlanningLLM(),  # type: ignore[arg-type]
        emit=emit,
        state_payload=lambda state: {"question": state.question},
        resolve_hypothesis_name=lambda name, state: state.hypothesis_name_index.get(name.casefold()),
    )
    state = InvestigationState(
        investigation_id="inv_legacy_weather_step",
        question="Why did outdoor sales fall?",
        datasources=[],
        hypotheses=[{
            "id": "hyp_weather", "name": "Weather disruption", "description": "Weather reduced visits.",
            "category": "external", "research_scope": "external", "evidence_topics": ["weather.conditions"], "confidence": 0.4,
        }],
        hypothesis_name_index={"weather disruption": "hyp_weather"},
        investigation_history=[InvestigationStep(
            iteration=1,
            hypothesis_id="hyp_weather",
            action="Check historical weather.",
            rationale="Check weather.",
        )],
    )

    selection = await coordinator.select(state)

    assert selection["external_request"] is None
    assert "hypotheses" not in selection


class ComparisonBoundaryResearchFixture:
    received: tuple[object, ...] | None = None

    def available_agents(self) -> list[dict[str, object]]:
        return [{"id": "weather", "name": "Historical weather", "evidence_topics": ["weather.conditions"]}]

    async def research(self, *args: object) -> ExternalFinding:
        self.received = args
        return ExternalFinding(
            type="weather",
            period="2024-06-01 to 2024-07-31",
            observation="The period comparison is available.",
            confidence=0.7,
            source_url="https://weather.example.test/",
            source_title="Historical weather",
        )


class ComparisonBoundaryPlanningLLM:
    async def structured(self, response_model, _instruction: str, _payload: dict[str, object]) -> ExternalResearchPlans:
        assert response_model is ExternalResearchPlans
        return ExternalResearchPlans(plans=[
            ExternalResearchPlan(
                hypothesis_name="External conditions",
                agent_id="weather",
                subject="Milan, Italy",
                start_date="2024-06-01",
                end_date="2024-07-31",
                comparison_start_date="2024-07-01",
                rationale="Compare conditions before and after the sales change.",
            )
        ])


@pytest.mark.asyncio
async def test_external_research_passes_an_explicit_comparison_boundary_to_a_time_series_agent() -> None:
    researcher = ComparisonBoundaryResearchFixture()

    async def emit(_state: InvestigationState, _event_type: str, _message: str, **_data: object) -> None:
        return None

    coordinator = ExternalResearchCoordinator(
        researcher=researcher,  # type: ignore[arg-type]
        llm=ComparisonBoundaryPlanningLLM(),  # type: ignore[arg-type]
        emit=emit,
        state_payload=lambda _state: {},
        resolve_hypothesis_name=lambda _name, _state: "hyp_external",
    )
    state = InvestigationState(
        investigation_id="inv_boundary",
        question="Why did sales fall?",
        datasources=[],
        hypotheses=[{
            "id": "hyp_external", "name": "External conditions", "description": "Outside conditions changed demand.",
            "category": "external", "research_scope": "external", "evidence_topics": ["weather.conditions"], "confidence": 0.2,
        }],
        hypothesis_name_index={"external conditions": "hyp_external"},
    )

    selected = await coordinator.select(state)
    await coordinator.execute(state.model_copy(update=selected))

    assert researcher.received is not None
    assert researcher.received[-1] == "2024-07-01"


def test_external_research_normalizes_a_reversed_before_after_window() -> None:
    result = ExternalResearchPlans(plans=[
        ExternalResearchPlan(
            hypothesis_name="Weather",
            agent_id="weather",
            subject="Milan, Italy",
            # This is the invalid shape returned by inv_8c2ec8d531db.
            start_date="2024-07-01",
            end_date="2024-07-31",
            comparison_start_date="2024-06-01",
            rationale="Compare June and July weather.",
        )
    ])

    normalized = ExternalResearchCoordinator._normalize_comparison_windows(result)

    assert normalized.plans[0].start_date == "2024-06-01"
    assert normalized.plans[0].comparison_start_date == "2024-07-01"
    assert normalized.plans[0].end_date == "2024-07-31"


class UnfixableExternalPlanLLM:
    async def structured(self, response_model, _instruction: str, _payload: dict[str, object]) -> ExternalResearchPlans:
        assert response_model is ExternalResearchPlans
        return ExternalResearchPlans(plans=[
            ExternalResearchPlan(
                hypothesis_name="External conditions",
                agent_id="unknown-agent",
                subject="Milan, Italy",
                start_date="2024-06-01",
                end_date="2024-07-31",
                rationale="An invalid configured agent.",
            )
        ])


@pytest.mark.asyncio
async def test_unfixable_external_plan_is_contained_without_crashing_the_investigation() -> None:
    async def emit(_state: InvestigationState, _event_type: str, _message: str, **_data: object) -> None:
        return None

    coordinator = ExternalResearchCoordinator(
        researcher=ExternalResearchFixture(),  # type: ignore[arg-type]
        llm=UnfixableExternalPlanLLM(),  # type: ignore[arg-type]
        emit=emit,
        state_payload=lambda _state: {},
        resolve_hypothesis_name=lambda _name, _state: "hyp_external",
    )
    state = InvestigationState(
        investigation_id="inv_unfixable_external_plan",
        question="Why did sales fall?",
        datasources=[],
        hypotheses=[{
            "id": "hyp_external", "name": "External conditions", "description": "Outside conditions changed demand.",
            "category": "external", "research_scope": "external", "evidence_topics": ["weather.conditions"], "confidence": 0.2,
        }],
        hypothesis_name_index={"external conditions": "hyp_external"},
    )

    selected = await coordinator.select(state)

    assert selected["external_request"] is None
    assert selected["next_action"] == "continue"
    assert selected["hypotheses"][0].status.value == "needs_more_evidence"
    assert selected["failed_checks"]
