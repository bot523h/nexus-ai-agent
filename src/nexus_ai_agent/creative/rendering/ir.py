"""Typed IR for the ``nexus.apply.lane`` (Wave 8 apply lane).

The lane is the bridge from the packs' pure ``Project`` IR to a real file:
a caller picks :class:`LaneOp` values (never filtergraph syntax), binds asset
ids to staged media paths, and the :mod:`compiler` derives one deterministic
FFmpeg invocation from the resulting :class:`LaneIR`.

Duration algebra (microseconds, integer math — no float drift):

* ``trim``    → ``out_us - in_us``
* ``speed``   → ``duration / factor`` (integer division, minimum 1µs)
* ``reverse`` → unchanged
* ``freeze``  → ``hold_us`` (the stream becomes the held still)
* ``xfade``   → ``d_main + d_other - overlap_us``
* ``title`` / ``loudnorm`` / ``duck`` / ``exposure`` / ``lut`` / ``subtitle`` → unchanged

Photometric exposure (the ``exposure`` op) is a pure value mapping and lives
here next to the IR so the pack twin (``color.adjust_exposure``), the compiler
and the golden tests all read the same numbers — see D-0007 in
``docs/DECISION_LOG.md`` for the sources.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

LANE_PACKAGE_ID = "nexus.apply.lane"

XFADE_KINDS = (
    "fade",
    "wipeleft",
    "wiperight",
    "wipeup",
    "wipedown",
    "slideleft",
    "slideright",
    "circlecrop",
    "dissolve",
)

# ── Photometric exposure mapping ─────────────────────────────────────────────
# Every bound below is read off FFmpeg's own sources rather than prose, because
# the three filters disagree about what "out of range" means:
#
# * ``libavfilter/vf_eq.c`` — ``set_gamma`` runs
#   ``av_clipf(..., 0.1, 10.0)`` and ``set_contrast`` runs
#   ``av_clipf(..., -1000.0, 1000.0)``: out-of-range values are **silently
#   clipped**, so an unclamped gamma of 16 would render as 10 with no warning.
# * ``libavfilter/vf_colortemperature.c`` — ``temperature`` is declared
#   ``AV_OPT_TYPE_FLOAT, {.dbl=6500}, 1000, 40000``: 6500 K is the filter's own
#   default, which is why 6500 K is this lane's neutral.
# * ``libavfilter/vf_colorbalance.c`` — ``gm`` ("set green midtones") is
#   declared ``AV_OPT_TYPE_FLOAT, {.dbl=0}, -1, 1``.
#
# Clipping in the IR (not in FFmpeg) keeps the emitted argv honest: what the
# argv says is what the pixels get.
EQ_GAMMA_MIN = 0.1
EQ_GAMMA_MAX = 10.0
EQ_CONTRAST_MIN = -1000.0
EQ_CONTRAST_MAX = 1000.0
COLOR_TEMPERATURE_MIN_K = 1000
COLOR_TEMPERATURE_MAX_K = 40000
COLOR_TEMPERATURE_NEUTRAL_K = 6500
COLORBALANCE_LIMIT = 1.0
TINT_FULL_SCALE = 50.0


class LaneError(ValueError):
    """A typed, fail-closed lane failure (bad op, unknown asset, missing media)."""


def ev_to_gamma(exposure_ev: float) -> float:
    """Map an EV exposure shift onto ``eq``'s gamma, clamped to FFmpeg's range.

    ``eq``'s LUT (``vf_eq.c`` ``create_lut``) is ``v -> v ** (1 / gamma)``, so
    ``gamma = 2 ** EV`` doubles the exposure per stop: positive EV brightens,
    which is the photographic convention callers expect.  The clamp is a real
    ceiling, not a formality — ``2 ** 4 = 16`` exceeds ``EQ_GAMMA_MAX``, so the
    usable unclamped band is ±log2(10) ≈ ±3.32 EV and the golden tests pin both
    ends as a contract (see ``tests/unit/test_rendering_lane_exposure.py``).
    """
    return max(EQ_GAMMA_MIN, min(EQ_GAMMA_MAX, 2.0**exposure_ev))


def tint_to_gm(tint: float) -> float:
    """Map the pack's −50..+50 tint onto ``colorbalance``'s ``gm`` (−1..+1).

    Sign convention (D-0007): **positive tint pushes green**, negative pushes
    magenta, because ``vf_colorbalance.c`` adds ``gm`` to the *green* midtones.
    The endpoints land exactly on the filter's declared bounds.
    """
    return tint / TINT_FULL_SCALE


class TrimOp(BaseModel):
    """Non-destructive in/out trim of the main stream."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    op: Literal["trim"] = "trim"
    in_us: int = Field(ge=0)
    out_us: int = Field(gt=0)

    @model_validator(mode="after")
    def _validate_range(self) -> TrimOp:
        if self.out_us <= self.in_us:
            raise ValueError(f"out_us ({self.out_us}) must be > in_us ({self.in_us})")
        return self


