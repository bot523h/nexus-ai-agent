from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Sequence
from typing import Any

import pytest
from pydantic import ValidationError

from nexus_ai_agent.nagar.cognition.context import (
    CognitionContext,
    ProducerIdentity,
    ProposalSchema,
)
from nexus_ai_agent.nagar.cognition.proposal import (
    ProposalProvenance,
    Refusal,
    RefusalReason,
    TypedProposal,
)
from nexus_ai_agent.nagar.cognition.router import IntentClass, RoutingRequest
from nexus_ai_agent.nagar.director import (
    Director,
    DirectorBudget,
    DirectorFailureCode,
    DirectorScope,
    DirectorStatus,
    SafetyVetoDecision,
)

PROJECT_ID = "project-a"
SUBJECT_ID = "job-a"
SCHEMA_ID = "nagar.gateway.proposal.v1"
OPERATIONS = frozenset({"timeline.trim", "timeline.mark"})


def _proposal(
    operation: str = "timeline.trim",
    input_data: dict[str, Any] | None = None,
    *,
    rationale: str = "private model rationale",
) -> TypedProposal:
    return TypedProposal(
        schema_id=SCHEMA_ID,
        schema_version=1,
        operation=operation,
        input=input_data or {"start_us": 1_000_000, "end_us": 2_000_000},
        rationale=rationale,
        confidence=0.9,
        provenance=ProposalProvenance(
            producer=ProducerIdentity(kind="test", name="fake"),
            created_at="2026-10-29T00:00:00Z",
            source="unit-test",
        ),
    )


def _context(
    *,
    project_id: str = PROJECT_ID,
    subject_id: str = SUBJECT_ID,
    extra: dict[str, Any] | None = None,
) -> CognitionContext:
    facts: dict[str, Any] = {"project_id": project_id, **(extra or {})}
    return CognitionContext(
        subject_id=subject_id,
        intent_text="sensitive source prompt that must not be copied to the result",
        deterministic_facts=facts,
    )


def _request() -> RoutingRequest:
    return RoutingRequest(intent_class=IntentClass.OPEN_ENDED, local_model_available=True)


def _schema(operations: frozenset[str] = OPERATIONS) -> ProposalSchema:
    return ProposalSchema(
        schema_id=SCHEMA_ID,
        schema_version=1,
        allowed_operations=operations,
    )


