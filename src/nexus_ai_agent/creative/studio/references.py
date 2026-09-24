"""Semantic reference resolution for Nagar commands (Wave 1).

A reference -- "اینجا" (here), "۵ ثانیه قبل" (5 seconds ago), an absolute
timecode, or a relative offset -- is pinned to an exact ``timecode_us``
**at the moment the command is received**, and the resulting ``Playhead``
carries ``captured_at_command=True``.  Downstream operations must never
re-interpret the expression against a moved playhead: the pinning happens
once, inside the command bus, before validation and application.

Wave 1 vocabulary (deliberately small and extensible):

* playhead words: "اینجا", "این لحظه", "now", "here", "playhead", "current"
* anchors: "شروع"/"start"/"zero", "پایان"/"end"
* relative: "<N> <ثانیه|دقیقه|second|minute> <قبل|بعد|ago|before|after>"
  with full support for Arabic-Indic digits (۰-۹ / ٠-٩) and comma decimals.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexus_ai_agent.creative.studio.models import (
    MICROSECONDS_PER_SECOND,
    InputRef,
    InputReferenceError,
    Playhead,
    Project,
    ReferenceResolutionError,
    frame_number_for,
)

MINUTES_US = 60 * MICROSECONDS_PER_SECOND

_DIGIT_TRANSLATION = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

PLAYHEAD_WORDS = frozenset(
    {
        "اینجا",
        "این لحظه",
        "لحظه فعلی",
        "لحظه ی فعلی",
        "این کادر",
        "now",
        "here",
        "playhead",
        "current",
        "current position",
    }
)
START_WORDS = frozenset({"شروع", "آغاز", "صفر", "start", "head", "zero", "0"})
END_WORDS = frozenset({"پایان", "آخر", "end", "tail"})

_RELATIVE_RE = re.compile(
    r"^(?P<amount>\d+(?:[.,]\d+)?)\s*"
    r"(?P<unit>ثانیه|ث|second|seconds|sec|s|دقیقه|دقیقا|minute|minutes|min|m)\s*"
    r"(?P<direction>قبل|بعد|ago|before|after|from now)$"
)
_MINUS_DIRECTIONS = frozenset({"قبل", "ago", "before"})
_UNIT_US = {
    "ثانیه": MICROSECONDS_PER_SECOND,
    "ث": MICROSECONDS_PER_SECOND,
    "second": MICROSECONDS_PER_SECOND,
    "seconds": MICROSECONDS_PER_SECOND,
    "sec": MICROSECONDS_PER_SECOND,
    "s": MICROSECONDS_PER_SECOND,
    "دقیقه": MINUTES_US,
    "دقیقا": MINUTES_US,
    "minute": MINUTES_US,
    "minutes": MINUTES_US,
    "min": MINUTES_US,
    "m": MINUTES_US,
}


class ReferenceExpr(BaseModel):
    """Structured (non-text) reference expression."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["absolute", "playhead", "relative", "timeline_start", "timeline_end"]
    timecode_us: int | None = Field(default=None, ge=0)
    offset_us: int | None = None
    relative_to: Literal["playhead", "timeline_start"] = "playhead"

    @model_validator(mode="after")
    def _validate_shape(self) -> ReferenceExpr:
        if self.kind == "absolute" and self.timecode_us is None:
            raise ValueError("kind='absolute' requires timecode_us")
        if self.kind == "relative" and self.offset_us is None:
            raise ValueError("kind='relative' requires offset_us")
        return self


ReferenceInput = str | ReferenceExpr | Playhead


def normalize_expression(expression: str) -> str:
    """Normalize a raw reference string: Arabic-Indic digits, spaces, case."""
    text = expression.translate(_DIGIT_TRANSLATION)
    text = text.replace("\u00a0", " ").replace("\u200f", " ")
    return re.sub(r"\s+", " ", text).strip().lower()