class SpeedOp(BaseModel):
    """Playback-rate change; audio keeps pitch via an ``atempo`` chain."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    op: Literal["speed"] = "speed"
    factor: float = Field(ge=0.1, le=16.0)


class ReverseOp(BaseModel):
    """Time-reversed playback of the main stream."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    op: Literal["reverse"] = "reverse"


class FreezeOp(BaseModel):
    """Hold the frame at ``at_us`` for ``hold_us`` (stream becomes the still)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    op: Literal["freeze"] = "freeze"
    at_us: int = Field(ge=0)
    hold_us: int = Field(gt=0)


class XfadeOp(BaseModel):
    """Crossfade/wipe from the main stream into ``other_asset_id`` at ``offset_us``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    op: Literal["xfade"] = "xfade"
    other_asset_id: str = Field(min_length=1)
    offset_us: int = Field(ge=0)
    duration_us: int = Field(gt=0)
    kind: str = Field(default="fade")

    @model_validator(mode="after")
    def _validate_kind(self) -> XfadeOp:
        if self.kind not in XFADE_KINDS:
            raise ValueError(f"unsupported xfade kind: {self.kind!r}")
        return self


class TitleOp(BaseModel):
    """Burn a single title line with ``drawtext`` (explicit fontfile at compile)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    op: Literal["title"] = "title"
    text: str = Field(min_length=1, max_length=500)
    font_size: int = Field(default=64, ge=12, le=256)
    color: str = Field(default="#FFFFFF", pattern=r"^#[0-9a-fA-F]{6}$")
    position: Literal["center", "lower_third", "top_header"] = "lower_third"
    start_us: int = Field(default=0, ge=0)
    end_us: int | None = Field(default=None, gt=0)


class LoudnormOp(BaseModel):
    """Two-pass EBU R128 loudness (measure pass first, then linear apply)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    op: Literal["loudnorm"] = "loudnorm"
    target_lufs: float = Field(default=-14.0, ge=-70.0, le=-5.0)
    true_peak_db: float = Field(default=-1.0, ge=-9.0, le=0.0)
    lra: float = Field(default=11.0, ge=1.0, le=20.0)


class DuckOp(BaseModel):
    """Sidechain-duck the main audio under ``voice_asset_id`` (voice stays audible)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    op: Literal["duck"] = "duck"
    voice_asset_id: str = Field(min_length=1)
    attenuation_db: float = Field(default=-12.0, ge=-60.0, le=0.0)
    attack_ms: int = Field(default=100, ge=1, le=2000)
    release_ms: int = Field(default=400, ge=1, le=5000)


class ExposureOp(BaseModel):
    """Photometric exposure + white balance — the executable twin of ``color.adjust_exposure``.

    Field bounds mirror the delivery pack's :class:`AdjustExposureInput` where
    the pack is the stricter of the two (EV ±4, contrast 0.2–3.0, tint ±50), and
    widen only where the pack is artificially tight: ``temperature_k`` accepts
    the full 1000–40000 K that ``colortemperature`` declares, so the lane can
    render anything the pack can describe and more.  Duration is unaffected.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    op: Literal["exposure"] = "exposure"
    exposure_ev: float = Field(
        default=0.0,
        ge=-4.0,
        le=4.0,
        description="Exposure shift in stops; compiled as eq gamma = clamp(2**EV, 0.1, 10).",
    )
    contrast: float = Field(
        default=1.0,
        ge=0.2,
        le=3.0,
        description="Contrast slope, passed straight to eq (declared range -1000..1000).",
    )
    temperature_k: int = Field(
        default=COLOR_TEMPERATURE_NEUTRAL_K,
        ge=COLOR_TEMPERATURE_MIN_K,
        le=COLOR_TEMPERATURE_MAX_K,
        description="White-balance target in Kelvin; 6500 is the filter's neutral default.",
    )
    tint: float = Field(
        default=0.0,
        ge=-TINT_FULL_SCALE,
        le=TINT_FULL_SCALE,
        description="Green-magenta tint; compiled as colorbalance gm = tint/50.",
    )
    preserve_lightness: bool = Field(
        default=True,
        description="Emit pl=1 on the RGB stages so white balance does not shift luma.",
    )

    @property
    def gamma(self) -> float:
        """The clamped ``eq`` gamma this op compiles to."""
        return ev_to_gamma(self.exposure_ev)

    @property
    def tint_gm(self) -> float:
        """The ``colorbalance`` ``gm`` coefficient this op compiles to."""
        return tint_to_gm(self.tint)

    @property
    def gamma_is_clamped(self) -> bool:
        """True when the EV lies outside the band ``eq`` can represent exactly."""
        return 2.0**self.exposure_ev != self.gamma

    @property
    def needs_rgb_pass(self) -> bool:
        """True when a white-balance stage is emitted (and so a yuv→rgb→yuv round trip).

        ``eq`` is YUV-only; ``colortemperature`` and ``colorbalance`` are
        RGB-only.  At neutral both RGB stages are elided — ``kelvin2rgb(6500)``
        is *not* an exact identity and neither filter short-circuits — which
        also removes the pixel-format round trip entirely.
        """
        return self.temperature_k != COLOR_TEMPERATURE_NEUTRAL_K or self.tint != 0.0


