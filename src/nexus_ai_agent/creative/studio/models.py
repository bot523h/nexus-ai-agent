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
import re
import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PROTOCOL_VERSION: Literal["nagar.command.v1"] = "nagar.command.v1"
COMMAND_SCHEMA_VERSION: Literal[2] = 2
MICROSECONDS_PER_SECOND = 1_000_000
_MAX_COMMAND_INPUT_BYTES = 512 * 1024
_REF_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


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
    """The actor, operation permissions or A/B/C/D gate rejected the command."""


class AuthorizationError(PermissionDeniedError):
    """The claimed actor does not have trusted access to this project."""


class CapabilityError(NagarError):
    """A capability was not declared, installed or available for this command."""


class UnknownCapabilityError(CapabilityError):
    """The requested capability is not in the authoritative registry."""


class CapabilityVersionError(CapabilityError):
    """The advertised capability or operation schema version is incompatible."""


class ExecutionPolicyError(PermissionDeniedError):
    """The requested execution mode or confirmation is forbidden."""


class IdempotencyConflictError(CommandValidationError):
    """The same scoped key was previously reserved for a different payload."""


class PreconditionError(NagarError):
    """Command preconditions do not match the current state revision/hash."""


class ReferenceResolutionError(NagarError):
    """A time or project input reference could not be resolved."""


class InputReferenceError(ReferenceResolutionError):
    """An input reference is not a safe, existing member of this project."""


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
    media_kind: Literal["video", "audio", "image", "caption"]
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


