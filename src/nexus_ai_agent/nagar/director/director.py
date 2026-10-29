from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Protocol

from pydantic import ValidationError

from nexus_ai_agent.nagar.cognition.context import CognitionContext, ProposalSchema
from nexus_ai_agent.nagar.cognition.proposal import Refusal, TypedProposal, parse_proposal
from nexus_ai_agent.nagar.cognition.router import RoutingRequest
from nexus_ai_agent.nagar.director.models import (
    DirectorBudget,
    DirectorCandidatePreview,
    DirectorFailureCode,
    DirectorOutcome,
    DirectorScope,
    DirectorStatus,
    SafetyVetoDecision,
)


class ProposalOnlyGateway(Protocol):
    """Narrow structural view of CognitionGateway; execution methods are absent."""

    @property
    def project_id(self) -> str: ...

    async def propose(
        self,
        context: CognitionContext,
        request: RoutingRequest,
        *,
        requested_operations: frozenset[str] | None = None,
        idempotency_key: str | None = None,
    ) -> object: ...


class SafetyVeto(Protocol):
    """Host-owned, local, negative-only safety check for one preview candidate.

    Implementations should be pure and bounded. The Director runs this
    synchronous predicate off the event loop and applies the remaining preview
    deadline while awaiting it.
    """

    def check(self, *, scope: DirectorScope, candidate: DirectorCandidatePreview) -> object: ...


