"""Selection and execution of catalog-defined external research within an investigation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

from business_signals.config import settings
from business_signals.external import ExternalResearcher
from business_signals.llm import LLM, ExternalResearchPlans, ExternalResearchRelevance
from business_signals.models import (
    ExternalFinding,
    ExternalResearchCheck,
    HypothesisStatus,
    InvestigationState,
    InvestigationStep,
    Observation,
)
from business_signals.prompt_catalog import prompts

logger = logging.getLogger("uvicorn.error")

Emit = Callable[..., Awaitable[None]]
StatePayload = Callable[[InvestigationState], dict[str, Any]]
HypothesisResolver = Callable[[str, InvestigationState], str | None]


class ExternalResearchCoordinator:
    """Keep optional, bounded external evidence separate from graph SQL orchestration."""

    def __init__(
        self,
        researcher: ExternalResearcher,
        llm: LLM,
        emit: Emit,
        state_payload: StatePayload,
        resolve_hypothesis_name: HypothesisResolver,
    ) -> None:
        self.researcher = researcher
        self.llm = llm
        self._emit = emit
        self._state_payload = state_payload
        self._resolve_hypothesis_name = resolve_hypothesis_name

    def available_agents(self) -> list[dict[str, Any]]:
        return self.researcher.available_agents()

    def eligible_agents_by_hypothesis(
        self, state: InvestigationState, available_agents: list[dict[str, Any]] | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        """Return configured agents explicitly selected for each causal claim.

        New hypotheses contain agent IDs chosen from each agent's declared
        purpose and input contract.  This is generic: the workflow does not
        infer that a particular mechanism requires a particular provider. Topic
        matching remains only as an archive-compatibility fallback.
        """
        agents = available_agents if available_agents is not None else self.available_agents()
        checked_agents = {(check.hypothesis_id, check.agent_id) for check in state.external_research_checks}
        legacy_checks = self._legacy_executed_agents(state, agents)
        terminal = {
            HypothesisStatus.REJECTED,
            HypothesisStatus.CONFIRMED,
            HypothesisStatus.NEEDS_MORE_EVIDENCE,
        }
        eligible: dict[str, list[dict[str, Any]]] = {}
        for hypothesis in state.hypotheses:
            if hypothesis.research_scope != "external" or hypothesis.status in terminal:
                continue
            selected_ids = set(hypothesis.external_agent_ids)
            if not selected_ids:
                hypothesis_topics = {
                    str(topic).strip().casefold() for topic in hypothesis.evidence_topics if str(topic).strip()
                }
                selected_ids = {
                    str(agent["id"])
                    for agent in agents
                    if hypothesis_topics.intersection({
                        str(topic).strip().casefold()
                        for topic in agent.get("evidence_topics", [])
                        if str(topic).strip()
                    })
                }
            if not selected_ids:
                continue
            matching = [
                agent
                for agent in agents
                if str(agent["id"]) in selected_ids
                and (hypothesis.id, str(agent["id"])) not in checked_agents
                and (hypothesis.id, str(agent["id"])) not in legacy_checks
            ]
            if matching:
                eligible[hypothesis.id] = matching
        return eligible

    def has_eligible_check(
        self, state: InvestigationState, available_agents: list[dict[str, Any]] | None = None
    ) -> bool:
        return bool(self.eligible_agents_by_hypothesis(state, available_agents))

    @staticmethod
    def _request_key(
        hypothesis_id: str,
        agent_id: str,
        subject: str,
        start_date: str,
        end_date: str,
        comparison_start_date: str | None,
    ) -> tuple[str, str, str, str, str, str]:
        """Identify the actual external question, independent of planner wording."""
        return (
            hypothesis_id,
            agent_id,
            subject.strip().casefold(),
            start_date,
            end_date,
            comparison_start_date or "",
        )

    @classmethod
    def _recorded_request_keys(cls, state: InvestigationState) -> set[tuple[str, str, str, str, str, str]]:
        return {
            cls._request_key(
                check.hypothesis_id,
                check.agent_id,
                check.subject,
                check.start_date,
                check.end_date,
                check.comparison_start_date,
            )
            for check in state.external_research_checks
        }

    @staticmethod
    def _legacy_executed_agents(
        state: InvestigationState, available_agents: list[dict[str, Any]]
    ) -> set[tuple[str, str]]:
        """Avoid repeating agent calls in archives created before request keys existed."""
        completed: set[tuple[str, str]] = set()
        for agent in available_agents:
            expected_action = f"Check {str(agent['name']).casefold()}."
            for step in state.investigation_history:
                if step.hypothesis_id and step.action.casefold() == expected_action.casefold():
                    completed.add((step.hypothesis_id, str(agent["id"])))
        return completed

    @classmethod
    def _check_from_request(cls, request: dict[str, str], status: str) -> ExternalResearchCheck:
        return ExternalResearchCheck(
            hypothesis_id=request["hypothesis_id"],
            agent_id=request["agent_id"],
            subject=request["subject"],
            start_date=request["start_date"],
            end_date=request["end_date"],
            comparison_start_date=request.get("comparison_start_date"),
            status=status,
        )

    @staticmethod
    def _safe_failure_diagnostic(agent_id: str, error: BaseException) -> str:
        """Persist a useful, credential-safe reason for an unavailable agent."""
        response = getattr(error, "response", None)
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int):
            return f"{agent_id}: HTTP {status_code} ({type(error).__name__})"
        return f"{agent_id}: {type(error).__name__}"

    async def _select_relevant_finding(
        self,
        state: InvestigationState,
        request: dict[str, str],
        finding: ExternalFinding,
    ) -> ExternalFinding | None:
        """Use only a provider document that actually bears on the assigned claim."""
        if not finding.candidates:
            return finding
        hypothesis = next((item for item in state.hypotheses if item.id == request["hypothesis_id"]), None)
        if hypothesis is None:
            raise RuntimeError("External research request has no matching hypothesis")
        result = await self.llm.structured(
            ExternalResearchRelevance,
            prompts.load("external_research_relevance"),
            {
                "question": state.current_conversation_question or state.question,
                "hypothesis": hypothesis.model_dump(mode="json"),
                "request": request,
                "provider_candidates": [candidate.model_dump(mode="json") for candidate in finding.candidates],
            },
        )
        if result.relevance == "none":
            logger.info(
                "External research returned no relevant candidate: investigation=%s agent=%s candidates=%d rationale=%s",
                state.investigation_id,
                request["agent_id"],
                len(finding.candidates),
                result.rationale,
            )
            return None
        if result.candidate_index is None or result.candidate_index >= len(finding.candidates):
            raise ValueError("External relevance selection did not identify a returned candidate")
        candidate = finding.candidates[result.candidate_index]
        observation = candidate.title
        if candidate.summary:
            observation = f"{observation} — {candidate.summary}"
        return finding.model_copy(
            update={
                "observation": observation,
                "confidence": min(finding.confidence, result.confidence),
                "source_url": candidate.url,
                "source_title": (
                    f"{finding.source_title}: {candidate.section}"
                    if candidate.section and candidate.section != finding.source_title.removeprefix("The Guardian: ")
                    else finding.source_title
                ),
            }
        )

    @staticmethod
    def _normalize_comparison_windows(result: ExternalResearchPlans) -> ExternalResearchPlans:
        """Repair the unambiguous reversed representation of a before/after window.

        The contract is ``start_date`` = baseline start, ``comparison_start_date``
        = comparison start, and ``end_date`` = comparison end. Models sometimes
        express the same intent as a July window with a June comparison boundary.
        That is mechanically reversible without guessing a date.
        """
        plans = []
        for plan in result.plans:
            if not plan.comparison_start_date:
                plans.append(plan)
                continue
            try:
                start, boundary = date.fromisoformat(plan.start_date), date.fromisoformat(plan.comparison_start_date)
            except ValueError:
                plans.append(plan)
                continue
            if boundary < start:
                logger.info(
                    "Normalized reversed external comparison window: agent=%s baseline_start=%s comparison_start=%s end=%s",
                    plan.agent_id,
                    boundary.isoformat(),
                    start.isoformat(),
                    plan.end_date,
                )
                plans.append(
                    plan.model_copy(
                        update={"start_date": boundary.isoformat(), "comparison_start_date": start.isoformat()}
                    )
                )
            else:
                plans.append(plan)
        return result.model_copy(update={"plans": plans})

    async def select(self, state: InvestigationState) -> dict[str, Any]:
        available_agents = self.available_agents()
        if not available_agents or (
            not settings.ignore_investigation_limits
            and state.external_call_count >= state.limits.max_external_calls
        ):
            return {"external_request": None, "next_action": "continue"}
        eligible_by_hypothesis = self.eligible_agents_by_hypothesis(state, available_agents)
        external_hypotheses = [
            hypothesis for hypothesis in state.hypotheses if hypothesis.id in eligible_by_hypothesis
        ]
        if not external_hypotheses:
            logger.warning("External research was selected without an externally testable hypothesis")
            return {"external_request": None, "next_action": "continue"}
        valid_hypothesis_names = {hypothesis.name.casefold() for hypothesis in external_hypotheses}
        recorded_request_keys = self._recorded_request_keys(state)
        legacy_executed_agents = self._legacy_executed_agents(state, available_agents)
        maximum_checks = (
            len(available_agents)
            if settings.ignore_investigation_limits
            else min(len(available_agents), state.limits.max_external_calls - state.external_call_count)
        )
        result = await self.llm.structured(
            ExternalResearchPlans,
            prompts.load("external_research_plan"),
            {
                **self._state_payload(state),
                "available_research_agents": available_agents,
                "eligible_external_hypotheses": [
                    hypothesis.model_dump(mode="json") for hypothesis in external_hypotheses
                ],
                "eligible_external_agents_by_hypothesis": {
                    hypothesis.name: eligible_by_hypothesis[hypothesis.id]
                    for hypothesis in external_hypotheses
                },
                "completed_external_checks": [
                    check.model_dump(mode="json") for check in state.external_research_checks
                ],
                "maximum_checks": maximum_checks,
            },
        )
        result = self._normalize_comparison_windows(result)
        available_ids = {agent["id"] for agent in available_agents}

        def resolved_hypothesis_id(plan: Any) -> str | None:
            return self._resolve_hypothesis_name(plan.hypothesis_name, state)

        def is_new_request(plan: Any) -> bool:
            hypothesis_id = resolved_hypothesis_id(plan)
            if hypothesis_id is None:
                return False
            if (hypothesis_id, plan.agent_id) in legacy_executed_agents:
                return False
            return self._request_key(
                hypothesis_id,
                plan.agent_id,
                plan.subject,
                plan.start_date,
                plan.end_date,
                plan.comparison_start_date,
            ) not in recorded_request_keys

        def has_valid_agent_input(plan: Any) -> bool:
            validator = getattr(self.researcher, "validate_request", None)
            if validator is None:
                return True
            try:
                validator(
                    plan.agent_id,
                    plan.subject,
                    plan.start_date,
                    plan.end_date,
                    plan.comparison_start_date,
                )
            except ValueError as exc:
                logger.warning(
                    "External-research plan has invalid agent input: agent=%s error=%s",
                    plan.agent_id,
                    exc,
                )
                return False
            return True

        def valid(plans: list) -> bool:
            return (
                len(plans) <= maximum_checks
                and len({plan.agent_id for plan in plans}) == len(plans)
                and all(
                    plan.agent_id in available_ids
                    and plan.hypothesis_name.casefold() in valid_hypothesis_names
                    and resolved_hypothesis_id(plan) is not None
                    and plan.agent_id in {
                        str(agent["id"])
                        for agent in eligible_by_hypothesis.get(resolved_hypothesis_id(plan) or "", [])
                    }
                    and has_valid_agent_input(plan)
                    and is_new_request(plan)
                    for plan in plans
                )
            )

        if not valid(result.plans):
            logger.warning("External-research planner returned an invalid set of checks; requesting one correction")
            result = await self.llm.structured(
                ExternalResearchPlans,
                prompts.load("external_research_plan_correction"),
                {
                    **self._state_payload(state),
                    "available_research_agents": available_agents,
                    "valid_hypothesis_names": [hypothesis.name for hypothesis in external_hypotheses],
                    "eligible_external_agents_by_hypothesis": {
                        hypothesis.name: eligible_by_hypothesis[hypothesis.id]
                        for hypothesis in external_hypotheses
                    },
                    "maximum_checks": maximum_checks,
                    "completed_external_checks": [
                        check.model_dump(mode="json") for check in state.external_research_checks
                    ],
                },
            )
            result = self._normalize_comparison_windows(result)
        if not valid(result.plans):
            repeated_hypothesis_ids = {
                hypothesis_id
                for plan in result.plans
                if (hypothesis_id := resolved_hypothesis_id(plan)) is not None and not is_new_request(plan)
            }
            if repeated_hypothesis_ids:
                logger.warning(
                    "External-research planner repeated completed checks; continuing without duplicates: "
                    "investigation=%s hypotheses=%s",
                    state.investigation_id,
                    sorted(repeated_hypothesis_ids),
                )
                hypotheses = [
                    hypothesis.model_copy(update={"status": HypothesisStatus.NEEDS_MORE_EVIDENCE})
                    if hypothesis.id in repeated_hypothesis_ids
                    else hypothesis
                    for hypothesis in state.hypotheses
                ]
                for hypothesis_id in sorted(repeated_hypothesis_ids):
                    await self._emit(
                        state,
                        "ExternalResearchSkipped",
                        "This external check already ran with the same inputs, so it was not repeated.",
                        hypothesis_id=hypothesis_id,
                        error_code="DuplicateExternalResearch",
                    )
                return {
                    "external_request": None,
                    "hypotheses": hypotheses,
                    "failed_checks": [
                        *state.failed_checks,
                        "An identical external check had already completed, so the investigation continued without repeating it.",
                    ],
                    "next_action": "continue",
                }
            logger.error(
                "External-research planner did not return valid configured checks; continuing without external evidence: investigation=%s",
                state.investigation_id,
            )
            hypotheses = [
                hypothesis.model_copy(update={"status": HypothesisStatus.NEEDS_MORE_EVIDENCE})
                if hypothesis.research_scope == "external"
                else hypothesis
                for hypothesis in state.hypotheses
            ]
            return {
                "external_request": None,
                "hypotheses": hypotheses,
                "failed_checks": [
                    *state.failed_checks,
                    "An external comparison could not be planned safely, so it was not used as evidence.",
                ],
                "next_action": "continue",
            }
        requests = []
        for plan in result.plans:
            hypothesis_id = resolved_hypothesis_id(plan)
            assert hypothesis_id is not None  # validated above
            request = plan.model_dump(mode="json", exclude_none=True)
            request["hypothesis_id"] = hypothesis_id
            requests.append(request)
            await self._emit(
                state,
                "ExternalResearchSelected",
                plan.rationale,
                agent_id=plan.agent_id,
                hypothesis_id=hypothesis_id,
            )
        return {"external_request": requests, "current_focus": requests[0]["hypothesis_id"], "next_action": None}

    async def execute(self, state: InvestigationState) -> dict[str, Any]:
        raw_requests = state.external_request
        if not raw_requests:
            return {"next_action": "continue"}
        requests = raw_requests if isinstance(raw_requests, list) else [raw_requests]
        agents = {agent["id"]: agent for agent in self.available_agents()}

        async def run(
            request: dict[str, str],
        ) -> tuple[ExternalFinding | None, Observation | None, InvestigationStep]:
            agent_id = request["agent_id"]
            hypothesis_id = request["hypothesis_id"]
            agent = agents.get(agent_id)
            if agent is None:
                raise RuntimeError(f"Selected research agent is no longer available: {agent_id}")
            await self._emit(
                state,
                "ExternalResearchStarted",
                f"Checking {agent['name'].casefold()}.",
                agent_id=agent_id,
                hypothesis_id=hypothesis_id,
            )
            logger.info(
                "External research started: investigation=%s agent=%s subject=%r period=%s..%s",
                state.investigation_id,
                agent_id,
                request["subject"],
                request["start_date"],
                request["end_date"],
            )
            research_args = (
                agent_id,
                request["subject"],
                request["start_date"],
                request["end_date"],
                state.current_conversation_question or state.question,
            )
            # Preserve the five-argument research-agent contract for an ordinary
            # historical lookup. A comparison boundary is an opt-in extension for
            # agents that expose a time series.
            if request.get("comparison_start_date"):
                finding = await self.researcher.research(
                    *research_args, request["comparison_start_date"]
                )
            else:
                finding = await self.researcher.research(*research_args)
            finding = await self._select_relevant_finding(state, request, finding)
            step = InvestigationStep(
                iteration=state.iteration + 1,
                hypothesis_id=hypothesis_id,
                action=f"Check {agent['name'].casefold()}.",
                rationale=request["rationale"],
                expected_information_gain=0.5,
            )
            if finding is None:
                await self._emit(
                    state,
                    "ExternalResearchNoRelevantResult",
                    "The external search returned no result relevant enough to use as evidence.",
                    agent_id=agent_id,
                    hypothesis_id=hypothesis_id,
                )
                return None, None, step
            logger.info(
                "External research completed: investigation=%s agent=%s finding=%s",
                state.investigation_id,
                agent_id,
                finding.model_dump(mode="json"),
            )
            await self._emit(
                state,
                "ExternalResearchCompleted",
                finding.observation,
                agent_id=agent_id,
                hypothesis_id=hypothesis_id,
                finding=finding.model_dump(mode="json"),
            )
            return (
                finding,
                Observation(
                    description=finding.observation,
                    # Ownership is set by the selected external hypothesis,
                    # not re-guessed when the finding is interpreted.
                    value={
                        "external_finding": finding.model_dump(mode="json"),
                        "hypothesis_id": hypothesis_id,
                    },
                    source=f"external:{finding.type}",
                ),
                step,
            )

        results = await asyncio.gather(*(run(request) for request in requests), return_exceptions=True)
        successful = []
        checks: list[ExternalResearchCheck] = []
        failed_hypothesis_ids: list[str] = []
        failed_external_checks: list[dict[str, str]] = []
        for request, result in zip(requests, results, strict=True):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                logger.error(
                    "External research check failed; continuing with other hypotheses: investigation=%s agent=%s",
                    state.investigation_id,
                    request["agent_id"],
                    exc_info=(type(result), result, result.__traceback__),
                )
                failed_hypothesis_ids.append(request["hypothesis_id"])
                checks.append(self._check_from_request(request, "unavailable"))
                diagnostic = self._safe_failure_diagnostic(request["agent_id"], result)
                failed_external_checks.append(
                    {"hypothesis_id": request["hypothesis_id"], "diagnostic": diagnostic}
                )
                await self._emit(
                    state,
                    "ExternalResearchUnavailable",
                    "This external check was unavailable, so the investigation will continue with other explanations.",
                    agent_id=request["agent_id"],
                    hypothesis_id=request["hypothesis_id"],
                    error_code=type(result).__name__,
                    diagnostic=diagnostic,
                )
                continue
            successful.append(result)
            checks.append(self._check_from_request(request, "completed"))
        if not successful:
            return {
                "external_request": None,
                "external_call_count": state.external_call_count + len(requests),
                "external_research_checks": [*state.external_research_checks, *checks],
                "failed_external_hypothesis_ids": failed_hypothesis_ids,
                "failed_external_checks": failed_external_checks,
                "next_action": "continue",
            }
        findings = [finding for finding, _observation, _step in successful if finding is not None]
        observations = [observation for _finding, observation, _step in successful if observation is not None]
        steps = [step for _finding, _observation, step in successful]
        return {
            "external_request": None,
            "external_call_count": state.external_call_count + len(requests),
            "external_research_checks": [*state.external_research_checks, *checks],
            "external_findings": [*state.external_findings, *findings],
            "observations": [*state.observations, *observations],
            "investigation_history": [*state.investigation_history, *steps],
            "pending_step": None,
            "current_focus": steps[-1].hypothesis_id,
            "failed_external_hypothesis_ids": failed_hypothesis_ids,
            "failed_external_checks": failed_external_checks,
            "next_action": None,
        }
