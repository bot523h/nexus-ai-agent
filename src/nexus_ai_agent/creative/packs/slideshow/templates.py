"""Tone templates: the shipped ``tone_templates.json`` library, typed.

The library is data (JSON), loaded and validated with Pydantic.  It carries
*parameter sets* — rhythm, transition, motion, color, audio and render
defaults — never an FFmpeg filtergraph: the render IR is derived from these
parameters by the pack's pure planner, so a template can never smuggle code
into the render path (TDD section 2.4).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

TEMPLATES_SCHEMA: Literal["nexus.slideshow.tone-templates.v1"] = "nexus.slideshow.tone-templates.v1"
TEMPLATES_PATH = Path(__file__).parent / "templates" / "tone_templates.json"

#: ``xfade`` transition names the renderer is allowed to build.
TransitionKind = Literal[
    "fade",
    "fadeblack",
    "fadewhite",
    "dissolve",
    "wipeleft",
    "slideright",
    "circleopen",
    "radial",
    "hblur",
    "distance",
    "pixelize",
]

#: Camera-move names the render IR understands (mapped to ``zoompan``).
MotionKind = Literal[
    "static",
    "push_in",
    "pull_out",
    "drift_up",
    "drift_down",
    "drift_left",
    "drift_right",
    "breathe",
]


class TemplateError(ValueError):
    """The template library is missing, malformed or unknown."""


class RhythmSpec(BaseModel):
    """Shot-length grammar; ``beat_quantization`` is beats-per-shot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_shot_seconds: float = Field(gt=0.0, le=60.0)
    min_shot_seconds: float = Field(gt=0.0, le=60.0)
    beat_quantization: int = Field(ge=1, le=32)
    align_to_strong_beats: bool = False

    @model_validator(mode="after")
    def _min_not_above_base(self) -> RhythmSpec:
        if self.min_shot_seconds > self.base_shot_seconds:
            raise ValueError("min_shot_seconds must not exceed base_shot_seconds")
        return self


class TransitionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: TransitionKind
    duration_seconds: float = Field(gt=0.0, le=5.0)


class MotionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: MotionKind
    zoom_from: float = Field(ge=1.0, le=1.5)
    zoom_to: float = Field(ge=1.0, le=1.5)
    alternate: bool = False


class ColorSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contrast: float = Field(ge=0.5, le=2.0, default=1.0)
    saturation: float = Field(ge=0.0, le=3.0, default=1.0)
    brightness: float = Field(ge=-0.5, le=0.5, default=0.0)
    gamma: float = Field(ge=0.5, le=2.0, default=1.0)
    hue_degrees: float = Field(ge=-90.0, le=90.0, default=0.0)
    temperature: float = Field(ge=-1.0, le=1.0, default=0.0)
    vignette: float = Field(ge=0.0, le=1.0, default=0.0)
    grain: int = Field(ge=0, le=50, default=0)


class AudioSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bpm_fallback: float = Field(gt=20.0, le=300.0)
    fade_in_seconds: float = Field(ge=0.0, le=10.0, default=1.0)
    fade_out_seconds: float = Field(ge=0.0, le=10.0, default=2.0)
    loudness_lufs: float = Field(ge=-30.0, le=-5.0, default=-16.0)


class RenderDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fps: int = Field(ge=12, le=60)
    crf: int = Field(ge=0, le=51)
    preset: str = Field(min_length=1)
    audio_bitrate: str = Field(pattern=r"^\d{2,3}k$")


class ToneTemplate(BaseModel):
    """One named tone palette."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    template_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,40}$")
    display_name_fa: str = Field(min_length=1)
    summary_fa: str = Field(min_length=1)
    tier: Literal["primary", "alternate"] = "primary"
    mood_tags: tuple[str, ...] = ()
    rhythm: RhythmSpec
    transition: TransitionSpec
    motion: MotionSpec
    color: ColorSpec
    audio: AudioSpec
    render: RenderDefaults

    @model_validator(mode="after")
    def _transition_fits_the_shortest_shot(self) -> ToneTemplate:
        if self.transition.duration_seconds >= self.rhythm.min_shot_seconds:
            raise ValueError(
                f"{self.template_id}: the transition ({self.transition.duration_seconds}s) must be "
                f"shorter than the shortest shot ({self.rhythm.min_shot_seconds}s)"
            )
        return self


class ToneTemplateLibrary(BaseModel):
    """The whole shipped library, validated once."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_id: Literal["nexus.slideshow.tone-templates.v1"] = Field(
        default="nexus.slideshow.tone-templates.v1", alias="schema"
    )
    note: str = ""
    templates: tuple[ToneTemplate, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _ids_are_unique(self) -> ToneTemplateLibrary:
        seen: set[str] = set()
        for template in self.templates:
            if template.template_id in seen:
                raise ValueError(f"duplicate template_id: {template.template_id!r}")
            seen.add(template.template_id)
        return self

    # -- queries -----------------------------------------------------------
    def ids(self, *, tier: Literal["primary", "alternate"] | None = None) -> tuple[str, ...]:
        return tuple(
            template.template_id
            for template in self.templates
            if tier is None or template.tier == tier
        )

    def get(self, template_id: str) -> ToneTemplate:
        for template in self.templates:
            if template.template_id == template_id:
                return template
        raise TemplateError(
            f"unknown tone template {template_id!r}; known ids: {', '.join(self.ids())}"
        )

    def __contains__(self, template_id: object) -> bool:
        return template_id in self.ids()


def load_tone_templates(path: Path | None = None) -> ToneTemplateLibrary:
    """Load and validate the tone-template library (default: the shipped file)."""
    source = Path(TEMPLATES_PATH if path is None else path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise TemplateError(f"tone template library not found: {source}") from None
    except json.JSONDecodeError as exc:
        raise TemplateError(f"tone template library is not valid JSON: {exc}") from exc
    try:
        return ToneTemplateLibrary.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError -> typed error
        raise TemplateError(f"tone template library failed validation: {exc}") from exc
