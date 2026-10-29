from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest

from nexus_ai_agent.nagar.cognition.context import (
    CognitionContext,
    ProducerIdentity,
    ProposalSchema,
)
from nexus_ai_agent.nagar.cognition.proposal import ProposalProvenance, TypedProposal
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


def _proposal(operation: str, input_data: dict[str, Any]) -> TypedProposal:
    return TypedProposal(
        schema_id=SCHEMA_ID,
        schema_version=1,
        operation=operation,
        input=input_data,
        rationale="discarded rationale",
        confidence=0.99,
        provenance=ProposalProvenance(
            producer=ProducerIdentity(kind="test", name="vote"),
            created_at="2026-10-29T00:00:00Z",
            source="test",
        ),
    )


def _trim(value: int = 1) -> TypedProposal:
    return _proposal("timeline.trim", {"start_us": value, "end_us": value + 1})


def _mark(value: int = 1) -> TypedProposal:
    return _proposal("timeline.mark", {"at_us": value, "label": "cut"})


class _Gateway:
    project_id = PROJECT_ID

    def __init__(self, responses: Sequence[object]) -> None:
        self.responses = list(responses)
        self.calls = 0

    async def propose(
        self,
        context: CognitionContext,
        request: RoutingRequest,
        *,
        requested_operations: frozenset[str] | None = None,
        idempotency_key: str | None = None,
    ) -> object:
        del context, request, requested_operations, idempotency_key
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            result = response()
            if asyncio.iscoroutine(result):
                return await result
            return result
        return response


class _ClearVeto:
    def check(self, *, scope: DirectorScope, candidate: object) -> SafetyVetoDecision:
        del scope, candidate
        return SafetyVetoDecision(veto=False, reason_code="clear")


class _AbsoluteVeto:
    def check(self, *, scope: DirectorScope, candidate: object) -> SafetyVetoDecision:
        del scope, candidate
        return SafetyVetoDecision(veto=True, reason_code="host_safety_veto")


class _MalformedVeto:
    def check(self, *, scope: DirectorScope, candidate: object) -> object:
        del scope, candidate
        return {"veto": False, "reason_code": "clear", "grant": "execute"}


def _director(
    responses: Sequence[object],
    *,
    veto: object | None = None,
    budget: DirectorBudget | None = None,
) -> tuple[Director, _Gateway]:
    gateway = _Gateway(responses)
    director = Director(
        proposal_gateway=gateway,
        proposal_schema=ProposalSchema(
            schema_id=SCHEMA_ID,
            schema_version=1,
            allowed_operations=OPERATIONS,
        ),
        safety_veto=veto or _ClearVeto(),
        budget=budget or DirectorBudget(),
    )
    return director, gateway


def _inputs() -> tuple[DirectorScope, CognitionContext, RoutingRequest]:
    scope = DirectorScope(project_id=PROJECT_ID, subject_id=SUBJECT_ID)
    context = CognitionContext(
        subject_id=SUBJECT_ID,
        intent_text="trim the source",
        deterministic_facts={"project_id": PROJECT_ID},
    )
    request = RoutingRequest(intent_class=IntentClass.OPEN_ENDED, local_model_available=True)
    return scope, context, request


async def _preview(director: Director):
    scope, context, request = _inputs()
    return await director.preview(
        scope=scope,
        context=context,
        request=request,
        requested_operations=OPERATIONS,
    )


@pytest.mark.asyncio
async def test_duplicate_proposals_form_a_stable_majority_and_order() -> None:
    ballots = [_trim(), _mark(), _trim(), _trim(), _mark()]
    first_director, _ = _director(ballots)
    second_director, _ = _director([_mark(), _trim(), _mark(), _trim(), _trim()])

    first = await _preview(first_director)
    second = await _preview(second_director)

    assert first.status is DirectorStatus.PREVIEW_READY
    assert first.selected_candidate_id == second.selected_candidate_id
    assert first.selected_candidate_id is not None
    selected = next(
        item for item in first.candidates if item.candidate_id == first.selected_candidate_id
    )
    assert selected.operation == "timeline.trim"
    assert selected.support_count == 3
    assert first.review_rounds == 3
    assert [item.candidate_id for item in first.candidates] == [
        item.candidate_id for item in second.candidates
    ]
    assert all(item.vetoed is False for item in first.candidates)