class ReferenceResolver:
    """Pins reference expressions to exact timecodes at command receipt."""

    def resolve(self, expression: ReferenceInput, project: Project) -> Playhead:
        playhead = project.timeline.playhead
        timecode_us = self._to_timecode_us(expression, project, playhead)
        duration_us = project.timeline.duration_us
        if timecode_us < 0 or timecode_us > duration_us:
            raise ReferenceResolutionError(
                f"reference {expression!r} resolves to {timecode_us}us, "
                f"outside project bounds [0, {duration_us}us]"
            )
        return Playhead(
            timecode_us=timecode_us,
            frame_number=frame_number_for(timecode_us, playhead.timebase),
            timebase=playhead.timebase,
            captured_at_command=True,
            is_playing=playhead.is_playing,
        )

    def validate_input_refs(self, refs: tuple[InputRef, ...], project: Project) -> None:
        """Validate *every* logical ref against this project's authoritative state.

        This is deliberately not a filesystem/URL resolver. File existence and
        containment belong to the adapter that stages media; an asset record
        proves logical project membership, not a local file's physical bytes.
        """
        assets = {asset.asset_id: asset for asset in project.assets}
        for ref in refs:
            if ref.project_id != project.project_id:
                raise InputReferenceError("input reference crosses the project boundary")
            if ref.ref_type == "asset":
                asset = assets.get(ref.ref_id)
                if asset is None:
                    raise InputReferenceError(
                        f"asset is not registered in this project: {ref.ref_id!r}"
                    )
                kind, digest = asset.media_kind, asset.content_sha256
            elif ref.ref_type == "clip":
                clip = next(
                    (
                        clip
                        for track in project.timeline.tracks
                        for clip in track.clips
                        if clip.clip_id == ref.ref_id
                    ),
                    None,
                )
                if clip is None:
                    raise InputReferenceError(
                        f"clip is not in this project's timeline: {ref.ref_id!r}"
                    )
                kind, digest = clip.media_ref.media_kind, clip.media_ref.content_sha256
            else:
                if ref.ref_id != project.timeline.timeline_id:
                    raise InputReferenceError("timeline is not in this project")
                if ref.metadata.media_kind is not None or ref.metadata.content_sha256 is not None:
                    raise InputReferenceError("timeline references cannot carry asset metadata")
                continue
            if ref.metadata.media_kind is not None and ref.metadata.media_kind != kind:
                raise InputReferenceError(f"reference has wrong media kind: {ref.ref_id!r}")
            if ref.metadata.content_sha256 is not None and ref.metadata.content_sha256 != digest:
                raise InputReferenceError(f"reference has wrong content digest: {ref.ref_id!r}")

    def _to_timecode_us(
        self, expression: ReferenceInput, project: Project, playhead: Playhead
    ) -> int:
        if isinstance(expression, Playhead):
            return expression.timecode_us
        if isinstance(expression, ReferenceExpr):
            return self._expr_to_us(expression, project, playhead)
        return self._text_to_us(normalize_expression(expression), project, playhead)

    def _expr_to_us(self, expr: ReferenceExpr, project: Project, playhead: Playhead) -> int:
        if expr.kind == "absolute":
            if expr.timecode_us is None:
                raise ReferenceResolutionError("kind='absolute' requires timecode_us")
            return expr.timecode_us
        if expr.kind == "playhead":
            return playhead.timecode_us
        if expr.kind == "timeline_start":
            return 0
        if expr.kind == "timeline_end":
            return project.timeline.duration_us
        anchor = playhead.timecode_us if expr.relative_to == "playhead" else 0
        if expr.offset_us is None:
            raise ReferenceResolutionError("kind='relative' requires offset_us")
        return anchor + expr.offset_us

    def _text_to_us(self, text: str, project: Project, playhead: Playhead) -> int:
        if text in PLAYHEAD_WORDS:
            return playhead.timecode_us
        if text in START_WORDS:
            return 0
        if text in END_WORDS:
            return project.timeline.duration_us
        match = _RELATIVE_RE.match(text)
        if match is None:
            raise ReferenceResolutionError(f"unrecognized reference expression: {text!r}")
        amount = float(match.group("amount").replace(",", "."))
        unit_us = _UNIT_US[match.group("unit")]
        offset_us = round(amount * unit_us)
        if match.group("direction") in _MINUS_DIRECTIONS:
            return playhead.timecode_us - offset_us
        return playhead.timecode_us + offset_us
