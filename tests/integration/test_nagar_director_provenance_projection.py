from __future__ import annotations

import pytest

from nexus_ai_agent.nagar.causal_memory.models import (
    MemoryNode,
    MemoryNodeKind,
    MemorySnapshot,
    MemoryStatus,
)
from nexus_ai_agent.nagar.cognition.context import CognitionContext, ProposalSchema
from nexus_ai_agent.nagar.cognition.proposal import (
    ProducerIdentity,
    ProposalProvenance,
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
JOB_ID = "job-a"
SCHEMA_ID = "nagar.gateway.proposal.v1"


class _Gateway:
    project_id = PROJECT_ID

    def __init__(self) -> None:
        self.calls = 0

    async def propose(
        self,
        context: CognitionContext,
        request: RoutingRequest,
        *,
        requested_operations: frozenset[str] | None = None,
        idempotency_key: str | None = None,
    ) -> TypedProposal:
        del context, request, requested_operations, idempotency_key
        self.calls += 1
        return TypedProposal(
            schema_id=SCHEMA_ID,
            schema_version=1,
            operation="timeline.trim",
            input={"start_us": 1, "end_us": 2},
            rationale="not exposed by the Director",
            confidence=0.99,
            provenance=ProposalProvenance(
                producer=ProducerIdentity(kind="test", name="integration"),
                created_at="2026-10-29T00:00:00Z",
            ),
        )


class _ClearVeto:
    def check(self, *, scope: DirectorScope, candidate: object) -> SafetyVetoDecision:
        del scope, candidate
        return SafetyVetoDecision(veto=False, reason_code="clear")


def _memory_snapshot() -> MemorySnapshot:
    return MemorySnapshot(
        job_id=JOB_ID,
        project_id=PROJECT_ID,
        status=MemoryStatus.INCOMPLETE,
        passport_status="INCOMPLETE",
        tenant_scope="unavailable",
        completeness="single_job",
        nodes=(
            MemoryNode(
                node_id=f"job:{JOB_ID}",
                kind=MemoryNodeKind.JOB,
                attributes={"status": "completed"},
            ),
        ),
        edges=(),
        evidence=(),
        finding_codes=("independent_verification_missing",),
        snapshot_digest="sha256:" + ("a" * 64),
    )


@pytest.mark.asyncio
async def test_incomplete_provenance_snapshot_is_not_promoted_into_director_proposal() -> None:
    snapshot = _memory_snapshot()
    context = CognitionContext(
        subject_id=snapshot.job_id,
        intent_text="sensitive request that must not enter an outcome",
        deterministic_facts={
            "project_id": snapshot.project_id,
            "memory_status": snapshot.status.value,
            "tenant_scope": snapshot.tenant_scope,
            "snapshot_digest": snapshot.snapshot_digest,
            "finding_codes": list(snapshot.finding_codes),
            "nodes": [node.model_dump(mode="json") for node in snapshot.nodes],
        },
    )
    gateway = _Gateway()
    director = Director(
        proposal_gateway=gateway,
        proposal_schema=ProposalSchema(
            schema_id=SCHEMA_ID,
            schema_version=1,
            allowed_operations=frozenset({"timeline.trim"}),
        ),
        safety_veto=_ClearVeto(),
        budget=DirectorBudget(max_candidates=5),
    )

    outcome = await director.preview(
        scope=DirectorScope(project_id=PROJECT_ID, subject_id=JOB_ID),
        context=context,
        request=RoutingRequest(
            intent_class=IntentClass.OPEN_ENDED,
            local_model_available=True,
        ),
        requested_operations=frozenset({"timeline.trim"}),
    )

    assert snapshot.status is MemoryStatus.INCOMPLETE
    assert outcome.status is DirectorStatus.REJECTED
    assert outcome.failure_code is DirectorFailureCode.TENANT_SCOPE_UNAVAILABLE
    assert outcome.selected_candidate_id is None
    assert outcome.dispatchable is False
    assert gateway.calls == 0
    assert "sensitive request" not in str(outcome.model_dump(mode="json"))
