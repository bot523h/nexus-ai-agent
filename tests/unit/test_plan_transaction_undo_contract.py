"""Adversarial regression and contract tests for PlanTransaction, transaction identity,
logical idempotency replay, and targeted undo / rollback safety.
"""

from __future__ import annotations

from typing import Any

import pytest

from nexus_ai_agent.creative.studio import (
    ActorIdentity,
    AuthorizationError,
    Clip,
    CommandBus,
    CommandExecutionError,
    CommandProvenance,
    CommandValidationError,
    MediaRef,
    Playhead,
    Project,
    RequestContext,
    TimeBase,
    Timeline,
    TimeRangeUS,
    Track,
    TypedCommand,
    UndoConflictError,
    new_project,
)
from nexus_ai_agent.creative.studio.authorization import ProjectAccess
from nexus_ai_agent.creative.studio.models import (
    PlanResult,
    PlanTransaction,
    TargetRef,
)

TIMEBASE = TimeBase(numerator=30, denominator=1)
DIGEST = "sha256:" + "ab" * 32
DEFAULT_ACTOR = ActorIdentity(kind="user", actor_id="alice")


def make_project(project_id: str = "project_01") -> Project:
    media = MediaRef(
        asset_id="asset_01",
        content_sha256=DIGEST,
        media_kind="video",
        duration_us=10_000_000,
        timebase=TIMEBASE,
    )
    clip = Clip(
        clip_id="clip_01",
        media_ref=media,
        source_range=TimeRangeUS(start_us=0, end_us=10_000_000),
        timeline_range=TimeRangeUS(start_us=0, end_us=10_000_000),
    )
    track = Track(track_id="video_01", name="Video 1", kind="video", clips=[clip])
    timeline = Timeline(
        timeline_id="tl_01",
        duration_us=12_000_000,
        tracks=[track],
        playhead=Playhead(timecode_us=2_500_000, frame_number=75, timebase=TIMEBASE),
    )
    project = new_project(project_id, "Plan Contract Project", timeline)
    return Project.model_validate(
        {
            **project.model_dump(mode="json"),
            "assets": [
                {
                    "asset_id": "asset_01",
                    "media_kind": "video",
                    "content_sha256": DIGEST,
                    "duration_us": 10_000_000,
                    "parent_asset_ids": [],
                    "provenance": {"origin": "test"},
                }
            ],
        }
    )


def make_actor(actor_id: str = "alice") -> ActorIdentity:
    return ActorIdentity(kind="user", actor_id=actor_id)


def make_provenance() -> CommandProvenance:
    return CommandProvenance(source="local", source_id="plan-test")


def make_command(
    operation: str = "media.play",
    command_id: str = "cmd_01",
    *,
    input: dict[str, Any] | None = None,
    schema_version: int = 1,
    actor: ActorIdentity | None = DEFAULT_ACTOR,
    project_id: str | None = None,
    provenance: CommandProvenance | None = None,
    idempotency_key: str | None = None,
    request_context: RequestContext | None = None,
    target: TargetRef | None = None,
) -> TypedCommand:
    payload: dict[str, Any] = {
        "command_id": command_id,
        "operation": operation,
        "schema_version": schema_version,
        "input": input if input is not None else {},
    }
    if actor is not None:
        payload["actor"] = actor.model_dump(mode="json")
    if target is not None:
        target_dict = target.model_dump(mode="json")
        if project_id is not None and target_dict.get("project_id") is None:
            target_dict["project_id"] = project_id
        payload["target"] = target_dict
    elif project_id is not None:
        payload["target"] = {"project_id": project_id}
    if provenance is not None:
        payload["provenance"] = provenance.model_dump(mode="json")
    if idempotency_key is not None:
        payload["idempotency_key"] = idempotency_key
    if request_context is not None:
        payload["request_context"] = request_context.model_dump(mode="json")
    return TypedCommand.model_validate(payload)


class StaticAuthorizer:
    def __init__(self, *grants: ProjectAccess) -> None:
        self._grants = {(grant.actor, grant.project_id): grant for grant in grants}

    def authorize(self, actor: ActorIdentity, project_id: str) -> ProjectAccess:
        try:
            return self._grants[(actor, project_id)]
        except KeyError:
            raise AuthorizationError("actor is not authorized for this project") from None


def grant(
    actor: ActorIdentity | None = None,
    project_id: str = "project_01",
    permissions: frozenset[str] = frozenset({"project:read", "project:write"}),
) -> ProjectAccess:
    return ProjectAccess(
        actor=actor if actor is not None else make_actor(),
        project_id=project_id,
        permissions=permissions,
    )


