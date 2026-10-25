"""Integration: a model proposal can never become execution on its own.

This is the load-bearing test for the mission rule **"never MODEL →
EXECUTION"**.  It wires a producer's proposal through the bridge into a
*real* ``CommandBus`` (the edit pack registry, a real authorizer port) and
proves:

* an accepted proposal becomes a schema-2 command that still has to pass
  the bus authorization pipeline to execute;
* a proposal naming an operation the caller did not offer is refused before
  the bridge, so the model cannot widen the candidate set;
* a bridged command with no trusted authorizer is refused by the bus — the
  proposal cannot self-grant access by naming an actor;
* the executed command produces a real derived asset (an artifact-shaped
  outcome) with content identity.
"""

from __future__ import annotations

import json

import pytest

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
    ProposalSchema,
    Refusal,
    RefusalReason,
    TypedProposal,
    parse_proposal,
    proposal_to_command,
)

SCHEMA_ID = "nagar.edit.trim.v1"
ACTOR = ActorIdentity(kind="user", actor_id="user_42")
PROJECT_ID = "p_edit_01"


class StaticAuthorizer:
    """A real ``ProjectAuthorizer`` port impl bound to one trusted principal."""

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
    project = new_project(PROJECT_ID, "Cognition Integration Project", timeline)
    return project.model_copy(update={"assets": [clip]})


def _schema() -> ProposalSchema:
    return ProposalSchema(
        schema_id=SCHEMA_ID,
        schema_version=1,
        allowed_operations=frozenset({"timeline.trim"}),
    )


def _raw_proposal(*, operation: str = "timeline.trim", **extra: object) -> str:
    payload: dict[str, object] = {
        "schema_id": SCHEMA_ID,
        "schema_version": 1,
        "operation": operation,
        "input": {
            "clip_asset_id": "clip_main_01",
            "in_point_us": 2_000_000,
            "out_point_us": 6_000_000,
        },
        "rationale": "trim the interview clip to the requested window",
        "confidence": 0.9,
        "provenance": {
            "producer": {"kind": "model", "name": "test-producer"},
            "created_at": "2026-10-04T00:00:00Z",
            "source": "test",
        },
    }
    payload.update(extra)
    return json.dumps(payload)


def _authed_bus() -> CommandBus:
    access = ProjectAccess(
        actor=ACTOR, project_id=PROJECT_ID, permissions=frozenset({"project:read", "project:write"})
    )
    return CommandBus(
        state=_project(), registry=build_edit_registry(), authorizer=StaticAuthorizer(access)
    )


def test_accepted_proposal_executes_only_through_the_bus() -> None:
    bus = _authed_bus()
    parsed = parse_proposal(_raw_proposal(), _schema())
    assert isinstance(parsed, TypedProposal)

    command = proposal_to_command(
        parsed,
        actor=ACTOR,
        project_id=PROJECT_ID,
        operation_schema_version=1,
    )
    result = bus.dispatch(command)

    assert result.status == "applied"
    derived_id = result.output["asset_id"]
    derived = next(a for a in bus.project.assets if a.asset_id == derived_id)
    # A real artifact-shaped outcome: derived content identity + provenance.
    assert derived.content_sha256.startswith("sha256:")
    assert derived.provenance["action"] == "trim"
    assert derived.parent_asset_ids == ("clip_main_01",)


def test_proposal_naming_an_unoffered_operation_never_reaches_the_bus() -> None:
    # ``system.undo`` exists in the registry, but the caller did not offer it.
    parsed = parse_proposal(_raw_proposal(operation="system.undo"), _schema())
    assert isinstance(parsed, Refusal)
    assert parsed.reason is RefusalReason.DISALLOWED_OPERATION


def test_proposal_smuggling_authority_is_refused_before_the_bridge() -> None:
    parsed = parse_proposal(_raw_proposal(actor={"kind": "user", "actor_id": "root"}), _schema())
    assert isinstance(parsed, Refusal)
    assert parsed.reason is RefusalReason.AUTHORITY_FIELD


def test_bridged_command_without_authorizer_is_refused_by_the_bus() -> None:
    # Same command, no trusted authorizer: the bus must refuse the actor claim.
    # This is the proof that the proposal cannot self-grant by naming an actor.
    parsed = parse_proposal(_raw_proposal(), _schema())
    assert isinstance(parsed, TypedProposal)
    command = proposal_to_command(
        parsed,
        actor=ACTOR,
        project_id=PROJECT_ID,
        operation_schema_version=1,
    )
    unauthed = CommandBus(state=_project(), registry=build_edit_registry())
    with pytest.raises(AuthorizationError):
        unauthed.dispatch(command)


def test_bridged_command_for_another_project_is_refused() -> None:
    parsed = parse_proposal(_raw_proposal(), _schema())
    assert isinstance(parsed, TypedProposal)
    command = proposal_to_command(
        parsed,
        actor=ACTOR,
        project_id="a_different_project",
        operation_schema_version=1,
    )
    bus = _authed_bus()
    with pytest.raises(AuthorizationError):
        bus.dispatch(command)
