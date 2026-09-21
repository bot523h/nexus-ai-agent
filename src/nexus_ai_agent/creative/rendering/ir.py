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
* ``title`` / ``loudnorm`` / ``duck`` → unchanged
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


class LaneError(ValueError):
    """A typed, fail-closed lane failure (bad op, unknown asset, missing media)."""


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


LaneOp = Annotated[
    TrimOp | SpeedOp | ReverseOp | FreezeOp | XfadeOp | TitleOp | LoudnormOp | DuckOp,
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
    "LANE_PACKAGE_ID",
    "XFADE_KINDS",
    "DuckOp",
    "FreezeOp",
    "LaneError",
    "LaneIR",
    "LaneOp",
    "LaneProfile",
    "LaneSource",
    "LoudnormOp",
    "ReverseOp",
    "SpeedOp",
    "TitleOp",
    "TrimOp",
    "XfadeOp",
    "lane_ir_from_project",
]