@pytest.mark.asyncio
async def test_tied_or_split_votes_require_clarification_not_best_effort_selection() -> None:
    director, _ = _director([_trim(1), _mark(1), _trim(1), _mark(1), _trim(2)])

    result = await _preview(director)

    assert result.status is DirectorStatus.CLARIFICATION_REQUIRED
    assert result.failure_code is DirectorFailureCode.NO_CONSENSUS
    assert result.selected_candidate_id is None
    assert result.review_rounds == 3
    assert [candidate.support_count for candidate in result.candidates] == [2, 2, 1]


@pytest.mark.asyncio
async def test_absolute_safety_veto_beats_unanimous_majority() -> None:
    director, gateway = _director([_trim() for _ in range(5)], veto=_AbsoluteVeto())

    result = await _preview(director)

    assert gateway.calls == 5
    assert result.status is DirectorStatus.REJECTED
    assert result.failure_code is DirectorFailureCode.SAFETY_VETO
    assert result.selected_candidate_id is None
    assert result.candidates[0].support_count == 5
    assert result.candidates[0].vetoed is True


@pytest.mark.asyncio
async def test_unknown_or_malformed_safety_result_fails_closed() -> None:
    director, _ = _director([_trim() for _ in range(5)], veto=_MalformedVeto())

    result = await _preview(director)

    assert result.status is DirectorStatus.FAILED
    assert result.failure_code is DirectorFailureCode.SAFETY_CHECK_FAILED
    assert result.selected_candidate_id is None


@pytest.mark.asyncio
async def test_authority_bearing_malformed_candidate_is_rejected_without_partial_winner() -> None:
    malformed = {
        "schema_id": SCHEMA_ID,
        "schema_version": 1,
        "operation": "timeline.trim",
        "input": {"start_us": 1, "end_us": 2},
        "rationale": "untrusted",
        "confidence": 1.0,
        "permissions": ["project:write"],
        "provenance": {
            "producer": {"kind": "test", "name": "bad"},
            "created_at": "2026-10-29T00:00:00Z",
            "source": "test",
        },
    }
    director, gateway = _director([_trim(), malformed])

    result = await _preview(director)

    assert gateway.calls == 2
    assert result.status is DirectorStatus.REJECTED
    assert result.failure_code is DirectorFailureCode.MALFORMED_PROPOSAL
    assert result.selected_candidate_id is None
    assert result.candidates == ()


@pytest.mark.asyncio
async def test_forged_typed_proposal_is_revalidated_before_consensus() -> None:
    forged = _trim().model_copy(update={"schema_version": 99, "confidence": 1.5})
    director, gateway = _director(
        [forged],
        budget=DirectorBudget(max_candidates=1, max_total_calls=1),
    )

    result = await _preview(director)

    assert gateway.calls == 1
    assert result.status is DirectorStatus.REJECTED
    assert result.failure_code is DirectorFailureCode.MALFORMED_PROPOSAL
    assert result.selected_candidate_id is None
    assert result.candidates == ()


@pytest.mark.asyncio
async def test_deeply_nested_malformed_proposal_returns_typed_rejection() -> None:
    nested: dict[str, Any] = {}
    cursor = nested
    for _ in range(10_000):
        child: dict[str, Any] = {}
        cursor["nested"] = child
        cursor = child
    malformed = _trim().model_dump(mode="json")
    malformed["input"]["nested"] = nested
    director, gateway = _director(
        [malformed],
        budget=DirectorBudget(max_candidates=1, max_total_calls=1),
    )

    result = await _preview(director)

    assert gateway.calls == 1
    assert result.status is DirectorStatus.REJECTED
    assert result.failure_code is DirectorFailureCode.MALFORMED_PROPOSAL
    assert result.selected_candidate_id is None
    assert result.candidates == ()


@pytest.mark.asyncio
async def test_candidate_payload_resource_limit_is_typed_and_never_selected() -> None:
    director, _ = _director(
        [_proposal("timeline.mark", {"label": "a value larger than the cap"}) for _ in range(5)],
        budget=DirectorBudget(max_candidate_input_bytes=8),
    )

    result = await _preview(director)

    assert result.status is DirectorStatus.FAILED
    assert result.failure_code is DirectorFailureCode.BUDGET_EXHAUSTED
    assert result.selected_candidate_id is None
    assert result.candidates == ()