class LutOp(BaseModel):
    """3D LUT grade via ``lut3d`` — the executable twin of ``color.apply_lut``.

    ``lut_path`` is a staged ``.cube`` file (resolved by the caller through
    :mod:`nexus_ai_agent.creative.luts`, which validates strictly); the IR
    carries it opaquely and FFmpeg fails closed on a missing file.
    ``intensity`` blends graded over original (``split → lut3d → blend``);
    at exactly ``1.0`` the blend is elided and the LUT applies directly.
    Duration is unaffected.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    op: Literal["lut"] = "lut"
    lut_name: str = Field(min_length=1, max_length=64)
    lut_path: str = Field(min_length=1)
    intensity: float = Field(default=1.0, ge=0.0, le=1.0)


class SubtitleOp(BaseModel):
    """Burn staged captions via ``subtitles`` (libass) — the executable twin of
    ``caption.burn_in``.

    ``subtitle_path`` is a staged ``.srt``/``.ass`` file; the font directory is
    threaded at compile time (``fontsdir``) so tests point at the shipped
    Vazirmatn without depending on system fontconfig.  Duration is unaffected.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    op: Literal["subtitle"] = "subtitle"
    subtitle_path: str = Field(min_length=1)
    force_style: str | None = Field(default=None, max_length=500)


LaneOp = Annotated[
    TrimOp
    | SpeedOp
    | ReverseOp
    | FreezeOp
    | XfadeOp
    | TitleOp
    | LoudnormOp
    | DuckOp
    | ExposureOp
    | LutOp
    | SubtitleOp,
    Field(discriminator="op"),
]


@dataclass(frozen=True)
class LaneSource:
    asset_id: str
    path: str
    media_kind: str  # "video" | "audio" | "image"
    duration_us: int


@dataclass(frozen=True)
class LaneProfile:
    width: int = 1280
    height: int = 720
    fps: int = 30
    crf: int = 20
    preset: str = "veryfast"
    video_codec: str = "libx264"
    audio_codec: str = "aac"
    audio_bitrate: str = "128k"


@dataclass(frozen=True)
class AssemblyGap:
    """Timeline silence/black between two assembly pieces (integer microseconds).

    A gap is *editorial* time: the compiler renders it as generated black
    video plus generated silence, so a track with ``Segment A, Gap, Segment B``
    keeps its timeline positions without inventing media.
    """

    duration_us: int


