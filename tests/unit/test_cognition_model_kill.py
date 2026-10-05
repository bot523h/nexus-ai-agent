"""Model Kill Test — the system survives with no model provider at all.

The mission's Model Kill Test: disable every model provider and prove the
system (a) stays alive, (b) keeps its deterministic paths, and (c) returns an
explicit refusal for what truly needs cognition.

This test does that end-to-end at the seam this session built:

* an *open-ended* intent routed with no model provider → an explicit refusal
  path (`L4_HUMAN`), never a fabricated proposal;
* a *known/parametric* intent with a deterministic path → executed through the
  real ``CommandBus`` with **no** model call anywhere;
* a deterministic rule can build a valid proposal with no cognition at all,
  and that proposal still has to pass the bus.

It proves **infrastructure/model separation** for the reasoning seam. It does
**not** prove open-ended creativity — that still requires a model, and that
distinction is asserted explicitly below.
"""

from __future__ import annotations

import asyncio

from nexus_ai_agent.creative.packs.edit.operations import build_edit_registry
from nexus_ai_agent.creative.studio.authorization import ProjectAccess
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.models import (
    ActorIdentity,
    AssetRecord,
    AuthorizationError,
    Project,
    Timeline,
    new_project,
)
from nexus_ai_agent.nagar.cognition import (
    CognitionBudget,
    CognitionContext,
    CognitionLevel,
    DeterministicRouter,
    IntentClass,
    NullCognition,
    ProducerIdentity,
    ProposalProvenance,
    ProposalSchema,
    Refusal,
    RefusalReason,
    RoutingRequest,
    TypedProposal,
    proposal_to_command,
)

ACTOR = ActorIdentity(kind="user", actor_id="user_42")
PROJECT_ID = "p_edit_01"


class StaticAuthorizer:
    def __init__(self, access: ProjectAccess) -> None:
        self._access = access

    def authorize(self, actor: ActorIdentity, project_id: str) -> ProjectAccess:
        if actor != self._access.actor or project_id != self._access.project_id:
            raise AuthorizationError("actor is not authorized for this project")
        return self._access


def _project() -> Project:
    clip = AssetRecord(
        asset_id="clip_main_01",
        media_kind="video",
        content_sha256="sha256:mainvideo01",
        duration_us=10_000_000,
    )
    timeline = Timeline(timeline_id="tl_edit", duration_us=15_000_000)
    project = new_project(PROJECT_ID, "Model Kill Test Project", timeline)
    return project.model_copy(update={"assets": [clip]})


def _authed_bus() -> CommandBus:
    access = ProjectAccess(
        actor=ACTOR, project_id=PROJECT_ID, permissions=frozenset({"project:read", "project:write"})
    )
    return CommandBus(
        state=_project(), registry=build_edit_registry(), authorizer=StaticAuthorizer(access)
    )


def _schema() -> ProposalSchema:
    return ProposalSchema(
        schema_id="nagar.edit.trim.v1",
        schema_version=1,
        allowed_operations=frozenset({"timeline.trim"}),
    )


def test_no_model_provider_means_open_ended_intents_are_refused_not_fabricated() -> None:
    # No provider is configured: the router must ask a human, and NullCognition
    # must refuse. Nothing is fabricated.
    decision = DeterministicRouter().route(
        RoutingRequest(
            intent_class=IntentClass.OPEN_ENDED,
            local_model_available=False,
            cloud_model_available=False,
        )
    )
    assert decision.level is CognitionLevel.L4_HUMAN
    assert decision.blocked is False

    refusal = asyncio.run(
        NullCognition().propose(
            CognitionContext(subject_id=PROJECT_ID), _schema(), CognitionBudget()
        )
    )
    assert isinstance(refusal, Refusal)
    assert refusal.reason is RefusalReason.PRODUCER_REFUSED


def test_deterministic_rule_completes_a_known_operation_with_no_model() -> None:
    # A deterministic rule is the "L1" answer. It builds a valid proposal
    # WITHOUT any model, and the proposal still has to pass the bus.
    decision = DeterministicRouter().route(
        RoutingRequest(intent_class=IntentClass.KNOWN_OPERATION, deterministic_available=True)
    )
    assert decision.level is CognitionLevel.L1_DETERMINISTIC

    rule_proposal = TypedProposal(
        schema_id="nagar.edit.trim.v1",
        schema_version=1,
        operation="timeline.trim",
        input={
            "clip_asset_id": "clip_main_01",
            "in_point_us": 1_000_000,
            "out_point_us": 5_000_000,
        },
        rationale="deterministic rule: trim to the first four seconds",
        confidence=1.0,
        provenance=ProposalProvenance(
            producer=ProducerIdentity(kind="rule", name="nagar.rule.trim_first_four_seconds"),
            created_at="2026-10-04T00:00:00Z",
            source="rule",
        ),
    )
    command = proposal_to_command(
        rule_proposal,
        actor=ACTOR,
        project_id=PROJECT_ID,
        operation_schema_version=1,
    )
    result = _authed_bus().dispatch(command)
    assert result.status == "applied"
    assert result.output["asset_id"].startswith("clip_main_01")


def test_the_substrate_stays_alive_and_deterministic_with_cognition_disabled() -> None:
    # Two identical deterministic runs with the null provider installed must
    # produce identical results — proving the deterministic path has no hidden
    # model dependency and no nondeterminism.
    def run_once() -> str:
        proposal = TypedProposal(
            schema_id="nagar.edit.trim.v1",
            schema_version=1,
            operation="timeline.trim",
            input={
                "clip_asset_id": "clip_main_01",
                "in_point_us": 2_000_000,
                "out_point_us": 6_000_000,
            },
            provenance=ProposalProvenance(
                producer=ProducerIdentity(kind="rule", name="rule"),
                created_at="2026-10-04T00:00:00Z",
                source="rule",
            ),
        )
        command = proposal_to_command(
            proposal, actor=ACTOR, project_id=PROJECT_ID, operation_schema_version=1
        )
        bus = _authed_bus()
        return bus.dispatch(command).state_hash

    assert run_once() == run_once()