class Director:
    """Produce an ephemeral preview and consensus; never create or dispatch a command.

    The host must authorize/project the input context before calling this class.
    The context must carry its exact ``project_id`` and ``subject_id``. This
    component does not resolve membership, tenant identity, capabilities, policy,
    queue state, or artifact truth. A project-to-tenant mapping is not currently
    available, so tenant-scoped requests fail closed.

    Up to five sequential gateway ``propose`` calls are made. Returned values are
    revalidated against a fixed ProposalSchema; prompt text, model rationale, and
    proposal provenance are not copied into the outcome. Three pure review passes
    are bounded: proposal validation, negative-only safety veto, and deterministic
    majority consensus. A safety veto cannot be outvoted. Ties are ordered by
    support count descending, then operation and canonical JSON input ascending;
    a candidate is selected only with a strict majority of all proposal calls.
    """

    def __init__(
        self,
        *,
        proposal_gateway: ProposalOnlyGateway,
        proposal_schema: ProposalSchema,
        safety_veto: SafetyVeto,
        budget: DirectorBudget | None = None,
    ) -> None:
        self._proposal_gateway = proposal_gateway
        self._proposal_schema = proposal_schema
        self._safety_veto = safety_veto
        self._budget = budget if budget is not None else DirectorBudget()

    async def preview(
        self,
        *,
        scope: DirectorScope,
        context: CognitionContext,
        request: RoutingRequest,
        requested_operations: frozenset[str],
        idempotency_key: str | None = None,
    ) -> DirectorOutcome:
        scope_failure = self._validate_scope(scope=scope, context=context)
        if scope_failure is not None:
            return scope_failure
        if self._proposal_gateway.project_id != scope.project_id:
            return self._rejected(DirectorFailureCode.INVALID_SCOPE)

        allowed_operations = self._proposal_schema.allowed_operations.intersection(
            requested_operations
        )
        if not allowed_operations:
            return self._rejected(DirectorFailureCode.NO_OPERATIONS)
        schema = ProposalSchema(
            schema_id=self._proposal_schema.schema_id,
            schema_version=self._proposal_schema.schema_version,
            allowed_operations=allowed_operations,
            input_schema=self._proposal_schema.input_schema,
        )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._budget.max_total_wall_clock_seconds
        grouped: dict[str, tuple[str, dict[str, object], int]] = {}
        for index in range(self._budget.max_candidates):
            if index >= self._budget.max_total_calls:
                return self._failed(DirectorFailureCode.BUDGET_EXHAUSTED)
            remaining = deadline - loop.time()
            if remaining <= 0:
                return self._failed(DirectorFailureCode.BUDGET_EXHAUSTED)
            timeout = min(float(self._budget.max_call_timeout_seconds), remaining)
            call_key = (
                f"{idempotency_key}:candidate:{index}" if idempotency_key is not None else None
            )
            try:
                raw = await asyncio.wait_for(
                    self._proposal_gateway.propose(
                        context,
                        request,
                        requested_operations=allowed_operations,
                        idempotency_key=call_key,
                    ),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                if deadline - loop.time() <= 0:
                    return self._failed(DirectorFailureCode.BUDGET_EXHAUSTED)
                return self._failed(DirectorFailureCode.PROPOSAL_TIMEOUT)
            except Exception:
                return self._failed(DirectorFailureCode.PROPOSAL_FAILED)

            if isinstance(raw, Refusal):
                return self._clarification(DirectorFailureCode.PROPOSAL_REFUSED)
            try:
                payload = raw.model_dump(mode="json") if isinstance(raw, TypedProposal) else raw
                parsed = parse_proposal(payload, schema)
            except (TypeError, ValueError, ValidationError, RecursionError):
                return self._rejected(DirectorFailureCode.MALFORMED_PROPOSAL)
            if isinstance(parsed, Refusal) or not isinstance(parsed, TypedProposal):
                return self._rejected(DirectorFailureCode.MALFORMED_PROPOSAL)

            try:
                canonical_input = self._canonical_json(parsed.input)
                if len(canonical_input.encode("utf-8")) > self._budget.max_candidate_input_bytes:
                    return self._failed(DirectorFailureCode.BUDGET_EXHAUSTED)
                canonical_candidate = self._canonical_json(
                    {"operation": parsed.operation, "input": parsed.input}
                )
            except (TypeError, ValueError, RecursionError):
                return self._rejected(DirectorFailureCode.MALFORMED_PROPOSAL)
            existing = grouped.get(canonical_candidate)
            if existing is None:
                candidate_id = (
                    "candidate:" + hashlib.sha256(canonical_candidate.encode("utf-8")).hexdigest()
                )
                grouped[canonical_candidate] = (candidate_id, parsed.input, 1)
            else:
                grouped[canonical_candidate] = (existing[0], existing[1], existing[2] + 1)

        candidates = self._ordered_candidates(grouped)
        # Review pass 1: all proposals were parsed and bounded before any result
        # can be selected. Do not expose a partial set after a producer failure.
        review_rounds = 1

        checked: list[DirectorCandidatePreview] = []
        for candidate in candidates:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return self._failed(DirectorFailureCode.BUDGET_EXHAUSTED, review_rounds=1)
            timeout = min(float(self._budget.max_call_timeout_seconds), remaining)
            try:
                raw_verdict = await asyncio.wait_for(
                    asyncio.to_thread(self._safety_veto.check, scope=scope, candidate=candidate),
                    timeout=timeout,
                )
                verdict = self._validate_veto(raw_verdict)
            except asyncio.TimeoutError:
                failure_code = (
                    DirectorFailureCode.BUDGET_EXHAUSTED
                    if deadline - loop.time() <= 0
                    else DirectorFailureCode.SAFETY_CHECK_TIMEOUT
                )
                return self._failed(failure_code, review_rounds=1)
            except Exception:
                return self._failed(DirectorFailureCode.SAFETY_CHECK_FAILED, review_rounds=1)
            checked.append(
                DirectorCandidatePreview.model_validate(
                    {
                        **candidate.model_dump(mode="python"),
                        "vetoed": verdict.veto,
                        "veto_code": verdict.reason_code if verdict.veto else None,
                    },
                    strict=True,
                )
            )
        review_rounds = 2
        checked_candidates = tuple(checked)
        if any(candidate.vetoed for candidate in checked_candidates):
            return DirectorOutcome(
                status=DirectorStatus.REJECTED,
                candidates=checked_candidates,
                failure_code=DirectorFailureCode.SAFETY_VETO,
                review_rounds=review_rounds,
            )

        # Review pass 3: strict-majority consensus only. Confidence, timestamps,
        # rationale, and input order are not votes or tie-breakers.
        review_rounds = 3
        quorum = self._budget.max_candidates // 2 + 1
        winner = checked_candidates[0]
        if winner.support_count < quorum:
            return DirectorOutcome(
                status=DirectorStatus.CLARIFICATION_REQUIRED,
                candidates=checked_candidates,
                failure_code=DirectorFailureCode.NO_CONSENSUS,
                review_rounds=review_rounds,
            )
        return DirectorOutcome(
            status=DirectorStatus.PREVIEW_READY,
            candidates=checked_candidates,
            selected_candidate_id=winner.candidate_id,
            review_rounds=review_rounds,
        )

    @staticmethod
    def _validate_scope(
        *, scope: DirectorScope, context: CognitionContext
    ) -> DirectorOutcome | None:
        if (
            scope.tenant_id is not None
            or "tenant_id" in context.deterministic_facts
            or "tenant_scope" in context.deterministic_facts
        ):
            return Director._rejected(DirectorFailureCode.TENANT_SCOPE_UNAVAILABLE)
        if (
            context.subject_id != scope.subject_id
            or context.deterministic_facts.get("project_id") != scope.project_id
            or (
                "subject_id" in context.deterministic_facts
                and context.deterministic_facts.get("subject_id") != scope.subject_id
            )
        ):
            return Director._rejected(DirectorFailureCode.INVALID_SCOPE)
        source_status = context.deterministic_facts.get("memory_status")
        if source_status is not None and source_status not in {
            "VERIFIED",
            "VERIFIED_WITH_LIMITATIONS",
        }:
            return Director._rejected(DirectorFailureCode.INCOMPLETE_SOURCE_EVIDENCE)
        return None

    def _ordered_candidates(
        self, grouped: dict[str, tuple[str, dict[str, object], int]]
    ) -> tuple[DirectorCandidatePreview, ...]:
        candidates: list[DirectorCandidatePreview] = []
        for canonical_candidate, (candidate_id, candidate_input, support_count) in grouped.items():
            operation = json.loads(canonical_candidate)["operation"]
            candidates.append(
                DirectorCandidatePreview(
                    candidate_id=candidate_id,
                    operation=operation,
                    input=candidate_input,
                    support_count=support_count,
                )
            )
        return tuple(
            sorted(
                candidates,
                key=lambda candidate: (
                    -candidate.support_count,
                    candidate.operation,
                    self._canonical_json(candidate.input),
                    candidate.candidate_id,
                ),
            )
        )

    @staticmethod
    def _canonical_json(value: object) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @staticmethod
    def _validate_veto(raw: object) -> SafetyVetoDecision:
        value = raw.model_dump(mode="python") if isinstance(raw, SafetyVetoDecision) else raw
        return SafetyVetoDecision.model_validate(value, strict=True)

    @staticmethod
    def _rejected(code: DirectorFailureCode) -> DirectorOutcome:
        return DirectorOutcome(status=DirectorStatus.REJECTED, failure_code=code)

    @staticmethod
    def _clarification(code: DirectorFailureCode) -> DirectorOutcome:
        return DirectorOutcome(
            status=DirectorStatus.CLARIFICATION_REQUIRED,
            failure_code=code,
        )

    @staticmethod
    def _failed(code: DirectorFailureCode, *, review_rounds: int = 0) -> DirectorOutcome:
        return DirectorOutcome(
            status=DirectorStatus.FAILED,
            failure_code=code,
            review_rounds=review_rounds,
        )


__all__ = ["Director", "ProposalOnlyGateway", "SafetyVeto"]
