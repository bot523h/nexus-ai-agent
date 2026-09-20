"""Typed state skeleton for the Nagar Green Cockpit (Phase 6, Wave 1).

This module is the single source of truth for the *data* side of the green
cockpit: the project/timeline/track/clip models, the playhead and marker,
the typed command envelope, the edit-transaction record used for undo, and
the state-hash contract.

Design constraints (Wave 1, non-negotiable):

* No UI of any kind -- no React, DOM or Canvas code.  Input is a typed JSON
  command; output is an in-memory state update.
* No heavy dependencies -- no ``torch``, ``transformers`` or CV imports.
* No ``storage/`` or ``llm/`` imports -- the studio core reads settings only
  through explicit composition outside this package.

Time reference is integer microseconds (``timecode_us``).  Frame numbers are
derived conveniences (see TDD black swan #1 on VFR), never the source of
truth.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROTOCOL_VERSION: Literal["nagar.command.v1"] = "nagar.command.v1"
MICROSECONDS_PER_SECOND = 1_000_000


# ---------------------------------------------------------------------------
# Errors (typed contract of the green cockpit core)
# ---------------------------------------------------------------------------


class NagarError(Exception):
    """Base error for the Nagar green cockpit core."""


class CommandValidationError(NagarError):
    """The command envelope or an operation input failed validation."""


class UnknownOperationError(CommandValidationError):
    """The operation id is not present in the CapabilityRegistry."""


class PermissionDeniedError(NagarError):
    """The registry permission gate (A/B/C/D) rejected the command."""


class PreconditionError(NagarError):
    """Command preconditions do not match the current state revision/hash."""


class ReferenceResolutionError(NagarError):
    """A reference expression could not be pinned to a timecode."""


class CommandExecutionError(NagarError):
    """An operation handler failed while executing."""


class UndoStackEmptyError(CommandExecutionError):
    """``system.undo`` was requested but the transaction history is empty."""


class PermissionLevel(str, Enum):
    """Permission ladder from the Nagar TDD (section 1.1).

    * A -- Immediate: read/analysis/playback control; non-destructive.
    * B -- Reversible: timeline/effect changes applied as an atomic
      ``EditTransaction`` with undo.
    * C -- Confirmation: heavy or identity-changing operations that need an
      explicit confirmation flag (none registered in Wave 1).
    * D -- Denied: shell, raw network upload, code execution, unregistered
      operations.
    """

    IMMEDIATE = "A"
    REVERSIBLE = "B"
    CONFIRMATION = "C"
    DENIED = "D"


# ---------------------------------------------------------------------------
# Time primitives
# ---------------------------------------------------------------------------


class TimeBase(BaseModel):
    model_config = ConfigDict(frozen=True)

    numerator: int = Field(gt=0)
    denominator: int = Field(gt=0)


class TimeRangeUS(BaseModel):
    """Half-open ``[start_us, end_us)`` range in integer microseconds."""

    model_config = ConfigDict(frozen=True)

    start_us: int = Field(ge=0)
    end_us: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_ordering(self) -> TimeRangeUS:
        if self.start_us >= self.end_us:
            raise ValueError(
                f"TimeRangeUS requires start_us < end_us, got {self.start_us}..{self.end_us}"
            )
        return self


def frame_number_for(timecode_us: int, timebase: TimeBase) -> int:
    """Derive a frame index from a timecode (approximate under VFR).

    ``timecode_us`` remains the authoritative reference; this exists so the
    command envelope can carry the TDD ``PlayheadRef`` shape.
    """
    return timecode_us * timebase.numerator // (timebase.denominator * MICROSECONDS_PER_SECOND)


def _default_timebase() -> TimeBase:
    return TimeBase(numerator=30, denominator=1)


# ---------------------------------------------------------------------------
# Media / timeline models
# ---------------------------------------------------------------------------


class MediaRef(BaseModel):
    """Reference to an immutable source asset (the source is never mutated)."""

    asset_id: str
    content_sha256: str
    media_kind: Literal["video", "audio", "image"]
    duration_us: int = Field(ge=0)
    timebase: TimeBase = Field(default_factory=_default_timebase)
    color_space: str | None = None


class Playhead(BaseModel):
    """Pinned playhead position.

    ``captured_at_command=True`` marks that ``timecode_us`` was frozen at the
    moment the command was received -- later operations must never
    re-interpret the originating expression against a moved playhead.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    timecode_us: int = Field(ge=0)
    frame_number: int | None = None
    timebase: TimeBase = Field(default_factory=_default_timebase)
    captured_at_command: bool = True
    is_playing: bool = False


