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

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from nexus_ai_agent.creative.studio.models import (
    CapabilityError,
    CapabilitySnapshot,
    CapabilityVersionError,
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
    UnknownCapabilityError,
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
ExecutionMode = Literal["local", "preview"]
_VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def _semver(version: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(version)
    if match is None:
        raise ValueError(f"invalid capability version: {version!r} (expected MAJOR.MINOR.PATCH)")
    return int(match[1]), int(match[2]), int(match[3])


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
    schema_version: int = 1
    execution_modes: tuple[ExecutionMode, ...] = ("local",)
    required_permissions: tuple[str, ...] = ()

    @property
    def effective_permissions(self) -> tuple[str, ...]:
        baseline = (
            "project:read"
            if self.permission_level is PermissionLevel.IMMEDIATE
            else "project:write"
        )
        return tuple(sorted({baseline, *self.required_permissions}))


@dataclass
class Capability:
    name: str
    capability_id: str
    version: str = "1.0.0"
    available: bool = True
    description: str = ""
    operations: dict[str, OperationSpec] = dc_field(default_factory=dict)


@dataclass
class Domain:
    name: str
    description: str = ""
    capabilities: dict[str, Capability] = dc_field(default_factory=dict)


class CapabilityDescription(BaseModel):
    """Authoritative, JSON-serializable registry view for API/local clients."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_id: str
    version: str
    operations: tuple[str, ...]
    operation: str
    operation_schema_version: int
    operation_schema: dict[str, Any]
    available: bool
    execution_modes: tuple[ExecutionMode, ...]
    required_permissions: tuple[str, ...]
    required_packs: tuple[str, ...]
    pack_provider: str

    def snapshot(self) -> CapabilitySnapshot:
        return CapabilitySnapshot(
            capability_id=self.capability_id,
            operation=self.operation,
            version=self.version,
            operation_schema_version=self.operation_schema_version,
            pack_provider=self.pack_provider,
        )


@dataclass(frozen=True)
class PermissionDecision:
    operation_id: str
    level: PermissionLevel
    allowed: bool
    reason: str


class CapabilityRegistry:
    """Domain > Capability > Operation; the *only* operation allow-list."""

    def __init__(self) -> None:
        self._domains: dict[str, Domain] = {}
        self._index: dict[str, OperationSpec] = {}
        self._domain_of: dict[str, str] = {}
        self._capability_of: dict[str, Capability] = {}
        self._capabilities: dict[str, Capability] = {}

    def register_domain(self, name: str, description: str = "") -> None:
        self._domains.setdefault(name, Domain(name=name, description=description))

    def register_operation(
        self,
        domain: str,
        capability: str,
        spec: OperationSpec,
        *,
        capability_version: str = "1.0.0",
        available: bool = True,
    ) -> None:
        if not spec.operation_id.startswith(f"{domain}."):
            raise ValueError(
                f"operation_id {spec.operation_id!r} does not belong to domain {domain!r}"
            )
        if spec.operation_id in self._index:
            raise ValueError(f"duplicate operation: {spec.operation_id!r}")
        if spec.input_model.model_config.get("extra") != "forbid":
            raise ValueError(f"{spec.operation_id}: operation schema must forbid extra fields")
        if spec.schema_version < 1 or not spec.execution_modes:
            raise ValueError(f"{spec.operation_id}: invalid schema version or execution modes")
        if any(mode not in ("local", "preview") for mode in spec.execution_modes):
            raise ValueError(f"{spec.operation_id}: unknown execution mode")
        _semver(capability_version)
        capability_id = f"{domain}.{capability}"
        dom = self._domains.setdefault(domain, Domain(name=domain))
        cap = dom.capabilities.get(capability)
        if cap is None:
            cap = Capability(
                name=capability,
                capability_id=capability_id,
                version=capability_version,
                available=available,
            )
            dom.capabilities[capability] = cap
            self._capabilities[capability_id] = cap
        elif cap.version != capability_version or cap.available != available:
            raise ValueError(f"{capability_id}: conflicting capability version/availability")
        cap.operations[spec.operation_id] = spec
        self._index[spec.operation_id] = spec
        self._domain_of[spec.operation_id] = domain
        self._capability_of[spec.operation_id] = cap

    def get_spec(self, operation_id: str) -> OperationSpec:
        try:
            return self._index[operation_id]
        except KeyError:
            raise UnknownOperationError(f"unknown operation: {operation_id!r}") from None

    def get_capability(self, capability_id: str) -> Capability:
        try:
            return self._capabilities[capability_id]
        except KeyError:
            raise UnknownCapabilityError(f"unknown capability: {capability_id!r}") from None

    def describe(self, operation_id: str) -> CapabilityDescription:
        spec = self.get_spec(operation_id)
        cap = self._capability_of[operation_id]
        return CapabilityDescription(
            capability_id=cap.capability_id,
            version=cap.version,
            operations=tuple(sorted(cap.operations)),
            operation=operation_id,
            operation_schema_version=spec.schema_version,
            operation_schema=spec.input_model.model_json_schema(),
            available=cap.available,
            execution_modes=spec.execution_modes,
            required_permissions=spec.effective_permissions,
            required_packs=spec.required_packs,
            pack_provider=spec.required_packs[0] if spec.required_packs else "nagar.core",
        )

    def check_capability(self, command: TypedCommand) -> CapabilityDescription:
        """Treat the client's snapshot as a hint, never as a declaration."""
        descriptor = self.describe(command.operation)
        snapshot = command.capability_snapshot
        if snapshot is not None:
            self.get_capability(snapshot.capability_id)  # unknown id fails separately
            if (
                snapshot.capability_id != descriptor.capability_id
                or snapshot.operation != command.operation
            ):
                raise CapabilityError("capability snapshot does not match the requested operation")
            if snapshot.pack_provider != descriptor.pack_provider:
                raise CapabilityError("capability snapshot has a forged provider")
            required = _semver(snapshot.version)
            installed = _semver(descriptor.version)
            if required[0] != installed[0] or required > installed:
                raise CapabilityVersionError(
                    "capability version is not compatible with this runtime"
                )
            if snapshot.operation_schema_version != descriptor.operation_schema_version:
                raise CapabilityVersionError(
                    "capability snapshot has an incompatible operation schema"
                )
        if not descriptor.available:
            raise CapabilityError(f"capability is unavailable: {descriptor.capability_id}")
        return descriptor

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
            required_permissions=("project:write",),
            deterministic=True,
        ),
    )
    return registry