@pytest.fixture()
def project() -> Project:
    return make_project()


@pytest.fixture()
def bus(project: Project) -> CommandBus:
    authorizer = StaticAuthorizer(
        grant(make_actor("alice"), project.project_id),
        grant(make_actor("bob"), project.project_id),
    )
    return CommandBus(state=project, authorizer=authorizer)


# ---------------------------------------------------------------------------
# 1. Idempotency & Redelivery Identity
# ---------------------------------------------------------------------------


class TestIdempotencyAndRedeliveryIdentity:
    def test_undone_command_can_be_reexecuted_with_same_idempotency_key(
        self, bus: CommandBus
    ) -> None:
        """Undo of a transaction MUST evict its idempotency reservation so re-execution works."""
        mark_cmd = make_command(
            "timeline.mark",
            "cmd_mark_1",
            input={"at": "اینجا", "label": "Marker 1"},
            idempotency_key="key_mark_1",
        )
        res1 = bus.dispatch(mark_cmd)
        assert res1.state_revision == 1
        assert len(bus.project.timeline.markers) == 1

        # Undo the command
        undo_cmd = make_command("system.undo", "cmd_undo_1")
        undo_res = bus.dispatch(undo_cmd)
        assert undo_res.status == "applied"
        assert len(bus.project.timeline.markers) == 0

        # Re-dispatch command with the exact same idempotency_key
        retry_res = bus.dispatch(mark_cmd)
        assert retry_res.status == "applied"
        assert len(bus.project.timeline.markers) == 1
        # It must actually execute on the state, NOT return stale cached result
        assert retry_res.state_revision == 3

    def test_redelivery_with_new_request_id_in_request_context_replays_safely(
        self, bus: CommandBus
    ) -> None:
        """Transport request_id in request_context must NOT break logical idempotency."""
        cmd1 = make_command(
            "media.play",
            "cmd_play_1",
            idempotency_key="key_play_1",
            request_context=RequestContext(channel="telegram", request_id="req_1001"),
        )
        res1 = bus.dispatch(cmd1)

        cmd2 = make_command(
            "media.play",
            "cmd_play_2",
            idempotency_key="key_play_1",
            request_context=RequestContext(channel="telegram", request_id="req_1002"),
        )
        res2 = bus.dispatch(cmd2)
        assert res1.transaction_id == res2.transaction_id
        assert res2.state_revision == 1


# ---------------------------------------------------------------------------
# 2. PlanTransaction Staged Speculative Execution
# ---------------------------------------------------------------------------


class TestPlanTransactionExecution:
    def test_atomic_plan_transaction_execution_success(self, bus: CommandBus) -> None:
        """A multi-step PlanTransaction applies all commands atomically."""
        cmd1 = make_command(
            "timeline.mark",
            "cmd_p1",
            input={"at": "اینجا", "label": "Step 1"},
        )
        cmd2 = make_command(
            "timeline.mark",
            "cmd_p2",
            input={"at": "اینجا", "label": "Step 2"},
        )
        plan = PlanTransaction(
            plan_id="plan_alpha",
            actor=make_actor("alice"),
            target=TargetRef(project_id="project_01"),
            commands=(cmd1, cmd2),
            idempotency_key="plan_key_1",
        )

        plan_res = bus.dispatch_plan(plan)
        assert isinstance(plan_res, PlanResult)
        assert plan_res.plan_id == "plan_alpha"
        assert len(plan_res.command_results) == 2
        assert bus.state_revision == 2
        assert len(bus.project.timeline.markers) == 2
        assert all(tx.plan_id == "plan_alpha" for tx in bus.history)

    def test_atomic_plan_transaction_partial_failure_rolls_back_completely(
        self, bus: CommandBus
    ) -> None:
        """If step 2 of a plan fails, speculative state rolls back 100% (fail-closed)."""
        cmd_valid = make_command(
            "timeline.mark",
            "cmd_p1",
            input={"at": "اینجا", "label": "Step 1"},
        )
        cmd_invalid = make_command(
            "timeline.split_at_playhead",
            "cmd_p2",
            input={"at": "اینجا"},
            target=TargetRef(track_id="non_existent_track", clip_id="non_existent_clip"),
        )
        plan = PlanTransaction(
            plan_id="plan_failing",
            actor=make_actor("alice"),
            target=TargetRef(project_id="project_01"),
            commands=(cmd_valid, cmd_invalid),
        )

        with pytest.raises(CommandValidationError):
            bus.dispatch_plan(plan)

        # Central project state MUST be untouched (0 markers added, revision stays 0)
        assert bus.state_revision == 0
        assert len(bus.project.timeline.markers) == 0
        assert len(bus.history) == 0