def compute_parameters_hash(
    operation: str, parameters: dict[str, Any], time_range: TimeRangeUS
) -> str:
    """Content hash of one effect layer (operation + parameters + range)."""
    payload = {
        "operation": operation,
        "parameters": parameters,
        "range": time_range.model_dump(mode="json"),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class EffectLayerRef(BaseModel):
    """A reversible, content-addressed effect layer (TDD section 1.1).

    Wave 2 keeps the parameters **in state** in addition to their hash: a render
    must be reproducible from the state hash alone, so the hash is derived from
    the parameters rather than trusted as an independent source of truth.
    """

    layer_id: str = Field(default_factory=lambda: f"fx_{uuid.uuid4().hex[:12]}")
    operation: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    range: TimeRangeUS
    reversible: bool = True
    parameters_hash: str = ""

    @model_validator(mode="after")
    def _maintain_parameters_hash(self) -> EffectLayerRef:
        self.parameters_hash = compute_parameters_hash(self.operation, self.parameters, self.range)
        return self


class AssetRecord(BaseModel):
    """One entry of the project asset registry: a source asset or a derived one.

    The registry is additive: Wave 1 states carry no assets and remain valid.
    A record with ``parent_asset_ids`` is a *derived* asset (TDD section 1.1);
    the source asset it came from is never mutated.
    """

    asset_id: str
    media_kind: Literal["video", "audio", "image", "caption"]
    content_sha256: str
    duration_us: int = Field(ge=0, default=0)
    parent_asset_ids: tuple[str, ...] = ()
    provenance: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_derived(self) -> bool:
        return bool(self.parent_asset_ids)


class Clip(BaseModel):
    """Non-destructive clip: a window on an immutable asset, placed on a track.

    ``source_range`` is the window into the asset; ``timeline_range`` is the
    editorial placement on the track.  Splitting/trimming only rewrites these
    windows -- the underlying ``MediaRef`` (and its hash) is untouched.
    ``effects`` holds the clip's reversible effect layers (motion, grade, ...).
    """

    clip_id: str
    media_ref: MediaRef
    source_range: TimeRangeUS
    timeline_range: TimeRangeUS
    effects: list[EffectLayerRef] = Field(default_factory=list)


class Track(BaseModel):
    track_id: str
    name: str
    kind: Literal["video", "audio", "image"]
    clips: list[Clip] = Field(default_factory=list)
    effects: list[EffectLayerRef] = Field(default_factory=list)


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
    content (``project_id``, ``name``, ``timeline``, ``assets``) via a canonical
    serialization, so a stored hash can never drift from the state it claims
    to describe.  ``state_revision`` is a monotonic counter owned by the
    command bus; it is deliberately excluded from the hash so that
    revision+hash pairs remain stable preconditions across undo cycles.
    """

    project_id: str
    name: str
    timeline: Timeline
    assets: list[AssetRecord] = Field(default_factory=list)
    state_revision: int = Field(ge=0, default=0)
    state_hash: str = ""

    @model_validator(mode="after")
    def _maintain_state_hash(self) -> Project:
        self.state_hash = compute_state_hash(self)
        return self


def compute_state_hash(project: Project) -> str:
    """Canonical content hash of a project state (revision excluded).

    Wave 2 extends the hashed payload with the asset registry, so a state that
    only differs by derived assets is still a *different* state for precondition
    purposes.  ``state_revision`` stays excluded and monotonic.
    """
    payload = {
        "project_id": project.project_id,
        "name": project.name,
        "timeline": project.timeline.model_dump(mode="json"),
        "assets": [asset.model_dump(mode="json") for asset in project.assets],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def new_project(project_id: str, name: str, timeline: Timeline) -> Project:
    """Build a fresh project at revision 0 with a verified state hash."""
    return Project(
        project_id=project_id, name=name, timeline=timeline, state_revision=0, state_hash=""
    )


# ---------------------------------------------------------------------------
# Typed command envelope (protocol v1, hardened envelope schema 2)
# ---------------------------------------------------------------------------


def _safe_ref_id(value: str) -> str:
    """Identifiers are not paths, URLs, schemes or percent-encoded paths."""
    if not _REF_ID.fullmatch(value) or ".." in value:
        raise ValueError("reference id must be a local identifier, never a path or URL")
    return value


def _safe_project_id(value: str) -> str:
    # Queue-generated project identities include ':' in their message key;
    # exact comparison to the trusted grant is still required by the bus.
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]*", value) or ".." in value:
        raise ValueError("project reference must be an opaque local identifier")
    return value


class ActorIdentity(BaseModel):
    """A caller's claim; the bus checks it against a trusted, injected grant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["user", "service", "agent"]
    actor_id: str = Field(min_length=1, max_length=128)


class TargetRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # Project identity already lived here in the original v1 envelope. Make it
    # required instead of introducing a second, diverging top-level project_id.
    project_id: str = Field(min_length=1, max_length=128)
    track_id: str | None = Field(default=None, min_length=1, max_length=128)
    clip_id: str | None = Field(default=None, min_length=1, max_length=128)


class Preconditions(BaseModel):
    """Optional optimistic-concurrency gate evaluated only for a new reservation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state_revision: int | None = Field(default=None, ge=0)
    state_hash: str | None = Field(default=None, max_length=128)


class ExecutionPolicy(BaseModel):
    """Requested execution mode; it cannot expand an OperationSpec's modes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["local", "preview"] = "local"
    network_access: Literal[False] = False


class InputRefMetadata(BaseModel):
    """Bounded assertions about an existing asset (never a path or an URL)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    media_kind: Literal["video", "audio", "image", "caption"] | None = None
    content_sha256: str | None = Field(
        default=None, pattern=r"^sha256:[a-fA-F0-9]{64}$", max_length=71
    )


class InputRef(BaseModel):
    """Project-scoped logical id, resolved against Project.assets/timeline."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ref_type: Literal["asset", "clip", "timeline"]
    project_id: str = Field(min_length=1, max_length=128)
    ref_id: str = Field(min_length=1, max_length=128)
    metadata: InputRefMetadata = Field(default_factory=InputRefMetadata)

    _check_project_id = field_validator("project_id")(_safe_project_id)
    _check_ref_id = field_validator("ref_id")(_safe_ref_id)


class CommandProvenance(BaseModel):
    """Required origin metadata; neither identity nor authorization evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["user", "agent", "service", "local"]
    source_id: str = Field(min_length=1, max_length=128)
    reason: str | None = Field(default=None, max_length=500)


class RequestContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: Literal["api", "local", "telegram", "ai"] = "local"
    request_id: str | None = Field(default=None, min_length=1, max_length=128)


class CapabilitySnapshot(BaseModel):
    """An advisory compatibility hint, NEVER an authorization or installation claim."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_id: str = Field(min_length=1, max_length=128)
    operation: str = Field(min_length=1, max_length=128)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$", max_length=32)
    operation_schema_version: int = Field(strict=True, ge=1)
    pack_provider: str = Field(min_length=1, max_length=128)


class TypedCommand(BaseModel):
    """Versioned, JSON-serializable, UI-independent input to CommandBus."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["nagar.command.v1"] = PROTOCOL_VERSION
    schema_version: Literal[2] = COMMAND_SCHEMA_VERSION
    operation_schema_version: int = Field(default=1, strict=True, ge=1)
    command_id: str = Field(min_length=1, max_length=128)
    actor: ActorIdentity
    session_id: str | None = Field(default=None, min_length=1, max_length=128)
    operation: str = Field(min_length=1, max_length=128)
    target: TargetRef
    input: dict[str, Any] = Field(default_factory=dict)
    # A slideshow may contain up to 500 registered images plus its audio bed.
    input_refs: tuple[InputRef, ...] = Field(default=(), max_length=512)
    provenance: CommandProvenance
    request_context: RequestContext = Field(default_factory=RequestContext)
    capability_snapshot: CapabilitySnapshot | None = None
    trace_id: str | None = Field(default=None, min_length=1, max_length=128)
    execution_policy: ExecutionPolicy = Field(default_factory=ExecutionPolicy)
    preconditions: Preconditions = Field(default_factory=Preconditions)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    confirmed: bool = False

    @field_validator("input")
    @classmethod
    def _json_input_only(cls, value: dict[str, Any]) -> dict[str, Any]:
        # The registered operation model performs semantic validation. This
        # gate ensures even a forged typed command can be serialized as JSON.
        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("command input must be finite JSON data") from exc
        if len(encoded.encode("utf-8")) > _MAX_COMMAND_INPUT_BYTES:
            raise ValueError("command input exceeds 512 KiB")
        return value

    @field_validator("idempotency_key")
    @classmethod
    def _non_blank_key(cls, value: str | None) -> str | None:
        if value is not None and (value != value.strip() or any(c.isspace() for c in value)):
            raise ValueError("idempotency_key must not contain whitespace")
        return value


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
