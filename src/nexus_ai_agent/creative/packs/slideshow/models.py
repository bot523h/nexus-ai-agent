"""Typed models for the ``nexus.slideshow.compose`` pack.

Two layers, deliberately separated:

* **evidence** (:class:`AssetEvidence`, :class:`BeatGrid`,
  :class:`SlideshowAnalysis`) is produced *above* the command bus by the pack's
  services — probing files, decoding audio, optionally asking a hosted model.
  It is then pinned into the typed command, exactly like Wave 1 pins reference
  expressions at receipt.
* **the plan** (:class:`SlideshowPlan`) is computed by the pure handlers inside
  the bus.  Nothing here touches the filesystem or the network, which is what
  keeps the atomic-apply guarantee intact.

Everything is JSON-serializable so a command can be produced by an agent, a CLI
or a human and replayed deterministically.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexus_ai_agent.creative.studio.models import TimeRangeUS

SLIDESHOW_PACKAGE_ID = "nexus.slideshow.compose"
OPERATION_COMPOSE = "slideshow.compose"
OPERATION_RENDER = "slideshow.render"
OPERATION_UPSCALE = "slideshow.upscale"

#: The owner-approved target durations (30 s, 1, 2 and 5 minutes).  The 30 s
#: entry was added by the Wave 2.5 revision r7: the Telegram surface caps every
#: request at half a minute, and a ceiling must be a representable plan target.
TARGET_DURATIONS_US = (30_000_000, 60_000_000, 120_000_000, 300_000_000)
TargetDurationUS = Literal[30_000_000, 60_000_000, 120_000_000, 300_000_000]

#: Soft guard rails from the product spec (10-20 images).  Outside them the plan
#: still works but reports a warning instead of silently pretending.
RECOMMENDED_IMAGE_COUNT = (10, 20)

NonEmptyStr = Annotated[str, Field(min_length=1)]


class AssetEvidence(BaseModel):
    """Facts about one input file, probed **before** the command was dispatched.

    The handler only sees this record — it never stats a path — which is what
    keeps the operation pure and the state transition replayable.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: NonEmptyStr
    path: NonEmptyStr
    content_sha256: NonEmptyStr
    media_kind: Literal["image", "audio"]
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    duration_us: int = Field(default=0, ge=0)
    mean_luma: float = Field(default=0.5, ge=0.0, le=1.0)
    captured_at: datetime | None = None


class ImageScore(BaseModel):
    """One image's score sheet, as returned by an analyzer (auto mode)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: NonEmptyStr
    labels: tuple[str, ...] = ()
    sharpness: float = Field(ge=0.0, le=1.0)
    aesthetic: float = Field(ge=0.0, le=1.0)
    subject: str = ""
    suggested_role: Literal["opener", "body", "climax", "closer"] = "body"


class SlideshowAnalysis(BaseModel):
    """Ordered, scored interpretation of the image set (auto mode)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ordered_evidence_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)
    scores: tuple[ImageScore, ...] = ()
    recommended_template_id: str | None = None
    alignment_quality: Literal["detected", "interpolated"] = "interpolated"
    reasoning: str = ""
    source: Literal["gemini", "local_heuristic"] = "local_heuristic"


class BeatGrid(BaseModel):
    """Beat evidence for the soundtrack — never a claim of precision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_path: str = ""
    duration_us: int = Field(ge=0)
    tempo_bpm: float | None = Field(default=None, gt=0)
    beats_us: tuple[int, ...] = ()
    strong_beats_us: tuple[int, ...] = ()
    tempo_source: Literal["detected", "fallback_bpm", "equal_division"] = "equal_division"
    alignment_quality: Literal["detected", "interpolated"] = "interpolated"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _non_negative_and_ordered(self) -> BeatGrid:
        if any(beat < 0 for beat in self.beats_us):
            raise ValueError("beats_us must be non-negative")
        if tuple(sorted(self.beats_us)) != self.beats_us:
            raise ValueError("beats_us must be ordered")
        return self


class ShotSelection(BaseModel):
    """Manual mode: the user's explicit order and per-image duration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: NonEmptyStr
    duration_us: int = Field(gt=0)


class ComposeInput(BaseModel):
    """The typed payload of ``slideshow.compose`` (protocol ``nagar.command.v1``).

    The payload carries **evidence**, not paths: every asset was probed, hashed
    and (optionally) analyzed above the command bus.  The pure handler therefore
    never touches the filesystem, and the same command can be replayed verbatim.
    """

    model_config = ConfigDict(extra="forbid")

    assets: tuple[AssetEvidence, ...] = Field(min_length=1)
    target_duration_us: TargetDurationUS
    mode: Literal["auto", "manual"] = "auto"
    tone_template_id: str | None = None
    audio: AssetEvidence | None = None
    beat_grid: BeatGrid | None = None
    analysis: SlideshowAnalysis | None = None
    shots: tuple[ShotSelection, ...] | None = None
    resolution: str = Field(pattern=r"^\d{2,5}x\d{2,5}$", default="1920x1080")
    aspect_ratio: str = Field(pattern=r"^\d{1,2}:\d{1,2}$", default="16:9")
    fps: int | None = Field(default=None, ge=12, le=60)

    @model_validator(mode="after")
    def _mode_requires_its_payload(self) -> ComposeInput:
        images = [asset for asset in self.assets if asset.media_kind == "image"]
        if len(images) != len(self.assets):
            raise ValueError("'assets' must contain image evidence only; pass audio via 'audio'")
        if self.mode == "manual":
            if not self.shots:
                raise ValueError("mode='manual' requires an explicit 'shots' list")
            known = {asset.evidence_id for asset in self.assets}
            unknown = [shot.evidence_id for shot in self.shots if shot.evidence_id not in known]
            if unknown:
                raise ValueError(f"shots reference unknown evidence ids: {unknown}")
            if len(self.shots) != len(self.assets):
                raise ValueError("'shots' must cover every image exactly once")
            if len({shot.evidence_id for shot in self.shots}) != len(self.shots):
                raise ValueError("'shots' must not reference an image twice")
        if self.audio is not None and self.audio.media_kind != "audio":
            raise ValueError("'audio' must be an audio asset")
        return self