# ---------------------------------------------------------------------------
# 3. Targeted Undo and Foreign Edit Protection
# ---------------------------------------------------------------------------


class TestTargetedUndoAndForeignEditProtection:
    def test_targeted_undo_by_plan_id_refuses_non_top_and_rewinds_top(
        self, bus: CommandBus
    ) -> None:
        """Targeted undo by plan_id refuses if newer plan exists, and rewinds top plan cleanly."""
        cmd1 = make_command(
            "timeline.mark", "cmd_p1", input={"at": "اینجا", "label": "Plan A Mark"}
        )
        plan_a = PlanTransaction(
            plan_id="plan_A",
            actor=make_actor("alice"),
            target=TargetRef(project_id="project_01"),
            commands=(cmd1,),
        )
        bus.dispatch_plan(plan_a)
        assert len(bus.project.timeline.markers) == 1

        cmd2 = make_command(
            "timeline.mark", "cmd_p2", input={"at": "اینجا", "label": "Plan B Mark"}
        )
        plan_b = PlanTransaction(
            plan_id="plan_B",
            actor=make_actor("bob"),
            target=TargetRef(project_id="project_01"),
            commands=(cmd2,),
        )
        bus.dispatch_plan(plan_b)
        assert len(bus.project.timeline.markers) == 2

        # Non-top undo of Plan A is refused with UndoConflictError
        with pytest.raises(UndoConflictError):
            bus.dispatch(
                make_command("system.undo", "cmd_undo_plan_a_stale", input={"plan_id": "plan_A"})
            )

        # Top-of-stack undo of Plan B succeeds
        undo_res = bus.dispatch(
            make_command("system.undo", "cmd_undo_plan_b", input={"plan_id": "plan_B"})
        )
        assert undo_res.status == "applied"
        markers = bus.project.timeline.markers
        assert len(markers) == 1
        assert markers[0].label == "Plan A Mark"

    def test_targeted_undo_refuses_when_foreign_edits_interleave_on_same_element(
        self, project: Project
    ) -> None:
        """Targeted undo fails closed if interleaved foreign edits modified the project."""
        authorizer = StaticAuthorizer(
            grant(make_actor("alice")),
            grant(make_actor("bob")),
        )
        authed_bus = CommandBus(state=project, authorizer=authorizer)

        split_cmd1 = make_command(
            "timeline.split_at_playhead",
            "cmd_split_1",
            schema_version=2,
            actor=make_actor("alice"),
            project_id="project_01",
            provenance=make_provenance(),
            input={"at": "اینجا"},
            target=TargetRef(track_id="video_01", clip_id="clip_01"),
        )
        res1 = authed_bus.dispatch(split_cmd1)
        right_clip_id = res1.output["right"]["clip_id"]

        split_cmd2 = make_command(
            "timeline.split_at_playhead",
            "cmd_split_2",
            schema_version=2,
            actor=make_actor("bob"),
            project_id="project_01",
            provenance=make_provenance(),
            input={"at": Playhead(timecode_us=5_000_000).model_dump(mode="json")},
            target=TargetRef(track_id="video_01", clip_id=right_clip_id),
        )
        authed_bus.dispatch(split_cmd2)

        undo_cmd = make_command(
            "system.undo",
            "cmd_undo_tx1",
            schema_version=2,
            actor=make_actor("alice"),
            project_id="project_01",
            provenance=make_provenance(),
            input={"transaction_id": res1.transaction_id},
        )
        with pytest.raises(UndoConflictError, match="not the newest editable transaction"):
            authed_bus.dispatch(undo_cmd)

    def test_exact_identity_matching_refuses_prefix_matches(self, bus: CommandBus) -> None:
        """Targeted undo requires exact string matching on transaction_id / plan_id."""
        cmd1 = make_command("timeline.mark", "cmd_p1", input={"at": "اینجا", "label": "Mark 1"})
        plan_1 = PlanTransaction(
            plan_id="plan_1",
            actor=make_actor("alice"),
            target=TargetRef(project_id="project_01"),
            commands=(cmd1,),
        )
        bus.dispatch_plan(plan_1)

        undo_cmd = make_command(
            "system.undo",
            "cmd_undo_prefix",
            input={"plan_id": "plan_10"},
        )
        with pytest.raises(CommandExecutionError, match="no transaction for plan"):
            bus.dispatch(undo_cmd)