@dataclass(frozen=True)
class LaneAssembly:
    """Ordered pieces (lane segments and gaps) compiled into **one** process.

    One :class:`LaneIR` compiles exactly one main source — the IR has no concat
    op, so a K-clip track yields K segment IRs.  The assembly is the minimal
    extension that closes that gap *without* a second pipeline: the pieces are
    compiled by the same compiler (:func:`compile_assembly`) into one
    filtergraph with one ``concat`` stage, and executed by the same executor
    (:func:`encode_lane`) in exactly one FFmpeg process.

    Rules (fail-closed, enforced by the compiler):

    * at least one :class:`LaneIR` piece; pieces are timeline-ordered;
    * no two adjacent gaps (merge them at plan level);
    * all segment mains share one media kind (all video or all audio);
    * every piece profile/container equals the assembly's own.
    """

    pieces: tuple[LaneIR | AssemblyGap, ...]
    profile: LaneProfile = LaneProfile()
    container: str = "mp4"

    @property
    def segments(self) -> tuple[LaneIR, ...]:
        """The lane pieces in timeline order (gaps excluded)."""
        return tuple(p for p in self.pieces if isinstance(p, LaneIR))

    @property
    def gaps(self) -> tuple[AssemblyGap, ...]:
        """The gap pieces in timeline order (segments excluded)."""
        return tuple(p for p in self.pieces if isinstance(p, AssemblyGap))


@dataclass(frozen=True)
class LaneIR:
    """Everything the compiler needs: one main source, ordered ops, extras, profile."""

    main: LaneSource
    ops: tuple[Any, ...]
    extra_sources: tuple[LaneSource, ...] = ()
    profile: LaneProfile = LaneProfile()
    container: str = "mp4"

    @property
    def sources_by_asset(self) -> dict[str, LaneSource]:
        return {s.asset_id: s for s in (self.main, *self.extra_sources)}


def lane_ir_from_project(
    project: Any,
    media_paths: dict[str, str],
    main_asset_id: str,
    ops: list[LaneOp],
    profile: LaneProfile | None = None,
) -> LaneIR:
    """Bind pack ``Project`` assets + staged paths into a validated :class:`LaneIR`.

    Every asset the ops reference must exist in the project registry **and**
    have a staged media path, otherwise rendering would silently drop media —
    so both are hard :class:`LaneError` failures.
    """
    known = {a.asset_id: a for a in project.assets}
    if main_asset_id not in known:
        raise LaneError(f"unknown main asset: {main_asset_id!r}")

    referenced: list[str] = [main_asset_id]
    for op in ops:
        if isinstance(op, XfadeOp):
            referenced.append(op.other_asset_id)
        elif isinstance(op, DuckOp):
            referenced.append(op.voice_asset_id)

    sources: list[LaneSource] = []
    for asset_id in referenced:
        record = known.get(asset_id)
        if record is None:
            raise LaneError(f"lane op references unknown asset: {asset_id!r}")
        path = media_paths.get(asset_id)
        if not path:
            raise LaneError(f"no staged media path for asset {asset_id!r}")
        sources.append(
            LaneSource(
                asset_id=asset_id,
                path=path,
                media_kind=record.media_kind,
                duration_us=record.duration_us or 0,
            )
        )
    main, extras = sources[0], sources[1:]
    if main.media_kind not in ("video", "audio"):
        raise LaneError(f"main asset must be video or audio, got {main.media_kind!r}")
    for extra in extras:
        if extra.media_kind not in ("video", "audio"):
            raise LaneError(f"extra asset must be video or audio, got {extra.media_kind!r}")
    return LaneIR(
        main=main,
        ops=tuple(ops),
        extra_sources=tuple(extras),
        profile=profile or LaneProfile(),
    )


__all__ = [
    "COLORBALANCE_LIMIT",
    "COLOR_TEMPERATURE_MAX_K",
    "COLOR_TEMPERATURE_MIN_K",
    "COLOR_TEMPERATURE_NEUTRAL_K",
    "EQ_CONTRAST_MAX",
    "EQ_CONTRAST_MIN",
    "EQ_GAMMA_MAX",
    "EQ_GAMMA_MIN",
    "LANE_PACKAGE_ID",
    "TINT_FULL_SCALE",
    "XFADE_KINDS",
    "AssemblyGap",
    "DuckOp",
    "ExposureOp",
    "FreezeOp",
    "LaneAssembly",
    "LaneError",
    "LaneIR",
    "LaneOp",
    "LaneProfile",
    "LaneSource",
    "LoudnormOp",
    "LutOp",
    "ReverseOp",
    "SpeedOp",
    "SubtitleOp",
    "TitleOp",
    "TrimOp",
    "XfadeOp",
    "ev_to_gamma",
    "lane_ir_from_project",
    "tint_to_gm",
]