class _Gateway:
    project_id = PROJECT_ID

    def __init__(self, responses: Sequence[object]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[CognitionContext, frozenset[str] | None, str | None]] = []

    async def propose(
        self,
        context: CognitionContext,
        request: RoutingRequest,
        *,
        requested_operations: frozenset[str] | None = None,
        idempotency_key: str | None = None,
    ) -> object:
        del request
        self.calls.append((context, requested_operations, idempotency_key))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            result = response()
            if asyncio.iscoroutine(result):
                return await result
            return result
        return response

    async def run(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("Director must never call a gateway execution method")


class _ClearVeto:
    def check(self, *, scope: DirectorScope, candidate: object) -> SafetyVetoDecision:
        del scope, candidate
        return SafetyVetoDecision(veto=False, reason_code="clear")


class _Veto:
    def check(self, *, scope: DirectorScope, candidate: object) -> SafetyVetoDecision:
        del scope, candidate
        return SafetyVetoDecision(veto=True, reason_code="host_safety_veto")


class _SlowVeto:
    def check(self, *, scope: DirectorScope, candidate: object) -> SafetyVetoDecision:
        del scope, candidate
        time.sleep(1.2)
        return SafetyVetoDecision(veto=False, reason_code="clear")


def _director(
    gateway: _Gateway,
    *,
    veto: object | None = None,
    budget: DirectorBudget | None = None,
    schema: ProposalSchema | None = None,
) -> Director:
    return Director(
        proposal_gateway=gateway,
        proposal_schema=schema or _schema(),
        safety_veto=veto or _ClearVeto(),
        budget=budget or DirectorBudget(),
    )


@pytest.mark.asyncio
async def test_preview_calls_only_propose_and_returns_minimized_non_executable_data() -> None:
    gateway = _Gateway([_proposal(rationale="do not copy me") for _ in range(5)])
    director = _director(gateway)

    result = await director.preview(
        scope=DirectorScope(project_id=PROJECT_ID, subject_id=SUBJECT_ID),
        context=_context(),
        request=_request(),
        requested_operations=OPERATIONS,
        idempotency_key="request-correlation-only",
    )

    assert result.status is DirectorStatus.PREVIEW_READY
    assert result.selected_candidate_id == result.candidates[0].candidate_id
    assert len(gateway.calls) == 5
    assert all(call[1] == OPERATIONS for call in gateway.calls)
    assert [call[2] for call in gateway.calls] == [
        f"request-correlation-only:candidate:{index}" for index in range(5)
    ]
    assert result.candidates[0].support_count == 5
    assert result.dispatchable is False
    assert result.execution_authority == "none"
    output = json.dumps(result.model_dump(mode="json"), sort_keys=True)
    assert "private model rationale" not in output
    assert "sensitive source prompt" not in output
    assert "provenance" not in output
    assert "execution_policy" not in output
    assert "command_id" not in output
    assert "queue_id" not in output


@pytest.mark.asyncio
async def test_foreign_project_or_tenant_scope_fails_before_proposal() -> None:
    gateway = _Gateway([_proposal() for _ in range(5)])
    director = _director(gateway)

    foreign = await director.preview(
        scope=DirectorScope(project_id="project-b", subject_id=SUBJECT_ID),
        context=_context(),
        request=_request(),
        requested_operations=OPERATIONS,
    )
    tenant = await director.preview(
        scope=DirectorScope(project_id=PROJECT_ID, subject_id=SUBJECT_ID, tenant_id="tenant-a"),
        context=_context(),
        request=_request(),
        requested_operations=OPERATIONS,
    )

    assert foreign.status is DirectorStatus.REJECTED
    assert foreign.failure_code is DirectorFailureCode.INVALID_SCOPE
    assert tenant.status is DirectorStatus.REJECTED
    assert tenant.failure_code is DirectorFailureCode.TENANT_SCOPE_UNAVAILABLE
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_project_fact_and_subject_must_match_the_bound_scope() -> None:
    gateway = _Gateway([_proposal() for _ in range(5)])
    director = _director(gateway)

    result = await director.preview(
        scope=DirectorScope(project_id=PROJECT_ID, subject_id=SUBJECT_ID),
        context=_context(project_id="project-foreign"),
        request=_request(),
        requested_operations=OPERATIONS,
    )

    assert result.status is DirectorStatus.REJECTED
    assert result.failure_code is DirectorFailureCode.INVALID_SCOPE
    assert gateway.calls == []

    narrow_gateway = _Gateway([_proposal("timeline.trim")])
    narrow_director = _director(
        narrow_gateway,
        budget=DirectorBudget(max_candidates=1, max_total_calls=1),
    )
    disallowed = await narrow_director.preview(
        scope=DirectorScope(project_id=PROJECT_ID, subject_id=SUBJECT_ID),
        context=_context(),
        request=_request(),
        requested_operations=frozenset({"timeline.mark"}),
    )
    assert disallowed.status is DirectorStatus.REJECTED
    assert disallowed.failure_code is DirectorFailureCode.MALFORMED_PROPOSAL
    assert disallowed.candidates == ()


@pytest.mark.asyncio
async def test_incomplete_or_unavailable_memory_scope_is_not_sent_to_proposer() -> None:
    gateway = _Gateway([_proposal() for _ in range(5)])
    director = _director(gateway)

    result = await director.preview(
        scope=DirectorScope(project_id=PROJECT_ID, subject_id=SUBJECT_ID),
        context=_context(extra={"memory_status": "INCOMPLETE", "tenant_scope": "unavailable"}),
        request=_request(),
        requested_operations=OPERATIONS,
    )

    assert result.status is DirectorStatus.REJECTED
    assert result.failure_code is DirectorFailureCode.TENANT_SCOPE_UNAVAILABLE
    assert gateway.calls == []

    incomplete = await director.preview(
        scope=DirectorScope(project_id=PROJECT_ID, subject_id=SUBJECT_ID),
        context=_context(extra={"memory_status": "INCOMPLETE"}),
        request=_request(),
        requested_operations=OPERATIONS,
    )
    assert incomplete.status is DirectorStatus.REJECTED
    assert incomplete.failure_code is DirectorFailureCode.INCOMPLETE_SOURCE_EVIDENCE
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_explicit_producer_refusal_is_a_typed_clarification() -> None:
    refusal = Refusal(reason=RefusalReason.PRODUCER_REFUSED, detail="provider declined")
    gateway = _Gateway([refusal])
    director = _director(gateway, budget=DirectorBudget(max_candidates=1, max_total_calls=1))

    result = await director.preview(
        scope=DirectorScope(project_id=PROJECT_ID, subject_id=SUBJECT_ID),
        context=_context(),
        request=_request(),
        requested_operations=OPERATIONS,
    )

    assert result.status is DirectorStatus.CLARIFICATION_REQUIRED
    assert result.failure_code is DirectorFailureCode.PROPOSAL_REFUSED
    assert result.candidates == ()
    assert "provider declined" not in json.dumps(result.model_dump(mode="json"))


@pytest.mark.asyncio
async def test_gateway_timeout_and_call_budget_exhaustion_are_typed_failures() -> None:
    timeout_gateway = _Gateway([asyncio.TimeoutError()])
    timeout_director = _director(
        timeout_gateway,
        budget=DirectorBudget(max_candidates=1, max_total_calls=1),
    )
    timeout = await timeout_director.preview(
        scope=DirectorScope(project_id=PROJECT_ID, subject_id=SUBJECT_ID),
        context=_context(),
        request=_request(),
        requested_operations=OPERATIONS,
    )

    exhausted_gateway = _Gateway([_proposal() for _ in range(5)])
    exhausted_director = _director(
        exhausted_gateway,
        budget=DirectorBudget(max_candidates=5, max_total_calls=2),
    )
    exhausted = await exhausted_director.preview(
        scope=DirectorScope(project_id=PROJECT_ID, subject_id=SUBJECT_ID),
        context=_context(),
        request=_request(),
        requested_operations=OPERATIONS,
    )

    assert timeout.status is DirectorStatus.FAILED
    assert timeout.failure_code is DirectorFailureCode.PROPOSAL_TIMEOUT
    assert timeout_gateway.calls and len(timeout_gateway.calls) == 1
    assert exhausted.status is DirectorStatus.FAILED
    assert exhausted.failure_code is DirectorFailureCode.BUDGET_EXHAUSTED
    assert len(exhausted_gateway.calls) == 2
    assert exhausted.candidates == ()

    async def slow_response() -> TypedProposal:
        await asyncio.sleep(2.0)
        return _proposal()

    slow_gateway = _Gateway([slow_response])
    slow_director = _director(
        slow_gateway,
        budget=DirectorBudget(
            max_candidates=1,
            max_total_calls=1,
            max_call_timeout_seconds=1,
            max_total_wall_clock_seconds=5,
        ),
    )
    slow = await slow_director.preview(
        scope=DirectorScope(project_id=PROJECT_ID, subject_id=SUBJECT_ID),
        context=_context(),
        request=_request(),
        requested_operations=OPERATIONS,
    )
    assert slow.status is DirectorStatus.FAILED
    assert slow.failure_code is DirectorFailureCode.PROPOSAL_TIMEOUT
    assert slow.candidates == ()

    async def deadline_response() -> TypedProposal:
        await asyncio.sleep(0.6)
        return _proposal()

    deadline_gateway = _Gateway([deadline_response, deadline_response])
    deadline_director = _director(
        deadline_gateway,
        budget=DirectorBudget(
            max_candidates=2,
            max_total_calls=2,
            max_call_timeout_seconds=2,
            max_total_wall_clock_seconds=1,
        ),
    )
    deadline_result = await deadline_director.preview(
        scope=DirectorScope(project_id=PROJECT_ID, subject_id=SUBJECT_ID),
        context=_context(),
        request=_request(),
        requested_operations=OPERATIONS,
    )
    assert deadline_result.status is DirectorStatus.FAILED
    assert deadline_result.failure_code is DirectorFailureCode.BUDGET_EXHAUSTED
    assert deadline_result.candidates == ()


@pytest.mark.asyncio
async def test_synchronous_safety_veto_is_bounded_by_total_deadline() -> None:
    gateway = _Gateway([_proposal()])
    director = _director(
        gateway,
        veto=_SlowVeto(),
        budget=DirectorBudget(
            max_candidates=1,
            max_total_calls=1,
            max_call_timeout_seconds=2,
            max_total_wall_clock_seconds=1,
        ),
    )

    result = await director.preview(
        scope=DirectorScope(project_id=PROJECT_ID, subject_id=SUBJECT_ID),
        context=_context(),
        request=_request(),
        requested_operations=OPERATIONS,
    )

    assert result.status is DirectorStatus.FAILED
    assert result.failure_code is DirectorFailureCode.BUDGET_EXHAUSTED
    assert result.candidates == ()


@pytest.mark.asyncio
async def test_synchronous_safety_veto_call_timeout_is_typed() -> None:
    director = _director(
        _Gateway([_proposal()]),
        veto=_SlowVeto(),
        budget=DirectorBudget(
            max_candidates=1,
            max_total_calls=1,
            max_call_timeout_seconds=1,
            max_total_wall_clock_seconds=5,
        ),
    )

    result = await director.preview(
        scope=DirectorScope(project_id=PROJECT_ID, subject_id=SUBJECT_ID),
        context=_context(),
        request=_request(),
        requested_operations=OPERATIONS,
    )

    assert result.status is DirectorStatus.FAILED
    assert result.failure_code is DirectorFailureCode.SAFETY_CHECK_TIMEOUT
    assert result.candidates == ()


def test_director_budget_cannot_exceed_global_candidate_and_round_caps() -> None:
    with pytest.raises(ValidationError):
        DirectorBudget(max_candidates=6)
    with pytest.raises(ValidationError):
        DirectorBudget(max_review_rounds=4)