class Marker(BaseModel):
    marker_id: str
    timecode_us: int = Field(ge=0)
    label: str
    color: str | None = None


class Clip(BaseModel):
    """Non-destructive clip: a window on an immutable asset, placed on a track.

    ``source_range`` is the window into the asset; ``timeline_range`` is the
    editorial placement on the track.  Splitting/trimming only rewrites these
    windows -- the underlying ``MediaRef`` (and its hash) is untouched.
    """

    clip_id: str
    media_ref: MediaRef
    source_range: TimeRangeUS
    timeline_range: TimeRangeUS


class Track(BaseModel):
    track_id: str
    name: str
    kind: Literal["video", "audio", "image"]
    clips: list[Clip] = Field(default_factory=list)


class Timeline(BaseModel):
    timeline_id: str
    duration_us: int = Field(ge=0)
    tracks: list[Track] = Field(default_factory=list)
    markers: list[Marker] = Field(default_factory=list)
    playhead: Playhead = Field(default_factory=lambda: Playhead(timecode_us=0))


# ---------------------------------------------------------------------------
# Central project state
# ---------------------------------------------------------------------------


class Project(BaseModel):
    """The central in-memory state of one studio project.

    ``state_hash`` is *derived*: every construction recomputes it from the
    content (``project_id``, ``name``, ``timeline``) via a canonical
    serialization, so a stored hash can never drift from the state it claims
    to describe.  ``state_revision`` is a monotonic counter owned by the
    command bus; it is deliberately excluded from the hash so that
    revision+hash pairs remain stable preconditions across undo cycles.
    """

    project_id: str
    name: str
    timeline: Timeline
    state_revision: int = Field(ge=0, default=0)
    state_hash: str = ""

    @model_validator(mode="after")
    def _maintain_state_hash(self) -> Project:
        self.state_hash = compute_state_hash(self)
        return self


def compute_state_hash(project: Project) -> str:
    """Canonical content hash of a project state (revision excluded)."""
    payload = {
        "project_id": project.project_id,
        "name": project.name,
        "timeline": project.timeline.model_dump(mode="json"),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def new_project(project_id: str, name: str, timeline: Timeline) -> Project:
    """Build a fresh project at revision 0 with a verified state hash."""
    return Project(
        project_id=project_id, name=name, timeline=timeline, state_revision=0, state_hash=""
    )


# ---------------------------------------------------------------------------
# Typed command envelope (Nagar protocol v1)
# ---------------------------------------------------------------------------


class TargetRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str | None = None
    track_id: str | None = None
    clip_id: str | None = None


class Preconditions(BaseModel):
    """Optional optimistic-concurrency gate evaluated by the command bus."""

    state_revision: int | None = Field(default=None, ge=0)
    state_hash: str | None = None


class TypedCommand(BaseModel):
    """Canonical typed command: the only input the studio accepts."""

    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal["nagar.command.v1"] = PROTOCOL_VERSION
    command_id: str = Field(min_length=1)
    session_id: str | None = None
    operation: str = Field(min_length=1)
    target: TargetRef = Field(default_factory=TargetRef)
    input: dict[str, Any] = Field(default_factory=dict)
    preconditions: Preconditions = Field(default_factory=Preconditions)
    idempotency_key: str | None = None
    confirmed: bool = False


# ---------------------------------------------------------------------------
# Transactions and results
# ---------------------------------------------------------------------------


class EditTransaction(BaseModel):
    """One atomic state change, revision+snapshot based (TDD decision 6).

    Undo restores ``state_before`` (the full in-memory snapshot taken *before*
    the change) and is verified against ``previous_state_hash`` /
    ``new_state_hash``.  Wave 1 stores a full snapshot per transaction
    (cheap and exact in-memory); content-addressed inverse patches are a
    later optimization that must not change this contract.
    """

    transaction_id: str
    command_id: str
    operation: str
    permission_level: PermissionLevel
    parent_revision: int = Field(ge=0)
    previous_state_hash: str
    new_state_hash: str
    state_before: dict[str, Any]


class CommandResult(BaseModel):
    """Standard operation output envelope (TDD section 1.1)."""

    transaction_id: str
    status: Literal["applied"] = "applied"
    state_revision: int = Field(ge=0)
    state_hash: str
    output: dict[str, Any] = Field(default_factory=dict)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    undo_available: bool = True
