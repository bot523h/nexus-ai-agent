"""Wave 1 (Green Cockpit core) unit tests.

Covers, per the Wave 1 non-negotiables:

* one unit test per registered operation proving the command validates,
  applies to in-memory state, and that undo restores the previous state;
* the reference resolver pinning semantic expressions to exact timecodes
  at command receipt (``captured_at_command=True``);
* the command bus guards (unknown operations, permission levels A/B/C/D,
  stale preconditions, idempotency replay, malformed envelopes).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from nagar_helpers import TEST_ACTOR, TEST_PROVENANCE, authorized_bus
from pydantic import ValidationError

from nexus_ai_agent.creative.studio import (
    CapabilityRegistry,
    Clip,
    CommandBus,
    CommandExecutionError,
    CommandValidationError,
    MediaRef,
    OperationContext,
    OperationOutcome,
    OperationSpec,
    PermissionDeniedError,
    PermissionLevel,
    Playhead,
    PreconditionError,
    Project,
    ReferenceExpr,
    ReferenceResolutionError,
    ReferenceResolver,
    TimeBase,
    Timeline,
    TimeRangeUS,
    Track,
    TypedCommand,
    UndoStackEmptyError,
    UnknownOperationError,
    build_wave1_registry,
    compute_state_hash,
    new_project,
)
from nexus_ai_agent.creative.studio.capabilities import UndoCommandInput
from nexus_ai_agent.creative.studio.models import Preconditions, TargetRef

TIMEBASE = TimeBase(numerator=30, denominator=1)
PLAYHEAD_US = 2_500_000
DURATION_US = 12_000_000
CLIP_END_US = 10_000_000


def make_project() -> Project:
    media = MediaRef(
        asset_id="asset_interview_a",
        content_sha256="sha256:" + "ab" * 32,
        media_kind="video",
        duration_us=DURATION_US,
        timebase=TIMEBASE,
    )
    clip = Clip(
        clip_id="clip_01",
        media_ref=media,
        source_range=TimeRangeUS(start_us=0, end_us=CLIP_END_US),
        timeline_range=TimeRangeUS(start_us=0, end_us=CLIP_END_US),
    )
    track = Track(track_id="video_01", name="Video 1", kind="video", clips=[clip])
    timeline = Timeline(
        timeline_id="tl_01",
        duration_us=DURATION_US,
        tracks=[track],
        playhead=Playhead(timecode_us=PLAYHEAD_US, frame_number=75, timebase=TIMEBASE),
    )
    return new_project("project_01", "Green Cockpit Demo", timeline)


def make_command(
    operation: str,
    input_: dict[str, Any] | None = None,
    track_id: str | None = None,
    clip_id: str | None = None,
    state_revision: int | None = None,
    state_hash: str | None = None,
    idempotency_key: str | None = None,
    confirmed: bool = False,
) -> TypedCommand:
    return TypedCommand(
        command_id=f"cmd_{uuid4().hex[:10]}",
        actor=TEST_ACTOR,
        provenance=TEST_PROVENANCE,
        operation=operation,
        target=TargetRef(project_id="project_01", track_id=track_id, clip_id=clip_id),
        input=input_ if input_ is not None else {},
        preconditions=Preconditions(state_revision=state_revision, state_hash=state_hash),
        idempotency_key=idempotency_key,
        confirmed=confirmed,
    )


@pytest.fixture()
def project() -> Project:
    return make_project()


@pytest.fixture()
def bus(project: Project) -> CommandBus:
    return authorized_bus(project)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class TestModels:
    def test_time_range_rejects_inverted_or_empty(self) -> None:
        with pytest.raises(ValidationError):
            TimeRangeUS(start_us=10, end_us=5)
        with pytest.raises(ValidationError):
            TimeRangeUS(start_us=5, end_us=5)

    def test_state_hash_is_content_stable_and_verifiable(self, project: Project) -> None:
        assert project.state_hash.startswith("sha256:")
        assert project.state_hash == compute_state_hash(project)
        clone = Project.model_validate(project.model_dump(mode="json"))
        assert clone.state_hash == project.state_hash


# ---------------------------------------------------------------------------
# Reference resolver
# ---------------------------------------------------------------------------


class TestReferenceResolver:
    def test_here_pins_current_playhead_with_capture_flag(self, project: Project) -> None:
        resolved = ReferenceResolver().resolve("اینجا", project)
        assert resolved.timecode_us == PLAYHEAD_US
        assert resolved.captured_at_command is True
        assert resolved.frame_number == 75  # 2.5s @ 30fps

    def test_persian_digits_and_directions(self, project: Project) -> None:
        resolver = ReferenceResolver()
        assert resolver.resolve("۲ ثانیه قبل", project).timecode_us == 500_000
        assert resolver.resolve("۵ ثانیه بعد", project).timecode_us == 7_500_000
        assert resolver.resolve("1 second ago", project).timecode_us == 1_500_000
        assert resolver.resolve("2 seconds after", project).timecode_us == 4_500_000
        assert resolver.resolve("شروع", project).timecode_us == 0
        assert resolver.resolve("پایان", project).timecode_us == DURATION_US

    def test_structured_expressions(self, project: Project) -> None:
        resolver = ReferenceResolver()
        absolute = resolver.resolve(ReferenceExpr(kind="absolute", timecode_us=42), project)
        assert absolute.timecode_us == 42
        relative = resolver.resolve(ReferenceExpr(kind="relative", offset_us=-2_000_000), project)
        assert relative.timecode_us == 500_000
        anchored = resolver.resolve(
            ReferenceExpr(kind="relative", offset_us=1_000_000, relative_to="timeline_start"),
            project,
        )
        assert anchored.timecode_us == 1_000_000
        timeline_end = resolver.resolve(ReferenceExpr(kind="timeline_end"), project)
        assert timeline_end.timecode_us == DURATION_US

    def test_frame_number_derived_from_timebase(self, project: Project) -> None:
        resolved = ReferenceResolver().resolve(
            ReferenceExpr(kind="absolute", timecode_us=1_000_000), project
        )
        assert resolved.frame_number == 30

    def test_out_of_bounds_reference_raises(self, project: Project) -> None:
        with pytest.raises(ReferenceResolutionError):
            ReferenceResolver().resolve("5 ثانیه قبل", project)

    def test_unrecognized_expression_raises(self, project: Project) -> None:
        with pytest.raises(ReferenceResolutionError):
            ReferenceResolver().resolve("the middle of the vibe", project)


# ---------------------------------------------------------------------------
# One test per registered operation: validate -> apply -> undo
# ---------------------------------------------------------------------------


class TestMediaPlay:
    def test_play_validates_applies_to_in_memory_state_and_undoes(self, bus: CommandBus) -> None:
        initial_hash = bus.state_hash
        result = bus.dispatch(make_command("media.play", input_={"start": "اینجا"}))

        assert result.status == "applied"
        assert result.state_revision == 1
        assert result.state_hash != initial_hash
        assert result.undo_available is True
        assert result.output == {"is_playing": True, "timecode_us": PLAYHEAD_US}
        assert bus.project.timeline.playhead.is_playing is True

        undo = bus.dispatch(make_command("system.undo"))
        assert undo.state_hash == initial_hash
        assert bus.project.timeline.playhead.is_playing is False


class TestMediaPause:
    def test_pause_validates_applies_and_undoes(self, bus: CommandBus) -> None:
        bus.dispatch(make_command("media.play"))
        result = bus.dispatch(make_command("media.pause"))

        assert result.output == {"is_playing": False, "timecode_us": PLAYHEAD_US}
        assert bus.project.timeline.playhead.is_playing is False

        bus.dispatch(make_command("system.undo"))
        assert bus.project.timeline.playhead.is_playing is True

    def test_pause_accepts_empty_input(self, bus: CommandBus) -> None:
        bus.dispatch(make_command("media.pause"))
        assert bus.project.timeline.playhead.is_playing is False


class TestTimelineMark:
    def test_mark_validates_applies_and_undoes(self, bus: CommandBus) -> None:
        initial_hash = bus.state_hash
        result = bus.dispatch(
            make_command("timeline.mark", input_={"at": "اینجا", "label": "shot change"})
        )

        assert result.state_revision == 1
        markers = bus.project.timeline.markers
        assert len(markers) == 1
        assert markers[0].timecode_us == PLAYHEAD_US
        assert markers[0].label == "shot change"

        undo = bus.dispatch(make_command("system.undo"))
        assert undo.state_hash == initial_hash
        assert bus.project.timeline.markers == []

    def test_mark_with_absolute_reference(self, bus: CommandBus) -> None:
        bus.dispatch(
            make_command(
                "timeline.mark",
                input_={"at": {"kind": "absolute", "timecode_us": 6_000_000}, "label": "mid"},
            )
        )
        assert bus.project.timeline.markers[0].timecode_us == 6_000_000

    def test_mark_with_persian_digit_relative_reference(self, bus: CommandBus) -> None:
        expression = "۱ ثانیه قبل"
        bus.dispatch(
            make_command("timeline.mark", input_={"at": expression, "label": "one second before"})
        )
        assert bus.project.timeline.markers[0].timecode_us == 1_500_000

    def test_mark_requires_label(self, bus: CommandBus) -> None:
        with pytest.raises(CommandValidationError):
            bus.dispatch(make_command("timeline.mark", input_={"at": "اینجا"}))

    def test_mark_rejects_out_of_bounds_reference(self, bus: CommandBus) -> None:
        with pytest.raises(ReferenceResolutionError):
            bus.dispatch(make_command("timeline.mark", input_={"at": "9 ثانیه قبل", "label": "x"}))


class TestTimelineSplitAtPlayhead:
    def test_split_validates_applies_and_undoes(self, bus: CommandBus) -> None:
        initial_hash = bus.state_hash
        result = bus.dispatch(
            make_command("timeline.split_at_playhead", track_id="video_01", clip_id="clip_01")
        )

        clips = bus.project.timeline.tracks[0].clips
        assert len(clips) == 2
        left, right = clips
        assert left.clip_id == "clip_01"
        assert left.timeline_range == TimeRangeUS(start_us=0, end_us=PLAYHEAD_US)
        assert right.timeline_range == TimeRangeUS(start_us=PLAYHEAD_US, end_us=CLIP_END_US)
        assert left.source_range == TimeRangeUS(start_us=0, end_us=PLAYHEAD_US)
        assert right.source_range == TimeRangeUS(start_us=PLAYHEAD_US, end_us=CLIP_END_US)
        assert left.media_ref.content_sha256 == right.media_ref.content_sha256
        assert result.output["source_unchanged"] is True
        assert result.state_revision == 1

        undo = bus.dispatch(make_command("system.undo"))
        assert undo.state_hash == initial_hash
        assert len(bus.project.timeline.tracks[0].clips) == 1

    def test_split_requires_target(self, bus: CommandBus) -> None:
        with pytest.raises(CommandValidationError):
            bus.dispatch(make_command("timeline.split_at_playhead", clip_id="clip_01"))
        assert len(bus.project.timeline.tracks[0].clips) == 1

    def test_split_at_clip_boundary_rejected(self, bus: CommandBus) -> None:
        with pytest.raises(CommandValidationError):
            bus.dispatch(
                make_command(
                    "timeline.split_at_playhead",
                    input_={"at": {"kind": "timeline_start"}},
                    track_id="video_01",
                    clip_id="clip_01",
                )
            )
        assert len(bus.project.timeline.tracks[0].clips) == 1

    def test_split_unknown_clip_rejected(self, bus: CommandBus) -> None:
        with pytest.raises(CommandValidationError):
            bus.dispatch(
                make_command("timeline.split_at_playhead", track_id="video_01", clip_id="clip_404")
            )
        assert len(bus.project.timeline.tracks[0].clips) == 1


class TestSystemUndo:
    def test_undo_restores_previous_state_atomically(self, bus: CommandBus) -> None:
        first_hash = bus.state_hash
        bus.dispatch(make_command("timeline.mark", input_={"at": "اینجا", "label": "a"}))
        bus.dispatch(make_command("timeline.mark", input_={"at": "پایان", "label": "b"}))

        undo = bus.dispatch(make_command("system.undo"))
        assert undo.output["undone_operation"] == "timeline.mark"
        assert len(bus.project.timeline.markers) == 1

        undo2 = bus.dispatch(make_command("system.undo"))
        assert undo2.state_hash == first_hash
        assert bus.project.timeline.markers == []

    def test_undo_on_empty_history_raises(self, bus: CommandBus) -> None:
        with pytest.raises(UndoStackEmptyError):
            bus.dispatch(make_command("system.undo"))
        assert bus.state_revision == 0

    def test_undo_stack_exhaustion_after_fully_undone(self, bus: CommandBus) -> None:
        bus.dispatch(make_command("timeline.mark", input_={"at": "اینجا", "label": "a"}))

        bus.dispatch(make_command("system.undo"))
        assert bus.project.timeline.markers == []

        # Classic NLE semantics: the undo record is not itself an undo target.
        with pytest.raises(UndoStackEmptyError):
            bus.dispatch(make_command("system.undo"))
        assert bus.project.timeline.markers == []

    def test_transaction_records_hashes_and_revision(self, bus: CommandBus) -> None:
        previous_hash = bus.state_hash
        bus.dispatch(make_command("timeline.mark", input_={"at": "اینجا", "label": "a"}))

        (transaction,) = bus.history
        assert transaction.operation == "timeline.mark"
        assert transaction.permission_level is PermissionLevel.REVERSIBLE
        assert transaction.parent_revision == 0
        assert transaction.previous_state_hash == previous_hash
        assert transaction.new_state_hash == bus.state_hash
        assert transaction.state_before["timeline"]["markers"] == []


# ---------------------------------------------------------------------------
# Command bus guards
# ---------------------------------------------------------------------------


class TestCommandBusGuards:
    def test_unknown_operation_rejected(self, bus: CommandBus) -> None:
        with pytest.raises(UnknownOperationError):
            bus.dispatch(make_command("timeline.luxurious_glitter"))
        assert bus.state_revision == 0
        assert bus.history == ()

    def test_level_d_operation_denied(self, project: Project) -> None:
        registry = build_wave1_registry()

        def never_called(_: Project, __: OperationContext) -> OperationOutcome:
            raise AssertionError("a level D operation must never execute")

        registry.register_operation(
            "system",
            "danger",
            OperationSpec(
                operation_id="system.shell_exec",
                description="shell escape (must be denied)",
                permission_level=PermissionLevel.DENIED,
                input_model=UndoCommandInput,
                handler=never_called,
            ),
        )
        denied_bus = authorized_bus(project, registry=registry)
        with pytest.raises(PermissionDeniedError):
            denied_bus.dispatch(make_command("system.shell_exec"))
        assert denied_bus.state_revision == 0

    def test_level_c_requires_explicit_confirmation(self, project: Project) -> None:
        registry = CapabilityRegistry()

        def confirmed_handler(p: Project, context: OperationContext) -> OperationOutcome:
            return OperationOutcome(p, context.history, {"confirmed": True})

        registry.register_operation(
            "timeline",
            "export",
            OperationSpec(
                operation_id="timeline.export_master",
                description="heavy export (level C)",
                permission_level=PermissionLevel.CONFIRMATION,
                input_model=UndoCommandInput,
                handler=confirmed_handler,
            ),
        )
        c_bus = authorized_bus(project, registry=registry)
        with pytest.raises(PermissionDeniedError):
            c_bus.dispatch(make_command("timeline.export_master"))
        result = c_bus.dispatch(make_command("timeline.export_master", confirmed=True))
        assert result.output == {"confirmed": True}

    def test_stale_preconditions_rejected_without_state_change(self, bus: CommandBus) -> None:
        bus.dispatch(make_command("media.play"))

        with pytest.raises(PreconditionError):
            bus.dispatch(make_command("media.pause", state_revision=0))
        assert bus.state_revision == 1

        with pytest.raises(PreconditionError):
            bus.dispatch(make_command("media.pause", state_hash="sha256:deadbeef"))
        assert bus.state_revision == 1

    def test_matching_preconditions_accepted(self, bus: CommandBus) -> None:
        current_hash = bus.state_hash
        result = bus.dispatch(make_command("media.play", state_revision=0, state_hash=current_hash))
        assert result.state_revision == 1

    def test_idempotency_key_replays_cached_result(self, bus: CommandBus) -> None:
        first = bus.dispatch(
            make_command(
                "timeline.mark",
                input_={"at": "اینجا", "label": "a"},
                idempotency_key="key-1",
            )
        )
        second = bus.dispatch(
            make_command(
                "timeline.mark",
                input_={"at": "اینجا", "label": "a"},
                idempotency_key="key-1",
            )
        )
        assert first.transaction_id == second.transaction_id
        assert len(bus.project.timeline.markers) == 1
        assert bus.state_revision == 1

    def test_malformed_envelope_rejected(self, bus: CommandBus) -> None:
        with pytest.raises(CommandValidationError):
            bus.dispatch({"operation": "media.play", "command_id": ""})
        with pytest.raises(CommandValidationError):
            bus.dispatch(
                {
                    "command_id": "c1",
                    "operation": "media.play",
                    "protocol_version": "nagar.command.v2",
                }
            )
        assert bus.state_revision == 0

    def test_handler_failure_leaves_state_untouched(self, project: Project) -> None:
        registry = CapabilityRegistry()
        original = build_wave1_registry().get_spec("timeline.mark")

        def exploding(_: Project, __: OperationContext) -> OperationOutcome:
            raise RuntimeError("reducer blew up")

        registry.register_operation(
            "timeline",
            "marking",
            OperationSpec(
                operation_id=original.operation_id,
                description=original.description,
                permission_level=original.permission_level,
                input_model=original.input_model,
                handler=exploding,
                reference_fields=original.reference_fields,
            ),
        )
        broken_bus = authorized_bus(project, registry=registry)
        with pytest.raises(CommandExecutionError):
            broken_bus.dispatch(make_command("timeline.mark", input_={"at": "اینجا", "label": "x"}))
        assert broken_bus.state_revision == 0
        assert broken_bus.state_hash == project.state_hash


class TestWave1Catalog:
    def test_default_registry_contains_exactly_the_wave1_operations(self) -> None:
        registry = build_wave1_registry()
        expected = {
            "media.play": "A",
            "media.pause": "A",
            "timeline.mark": "B",
            "timeline.split_at_playhead": "B",
            "system.undo": "A",
        }
        assert set(registry.list_operations()) == set(expected)
        for operation_id, level in expected.items():
            assert registry.get_spec(operation_id).permission_level.value == level
        assert set(registry.list_domains()) == {"media", "system", "timeline"}
