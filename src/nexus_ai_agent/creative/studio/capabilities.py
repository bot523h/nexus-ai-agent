"""Hierarchical capability registry and the Wave 1 operation catalog.

Hierarchy: ``Domain > Capability > OperationSpec``.  The registry is the
single allow-list the command bus consults: unknown operations are rejected
before anything runs, permission levels (A/B/C/D) gate execution, and every
spec carries its typed input schema, the input fields that must be pinned
by the reference resolver at command receipt, and a pure in-memory handler.

Wave 1 catalog (deliberately minimal -- "the intelligent skeleton"):

* ``media.play``                  (A)  playback control, non-destructive
* ``media.pause``                 (A)  playback control, non-destructive
* ``timeline.mark``               (B)  reversible: adds a marker as a transaction
* ``timeline.split_at_playhead``  (B)  reversible: splits a clip as a transaction
* ``system.undo``                 (A)  history control; restores the snapshot
                                     captured by the last transaction

Handlers are pure functions ``(project, context) -> outcome``: they never
mutate their input and never touch I/O, which is what lets the command bus
guarantee atomicity (a failing handler leaves the central state untouched).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from nexus_ai_agent.creative.studio.models import (
    Clip,
    CommandValidationError,
    EditTransaction,
    Marker,
    PermissionLevel,
    Playhead,
    Project,
    TimeRangeUS,
    TypedCommand,
    UndoStackEmptyError,
    UnknownOperationError,
    frame_number_for,
)
from nexus_ai_agent.creative.studio.references import ReferenceInput


@dataclass(frozen=True)
class OperationOutcome:
    """Result of a pure handler run: new state, new history, output payload."""

    project: Project
    history: tuple[EditTransaction, ...]
    output: dict[str, Any]


@dataclass(frozen=True)
class OperationContext:
    command: TypedCommand
    input_data: dict[str, Any]
    history: tuple[EditTransaction, ...]


OperationHandler = Callable[[Project, OperationContext], OperationOutcome]


@dataclass(frozen=True)
class OperationSpec:
    operation_id: str
    description: str
    permission_level: PermissionLevel
    input_model: type[BaseModel]
    handler: OperationHandler
    reference_fields: tuple[str, ...] = ()
    required_packs: tuple[str, ...] = ()
    deterministic: bool = True


@dataclass
class Capability:
    name: str
    description: str = ""
    operations: dict[str, OperationSpec] = dc_field(default_factory=dict)


@dataclass
class Domain:
    name: str
    description: str = ""
    capabilities: dict[str, Capability] = dc_field(default_factory=dict)


@dataclass(frozen=True)
class PermissionDecision:
    operation_id: str
    level: PermissionLevel
    allowed: bool
    reason: str


class CapabilityRegistry:
    """Domain > Capability > Operation allow-list for the command bus."""

    def __init__(self) -> None:
        self._domains: dict[str, Domain] = {}
        self._index: dict[str, OperationSpec] = {}
        self._domain_of: dict[str, str] = {}

    def register_domain(self, name: str, description: str = "") -> None:
        self._domains.setdefault(name, Domain(name=name, description=description))

    def register_operation(self, domain: str, capability: str, spec: OperationSpec) -> None:
        if not spec.operation_id.startswith(f"{domain}."):
            raise ValueError(
                f"operation_id {spec.operation_id!r} does not belong to domain {domain!r}"
            )
        if spec.operation_id in self._index:
            raise ValueError(f"duplicate operation: {spec.operation_id!r}")
        dom = self._domains.setdefault(domain, Domain(name=domain))
        cap = dom.capabilities.setdefault(capability, Capability(name=capability))
        cap.operations[spec.operation_id] = spec
        self._index[spec.operation_id] = spec
        self._domain_of[spec.operation_id] = domain

    def get_spec(self, operation_id: str) -> OperationSpec:
        try:
            return self._index[operation_id]
        except KeyError:
            raise UnknownOperationError(f"unknown operation: {operation_id!r}") from None

    def check_permission(self, operation_id: str, *, confirmed: bool = False) -> PermissionDecision:
        spec = self.get_spec(operation_id)
        level = spec.permission_level
        if level is PermissionLevel.IMMEDIATE:
            return PermissionDecision(
                operation_id, level, True, "level A: immediate, non-destructive"
            )
        if level is PermissionLevel.REVERSIBLE:
            return PermissionDecision(
                operation_id,
                level,
                True,
                "level B: reversible, applied as an atomic edit transaction",
            )
        if level is PermissionLevel.CONFIRMATION:
            if confirmed:
                return PermissionDecision(
                    operation_id, level, True, "level C: explicit confirmation provided"
                )
            return PermissionDecision(
                operation_id, level, False, "level C: requires explicit confirmation"
            )
        return PermissionDecision(operation_id, level, False, "level D: denied by policy")

    def list_domains(self) -> list[str]:
        return sorted(self._domains)

    def list_operations(self, domain: str | None = None) -> list[str]:
        if domain is None:
            return sorted(self._index)
        return sorted(op for op, owner in self._domain_of.items() if owner == domain)

    def __contains__(self, operation_id: object) -> bool:
        return operation_id in self._index


# ---------------------------------------------------------------------------
# Wave 1 typed operation inputs
# ---------------------------------------------------------------------------


class PlayCommandInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: ReferenceInput | None = None


class PauseCommandInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MarkCommandInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at: ReferenceInput
    label: str = Field(min_length=1, max_length=200)
    color: str | None = None


class SplitCommandInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at: ReferenceInput = "اینجا"


class UndoCommandInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Wave 1 pure handlers
# ---------------------------------------------------------------------------


def _resolved_playhead(input_data: dict[str, Any], key: str) -> Playhead:
    raw = input_data.get(key)
    if raw is None:
        raise CommandValidationError(f"operation requires reference field {key!r}")
    return Playhead.model_validate(raw)


def _media_play(project: Project, context: OperationContext) -> OperationOutcome:
    playhead = project.timeline.playhead
    start: Playhead | None = None
    if context.input_data.get("start") is not None:
        start = Playhead.model_validate(context.input_data["start"])
    position = start.timecode_us if start is not None else playhead.timecode_us
    new_playhead = Playhead(
        timecode_us=position,
        frame_number=frame_number_for(position, playhead.timebase),
        timebase=playhead.timebase,
        captured_at_command=True,
        is_playing=True,
    )
    new_timeline = project.timeline.model_copy(update={"playhead": new_playhead})
    new_project = project.model_copy(update={"timeline": new_timeline})
    return OperationOutcome(
        new_project, context.history, {"is_playing": True, "timecode_us": position}
    )


def _media_pause(project: Project, context: OperationContext) -> OperationOutcome:
    playhead = project.timeline.playhead
    new_playhead = playhead.model_copy(update={"is_playing": False})
    new_timeline = project.timeline.model_copy(update={"playhead": new_playhead})
    new_project = project.model_copy(update={"timeline": new_timeline})
    return OperationOutcome(
        new_project, context.history, {"is_playing": False, "timecode_us": playhead.timecode_us}
    )


def _timeline_mark(project: Project, context: OperationContext) -> OperationOutcome:
    at = _resolved_playhead(context.input_data, "at")
    label: str = context.input_data["label"]
    color: str | None = context.input_data.get("color")
    marker = Marker(
        marker_id=f"mk_{uuid.uuid4().hex[:12]}",
        timecode_us=at.timecode_us,
        label=label,
        color=color,
    )
    new_markers = [*project.timeline.markers, marker]
    new_timeline = project.timeline.model_copy(update={"markers": new_markers})
    new_project = project.model_copy(update={"timeline": new_timeline})
    return OperationOutcome(
        new_project,
        context.history,
        {"marker_id": marker.marker_id, "timecode_us": at.timecode_us},
    )


def _timeline_split_at_playhead(project: Project, context: OperationContext) -> OperationOutcome:
    target = context.command.target
    track_id = target.track_id
    clip_id = target.clip_id
    if not track_id or not clip_id:
        raise CommandValidationError(
            "timeline.split_at_playhead requires target.track_id and target.clip_id"
        )
    at = _resolved_playhead(context.input_data, "at")
    track = next((t for t in project.timeline.tracks if t.track_id == track_id), None)
    if track is None:
        raise CommandValidationError(f"track not found: {track_id!r}")
    clip = next((c for c in track.clips if c.clip_id == clip_id), None)
    if clip is None:
        raise CommandValidationError(f"clip not found: {clip_id!r} on track {track_id!r}")
    tl_start = clip.timeline_range.start_us
    tl_end = clip.timeline_range.end_us
    if not (tl_start < at.timecode_us < tl_end):
        raise CommandValidationError(
            f"split point {at.timecode_us}us must be strictly inside the clip "
            f"timeline range [{tl_start}us, {tl_end}us)"
        )
    left_duration_us = at.timecode_us - tl_start
    left_clip = Clip(
        clip_id=clip.clip_id,
        media_ref=clip.media_ref,
        source_range=TimeRangeUS(
            start_us=clip.source_range.start_us,
            end_us=clip.source_range.start_us + left_duration_us,
        ),
        timeline_range=TimeRangeUS(start_us=tl_start, end_us=at.timecode_us),
    )
    right_clip = Clip(
        clip_id=f"clip_{uuid.uuid4().hex[:12]}",
        media_ref=clip.media_ref,
        source_range=TimeRangeUS(
            start_us=clip.source_range.start_us + left_duration_us,
            end_us=clip.source_range.end_us,
        ),
        timeline_range=TimeRangeUS(start_us=at.timecode_us, end_us=tl_end),
    )
    new_clips: list[Clip] = []
    for existing in track.clips:
        if existing.clip_id == clip.clip_id:
            new_clips.append(left_clip)
            new_clips.append(right_clip)
        else:
            new_clips.append(existing)
    new_track = track.model_copy(update={"clips": new_clips})
    new_tracks = [new_track if t.track_id == track_id else t for t in project.timeline.tracks]
    new_timeline = project.timeline.model_copy(update={"tracks": new_tracks})
    new_project = project.model_copy(update={"timeline": new_timeline})
    source_unchanged = (
        left_clip.media_ref.content_sha256 == clip.media_ref.content_sha256
        and right_clip.media_ref.content_sha256 == clip.media_ref.content_sha256
    )
    return OperationOutcome(
        new_project,
        context.history,
        {
            "left": {
                "clip_id": left_clip.clip_id,
                "timeline_range": left_clip.timeline_range.model_dump(),
            },
            "right": {
                "clip_id": right_clip.clip_id,
                "timeline_range": right_clip.timeline_range.model_dump(),
            },
            "source_unchanged": source_unchanged,
        },
    )


def _system_undo(project: Project, context: OperationContext) -> OperationOutcome:
    history = context.history
    target_index: int | None = None
    for index in range(len(history) - 1, -1, -1):
        if history[index].operation != "system.undo":
            target_index = index
            break
    if target_index is None:
        raise UndoStackEmptyError(
            "nothing to undo: no editable transaction remains on the history stack"
        )
    last = history[target_index]
    restored = Project.model_validate(last.state_before)
    remaining = history[:target_index] + history[target_index + 1 :]
    return OperationOutcome(
        restored,
        remaining,
        {
            "undone_operation": last.operation,
            "undone_transaction_id": last.transaction_id,
            "restored_state_hash": last.previous_state_hash,
        },
    )


# ---------------------------------------------------------------------------
# Wave 1 catalog
# ---------------------------------------------------------------------------


def build_wave1_registry() -> CapabilityRegistry:
    """The default registry: exactly the five Wave 1 operations."""
    registry = CapabilityRegistry()
    registry.register_domain("media", "Playback and media transport control")
    registry.register_domain("timeline", "Non-destructive timeline editing")
    registry.register_domain("system", "Studio session and history control")

    registry.register_operation(
        "media",
        "playback",
        OperationSpec(
            operation_id="media.play",
            description="Start (or resume) playback; optionally jump to a pinned reference first.",
            permission_level=PermissionLevel.IMMEDIATE,
            input_model=PlayCommandInput,
            handler=_media_play,
            reference_fields=("start",),
            deterministic=True,
        ),
    )
    registry.register_operation(
        "media",
        "playback",
        OperationSpec(
            operation_id="media.pause",
            description="Pause playback at the current pinned position.",
            permission_level=PermissionLevel.IMMEDIATE,
            input_model=PauseCommandInput,
            handler=_media_pause,
            deterministic=True,
        ),
    )
    registry.register_operation(
        "timeline",
        "marking",
        OperationSpec(
            operation_id="timeline.mark",
            description="Add a named marker at a pinned reference.",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=MarkCommandInput,
            handler=_timeline_mark,
            reference_fields=("at",),
            deterministic=True,
        ),
    )
    registry.register_operation(
        "timeline",
        "splitting",
        OperationSpec(
            operation_id="timeline.split_at_playhead",
            description="Split the targeted clip at a pinned reference; source media untouched.",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=SplitCommandInput,
            handler=_timeline_split_at_playhead,
            reference_fields=("at",),
            deterministic=True,
        ),
    )
    registry.register_operation(
        "system",
        "history",
        OperationSpec(
            operation_id="system.undo",
            description="Rewind the most recent editable transaction (classic NLE undo).",
            permission_level=PermissionLevel.IMMEDIATE,
            input_model=UndoCommandInput,
            handler=_system_undo,
            deterministic=True,
        ),
    )
    return registry