class ShotPlan(BaseModel):
    """One placed shot: a contiguous timeline slot plus its effect parameters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: NonEmptyStr
    slot: TimeRangeUS
    transition_in: dict[str, Any] | None = None
    motion: dict[str, Any] = Field(default_factory=dict)
    color: dict[str, Any] = Field(default_factory=dict)


class SlideshowPlan(BaseModel):
    """The deterministic result of planning: slots sum exactly to the target."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    package_id: Literal["nexus.slideshow.compose"] = "nexus.slideshow.compose"
    template_id: NonEmptyStr
    mode: Literal["auto", "manual"]
    target_duration_us: int = Field(gt=0)
    shots: tuple[ShotPlan, ...] = Field(min_length=1)
    audio: dict[str, Any] = Field(default_factory=dict)
    render_profile: dict[str, Any] = Field(default_factory=dict)
    alignment_quality: Literal["detected", "interpolated"] = "interpolated"
    tempo_source: Literal["detected", "fallback_bpm", "equal_division"] = "equal_division"
    beat_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _slots_are_contiguous_and_exact(self) -> SlideshowPlan:
        if self.shots[0].slot.start_us != 0:
            raise ValueError("the first shot must start at 0")
        for previous, current in zip(self.shots, self.shots[1:], strict=False):
            if previous.slot.end_us != current.slot.start_us:
                raise ValueError(
                    "shots must tile the timeline without gaps or overlaps: "
                    f"{previous.slot.end_us} != {current.slot.start_us}"
                )
        if self.shots[-1].slot.end_us != self.target_duration_us:
            raise ValueError(
                f"the last shot must end at target_duration_us: "
                f"{self.shots[-1].slot.end_us} != {self.target_duration_us}"
            )
        return self


class UpscaleInput(BaseModel):
    """Pinned evidence for one local Lanczos image upscale (level B).

    FFmpeg runs above the command bus.  This payload records its measured
    result, keeping the handler pure and the derived image undoable.
    """

    model_config = ConfigDict(extra="forbid")

    source_asset_id: NonEmptyStr
    source_sha256: NonEmptyStr
    output_path: NonEmptyStr
    output_sha256: NonEmptyStr
    source_width: int = Field(gt=0)
    source_height: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    scale_factor: float | None = Field(default=None, gt=1.0, le=4.0)
    target_resolution: str | None = Field(default=None, pattern=r"^\d{2,5}x\d{2,5}$")
    filter_flags: Literal["lanczos"] = "lanczos"

    @model_validator(mode="after")
    def _one_target_and_measured_dimensions(self) -> UpscaleInput:
        if (self.scale_factor is None) == (self.target_resolution is None):
            raise ValueError("provide exactly one of scale_factor or target_resolution")
        if self.scale_factor is not None:
            expected = (
                round(self.source_width * self.scale_factor),
                round(self.source_height * self.scale_factor),
            )
        else:
            raw_width, raw_height = (self.target_resolution or "").split("x", 1)
            expected = (int(raw_width), int(raw_height))
        if (self.width, self.height) != expected:
            raise ValueError(
                f"measured output dimensions {(self.width, self.height)} do not match {expected}"
            )
        return self


class RenderInput(BaseModel):
    """The typed payload of ``slideshow.render`` (level C, needs ``confirmed``).

    Encoding is performed by the allow-listed native adapter (Wave 2c); this
    command carries the *pinned facts* of the produced file, so the state
    transition stays pure while the artifact itself stays verifiable: the
    content hash and duration are recorded together with the render IR hash and
    the state hash the master was rendered from.
    """

    model_config = ConfigDict(extra="forbid")

    output_path: NonEmptyStr
    output_sha256: NonEmptyStr
    duration_us: int = Field(gt=0)
    parent_asset_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)
    render_ir_hash: str = ""
    state_hash_before_render: str = ""
    encoder: dict[str, Any] = Field(default_factory=dict)
    template_id: str = ""


__all__ = [
    "AssetEvidence",
    "BeatGrid",
    "ComposeInput",
    "ImageScore",
    "OPERATION_COMPOSE",
    "OPERATION_RENDER",
    "OPERATION_UPSCALE",
    "RECOMMENDED_IMAGE_COUNT",
    "RenderInput",
    "SLIDESHOW_PACKAGE_ID",
    "ShotPlan",
    "ShotSelection",
    "SlideshowAnalysis",
    "SlideshowPlan",
    "TARGET_DURATIONS_US",
    "TargetDurationUS",
    "UpscaleInput",
]
