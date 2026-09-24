"""Typed interchange models for the canonical Nagar ↔ OTIO bridge.

These models describe **our** side of the interchange (the parsed, normalized
timeline state plus the documented loss contract).  The emitted OTIO JSON is
built to the real OpenTimelineIO serialisation shapes (``Timeline.1``,
``Stack.1``, ``Track.1``, ``Clip.2``/``Clip.1``, ``Gap.1``, ``Marker.2``,
``ExternalReference.1``, ``RationalTime.1``, ``TimeRange.1``) and is verified
against the real library in ``tests/unit/test_otio_roundtrip.py``.

Time semantics: every exact value travels in integer microseconds; frame values
in the document are the quantised OTIO view (see :data:`LOSS_CONTRACT` in
:mod:`nexus_ai_agent.creative.otio.convert`).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Legal OTIO ``Marker.2`` colors (opentimelineio 0.18 ``MarkerColor``).
MARKER_COLOR_PALETTE: tuple[str, ...] = (
    "BLACK",
    "BLUE",
    "CYAN",
    "GREEN",
    "MAGENTA",
    "ORANGE",
    "PINK",
    "PURPLE",
    "RED",
    "WHITE",
    "YELLOW",
)

#: Deterministic fallback when a Nagar marker color has no palette twin.
MARKER_COLOR_FALLBACK = "WHITE"

#: Free-form aliases folded onto the palette (original preserved in metadata).
MARKER_COLOR_ALIASES: dict[str, str] = {
    "grey": "WHITE",
    "gray": "WHITE",
    "lightgrey": "WHITE",
    "lightgray": "WHITE",
    "darkgrey": "BLACK",
    "darkgray": "BLACK",
    "gold": "YELLOW",
    "teal": "CYAN",
    "cyan-blue": "BLUE",
    "violet": "PURPLE",
    "fuchsia": "MAGENTA",
    "rose": "PINK",
}


class ParsedRangeUS(BaseModel):
    """Half-open exact microsecond range restored from (or carried into) OTIO."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start_us: int = Field(ge=0)
    duration_us: int = Field(ge=0)

    @property
    def end_us(self) -> int:
        return self.start_us + self.duration_us


class ParsedMediaRef(BaseModel):
    """Reconstructed media reference (``ExternalReference.1`` twin)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: str = Field(min_length=1)
    target_url: str = Field(min_length=1)
    content_sha256: str | None = None
    media_kind: Literal["video", "audio", "image", "caption"] | None = None
    duration_us: int = Field(ge=0, default=0)
    color_space: str | None = None


class ParsedClip(BaseModel):
    """One editorial clip: source window + timeline placement + effects."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    clip_id: str = Field(min_length=1)
    name: str = ""
    media_ref: ParsedMediaRef | None = None
    source_range: ParsedRangeUS
    timeline_range: ParsedRangeUS
    effects: tuple[dict[str, Any], ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)


class ParsedGap(BaseModel):
    """One explicit timeline gap (unpopulated span on a track)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = "gap"
    timeline_range: ParsedRangeUS


class ParsedMarker(BaseModel):
    """One timeline marker (``Marker.2`` on the Stack, point → 1 frame)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    marker_id: str = Field(min_length=1)
    label: str
    timecode_us: int = Field(ge=0)
    color: str | None = None
    otio_color: str = MARKER_COLOR_FALLBACK
    comment: str = ""


class ParsedTrack(BaseModel):
    """One ordered track of clips and gaps."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    track_id: str = Field(min_length=1)
    name: str = ""
    kind: Literal["video", "audio", "image", "other"]
    children: tuple[ParsedClip | ParsedGap, ...] = ()
    effects: tuple[dict[str, Any], ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)


class ParsedTimelineState(BaseModel):
    """The normalized Nagar-side timeline restored from an OTIO document."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = ""
    timeline_id: str = "main"
    duration_us: int = Field(ge=0)
    global_start_us: int = Field(ge=0, default=0)
    tracks: tuple[ParsedTrack, ...] = ()
    markers: tuple[ParsedMarker, ...] = ()
    playhead_us: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _track_ids_unique(self) -> ParsedTimelineState:
        ids = [track.track_id for track in self.tracks]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate track ids in parsed timeline: {ids}")
        return self


class LossEntry(BaseModel):
    """One documented, intentional interchange loss (mission §12 contract)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    what: str
    direction: Literal["to_otio", "from_otio", "both"]
    detail: str
    mitigated_by: str = "nagar metadata block"


__all__ = [
    "LossEntry",
    "MARKER_COLOR_ALIASES",
    "MARKER_COLOR_FALLBACK",
    "MARKER_COLOR_PALETTE",
    "ParsedClip",
    "ParsedGap",
    "ParsedMarker",
    "ParsedMediaRef",
    "ParsedRangeUS",
    "ParsedTimelineState",
    "ParsedTrack",
]
