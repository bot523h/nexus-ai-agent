"""Deterministic editorial timeline, J/L-cut split-edit solver, eye-trace, and SHA-256 manifest.

Constructs a frame-accurate, overlap-free editorial plan from a parsed bilingual
screenplay and its solved ``SE(3)`` cinematography plans:

* **Independent Picture & Sound Cut Points**: Automatically synthesizes
  **J-Cuts** (incoming shot's audio leads the picture cut by ``0.25`` to ``0.60``
  seconds) and **L-Cuts** (outgoing shot's audio trails over the incoming shot's
  picture by ``0.25`` to ``0.60`` seconds) across continuous dialogue and
  reaction cuts while proving zero illegal dialogue audio overlap.
* **Eye-Trace Continuity**: Bounds screen-space Euclidean saccade distance
  between every outgoing shot's terminal focus and the incoming shot's initial
  focus, applying closed-form Euclidean-ball guided reframing whenever a raw cut
  would exceed the perceptual saccade threshold.
* **SHA-256 Cryptographic Seal**: Produces ``CompiledStoryboardManifest`` with
  canonical JSON serialization and domain-separated SHA-256 digests over the
  script AST, cinematography matrices/trajectories, editorial timeline, and the
  full compiled manifest.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final

from .bilingual_script_parser import (
    ActionBeat,
    DialogueLine,
    EmotionTag,
    ParsedScript,
    parse_bilingual_script,
)
from .cinematography_solver import (
    CameraShotSpec,
    SceneCinematographyPlan,
    ScreenLookDirection,
    ShotKind,
    solve_script_cinematography,
    verify_180_degree_rule,
)

__all__ = [
    "CompiledStoryboardManifest",
    "CutTransition",
    "CutType",
    "DEFAULT_MAX_EYE_TRACE_SACCADE",
    "EditorialCutError",
    "EditorialShotEvent",
    "EditorialTimelinePlan",
    "EditorialVerificationReport",
    "EyeTraceTransition",
    "J_CUT_MAX_SEC",
    "J_CUT_MIN_SEC",
    "L_CUT_MAX_SEC",
    "L_CUT_MIN_SEC",
    "MANIFEST_SCHEMA_VERSION",
    "MIN_AUDIO_SEGMENT_SEC",
    "PacingProfile",
    "StoryboardCompilerConfig",
    "build_compiled_manifest",
    "compile_storyboard",
    "solve_editorial_timeline",
    "solve_eye_trace_transition",
    "verify_editorial_timeline",
    "verify_manifest_integrity",
]

MANIFEST_SCHEMA_VERSION: Final[str] = "nexus.storyboard_compiler.v1"

#: Canonical J-Cut pre-lap window (seconds before picture cut).
J_CUT_MIN_SEC: Final[float] = 0.25
J_CUT_MAX_SEC: Final[float] = 0.60

#: Canonical L-Cut post-lap window (seconds after picture cut).
L_CUT_MIN_SEC: Final[float] = 0.25
L_CUT_MAX_SEC: Final[float] = 0.60

#: Minimum retained dialogue/room-tone audio duration per shot (seconds).
MIN_AUDIO_SEGMENT_SEC: Final[float] = 0.25

#: Default maximum allowed normalized screen-space saccade across a cut.
#: Permits standard horizontal Rule-of-Thirds reverse shots (|2/3 - 1/3| = 0.3333)
#: while catching and reframing jarring diagonal jumps (sqrt(2)/3 = 0.4714).
DEFAULT_MAX_EYE_TRACE_SACCADE: Final[float] = 0.38

_LINGERING_EMOTIONS: Final[frozenset[EmotionTag]] = frozenset(
    {EmotionTag.SAD, EmotionTag.WHISPER, EmotionTag.HESITANT, EmotionTag.SARCASTIC}
)
_ASSERTIVE_EMOTIONS: Final[frozenset[EmotionTag]] = frozenset(
    {EmotionTag.ANGRY, EmotionTag.URGENT, EmotionTag.FEARFUL, EmotionTag.JOYFUL}
)
_SHA256_HEX_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")


class EditorialCutError(ValueError):
    """Raised when editorial timing, cut bounds, or eye-trace inputs are invalid."""


class CutType(str, Enum):
    """Split-edit classification at a picture boundary between two shots."""

    STRAIGHT_CUT = "STRAIGHT_CUT"
    J_CUT = "J_CUT"
    L_CUT = "L_CUT"


class PacingProfile(str, Enum):
    """Macro editorial rhythm classification derived from Average Shot Length (ASL)."""

    KINETIC = "KINETIC"
    DYNAMIC = "DYNAMIC"
    CONVERSATIONAL = "CONVERSATIONAL"
    CONTEMPLATIVE = "CONTEMPLATIVE"


@dataclass(frozen=True)
class EyeTraceTransition:
    """Screen-space gaze continuity specification across boundary ``k -> k + 1``."""

    transition_index: int
    from_shot_index: int
    to_shot_index: int
    exit_focus_uv: tuple[float, float]
    raw_entry_focus_uv: tuple[float, float]
    adjusted_entry_focus_uv: tuple[float, float]
    raw_saccade_uv: float
    effective_saccade_uv: float
    vertical_eyeline_delta: float
    reframe_applied: bool
    eyeline_matched: bool
    continuity_score: float


@dataclass(frozen=True)
class CutTransition:
    """Picture and audio transition specification between consecutive shots."""

    transition_index: int
    from_shot_index: int
    to_shot_index: int
    scene_boundary: bool
    cut_type: CutType
    video_cut_sec: float
    audio_cut_sec: float
    audio_offset_sec: float
    j_cut_prelap_sec: float
    l_cut_postlap_sec: float
    eye_trace: EyeTraceTransition


@dataclass(frozen=True)
class EditorialShotEvent:
    """Timeline clip with decoupled picture and audio edit points."""

    shot_index: int
    scene_index: int
    shot_kind: ShotKind
    speaker: str | None
    video_in_sec: float
    video_out_sec: float
    video_duration_sec: float
    audio_in_sec: float
    audio_out_sec: float
    audio_duration_sec: float
    entry_cut_type: CutType | None
    exit_cut_type: CutType | None
    entry_focus_uv: tuple[float, float]
    exit_focus_uv: tuple[float, float]


@dataclass(frozen=True)
class EditorialVerificationReport:
    """Audit result verifying non-overlap, J/L bounds, video contiguity, and eye-trace."""

    compliant: bool
    video_contiguous: bool
    audio_overlap_free: bool
    jl_bounds_valid: bool
    eye_trace_valid: bool
    max_audio_overlap_sec: float
    violations: tuple[str, ...]


@dataclass(frozen=True)
class EditorialTimelinePlan:
    """Complete solved editorial plan with decoupled A/V cuts, rhythm, and eye-trace."""

    events: tuple[EditorialShotEvent, ...]
    transitions: tuple[CutTransition, ...]
    total_duration_sec: float
    average_shot_length_sec: float
    cuts_per_minute: float
    pacing_profile: PacingProfile
    j_cut_count: int
    l_cut_count: int
    straight_cut_count: int
    mean_eye_trace_score: float
    max_eye_trace_saccade_limit: float
    breath_gap_sec: float
    verification: EditorialVerificationReport


@dataclass(frozen=True)
class StoryboardCompilerConfig:
    """Deterministic configuration knobs for end-to-end storyboard compilation."""

    initial_axis_side: int = 1
    num_trajectory_samples: int = 11
    j_cut_min_sec: float = J_CUT_MIN_SEC
    j_cut_max_sec: float = J_CUT_MAX_SEC
    l_cut_min_sec: float = L_CUT_MIN_SEC
    l_cut_max_sec: float = L_CUT_MAX_SEC
    breath_gap_sec: float = 0.0
    max_eye_trace_saccade: float = DEFAULT_MAX_EYE_TRACE_SACCADE
    allow_scene_prelap: bool = True


# --------------------------------------------------------------------------- #
# Eye-Trace Continuity Solver
# --------------------------------------------------------------------------- #


def _clamp_uv(uv: tuple[float, float]) -> tuple[float, float]:
    u, v = uv
    if not (math.isfinite(u) and math.isfinite(v)):
        raise EditorialCutError(f"focus UV coordinates must be finite, got {uv!r}")
    return (min(max(float(u), 0.05), 0.95), min(max(float(v), 0.05), 0.95))


def _are_eyelines_complementary(
    out_look: ScreenLookDirection,
    in_look: ScreenLookDirection,
    vertical_delta: float,
    axis_jumped: bool,
) -> bool:
    """Return True if screen look-directions preserve conversational eye-line matching."""
    if vertical_delta > 0.12 or axis_jumped:
        return False
    if out_look == ScreenLookDirection.CENTER or in_look == ScreenLookDirection.CENTER:
        return True
    return out_look != in_look


def solve_eye_trace_transition(
    *,
    transition_index: int,
    from_shot_index: int,
    to_shot_index: int,
    exit_focus_uv: tuple[float, float],
    entry_focus_uv: tuple[float, float],
    out_look: ScreenLookDirection = ScreenLookDirection.CENTER,
    in_look: ScreenLookDirection = ScreenLookDirection.CENTER,
    axis_jumped: bool = False,
    max_saccade_uv: float = DEFAULT_MAX_EYE_TRACE_SACCADE,
) -> EyeTraceTransition:
    """Enforce bounded viewer eye-trace saccade across consecutive shots.

    If the raw Euclidean saccade ``||entry - exit||_2`` exceeds ``max_saccade_uv``,
    projects the incoming entry focal point onto the closed Euclidean ball of
    radius ``max_saccade_uv`` centered at ``exit_focus_uv``.
    """
    if not math.isfinite(max_saccade_uv) or not (0.05 <= max_saccade_uv <= math.sqrt(2.0)):
        raise EditorialCutError(
            f"max_saccade_uv must be within [0.05, sqrt(2)], got {max_saccade_uv!r}"
        )

    out_u, out_v = _clamp_uv(exit_focus_uv)
    in_u, in_v = _clamp_uv(entry_focus_uv)
    du = in_u - out_u
    dv = in_v - out_v
    raw_dist = math.hypot(du, dv)

    if raw_dist <= max_saccade_uv + 1e-12:
        adj_u, adj_v = in_u, in_v
        reframe = False
    else:
        scale = max_saccade_uv / raw_dist
        adj_u = min(max(out_u + scale * du, 0.05), 0.95)
        adj_v = min(max(out_v + scale * dv, 0.05), 0.95)
        reframe = True

    eff_dist = math.hypot(adj_u - out_u, adj_v - out_v)
    vert_delta = abs(adj_v - out_v)
    matched = _are_eyelines_complementary(out_look, in_look, vert_delta, axis_jumped)
    score = max(0.0, min(1.0, 1.0 - eff_dist / math.sqrt(2.0)))

    return EyeTraceTransition(
        transition_index=transition_index,
        from_shot_index=from_shot_index,
        to_shot_index=to_shot_index,
        exit_focus_uv=(round(out_u, 6), round(out_v, 6)),
        raw_entry_focus_uv=(round(in_u, 6), round(in_v, 6)),
        adjusted_entry_focus_uv=(round(adj_u, 6), round(adj_v, 6)),
        raw_saccade_uv=round(raw_dist, 6),
        effective_saccade_uv=round(eff_dist, 6),
        vertical_eyeline_delta=round(vert_delta, 6),
        reframe_applied=reframe,
        eyeline_matched=matched,
        continuity_score=round(score, 6),
    )


# --------------------------------------------------------------------------- #
# J-Cut / L-Cut Split-Edit Timing Solver
# --------------------------------------------------------------------------- #


def _choose_desired_cut_type(
    out_shot: CameraShotSpec,
    in_shot: CameraShotSpec,
    *,
    transition_index: int,
    allow_scene_prelap: bool,
) -> CutType:
    """Select dramatically motivated cut grammar before feasibility clamping."""
    scene_boundary = out_shot.scene_index != in_shot.scene_index
    if scene_boundary:
        if allow_scene_prelap and in_shot.shot_kind == ShotKind.DIALOGUE:
            return CutType.J_CUT
        return CutType.STRAIGHT_CUT

    out_is_dlg = out_shot.shot_kind == ShotKind.DIALOGUE
    in_is_dlg = in_shot.shot_kind == ShotKind.DIALOGUE

    if not out_is_dlg and in_is_dlg:
        # Classic pre-lap J-Cut: incoming voice pulls viewer out of action shot.
        return CutType.J_CUT
    if out_is_dlg and not in_is_dlg:
        # Classic reaction L-Cut: speaker's tail carries over visual action/reaction beat.
        return CutType.L_CUT
    if not out_is_dlg and not in_is_dlg:
        return CutType.STRAIGHT_CUT

    # Both shots are dialogue turns within the same scene.
    if out_shot.subject_label == in_shot.subject_label:
        return CutType.STRAIGHT_CUT

    if out_shot.emotion in _LINGERING_EMOTIONS and in_shot.emotion not in (
        EmotionTag.ANGRY,
        EmotionTag.URGENT,
    ):
        return CutType.L_CUT
    if in_shot.emotion in _ASSERTIVE_EMOTIONS:
        return CutType.J_CUT
    if in_shot.dramatic_tension > out_shot.dramatic_tension + 0.04:
        return CutType.J_CUT
    if out_shot.dramatic_tension > in_shot.dramatic_tension + 0.04:
        return CutType.L_CUT

    # Balanced conversational volley: alternate J-Cut and L-Cut for natural flow.
    return CutType.J_CUT if transition_index % 2 == 0 else CutType.L_CUT


def _compute_target_overlap_sec(
    cut_type: CutType,
    out_shot: CameraShotSpec,
    in_shot: CameraShotSpec,
    *,
    min_sec: float,
    max_sec: float,
) -> float:
    """Map dramatic tension and emotion to an exact split-edit offset in [min_sec, max_sec]."""
    span = max_sec - min_sec
    avg_tension = 0.5 * (out_shot.dramatic_tension + in_shot.dramatic_tension)
    if cut_type == CutType.J_CUT:
        # High tension -> snappier J-Cut (closer to 0.25s); low tension -> gentler pre-lap (~0.55s).
        weight = 1.0 - 0.72 * avg_tension
        if in_shot.emotion in (EmotionTag.WHISPER, EmotionTag.SAD):
            weight = min(1.0, weight + 0.22)
    else:
        # Lingering emotion -> deeper L-Cut (toward 0.60s); clipped conflict -> tighter (~0.30s).
        weight = 1.0 - 0.65 * avg_tension
        if out_shot.emotion in _LINGERING_EMOTIONS:
            weight = min(1.0, weight + 0.28)
    clamped_w = min(max(weight, 0.0), 1.0)
    return min_sec + span * clamped_w


def _classify_pacing(average_shot_length_sec: float) -> PacingProfile:
    if average_shot_length_sec < 1.8:
        return PacingProfile.KINETIC
    if average_shot_length_sec < 3.2:
        return PacingProfile.DYNAMIC
    if average_shot_length_sec < 6.0:
        return PacingProfile.CONVERSATIONAL
    return PacingProfile.CONTEMPLATIVE


def verify_editorial_timeline(
    plan: EditorialTimelinePlan,
    *,
    j_cut_min_sec: float = J_CUT_MIN_SEC,
    j_cut_max_sec: float = J_CUT_MAX_SEC,
    l_cut_min_sec: float = L_CUT_MIN_SEC,
    l_cut_max_sec: float = L_CUT_MAX_SEC,
    tol: float = 1e-5,
) -> EditorialVerificationReport:
    """Independently verify video contiguity, audio non-overlap, J/L bounds, and eye-trace."""
    violations: list[str] = []
    video_ok = True
    audio_ok = True
    jl_ok = True
    eye_ok = True
    max_overlap = 0.0

    events = plan.events
    if not events:
        return EditorialVerificationReport(
            compliant=False,
            video_contiguous=False,
            audio_overlap_free=True,
            jl_bounds_valid=True,
            eye_trace_valid=True,
            max_audio_overlap_sec=0.0,
            violations=("timeline has zero shot events",),
        )

    if abs(events[0].video_in_sec) > tol:
        video_ok = False
        violations.append(f"shot 0 video_in_sec={events[0].video_in_sec} != 0.0")

    for idx, ev in enumerate(events):
        if ev.video_out_sec <= ev.video_in_sec + tol:
            video_ok = False
            violations.append(f"shot {idx}: non-positive video duration")
        if ev.audio_in_sec < -tol or ev.audio_out_sec > plan.total_duration_sec + tol:
            audio_ok = False
            violations.append(
                f"shot {idx}: audio window [{ev.audio_in_sec}, {ev.audio_out_sec}] out of bounds"
            )
        if ev.audio_out_sec <= ev.audio_in_sec + tol:
            audio_ok = False
            violations.append(f"shot {idx}: non-positive audio duration")
        if idx > 0:
            prev = events[idx - 1]
            if abs(ev.video_in_sec - prev.video_out_sec) > tol:
                video_ok = False
                violations.append(
                    f"boundary {idx - 1}: video gap/overlap "
                    f"({prev.video_out_sec} -> {ev.video_in_sec})"
                )
            overlap = prev.audio_out_sec - ev.audio_in_sec
            if overlap > max_overlap:
                max_overlap = overlap
            if overlap > tol:
                audio_ok = False
                violations.append(
                    f"boundary {idx - 1}: illegal audio overlap of {overlap:.6f}s "
                    f"({prev.audio_out_sec} > {ev.audio_in_sec})"
                )

    if abs(events[-1].video_out_sec - plan.total_duration_sec) > tol:
        video_ok = False
        violations.append("final video_out_sec does not match total_duration_sec")

    for tr in plan.transitions:
        out_ev = events[tr.transition_index]
        in_ev = events[tr.transition_index + 1]
        if tr.cut_type == CutType.J_CUT:
            actual_prelap = tr.video_cut_sec - in_ev.audio_in_sec
            if not (j_cut_min_sec - tol <= actual_prelap <= j_cut_max_sec + tol):
                jl_ok = False
                violations.append(
                    f"transition {tr.transition_index}: J-Cut pre-lap {actual_prelap:.6f}s "
                    f"outside [{j_cut_min_sec}, {j_cut_max_sec}]"
                )
        elif tr.cut_type == CutType.L_CUT:
            actual_postlap = out_ev.audio_out_sec - tr.video_cut_sec
            if not (l_cut_min_sec - tol <= actual_postlap <= l_cut_max_sec + tol):
                jl_ok = False
                violations.append(
                    f"transition {tr.transition_index}: L-Cut post-lap {actual_postlap:.6f}s "
                    f"outside [{l_cut_min_sec}, {l_cut_max_sec}]"
                )
        else:
            if abs(in_ev.audio_in_sec - tr.video_cut_sec) > tol:
                jl_ok = False
                violations.append(
                    f"transition {tr.transition_index}: STRAIGHT_CUT audio_in misaligned"
                )

        if tr.eye_trace.effective_saccade_uv > plan.max_eye_trace_saccade_limit + tol:
            eye_ok = False
            violations.append(
                f"transition {tr.transition_index}: eye-trace saccade "
                f"{tr.eye_trace.effective_saccade_uv:.6f} exceeds limit "
                f"{plan.max_eye_trace_saccade_limit:.6f}"
            )

    return EditorialVerificationReport(
        compliant=len(violations) == 0,
        video_contiguous=video_ok,
        audio_overlap_free=audio_ok,
        jl_bounds_valid=jl_ok,
        eye_trace_valid=eye_ok,
        max_audio_overlap_sec=round(max(0.0, max_overlap), 6),
        violations=tuple(violations),
    )


def _validate_split_bounds(
    j_cut_min_sec: float,
    j_cut_max_sec: float,
    l_cut_min_sec: float,
    l_cut_max_sec: float,
    breath_gap_sec: float,
) -> None:
    for label, val in (
        ("j_cut_min_sec", j_cut_min_sec),
        ("j_cut_max_sec", j_cut_max_sec),
        ("l_cut_min_sec", l_cut_min_sec),
        ("l_cut_max_sec", l_cut_max_sec),
        ("breath_gap_sec", breath_gap_sec),
    ):
        if not math.isfinite(val):
            raise EditorialCutError(f"{label} must be finite, got {val!r}")
    if not (J_CUT_MIN_SEC <= j_cut_min_sec <= j_cut_max_sec <= J_CUT_MAX_SEC):
        raise EditorialCutError(
            f"J-Cut bounds [{j_cut_min_sec}, {j_cut_max_sec}] must lie within "
            f"[{J_CUT_MIN_SEC}, {J_CUT_MAX_SEC}]"
        )
    if not (L_CUT_MIN_SEC <= l_cut_min_sec <= l_cut_max_sec <= L_CUT_MAX_SEC):
        raise EditorialCutError(
            f"L-Cut bounds [{l_cut_min_sec}, {l_cut_max_sec}] must lie within "
            f"[{L_CUT_MIN_SEC}, {L_CUT_MAX_SEC}]"
        )
    if not (0.0 <= breath_gap_sec <= 0.25):
        raise EditorialCutError(
            f"breath_gap_sec must lie within [0.0, 0.25], got {breath_gap_sec!r}"
        )


def solve_editorial_timeline(
    scene_plans: Sequence[SceneCinematographyPlan],
    *,
    j_cut_min_sec: float = J_CUT_MIN_SEC,
    j_cut_max_sec: float = J_CUT_MAX_SEC,
    l_cut_min_sec: float = L_CUT_MIN_SEC,
    l_cut_max_sec: float = L_CUT_MAX_SEC,
    breath_gap_sec: float = 0.0,
    max_eye_trace_saccade: float = DEFAULT_MAX_EYE_TRACE_SACCADE,
    allow_scene_prelap: bool = True,
) -> EditorialTimelinePlan:
    """Solve decoupled picture/audio cut points, J/L-Cuts, and eye-trace continuity."""
    _validate_split_bounds(
        j_cut_min_sec, j_cut_max_sec, l_cut_min_sec, l_cut_max_sec, breath_gap_sec
    )
    if not math.isfinite(max_eye_trace_saccade) or not (
        0.05 <= max_eye_trace_saccade <= math.sqrt(2.0)
    ):
        raise EditorialCutError(
            f"max_eye_trace_saccade must lie within [0.05, sqrt(2)], got {max_eye_trace_saccade!r}"
        )

    all_shots: list[CameraShotSpec] = [shot for plan in scene_plans for shot in plan.shots]
    if not all_shots:
        raise EditorialCutError("scene_plans contain no shots to edit")

    n = len(all_shots)
    video_in: list[float] = []
    video_out: list[float] = []
    cursor = 0.0
    for shot in all_shots:
        if shot.duration_sec <= 0.0 or not math.isfinite(shot.duration_sec):
            raise EditorialCutError(
                f"shot {shot.shot_index} has invalid duration {shot.duration_sec!r}"
            )
        start = round(cursor, 6)
        end = round(start + shot.duration_sec, 6)
        video_in.append(start)
        video_out.append(end)
        cursor = end

    total_duration = video_out[-1]
    audio_in: list[float] = [0.0] * n
    audio_out: list[float] = [0.0] * n
    entry_cuts: list[CutType | None] = [None] * n
    exit_cuts: list[CutType | None] = [None] * n

    entry_uvs: list[tuple[float, float]] = [
        _clamp_uv(shot.framing.projected_start_uv) for shot in all_shots
    ]
    exit_uvs: list[tuple[float, float]] = [
        _clamp_uv(shot.framing.projected_end_uv) for shot in all_shots
    ]

    transitions: list[CutTransition] = []

    for k in range(n - 1):
        out_shot = all_shots[k]
        in_shot = all_shots[k + 1]
        v_cut = video_out[k]
        out_dur = round(video_out[k] - video_in[k], 6)
        in_dur = round(video_out[k + 1] - video_in[k + 1], 6)

        desired = _choose_desired_cut_type(
            out_shot,
            in_shot,
            transition_index=k,
            allow_scene_prelap=allow_scene_prelap,
        )

        applied_cut = CutType.STRAIGHT_CUT
        j_prelap = 0.0
        l_postlap = 0.0
        a_cut = v_cut

        if desired == CutType.J_CUT:
            target = _compute_target_overlap_sec(
                CutType.J_CUT, out_shot, in_shot, min_sec=j_cut_min_sec, max_sec=j_cut_max_sec
            )
            # Ensure outgoing shot retains >= MIN_AUDIO_SEGMENT_SEC after breath_gap_sec.
            remaining_out = (v_cut - audio_in[k]) - MIN_AUDIO_SEGMENT_SEC - breath_gap_sec
            max_feasible = min(j_cut_max_sec, remaining_out, 0.45 * out_dur, 0.45 * in_dur)
            candidate = round(min(target, max_feasible), 6)
            if candidate >= j_cut_min_sec:
                applied_cut = CutType.J_CUT
                j_prelap = candidate
                in_audio_start = round(v_cut - j_prelap, 6)
                out_audio_end = round(in_audio_start - breath_gap_sec, 6)
                audio_out[k] = out_audio_end
                audio_in[k + 1] = in_audio_start
                a_cut = in_audio_start

        elif desired == CutType.L_CUT:
            target = _compute_target_overlap_sec(
                CutType.L_CUT, out_shot, in_shot, min_sec=l_cut_min_sec, max_sec=l_cut_max_sec
            )
            # Reserve enough duration in incoming shot (and leave room for potential next cut).
            remaining_in = in_dur - MIN_AUDIO_SEGMENT_SEC - breath_gap_sec
            max_feasible = min(l_cut_max_sec, remaining_in, 0.45 * out_dur, 0.45 * in_dur)
            candidate = round(min(target, max_feasible), 6)
            if candidate >= l_cut_min_sec:
                applied_cut = CutType.L_CUT
                l_postlap = candidate
                out_audio_end = round(v_cut + l_postlap, 6)
                in_audio_start = round(out_audio_end + breath_gap_sec, 6)
                audio_out[k] = out_audio_end
                audio_in[k + 1] = in_audio_start
                a_cut = out_audio_end

        if applied_cut == CutType.STRAIGHT_CUT:
            max_gap = max(0.0, (v_cut - audio_in[k]) - MIN_AUDIO_SEGMENT_SEC)
            eff_gap = min(breath_gap_sec, max_gap)
            audio_out[k] = round(v_cut - eff_gap, 6)
            audio_in[k + 1] = v_cut
            a_cut = v_cut

        exit_cuts[k] = applied_cut
        entry_cuts[k + 1] = applied_cut

        eye_trace = solve_eye_trace_transition(
            transition_index=k,
            from_shot_index=out_shot.shot_index,
            to_shot_index=in_shot.shot_index,
            exit_focus_uv=exit_uvs[k],
            entry_focus_uv=entry_uvs[k + 1],
            out_look=out_shot.framing.look_direction,
            in_look=in_shot.framing.look_direction,
            axis_jumped=in_shot.intentional_axis_jump,
            max_saccade_uv=max_eye_trace_saccade,
        )
        entry_uvs[k + 1] = eye_trace.adjusted_entry_focus_uv
        if all_shots[k + 1].trajectory.move_type.value == "STATIC":
            exit_uvs[k + 1] = eye_trace.adjusted_entry_focus_uv

        audio_offset = round(a_cut - v_cut, 6)
        transitions.append(
            CutTransition(
                transition_index=k,
                from_shot_index=out_shot.shot_index,
                to_shot_index=in_shot.shot_index,
                scene_boundary=(out_shot.scene_index != in_shot.scene_index),
                cut_type=applied_cut,
                video_cut_sec=v_cut,
                audio_cut_sec=a_cut,
                audio_offset_sec=audio_offset,
                j_cut_prelap_sec=j_prelap,
                l_cut_postlap_sec=l_postlap,
                eye_trace=eye_trace,
            )
        )

    audio_out[n - 1] = total_duration

    events: list[EditorialShotEvent] = []
    for i, shot in enumerate(all_shots):
        speaker = shot.subject_label if shot.shot_kind == ShotKind.DIALOGUE else None
        events.append(
            EditorialShotEvent(
                shot_index=shot.shot_index,
                scene_index=shot.scene_index,
                shot_kind=shot.shot_kind,
                speaker=speaker,
                video_in_sec=video_in[i],
                video_out_sec=video_out[i],
                video_duration_sec=round(video_out[i] - video_in[i], 6),
                audio_in_sec=audio_in[i],
                audio_out_sec=audio_out[i],
                audio_duration_sec=round(audio_out[i] - audio_in[i], 6),
                entry_cut_type=entry_cuts[i],
                exit_cut_type=exit_cuts[i],
                entry_focus_uv=(round(entry_uvs[i][0], 6), round(entry_uvs[i][1], 6)),
                exit_focus_uv=(round(exit_uvs[i][0], 6), round(exit_uvs[i][1], 6)),
            )
        )

    asl = round(total_duration / n, 6)
    cuts_per_min = round(((n - 1) / total_duration) * 60.0, 6)
    pacing = _classify_pacing(asl)

    j_count = sum(1 for tr in transitions if tr.cut_type == CutType.J_CUT)
    l_count = sum(1 for tr in transitions if tr.cut_type == CutType.L_CUT)
    s_count = sum(1 for tr in transitions if tr.cut_type == CutType.STRAIGHT_CUT)
    mean_eye = (
        round(sum(tr.eye_trace.continuity_score for tr in transitions) / len(transitions), 6)
        if transitions
        else 1.0
    )

    preliminary = EditorialTimelinePlan(
        events=tuple(events),
        transitions=tuple(transitions),
        total_duration_sec=total_duration,
        average_shot_length_sec=asl,
        cuts_per_minute=cuts_per_min,
        pacing_profile=pacing,
        j_cut_count=j_count,
        l_cut_count=l_count,
        straight_cut_count=s_count,
        mean_eye_trace_score=mean_eye,
        max_eye_trace_saccade_limit=round(max_eye_trace_saccade, 6),
        breath_gap_sec=round(breath_gap_sec, 6),
        verification=EditorialVerificationReport(
            compliant=True,
            video_contiguous=True,
            audio_overlap_free=True,
            jl_bounds_valid=True,
            eye_trace_valid=True,
            max_audio_overlap_sec=0.0,
            violations=(),
        ),
    )
    report = verify_editorial_timeline(
        preliminary,
        j_cut_min_sec=j_cut_min_sec,
        j_cut_max_sec=j_cut_max_sec,
        l_cut_min_sec=l_cut_min_sec,
        l_cut_max_sec=l_cut_max_sec,
    )
    return EditorialTimelinePlan(
        events=preliminary.events,
        transitions=preliminary.transitions,
        total_duration_sec=preliminary.total_duration_sec,
        average_shot_length_sec=preliminary.average_shot_length_sec,
        cuts_per_minute=preliminary.cuts_per_minute,
        pacing_profile=preliminary.pacing_profile,
        j_cut_count=preliminary.j_cut_count,
        l_cut_count=preliminary.l_cut_count,
        straight_cut_count=preliminary.straight_cut_count,
        mean_eye_trace_score=preliminary.mean_eye_trace_score,
        max_eye_trace_saccade_limit=preliminary.max_eye_trace_saccade_limit,
        breath_gap_sec=preliminary.breath_gap_sec,
        verification=report,
    )


# --------------------------------------------------------------------------- #
# Canonical Serialization & SHA-256 CompiledStoryboardManifest
# --------------------------------------------------------------------------- #


def _qfloat(value: float, digits: int = 8) -> float:
    """Deterministic float quantization normalizing IEEE-754 ``-0.0`` to ``+0.0``."""
    rounded = round(float(value), digits)
    return 0.0 if rounded == 0.0 else rounded


def _canonical_sha256(domain: str, payload: dict[str, Any]) -> str:
    """Domain-separated canonical UTF-8 JSON SHA-256 digest."""
    blob = json.dumps(
        {"domain": domain, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _serialize_script(script: ParsedScript) -> dict[str, Any]:
    scenes_out: list[dict[str, Any]] = []
    for sc in script.scenes:
        elems_out: list[dict[str, Any]] = []
        for el in sc.elements:
            if isinstance(el, DialogueLine):
                elems_out.append(
                    {
                        "kind": "dialogue",
                        "element_index": el.element_index,
                        "character": el.character,
                        "spoken_text": el.spoken_text,
                        "language": el.language.value,
                        "parentheticals": list(el.parentheticals),
                        "emotion": el.emotion.value,
                        "delivery_mode": el.delivery_mode.value,
                        "dramatic_tension": _qfloat(el.dramatic_tension),
                        "intentional_axis_jump": el.intentional_axis_jump,
                        "timing": {
                            "fa_words": el.timing.persian_word_count,
                            "en_words": el.timing.english_word_count,
                            "fa_syllables": el.timing.persian_syllable_count,
                            "en_syllables": el.timing.english_syllable_count,
                            "rate_mult": _qfloat(el.timing.speech_rate_multiplier),
                            "spoken_sec": _qfloat(el.timing.spoken_duration_sec),
                            "pause_sec": _qfloat(el.timing.pause_duration_sec),
                            "total_sec": _qfloat(el.timing.total_duration_sec),
                        },
                    }
                )
            else:
                assert isinstance(el, ActionBeat)
                elems_out.append(
                    {
                        "kind": "action",
                        "element_index": el.element_index,
                        "text": el.text,
                        "language": el.language.value,
                        "duration_sec": _qfloat(el.duration_sec),
                        "dramatic_tension": _qfloat(el.dramatic_tension),
                        "focus_characters": list(el.focus_characters),
                        "intentional_axis_jump": el.intentional_axis_jump,
                        "ambiguous_colon_action": el.ambiguous_colon_action,
                    }
                )
        scenes_out.append(
            {
                "scene_index": sc.scene_index,
                "heading": {
                    "scene_number": sc.heading.scene_number,
                    "setting": sc.heading.setting.value,
                    "location": sc.heading.location,
                    "time_of_day": sc.heading.time_of_day.value,
                    "weather": sc.heading.weather.value,
                    "language": sc.heading.language.value,
                },
                "characters": list(sc.characters),
                "total_duration_sec": _qfloat(sc.total_duration_sec),
                "peak_tension": _qfloat(sc.peak_tension),
                "mean_tension": _qfloat(sc.mean_tension),
                "elements": elems_out,
            }
        )
    return {
        "source_sha256": script.source_sha256,
        "language": script.language.value,
        "characters": list(script.characters),
        "total_duration_sec": _qfloat(script.total_duration_sec),
        "scenes": scenes_out,
    }


def _serialize_cinematography(plans: Sequence[SceneCinematographyPlan]) -> dict[str, Any]:
    out: list[dict[str, Any]] = []
    for plan in plans:
        shots_out: list[dict[str, Any]] = []
        for sh in plan.shots:
            shots_out.append(
                {
                    "shot_index": sh.shot_index,
                    "scene_index": sh.scene_index,
                    "shot_in_scene": sh.shot_in_scene,
                    "shot_kind": sh.shot_kind.value,
                    "subject_label": sh.subject_label,
                    "subject_position": [_qfloat(c) for c in sh.subject_position],
                    "duration_sec": _qfloat(sh.duration_sec),
                    "dramatic_tension": _qfloat(sh.dramatic_tension),
                    "emotion": sh.emotion.value,
                    "axis_side": sh.axis_side,
                    "original_axis_side": sh.original_axis_side,
                    "was_axis_reflected": sh.was_axis_reflected,
                    "axis_cross_z": _qfloat(sh.axis_cross_z),
                    "intentional_axis_jump": sh.intentional_axis_jump,
                    "pose_R": [[_qfloat(v) for v in row] for row in sh.pose.rotation],
                    "pose_t": [_qfloat(v) for v in sh.pose.translation],
                    "end_pose_R": [[_qfloat(v) for v in row] for row in sh.end_pose.rotation],
                    "end_pose_t": [_qfloat(v) for v in sh.end_pose.translation],
                    "lens": {
                        "focal_length_mm": sh.lens.focal_length_mm,
                        "f_stop": _qfloat(sh.lens.f_stop),
                        "shot_scale": sh.lens.shot_scale.value,
                        "focus_distance_m": _qfloat(sh.lens.focus_distance_m),
                        "dof_near_m": _qfloat(sh.lens.dof_near_m),
                        "dof_far_m": _qfloat(sh.lens.dof_far_m),
                    },
                    "framing": {
                        "power_point": sh.framing.power_point.value,
                        "target_uv": [_qfloat(v) for v in sh.framing.target_uv],
                        "projected_start_uv": [_qfloat(v) for v in sh.framing.projected_start_uv],
                        "projected_end_uv": [_qfloat(v) for v in sh.framing.projected_end_uv],
                        "look_direction": sh.framing.look_direction.value,
                    },
                    "trajectory": {
                        "move_type": sh.trajectory.move_type.value,
                        "duration_sec": _qfloat(sh.trajectory.duration_sec),
                        "crane_arc_m": _qfloat(sh.trajectory.crane_arc_m),
                        "sample_count": len(sh.trajectory.samples),
                        "total_translation_m": _qfloat(sh.trajectory.total_translation_m),
                        "total_rotation_deg": _qfloat(sh.trajectory.total_rotation_deg),
                    },
                }
            )
        out.append(
            {
                "scene_index": plan.scene_index,
                "axis": {
                    "p1": [_qfloat(c) for c in plan.axis.p1],
                    "p2": [_qfloat(c) for c in plan.axis.p2],
                    "initial_side": plan.axis.initial_side,
                    "character_a": plan.axis.character_a,
                    "character_b": plan.axis.character_b,
                },
                "axis_compliant": plan.axis_report.compliant,
                "reflected_shot_indices": list(plan.axis_report.reflected_shot_indices),
                "shots": shots_out,
            }
        )
    return {"scene_plans": out}


def _serialize_editorial(editorial: EditorialTimelinePlan) -> dict[str, Any]:
    return {
        "total_duration_sec": _qfloat(editorial.total_duration_sec),
        "average_shot_length_sec": _qfloat(editorial.average_shot_length_sec),
        "cuts_per_minute": _qfloat(editorial.cuts_per_minute),
        "pacing_profile": editorial.pacing_profile.value,
        "j_cut_count": editorial.j_cut_count,
        "l_cut_count": editorial.l_cut_count,
        "straight_cut_count": editorial.straight_cut_count,
        "mean_eye_trace_score": _qfloat(editorial.mean_eye_trace_score),
        "max_eye_trace_saccade_limit": _qfloat(editorial.max_eye_trace_saccade_limit),
        "breath_gap_sec": _qfloat(editorial.breath_gap_sec),
        "compliant": editorial.verification.compliant,
        "events": [
            {
                "shot_index": ev.shot_index,
                "scene_index": ev.scene_index,
                "shot_kind": ev.shot_kind.value,
                "speaker": ev.speaker,
                "video_in_sec": _qfloat(ev.video_in_sec),
                "video_out_sec": _qfloat(ev.video_out_sec),
                "audio_in_sec": _qfloat(ev.audio_in_sec),
                "audio_out_sec": _qfloat(ev.audio_out_sec),
                "entry_cut_type": ev.entry_cut_type.value if ev.entry_cut_type else None,
                "exit_cut_type": ev.exit_cut_type.value if ev.exit_cut_type else None,
                "entry_focus_uv": [_qfloat(v) for v in ev.entry_focus_uv],
                "exit_focus_uv": [_qfloat(v) for v in ev.exit_focus_uv],
            }
            for ev in editorial.events
        ],
        "transitions": [
            {
                "transition_index": tr.transition_index,
                "from_shot_index": tr.from_shot_index,
                "to_shot_index": tr.to_shot_index,
                "scene_boundary": tr.scene_boundary,
                "cut_type": tr.cut_type.value,
                "video_cut_sec": _qfloat(tr.video_cut_sec),
                "audio_cut_sec": _qfloat(tr.audio_cut_sec),
                "audio_offset_sec": _qfloat(tr.audio_offset_sec),
                "j_cut_prelap_sec": _qfloat(tr.j_cut_prelap_sec),
                "l_cut_postlap_sec": _qfloat(tr.l_cut_postlap_sec),
                "eye_trace": {
                    "exit_focus_uv": [_qfloat(v) for v in tr.eye_trace.exit_focus_uv],
                    "raw_entry_focus_uv": [_qfloat(v) for v in tr.eye_trace.raw_entry_focus_uv],
                    "adjusted_entry_focus_uv": [
                        _qfloat(v) for v in tr.eye_trace.adjusted_entry_focus_uv
                    ],
                    "effective_saccade_uv": _qfloat(tr.eye_trace.effective_saccade_uv),
                    "reframe_applied": tr.eye_trace.reframe_applied,
                    "eyeline_matched": tr.eye_trace.eyeline_matched,
                    "continuity_score": _qfloat(tr.eye_trace.continuity_score),
                },
            }
            for tr in editorial.transitions
        ],
    }


def _compute_digests(
    schema_version: str,
    script: ParsedScript,
    cinematography: Sequence[SceneCinematographyPlan],
    editorial: EditorialTimelinePlan,
    *,
    total_scenes: int,
    total_shots: int,
    total_duration_sec: float,
    axis_180_compliant: bool,
    editorial_compliant: bool,
    eye_trace_compliant: bool,
) -> tuple[str, str, str, str, dict[str, Any]]:
    script_dict = _serialize_script(script)
    cinema_dict = _serialize_cinematography(cinematography)
    edit_dict = _serialize_editorial(editorial)

    script_sha = _canonical_sha256("nexus.storyboard.script", script_dict)
    cinema_sha = _canonical_sha256("nexus.storyboard.cinematography", cinema_dict)
    edit_sha = _canonical_sha256("nexus.storyboard.editorial", edit_dict)

    envelope = {
        "schema_version": schema_version,
        "total_scenes": total_scenes,
        "total_shots": total_shots,
        "total_duration_sec": _qfloat(total_duration_sec),
        "axis_180_compliant": bool(axis_180_compliant),
        "editorial_compliant": bool(editorial_compliant),
        "eye_trace_compliant": bool(eye_trace_compliant),
        "script_sha256": script_sha,
        "cinematography_sha256": cinema_sha,
        "editorial_sha256": edit_sha,
        "script": script_dict,
        "cinematography": cinema_dict,
        "editorial": edit_dict,
    }
    manifest_sha = _canonical_sha256("nexus.storyboard.manifest", envelope)
    return script_sha, cinema_sha, edit_sha, manifest_sha, envelope


@dataclass(frozen=True)
class CompiledStoryboardManifest:
    """Immutable, SHA-256-sealed deliverable of the deterministic storyboard compiler."""

    schema_version: str
    script: ParsedScript
    cinematography: tuple[SceneCinematographyPlan, ...]
    editorial: EditorialTimelinePlan
    total_scenes: int
    total_shots: int
    total_duration_sec: float
    axis_180_compliant: bool
    editorial_compliant: bool
    eye_trace_compliant: bool
    script_sha256: str
    cinematography_sha256: str
    editorial_sha256: str
    manifest_sha256: str

    def to_canonical_dict(self) -> dict[str, Any]:
        """Return deterministic dictionary representation including SHA-256 seals."""
        return {
            "schema_version": self.schema_version,
            "manifest_sha256": self.manifest_sha256,
            "script_sha256": self.script_sha256,
            "cinematography_sha256": self.cinematography_sha256,
            "editorial_sha256": self.editorial_sha256,
            "total_scenes": self.total_scenes,
            "total_shots": self.total_shots,
            "total_duration_sec": _qfloat(self.total_duration_sec),
            "axis_180_compliant": self.axis_180_compliant,
            "editorial_compliant": self.editorial_compliant,
            "eye_trace_compliant": self.eye_trace_compliant,
            "script": _serialize_script(self.script),
            "cinematography": _serialize_cinematography(self.cinematography),
            "editorial": _serialize_editorial(self.editorial),
        }

    def to_canonical_json(self) -> str:
        """Serialize the complete manifest into canonical, reproducible UTF-8 JSON."""
        return json.dumps(
            self.to_canonical_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )


def build_compiled_manifest(
    script: ParsedScript,
    cinematography: Sequence[SceneCinematographyPlan],
    editorial: EditorialTimelinePlan,
) -> CompiledStoryboardManifest:
    """Assemble and cryptographically seal a ``CompiledStoryboardManifest`` with SHA-256."""
    cinema_tuple = tuple(cinematography)
    if len(cinema_tuple) != len(script.scenes):
        raise EditorialCutError(
            f"cinematography scene count ({len(cinema_tuple)}) != "
            f"script scene count ({len(script.scenes)})"
        )
    shot_count = sum(len(plan.shots) for plan in cinema_tuple)
    if shot_count != len(editorial.events):
        raise EditorialCutError(
            f"cinematography shot count ({shot_count}) != "
            f"editorial event count ({len(editorial.events)})"
        )

    total_scenes = len(script.scenes)
    axis_ok = all(
        verify_180_degree_rule(plan.shots, plan.axis).compliant and plan.axis_report.compliant
        for plan in cinema_tuple
    )
    live_edit = verify_editorial_timeline(editorial)
    edit_ok = live_edit.compliant and editorial.verification.compliant
    eye_ok = live_edit.eye_trace_valid and editorial.verification.eye_trace_valid

    script_sha, cinema_sha, edit_sha, manifest_sha, _ = _compute_digests(
        MANIFEST_SCHEMA_VERSION,
        script,
        cinema_tuple,
        editorial,
        total_scenes=total_scenes,
        total_shots=shot_count,
        total_duration_sec=editorial.total_duration_sec,
        axis_180_compliant=axis_ok,
        editorial_compliant=edit_ok,
        eye_trace_compliant=eye_ok,
    )
    return CompiledStoryboardManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        script=script,
        cinematography=cinema_tuple,
        editorial=editorial,
        total_scenes=total_scenes,
        total_shots=shot_count,
        total_duration_sec=editorial.total_duration_sec,
        axis_180_compliant=axis_ok,
        editorial_compliant=edit_ok,
        eye_trace_compliant=eye_ok,
        script_sha256=script_sha,
        cinematography_sha256=cinema_sha,
        editorial_sha256=edit_sha,
        manifest_sha256=manifest_sha,
    )


def verify_manifest_integrity(manifest: CompiledStoryboardManifest) -> bool:
    """Recompute all summary invariants and SHA-256 digests; return True iff untampered."""
    for digest in (
        manifest.script_sha256,
        manifest.cinematography_sha256,
        manifest.editorial_sha256,
        manifest.manifest_sha256,
    ):
        if not _SHA256_HEX_RE.match(digest):
            return False

    expected_total_scenes = len(manifest.script.scenes)
    expected_total_shots = sum(len(plan.shots) for plan in manifest.cinematography)
    expected_elements = sum(len(sc.elements) for sc in manifest.script.scenes)
    shots_duration = round(
        sum(sh.duration_sec for plan in manifest.cinematography for sh in plan.shots), 6
    )

    live_axis_pairs = [
        (verify_180_degree_rule(plan.shots, plan.axis).compliant, plan.axis_report.compliant)
        for plan in manifest.cinematography
    ]
    axis_reports_consistent = all(live == cached for live, cached in live_axis_pairs)
    expected_axis_ok = all(live for live, _ in live_axis_pairs)

    live_edit_report = verify_editorial_timeline(manifest.editorial)
    edit_reports_consistent = (
        live_edit_report.compliant == manifest.editorial.verification.compliant
        and live_edit_report.eye_trace_valid == manifest.editorial.verification.eye_trace_valid
    )
    expected_edit_ok = live_edit_report.compliant
    expected_eye_ok = live_edit_report.eye_trace_valid

    summary_valid = (
        manifest.schema_version == MANIFEST_SCHEMA_VERSION
        and len(manifest.cinematography) == expected_total_scenes
        and manifest.total_scenes == expected_total_scenes
        and expected_total_shots > 0
        and expected_total_shots == expected_elements
        and len(manifest.editorial.events) == expected_total_shots
        and manifest.total_shots == expected_total_shots
        and math.isfinite(manifest.total_duration_sec)
        and abs(manifest.total_duration_sec - manifest.editorial.total_duration_sec) <= 1e-6
        and abs(manifest.total_duration_sec - shots_duration) <= 1e-5
        and axis_reports_consistent
        and edit_reports_consistent
        and manifest.axis_180_compliant is expected_axis_ok
        and manifest.editorial_compliant is expected_edit_ok
        and manifest.eye_trace_compliant is expected_eye_ok
    )
    if not summary_valid:
        return False

    expected_script, expected_cinema, expected_edit, expected_manifest, _ = _compute_digests(
        manifest.schema_version,
        manifest.script,
        manifest.cinematography,
        manifest.editorial,
        total_scenes=manifest.total_scenes,
        total_shots=manifest.total_shots,
        total_duration_sec=manifest.total_duration_sec,
        axis_180_compliant=manifest.axis_180_compliant,
        editorial_compliant=manifest.editorial_compliant,
        eye_trace_compliant=manifest.eye_trace_compliant,
    )
    return (
        manifest.script_sha256 == expected_script
        and manifest.cinematography_sha256 == expected_cinema
        and manifest.editorial_sha256 == expected_edit
        and manifest.manifest_sha256 == expected_manifest
    )


def compile_storyboard(
    script_text: str,
    *,
    config: StoryboardCompilerConfig | None = None,
) -> CompiledStoryboardManifest:
    """Compile a Persian/English screenplay into a verified, SHA-256-sealed storyboard manifest."""
    cfg = config if config is not None else StoryboardCompilerConfig()
    parsed = parse_bilingual_script(script_text)
    cinema_plans = solve_script_cinematography(
        parsed,
        initial_axis_side=cfg.initial_axis_side,
        num_trajectory_samples=cfg.num_trajectory_samples,
    )
    editorial = solve_editorial_timeline(
        cinema_plans,
        j_cut_min_sec=cfg.j_cut_min_sec,
        j_cut_max_sec=cfg.j_cut_max_sec,
        l_cut_min_sec=cfg.l_cut_min_sec,
        l_cut_max_sec=cfg.l_cut_max_sec,
        breath_gap_sec=cfg.breath_gap_sec,
        max_eye_trace_saccade=cfg.max_eye_trace_saccade,
        allow_scene_prelap=cfg.allow_scene_prelap,
    )
    return build_compiled_manifest(parsed, cinema_plans, editorial)
