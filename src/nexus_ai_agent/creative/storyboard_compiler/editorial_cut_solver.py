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
import hmac
import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final

import numpy as np

from .bilingual_script_parser import (
    ActionBeat,
    DialogueLine,
    EmotionTag,
    ParsedScript,
    normalize_script_text,
    parse_bilingual_script,
    verify_script_provenance,
)
from .cinematography_solver import (
    AxisEnforcementTrace,
    AxisVerificationReport,
    CameraMoveType,
    CameraPoseSE3,
    CameraShotSpec,
    SceneCinematographyPlan,
    ScreenLookDirection,
    ShotKind,
    evaluate_trajectory_at,
    expected_shot_move_type,
    is_valid_se3,
    so3_log,
    solve_script_cinematography,
    verify_180_degree_rule,
    verify_scene_character_blocking,
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
    "MAX_EYE_TRACE_SACCADE_UPPER_BOUND",
    "MIN_AUDIO_SEGMENT_SEC",
    "MIN_EYE_TRACE_SACCADE_LOWER_BOUND",
    "PacingProfile",
    "StoryboardCompilerConfig",
    "build_compiled_manifest",
    "compile_storyboard",
    "solve_editorial_timeline",
    "solve_eye_trace_transition",
    "verify_editorial_timeline",
    "verify_manifest_integrity",
    "verify_manifest_integrity_strict",
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

#: Inclusive policy bounds for any declared eye-trace saccade limit (normalized UV space).
#:
#: Exact geometry: focus points live in the unit square ``[0, 1]^2``, whose maximum achievable
#: Euclidean distance is the corner-to-corner diagonal ``sqrt(2) ~= 1.4142``. A limit at or above
#: that diagonal is unreachable, so ``_radial_eye_trace_projection`` never fires, every
#: ``reframe_applied`` stays ``False``, ``adjusted_entry_focus_uv == raw_entry_focus_uv``, and the
#: eye-trace invariant silently degenerates into a no-op. Verifying only a lower bound let an
#: attacker inflate ``max_eye_trace_saccade_limit`` (e.g. to ``2.0``), re-seal all four SHA-256
#: digests, and pass verification with unbounded eye jumps.
#:
#: ``0.50`` is therefore a deliberately conservative *cinematic* ceiling -- about ``35%`` of the
#: frame diagonal (``0.50 / sqrt(2) ~= 0.3536``) -- not a geometric one. It still admits every
#: legal Rule-of-Thirds reverse (``|2/3 - 1/3| = 0.3333``) and the third-diagonal case
#: (``sqrt(2)/3 ~= 0.4714``), while guaranteeing that some viewer eye path always remains
#: bounded and reframing stays a live, testable mechanism.
MIN_EYE_TRACE_SACCADE_LOWER_BOUND: Final[float] = 0.05
MAX_EYE_TRACE_SACCADE_UPPER_BOUND: Final[float] = 0.50

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
    look_direction: ScreenLookDirection = ScreenLookDirection.CENTER
    intentional_axis_jump: bool = False


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
    max_saccade_jump: float = 0.0


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
    #: Declared eye-trace policy (normalized UV). Must lie within
    #: [MIN_EYE_TRACE_SACCADE_LOWER_BOUND, MAX_EYE_TRACE_SACCADE_UPPER_BOUND] = [0.05, 0.50];
    #: 0.50 is a cinematic ceiling (~35% of the unit-square diagonal sqrt(2) ~= 1.4142), and a
    #: limit at or above that diagonal would disable reframing entirely, so it is never accepted.
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
    return (min(max(float(u), 0.0), 1.0), min(max(float(v), 0.0), 1.0))


def _radial_eye_trace_projection(
    exit_pt: tuple[float, float],
    raw_pt: tuple[float, float],
    r_max: float,
) -> tuple[bool, tuple[float, float]]:
    """Compute exact radial projection onto the closed Euclidean ball around ``exit_pt``."""
    du = raw_pt[0] - exit_pt[0]
    dv = raw_pt[1] - exit_pt[1]
    raw_dist = math.hypot(du, dv)
    if raw_dist <= r_max:
        return (False, raw_pt)
    scale = r_max / raw_dist
    return (
        True,
        (
            min(max(exit_pt[0] + scale * du, 0.0), 1.0),
            min(max(exit_pt[1] + scale * dv, 0.0), 1.0),
        ),
    )


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
    if not math.isfinite(max_saccade_uv) or not (
        MIN_EYE_TRACE_SACCADE_LOWER_BOUND <= max_saccade_uv <= MAX_EYE_TRACE_SACCADE_UPPER_BOUND
    ):
        raise EditorialCutError(
            f"max_saccade_uv must be within ["
            f"{MIN_EYE_TRACE_SACCADE_LOWER_BOUND}, {MAX_EYE_TRACE_SACCADE_UPPER_BOUND}], "
            f"got {max_saccade_uv!r}"
        )

    out_u, out_v = _clamp_uv(exit_focus_uv)
    in_u, in_v = _clamp_uv(entry_focus_uv)
    out_pt = (round(out_u, 6), round(out_v, 6))
    in_pt = (round(in_u, 6), round(in_v, 6))
    raw_dist = math.hypot(in_pt[0] - out_pt[0], in_pt[1] - out_pt[1])

    reframe, proj_pt = _radial_eye_trace_projection(out_pt, in_pt, max_saccade_uv)
    adj_pt = (round(proj_pt[0], 6), round(proj_pt[1], 6)) if reframe else in_pt

    eff_dist = math.hypot(adj_pt[0] - out_pt[0], adj_pt[1] - out_pt[1])
    vert_delta = abs(adj_pt[1] - out_pt[1])
    matched = _are_eyelines_complementary(out_look, in_look, vert_delta, axis_jumped)
    score = max(0.0, min(1.0, 1.0 - eff_dist / math.sqrt(2.0)))

    return EyeTraceTransition(
        transition_index=transition_index,
        from_shot_index=from_shot_index,
        to_shot_index=to_shot_index,
        exit_focus_uv=out_pt,
        raw_entry_focus_uv=in_pt,
        adjusted_entry_focus_uv=adj_pt,
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
    shots: Sequence[CameraShotSpec] | None = None,
    j_cut_min_sec: float = J_CUT_MIN_SEC,
    j_cut_max_sec: float = J_CUT_MAX_SEC,
    l_cut_min_sec: float = L_CUT_MIN_SEC,
    l_cut_max_sec: float = L_CUT_MAX_SEC,
    tol: float = 1e-5,
    expected_max_saccade_limit: float | None = None,
) -> EditorialVerificationReport:
    """Independently verify video contiguity, audio non-overlap, J/L bounds, and eye-trace.

    The declared saccade policy is validated *unconditionally* (before any per-transition loop)
    against :data:`MIN_EYE_TRACE_SACCADE_LOWER_BOUND` / :data:`MAX_EYE_TRACE_SACCADE_UPPER_BOUND`,
    so a tampered plan cannot neutralize the eye-trace invariant by inflating
    ``max_eye_trace_saccade_limit`` and reframing nothing. Pass
    ``expected_max_saccade_limit`` to additionally pin the plan to an exact policy value.

    A *declared* limit that is not a real number (a string, ``None``, a bool) is attacker content,
    so it is reported as a policy violation rather than raised -- but it is never fed to
    :func:`math.isfinite`/comparison operators, so no ``TypeError`` can leak. A *caller-supplied*
    pin is deployment configuration, so an invalid ``expected_max_saccade_limit`` raises
    :class:`ValueError` up front, exactly like ``position_tolerance`` and ``hmac_key``.
    """
    pinned_limit = (
        None if expected_max_saccade_limit is None else _finite_real(expected_max_saccade_limit)
    )
    if expected_max_saccade_limit is not None and pinned_limit is None:
        raise ValueError(
            "expected_max_saccade_limit must be None or a finite real number, got "
            f"{expected_max_saccade_limit!r}"
        )
    violations: list[str] = []
    video_ok = True
    audio_ok = True
    jl_ok = True
    eye_ok = True
    max_overlap = 0.0

    limit = plan.max_eye_trace_saccade_limit
    declared_limit = _finite_real(limit)
    # A non-numeric or non-finite declared limit cannot participate in any comparison, so the
    # audit substitutes the strictest conceivable policy instead of raising: every eye-trace check
    # then fails on its own merits and the violation text still reports the original repr.
    limit_value = declared_limit if declared_limit is not None else float("-inf")
    limit_in_policy = (
        declared_limit is not None
        and MIN_EYE_TRACE_SACCADE_LOWER_BOUND <= declared_limit <= MAX_EYE_TRACE_SACCADE_UPPER_BOUND
    )
    limit_matches_policy = pinned_limit is None or abs(limit_value - pinned_limit) <= tol
    if not (limit_in_policy and limit_matches_policy):
        eye_ok = False
        pinned = (
            ""
            if expected_max_saccade_limit is None
            else f" or != pinned policy {expected_max_saccade_limit:.6f}"
        )
        violations.append(
            f"eye-trace saccade policy violation: max_eye_trace_saccade_limit={limit!r} outside "
            f"[{MIN_EYE_TRACE_SACCADE_LOWER_BOUND}, {MAX_EYE_TRACE_SACCADE_UPPER_BOUND}]{pinned}"
        )

    events = plan.events
    if not events:
        # Fidelity: report the policy findings already established above instead of hard-coding
        # eye_trace_valid/jl_bounds_valid, so an empty timeline cannot launder an illegal
        # saccade policy (or any other pre-loop violation) into a "valid" sub-flag.
        violations.append("timeline has zero shot events")
        return EditorialVerificationReport(
            compliant=False,
            video_contiguous=False,
            audio_overlap_free=audio_ok,
            jl_bounds_valid=jl_ok,
            eye_trace_valid=eye_ok,
            max_audio_overlap_sec=0.0,
            violations=tuple(violations),
        )

    if abs(events[0].video_in_sec) > tol:
        video_ok = False
        violations.append(f"shot 0 video_in_sec={events[0].video_in_sec} != 0.0")

    for idx, ev in enumerate(events):
        if (
            ev.video_out_sec <= ev.video_in_sec + tol
            or abs(ev.video_duration_sec - (ev.video_out_sec - ev.video_in_sec)) > tol
        ):
            video_ok = False
            violations.append(f"shot {idx}: non-positive or inconsistent video duration")
        if ev.audio_in_sec < -tol or ev.audio_out_sec > plan.total_duration_sec + tol:
            audio_ok = False
            violations.append(
                f"shot {idx}: audio window [{ev.audio_in_sec}, {ev.audio_out_sec}] out of bounds"
            )
        if (
            ev.audio_out_sec <= ev.audio_in_sec + tol
            or abs(ev.audio_duration_sec - (ev.audio_out_sec - ev.audio_in_sec)) > tol
        ):
            audio_ok = False
            violations.append(f"shot {idx}: non-positive or inconsistent audio duration")
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

    max_saccade = 0.0
    expected_transitions = max(0, len(events) - 1)
    if len(plan.transitions) != expected_transitions:
        eye_ok = False
        jl_ok = False
        violations.append(
            f"transition count mismatch: got {len(plan.transitions)}, "
            f"expected {expected_transitions}"
        )
    else:
        for k, tr in enumerate(plan.transitions):
            out_ev = events[k]
            in_ev = events[k + 1]
            chain_ok = (
                tr.transition_index == k
                and tr.from_shot_index == out_ev.shot_index
                and tr.to_shot_index == in_ev.shot_index
                and tr.eye_trace.transition_index == k
                and tr.eye_trace.from_shot_index == out_ev.shot_index
                and tr.eye_trace.to_shot_index == in_ev.shot_index
                and abs(tr.video_cut_sec - out_ev.video_out_sec) <= tol
                and abs(tr.video_cut_sec - in_ev.video_in_sec) <= tol
            )
            if not chain_ok:
                eye_ok = False
                jl_ok = False
                violations.append(
                    f"transition {k}: broken shot chain or cut boundary "
                    f"(idx={tr.transition_index}, {tr.from_shot_index}->{tr.to_shot_index}, "
                    f"expected {out_ev.shot_index}->{in_ev.shot_index} at t={out_ev.video_out_sec})"
                )

            if tr.cut_type == CutType.J_CUT:
                actual_prelap = tr.video_cut_sec - in_ev.audio_in_sec
                expected_audio_cut = in_ev.audio_in_sec
                expected_j_prelap = actual_prelap
                expected_l_postlap = 0.0
                if not (j_cut_min_sec - tol <= actual_prelap <= j_cut_max_sec + tol):
                    jl_ok = False
                    violations.append(
                        f"transition {k}: J-Cut pre-lap {actual_prelap:.6f}s "
                        f"outside [{j_cut_min_sec}, {j_cut_max_sec}]"
                    )
            elif tr.cut_type == CutType.L_CUT:
                actual_postlap = out_ev.audio_out_sec - tr.video_cut_sec
                expected_audio_cut = out_ev.audio_out_sec
                expected_j_prelap = 0.0
                expected_l_postlap = actual_postlap
                if not (l_cut_min_sec - tol <= actual_postlap <= l_cut_max_sec + tol):
                    jl_ok = False
                    violations.append(
                        f"transition {k}: L-Cut post-lap {actual_postlap:.6f}s "
                        f"outside [{l_cut_min_sec}, {l_cut_max_sec}]"
                    )
            else:
                expected_audio_cut = tr.video_cut_sec
                expected_j_prelap = 0.0
                expected_l_postlap = 0.0
                if abs(in_ev.audio_in_sec - tr.video_cut_sec) > tol:
                    jl_ok = False
                    violations.append(f"transition {k}: STRAIGHT_CUT audio_in misaligned")

            expected_audio_offset = expected_audio_cut - tr.video_cut_sec
            derived_cut_ok = (
                events[0].entry_cut_type is None
                and events[-1].exit_cut_type is None
                and tr.scene_boundary is (out_ev.scene_index != in_ev.scene_index)
                and out_ev.exit_cut_type == tr.cut_type
                and in_ev.entry_cut_type == tr.cut_type
                and abs(tr.audio_cut_sec - expected_audio_cut) <= 1e-6
                and abs(tr.audio_offset_sec - expected_audio_offset) <= 1e-6
                and abs(tr.j_cut_prelap_sec - expected_j_prelap) <= 1e-6
                and abs(tr.l_cut_postlap_sec - expected_l_postlap) <= 1e-6
            )
            if not derived_cut_ok:
                jl_ok = False
                violations.append(
                    f"transition {k}: derived CutTransition fields inconsistent with events"
                )

            et = tr.eye_trace
            coords_finite = (
                all(
                    math.isfinite(c) and 0.0 <= c <= 1.0
                    for c in (
                        *et.exit_focus_uv,
                        *et.raw_entry_focus_uv,
                        *et.adjusted_entry_focus_uv,
                    )
                )
                and MIN_EYE_TRACE_SACCADE_LOWER_BOUND
                <= limit_value
                <= MAX_EYE_TRACE_SACCADE_UPPER_BOUND
            )
            recomputed_raw_saccade = math.hypot(
                et.raw_entry_focus_uv[0] - et.exit_focus_uv[0],
                et.raw_entry_focus_uv[1] - et.exit_focus_uv[1],
            )
            recomputed_eff_saccade = math.hypot(
                et.adjusted_entry_focus_uv[0] - et.exit_focus_uv[0],
                et.adjusted_entry_focus_uv[1] - et.exit_focus_uv[1],
            )
            recomputed_vert_delta = abs(et.adjusted_entry_focus_uv[1] - et.exit_focus_uv[1])
            recomputed_score = max(0.0, min(1.0, 1.0 - recomputed_eff_saccade / math.sqrt(2.0)))
            expected_reframe, expected_proj_uv = (
                _radial_eye_trace_projection(
                    et.exit_focus_uv,
                    et.raw_entry_focus_uv,
                    limit_value,
                )
                if coords_finite
                else (False, et.raw_entry_focus_uv)
            )
            shots_aligned = shots is None or (
                len(shots) == len(events)
                and out_ev.look_direction == shots[k].framing.look_direction
                and in_ev.look_direction == shots[k + 1].framing.look_direction
                and out_ev.intentional_axis_jump is shots[k].intentional_axis_jump
                and in_ev.intentional_axis_jump is shots[k + 1].intentional_axis_jump
            )
            out_look = (
                shots[k].framing.look_direction
                if shots is not None and len(shots) == len(events)
                else out_ev.look_direction
            )
            in_look = (
                shots[k + 1].framing.look_direction
                if shots is not None and len(shots) == len(events)
                else in_ev.look_direction
            )
            axis_jumped = (
                shots[k + 1].intentional_axis_jump
                if shots is not None and len(shots) == len(events)
                else in_ev.intentional_axis_jump
            )
            expected_matched = _are_eyelines_complementary(
                out_look, in_look, recomputed_vert_delta, axis_jumped
            )
            eye_geom_ok = (
                coords_finite
                and shots_aligned
                and abs(et.exit_focus_uv[0] - out_ev.exit_focus_uv[0]) <= 1e-6
                and abs(et.exit_focus_uv[1] - out_ev.exit_focus_uv[1]) <= 1e-6
                and abs(et.adjusted_entry_focus_uv[0] - in_ev.entry_focus_uv[0]) <= 1e-6
                and abs(et.adjusted_entry_focus_uv[1] - in_ev.entry_focus_uv[1]) <= 1e-6
                and abs(et.raw_saccade_uv - recomputed_raw_saccade) <= 1e-6
                and abs(et.effective_saccade_uv - recomputed_eff_saccade) <= 1e-6
                and abs(et.vertical_eyeline_delta - recomputed_vert_delta) <= 1e-6
                and abs(et.continuity_score - recomputed_score) <= 1e-6
                and et.reframe_applied is expected_reframe
                and abs(et.adjusted_entry_focus_uv[0] - expected_proj_uv[0]) <= 1e-6
                and abs(et.adjusted_entry_focus_uv[1] - expected_proj_uv[1]) <= 1e-6
                and et.eyeline_matched is expected_matched
            )
            max_saccade = max(max_saccade, recomputed_eff_saccade)
            if not eye_geom_ok or recomputed_eff_saccade > limit_value + tol:
                eye_ok = False
                violations.append(
                    f"transition {k}: eye-trace geometry mismatch or saccade "
                    f"{recomputed_eff_saccade:.6f} exceeds limit "
                    f"{limit_value:.6f}"
                )

    return EditorialVerificationReport(
        compliant=len(violations) == 0,
        video_contiguous=video_ok,
        audio_overlap_free=audio_ok,
        jl_bounds_valid=jl_ok,
        eye_trace_valid=eye_ok,
        max_audio_overlap_sec=round(max(0.0, max_overlap), 6),
        violations=tuple(violations),
        max_saccade_jump=round(max(0.0, max_saccade), 6),
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
    policy_limit = _finite_real(max_eye_trace_saccade)
    limit_ok = (
        policy_limit is not None
        and MIN_EYE_TRACE_SACCADE_LOWER_BOUND <= policy_limit <= MAX_EYE_TRACE_SACCADE_UPPER_BOUND
    )
    if not limit_ok:
        raise EditorialCutError(
            f"max_eye_trace_saccade must lie within "
            f"[{MIN_EYE_TRACE_SACCADE_LOWER_BOUND}, {MAX_EYE_TRACE_SACCADE_UPPER_BOUND}], "
            f"got {max_eye_trace_saccade!r}"
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
                look_direction=shot.framing.look_direction,
                intentional_axis_jump=shot.intentional_axis_jump,
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
        shots=all_shots,
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
        # Provenance payload: script_sha256 now commits to the exact normalized screenplay text
        # that the AST claims to be the parse of, so the source cannot be swapped out silently.
        "normalized_source": script.normalized_source,
        "language": script.language.value,
        "characters": list(script.characters),
        "total_duration_sec": _qfloat(script.total_duration_sec),
        "scenes": scenes_out,
    }


def _serialize_axis_trace(trace: AxisEnforcementTrace) -> dict[str, Any]:
    return {
        "adjusted_position": [_qfloat(c) for c in trace.adjusted_position],
        "original_position": [_qfloat(c) for c in trace.original_position],
        "original_axis_side": trace.original_axis_side,
        "enforced_axis_side": trace.enforced_axis_side,
        "original_signed_distance_m": _qfloat(trace.original_signed_distance_m),
        "enforced_signed_distance_m": _qfloat(trace.enforced_signed_distance_m),
        "was_axis_reflected": trace.was_axis_reflected,
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
                    "element_index": sh.element_index,
                    "shot_kind": sh.shot_kind.value,
                    "subject_label": sh.subject_label,
                    "subject_position": [_qfloat(c) for c in sh.subject_position],
                    "duration_sec": _qfloat(sh.duration_sec),
                    "dramatic_tension": _qfloat(sh.dramatic_tension),
                    "emotion": sh.emotion.value,
                    "axis_side": sh.axis_side,
                    "original_axis_side": sh.original_axis_side,
                    "was_axis_reflected": sh.was_axis_reflected,
                    "start_original_axis_side": sh.start_original_axis_side,
                    "end_original_axis_side": sh.end_original_axis_side,
                    "start_was_reflected": sh.start_was_reflected,
                    "end_was_reflected": sh.end_was_reflected,
                    "axis_cross_z": _qfloat(sh.axis_cross_z),
                    "end_axis_cross_z": _qfloat(sh.end_axis_cross_z),
                    "intentional_axis_jump": sh.intentional_axis_jump,
                    "start_axis_trace": _serialize_axis_trace(sh.start_axis_trace),
                    "end_axis_trace": _serialize_axis_trace(sh.end_axis_trace),
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
                        "samples": [
                            {
                                "tau": _qfloat(smp.tau),
                                "time_sec": _qfloat(smp.time_sec),
                                "s": _qfloat(smp.s),
                                "ds_dtau": _qfloat(smp.ds_dtau),
                                "d2s_dtau2": _qfloat(smp.d2s_dtau2),
                                "d3s_dtau3": _qfloat(smp.d3s_dtau3),
                                "pose_R": [[_qfloat(v) for v in row] for row in smp.pose.rotation],
                                "pose_t": [_qfloat(v) for v in smp.pose.translation],
                                "linear_velocity": [_qfloat(v) for v in smp.linear_velocity],
                                "linear_acceleration": [
                                    _qfloat(v) for v in smp.linear_acceleration
                                ],
                                "linear_jerk": [_qfloat(v) for v in smp.linear_jerk],
                                "angular_velocity": [_qfloat(v) for v in smp.angular_velocity],
                                "angular_acceleration": [
                                    _qfloat(v) for v in smp.angular_acceleration
                                ],
                                "angular_jerk": [_qfloat(v) for v in smp.angular_jerk],
                            }
                            for smp in sh.trajectory.samples
                        ],
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
                # Canonical name-sorted blocking: cryptographically binds every character
                # position into cinematography_sha256 so it cannot be silently re-staged.
                "character_positions": [
                    [name, [_qfloat(c) for c in pos]]
                    for name, pos in sorted(plan.character_positions, key=lambda item: item[0])
                ],
                "axis_compliant": plan.axis_report.compliant,
                "reflected_shot_indices": list(plan.axis_report.reflected_shot_indices),
                "axis_report": {
                    "compliant": plan.axis_report.compliant,
                    "initial_side": plan.axis_report.initial_side,
                    "intentional_jumps": plan.axis_report.intentional_jumps,
                    "reflected_shot_indices": list(plan.axis_report.reflected_shot_indices),
                    "cross_products": [_qfloat(c) for c in plan.axis_report.cross_products],
                    "violations": list(plan.axis_report.violations),
                },
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
        "verification": {
            "compliant": editorial.verification.compliant,
            "video_contiguous": editorial.verification.video_contiguous,
            "audio_overlap_free": editorial.verification.audio_overlap_free,
            "jl_bounds_valid": editorial.verification.jl_bounds_valid,
            "eye_trace_valid": editorial.verification.eye_trace_valid,
            "max_audio_overlap_sec": _qfloat(editorial.verification.max_audio_overlap_sec),
            "max_saccade_jump": _qfloat(editorial.verification.max_saccade_jump),
            "violations": list(editorial.verification.violations),
        },
        "events": [
            {
                "shot_index": ev.shot_index,
                "scene_index": ev.scene_index,
                "shot_kind": ev.shot_kind.value,
                "speaker": ev.speaker,
                "video_in_sec": _qfloat(ev.video_in_sec),
                "video_out_sec": _qfloat(ev.video_out_sec),
                "video_duration_sec": _qfloat(ev.video_duration_sec),
                "audio_in_sec": _qfloat(ev.audio_in_sec),
                "audio_out_sec": _qfloat(ev.audio_out_sec),
                "audio_duration_sec": _qfloat(ev.audio_duration_sec),
                "entry_cut_type": ev.entry_cut_type.value if ev.entry_cut_type else None,
                "exit_cut_type": ev.exit_cut_type.value if ev.exit_cut_type else None,
                "entry_focus_uv": [_qfloat(v) for v in ev.entry_focus_uv],
                "exit_focus_uv": [_qfloat(v) for v in ev.exit_focus_uv],
                "look_direction": ev.look_direction.value,
                "intentional_axis_jump": ev.intentional_axis_jump,
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
                    "raw_saccade_uv": _qfloat(tr.eye_trace.raw_saccade_uv),
                    "effective_saccade_uv": _qfloat(tr.eye_trace.effective_saccade_uv),
                    "vertical_eyeline_delta": _qfloat(tr.eye_trace.vertical_eyeline_delta),
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
    """Immutable, SHA-256-sealed deliverable of the deterministic storyboard compiler.

    The four SHA-256 digests form the audit trail for deterministic, reproducible compilation.
    They are, however, *self-referential*: a forger who rewrites the whole scene graph can recompute
    all of them, so the manifest also carries an optional HMAC-SHA256 seal over ``manifest_sha256``
    keyed with a deployment secret. That seal is the external trust anchor: without the key, no
    forged -- however internally consistent -- manifest can be sealed. It defaults to ``""``
    (unsigned) for backward compatibility and is deliberately excluded from the canonical payload,
    because it seals the payload rather than forming part of it.
    """

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
    #: HMAC-SHA256 (hex) of ``manifest_sha256`` under the deployment secret; "" means unsigned.
    manifest_hmac_sha256: str = ""

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


def _finite_real(value: object) -> float | None:
    """Return ``value`` as a finite ``float``, or ``None`` for bools, non-numbers, NaN and Inf.

    Declared ``float`` parameters can still receive anything at runtime -- a manifest is
    deserialized, and a config may arrive from JSON. Centralising the coercion here means every
    numeric policy value is refused the same way (``None`` for the caller) instead of leaking a
    ``TypeError`` out of a comparison or out of :func:`math.isfinite`.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _hmac_key_bytes(hmac_key: object) -> bytes:
    """Coerce a deployment signing secret into non-empty bytes, refusing anything else.

    Accepts ``str`` (UTF-8) or ``bytes``/``bytearray``; every other type -- including ``None``,
    ``int``, and ``bool`` -- raises :class:`ValueError`, as does an empty secret. A verifier catches
    this and fails closed; a builder lets it stop the caller before any work is done.
    """
    if isinstance(hmac_key, str):
        key = hmac_key.encode("utf-8")
    elif isinstance(hmac_key, (bytes, bytearray)):
        key = bytes(hmac_key)
    else:
        raise ValueError(
            f"hmac_key must be a non-empty str or bytes secret, got {type(hmac_key).__name__}: "
            f"{hmac_key!r}"
        )
    if not key:
        raise ValueError("hmac_key must not be empty")
    return key


def _seal_manifest_hmac(manifest_sha256: str, key: bytes) -> str:
    """HMAC-SHA256 (lowercase hex) over a manifest digest under ``key``."""
    return hmac.new(key, manifest_sha256.encode("utf-8"), hashlib.sha256).hexdigest()


def build_compiled_manifest(
    script: ParsedScript,
    cinematography: Sequence[SceneCinematographyPlan],
    editorial: EditorialTimelinePlan,
    *,
    expected_max_saccade_limit: float | None = None,
    hmac_key: bytes | str | None = None,
) -> CompiledStoryboardManifest:
    """Assemble and cryptographically seal a ``CompiledStoryboardManifest`` with SHA-256.

    ``expected_max_saccade_limit`` optionally pins the editorial eye-trace policy: when given, the
    sealed plan must declare exactly that limit (in addition to the canonical bounds check), so a
    deployment with a fixed policy can refuse manifests that quietly relax it.

    ``hmac_key`` optionally seals ``manifest_sha256`` with HMAC-SHA256 under a deployment secret,
    producing the external trust anchor :func:`verify_manifest_integrity` can demand. A key that is
    not a non-empty ``str``/``bytes`` raises :class:`ValueError` before anything is built.
    """
    key_bytes: bytes | None = None
    if hmac_key is not None:
        key_bytes = _hmac_key_bytes(hmac_key)
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

    all_shots = [sh for plan in cinema_tuple for sh in plan.shots]
    total_scenes = len(script.scenes)
    # bind_axis=True keeps ``axis_180_compliant`` semantically identical to what
    # ``verify_manifest_integrity`` recomputes (canonical, scene-derived axis included), so a plan
    # can never be sealed as compliant and then refused for the very same reason.
    axis_ok = all(
        verify_180_degree_rule(
            plan.shots,
            plan.axis,
            scene=script.scenes[i],
            character_positions=plan.character_positions,
            bind_axis=True,
        ).compliant
        and plan.axis_report.compliant
        for i, plan in enumerate(cinema_tuple)
    )
    live_edit = verify_editorial_timeline(
        editorial,
        shots=all_shots,
        expected_max_saccade_limit=expected_max_saccade_limit,
    )
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
        manifest_hmac_sha256=(
            "" if key_bytes is None else _seal_manifest_hmac(manifest_sha, key_bytes)
        ),
    )


def _poses_match(pose_a: CameraPoseSE3, pose_b: CameraPoseSE3, *, tol: float = 1e-8) -> bool:
    return all(
        abs(pose_a.rotation[r][c] - pose_b.rotation[r][c]) <= tol
        for r in range(3)
        for c in range(3)
    ) and all(abs(pose_a.translation[i] - pose_b.translation[i]) <= tol for i in range(3))


def _shot_trajectory_valid(shot: CameraShotSpec, *, tol: float = 1e-6) -> bool:
    """Verify full quintic minimum-jerk kinematics & totals across every trajectory sample."""
    samples = shot.trajectory.samples
    num = len(samples)
    crane_arc = shot.trajectory.crane_arc_m
    se3_ok = is_valid_se3(shot.pose.matrix) and is_valid_se3(shot.end_pose.matrix)
    params_ok = (
        se3_ok
        and num >= 2
        and math.isfinite(shot.duration_sec)
        and shot.duration_sec > 0.0
        and abs(shot.trajectory.duration_sec - shot.duration_sec) <= 1e-6
        and math.isfinite(crane_arc)
        and (
            crane_arc == 0.0
            if shot.trajectory.move_type != CameraMoveType.CRANE
            else crane_arc >= 0.0
        )
        and _poses_match(shot.trajectory.start_pose, shot.pose, tol=tol)
        and _poses_match(shot.trajectory.end_pose, shot.end_pose, tol=tol)
        and all(
            math.isfinite(samples[i].tau) and abs(samples[i].tau - (i / (num - 1))) <= tol
            for i in range(num)
        )
    )
    expected_arc_len = (
        sum(
            math.sqrt(
                sum(
                    (samples[i + 1].pose.translation[j] - samples[i].pose.translation[j]) ** 2
                    for j in range(3)
                )
            )
            for i in range(num - 1)
        )
        if num >= 2
        else 0.0
    )
    expected_rot_deg = (
        math.degrees(
            float(
                np.linalg.norm(so3_log(shot.pose.rotation_matrix.T @ shot.end_pose.rotation_matrix))
            )
        )
        if se3_ok
        else -1.0
    )
    return (
        params_ok
        and abs(shot.trajectory.total_translation_m - expected_arc_len) <= 1e-6
        and abs(shot.trajectory.total_rotation_deg - expected_rot_deg) <= 1e-6
        and all(
            (
                lambda smp, exp: (
                    abs(smp.time_sec - exp.time_sec) <= 1e-6
                    and abs(smp.s - exp.s) <= 1e-6
                    and abs(smp.ds_dtau - exp.ds_dtau) <= 1e-6
                    and abs(smp.d2s_dtau2 - exp.d2s_dtau2) <= 1e-6
                    and abs(smp.d3s_dtau3 - exp.d3s_dtau3) <= 1e-6
                    and _poses_match(smp.pose, exp.pose, tol=tol)
                    and all(
                        abs(smp.linear_velocity[j] - exp.linear_velocity[j]) <= 1e-6
                        for j in range(3)
                    )
                    and all(
                        abs(smp.linear_acceleration[j] - exp.linear_acceleration[j]) <= 1e-6
                        for j in range(3)
                    )
                    and all(abs(smp.linear_jerk[j] - exp.linear_jerk[j]) <= 1e-6 for j in range(3))
                    and all(
                        abs(smp.angular_velocity[j] - exp.angular_velocity[j]) <= 1e-6
                        for j in range(3)
                    )
                    and all(
                        abs(smp.angular_acceleration[j] - exp.angular_acceleration[j]) <= 1e-6
                        for j in range(3)
                    )
                    and all(
                        abs(smp.angular_jerk[j] - exp.angular_jerk[j]) <= 1e-6 for j in range(3)
                    )
                )
            )(
                samples[i],
                evaluate_trajectory_at(
                    shot.pose,
                    shot.end_pose,
                    samples[i].tau,
                    duration_sec=shot.duration_sec,
                    crane_arc_m=crane_arc,
                ),
            )
            for i in range(num)
        )
        and all(
            abs(comp) <= tol
            for vec in (
                samples[0].linear_velocity,
                samples[-1].linear_velocity,
                samples[0].linear_acceleration,
                samples[-1].linear_acceleration,
                samples[0].angular_velocity,
                samples[-1].angular_velocity,
                samples[0].angular_acceleration,
                samples[-1].angular_acceleration,
            )
            for comp in vec
        )
    )


def _axis_reports_equal(
    live: AxisVerificationReport,
    cached: AxisVerificationReport,
    *,
    tol: float = 1e-6,
) -> bool:
    return (
        live.compliant == cached.compliant
        and live.initial_side == cached.initial_side
        and live.intentional_jumps == cached.intentional_jumps
        and live.reflected_shot_indices == cached.reflected_shot_indices
        and live.violations == cached.violations
        and len(live.cross_products) == len(cached.cross_products)
        and all(
            abs(live.cross_products[i] - cached.cross_products[i]) <= tol
            for i in range(len(live.cross_products))
        )
    )


def _editorial_reports_equal(
    live: EditorialVerificationReport,
    cached: EditorialVerificationReport,
    *,
    tol: float = 1e-6,
) -> bool:
    return (
        live.compliant == cached.compliant
        and live.video_contiguous == cached.video_contiguous
        and live.audio_overlap_free == cached.audio_overlap_free
        and live.jl_bounds_valid == cached.jl_bounds_valid
        and live.eye_trace_valid == cached.eye_trace_valid
        and abs(live.max_audio_overlap_sec - cached.max_audio_overlap_sec) <= tol
        and abs(live.max_saccade_jump - cached.max_saccade_jump) <= tol
        and live.violations == cached.violations
    )


def _element_duration_sec(elem: DialogueLine | ActionBeat) -> float:
    return elem.timing.total_duration_sec if isinstance(elem, DialogueLine) else elem.duration_sec


def _element_emotion(elem: DialogueLine | ActionBeat) -> EmotionTag:
    return elem.emotion if isinstance(elem, DialogueLine) else EmotionTag.NEUTRAL


def _scene_shot_bijection_valid(manifest: CompiledStoryboardManifest) -> bool:
    scenes = manifest.script.scenes
    plans = manifest.cinematography
    events = manifest.editorial.events
    transitions = manifest.editorial.transitions
    all_shots = [sh for plan in plans for sh in plan.shots]
    return (
        len(plans) == len(scenes)
        and all(
            scenes[i].scene_index == i
            and plans[i].scene_index == i
            and len(plans[i].shots) == len(scenes[i].elements)
            and [
                (
                    sh.scene_index,
                    sh.shot_in_scene,
                    sh.element_index,
                    sh.shot_kind == ShotKind.DIALOGUE,
                )
                for sh in plans[i].shots
            ]
            == [
                (
                    scenes[i].scene_index,
                    k,
                    scenes[i].elements[k].element_index,
                    isinstance(scenes[i].elements[k], DialogueLine),
                )
                for k in range(len(scenes[i].elements))
            ]
            and all(
                abs(plans[i].shots[k].duration_sec - _element_duration_sec(scenes[i].elements[k]))
                <= 1e-6
                and abs(plans[i].shots[k].dramatic_tension - scenes[i].elements[k].dramatic_tension)
                <= 1e-6
                and plans[i].shots[k].emotion == _element_emotion(scenes[i].elements[k])
                and plans[i].shots[k].intentional_axis_jump
                is scenes[i].elements[k].intentional_axis_jump
                and plans[i].shots[k].trajectory.move_type
                == expected_shot_move_type(plans[i].shots[k])
                for k in range(len(scenes[i].elements))
            )
            # Wave-5 (F7/R1): axis endpoints, character blocking, and every shot subject must be
            # exactly what deterministic staging of this parsed scene produces -- recomputed from
            # the script, so re-sealing hashes cannot launder forged coordinates.
            and verify_scene_character_blocking(
                plans[i].shots,
                plans[i].axis,
                scene=scenes[i],
                character_positions=plans[i].character_positions,
                bind_axis=True,
            )
            for i in range(len(scenes))
        )
        and len(all_shots) > 0
        and [sh.shot_index for sh in all_shots] == list(range(len(all_shots)))
        and [(sh.shot_index, sh.scene_index, sh.shot_kind) for sh in all_shots]
        == [(ev.shot_index, ev.scene_index, ev.shot_kind) for ev in events]
        and len(transitions) == len(all_shots) - 1
        and all(
            abs((events[k].video_out_sec - events[k].video_in_sec) - all_shots[k].duration_sec)
            <= 1e-6
            and abs(events[k].video_duration_sec - all_shots[k].duration_sec) <= 1e-6
            and abs(all_shots[k].trajectory.duration_sec - all_shots[k].duration_sec) <= 1e-6
            and abs(events[k].exit_focus_uv[0] - all_shots[k].framing.projected_end_uv[0]) <= 1e-6
            and abs(events[k].exit_focus_uv[1] - all_shots[k].framing.projected_end_uv[1]) <= 1e-6
            and events[k].look_direction == all_shots[k].framing.look_direction
            and events[k].intentional_axis_jump is all_shots[k].intentional_axis_jump
            and events[k].speaker
            == (all_shots[k].subject_label if all_shots[k].shot_kind == ShotKind.DIALOGUE else None)
            for k in range(len(all_shots))
        )
        and abs(events[0].entry_focus_uv[0] - all_shots[0].framing.projected_start_uv[0]) <= 1e-6
        and abs(events[0].entry_focus_uv[1] - all_shots[0].framing.projected_start_uv[1]) <= 1e-6
        and all(
            abs(
                transitions[k].eye_trace.exit_focus_uv[0] - all_shots[k].framing.projected_end_uv[0]
            )
            <= 1e-6
            and abs(
                transitions[k].eye_trace.exit_focus_uv[1] - all_shots[k].framing.projected_end_uv[1]
            )
            <= 1e-6
            and abs(
                transitions[k].eye_trace.raw_entry_focus_uv[0]
                - all_shots[k + 1].framing.projected_start_uv[0]
            )
            <= 1e-6
            and abs(
                transitions[k].eye_trace.raw_entry_focus_uv[1]
                - all_shots[k + 1].framing.projected_start_uv[1]
            )
            <= 1e-6
            and abs(
                events[k + 1].entry_focus_uv[0]
                - transitions[k].eye_trace.adjusted_entry_focus_uv[0]
            )
            <= 1e-6
            and abs(
                events[k + 1].entry_focus_uv[1]
                - transitions[k].eye_trace.adjusted_entry_focus_uv[1]
            )
            <= 1e-6
            for k in range(len(transitions))
        )
    )


def _editorial_summary_valid(editorial: EditorialTimelinePlan) -> bool:
    n_events = len(editorial.events)
    n_trans = len(editorial.transitions)
    expected_asl = round(editorial.total_duration_sec / n_events, 6) if n_events > 0 else 0.0
    expected_cpm = (
        round(((n_events - 1) / editorial.total_duration_sec) * 60.0, 6)
        if n_events > 0 and editorial.total_duration_sec > 0.0
        else 0.0
    )
    expected_mean_eye = (
        round(sum(tr.eye_trace.continuity_score for tr in editorial.transitions) / n_trans, 6)
        if n_trans > 0
        else 1.0
    )
    declared_limit = _finite_real(editorial.max_eye_trace_saccade_limit)
    return (
        n_events > 0
        and declared_limit is not None
        and MIN_EYE_TRACE_SACCADE_LOWER_BOUND <= declared_limit <= MAX_EYE_TRACE_SACCADE_UPPER_BOUND
        and 0.0 <= editorial.breath_gap_sec <= 0.25
        and editorial.events[0].entry_cut_type is None
        and editorial.events[-1].exit_cut_type is None
        and editorial.j_cut_count
        == sum(1 for tr in editorial.transitions if tr.cut_type == CutType.J_CUT)
        and editorial.l_cut_count
        == sum(1 for tr in editorial.transitions if tr.cut_type == CutType.L_CUT)
        and editorial.straight_cut_count
        == sum(1 for tr in editorial.transitions if tr.cut_type == CutType.STRAIGHT_CUT)
        and abs(editorial.average_shot_length_sec - expected_asl) <= 1e-5
        and abs(editorial.cuts_per_minute - expected_cpm) <= 1e-4
        and abs(editorial.mean_eye_trace_score - expected_mean_eye) <= 1e-5
        and editorial.pacing_profile == _classify_pacing(editorial.average_shot_length_sec)
    )


def verify_manifest_integrity(
    manifest: CompiledStoryboardManifest,
    *,
    expected_max_saccade_limit: float | None = None,
    expected_source_sha256: str | None = None,
    expected_initial_axis_side: int | None = None,
    hmac_key: bytes | str | None = None,
    require_trusted_anchor: bool = False,
) -> bool:
    """Recompute scene graph, reports, trajectories, and SHA-256 digests; True iff untampered.

    Optional deployment policy pins, all fail-closed: ``expected_max_saccade_limit`` requires the
    sealed eye-trace policy to equal that value (the canonical ``[0.05, 0.50]`` bounds hold
    either way); ``expected_source_sha256`` must match the hashed screenplay; and
    ``expected_initial_axis_side`` (only ``-1``/``+1``) must equal every scene's opening side.

    The AST is never trusted on its own word: the deterministic parser is replayed on the
    manifest's retained ``normalized_source``, so a hand-forged scene graph is rejected even
    after all four SHA-256 digests have been recomputed to match it.

    That replay still leaves a *self-consistent* forgery possible -- an attacker who writes a whole
    second screenplay, rebuilds its AST, and recomputes every digest produces a manifest that
    verifies internally. Only a key the attacker does not have can rule that out, hence:

    * ``hmac_key`` -- verify the manifest's ``manifest_hmac_sha256`` seal with HMAC-SHA256 over
      ``manifest_sha256``. A missing (``""``), malformed, or wrong-key seal rejects, compared with
      :func:`hmac.compare_digest` so a mismatch leaks nothing through timing. An unusable key
      (non-``str``/``bytes``, or empty) rejects rather than raising.
    * ``require_trusted_anchor`` -- refuse any manifest that cannot be tied to something outside
      itself: with this set, at least one of ``expected_source_sha256`` / ``hmac_key`` must be
      supplied, otherwise verification fails immediately.
    """
    if require_trusted_anchor and expected_source_sha256 is None and hmac_key is None:
        return False
    for digest in (
        manifest.script_sha256,
        manifest.cinematography_sha256,
        manifest.editorial_sha256,
        manifest.manifest_sha256,
    ):
        if not _SHA256_HEX_RE.match(digest):
            return False

    if hmac_key is not None:
        try:
            key_bytes = _hmac_key_bytes(hmac_key)
        except ValueError:
            return False
        seal = manifest.manifest_hmac_sha256
        if not _SHA256_HEX_RE.match(seal):
            return False
        if not hmac.compare_digest(_seal_manifest_hmac(manifest.manifest_sha256, key_bytes), seal):
            return False

    if not verify_script_provenance(manifest.script, expected_source_sha256=expected_source_sha256):
        return False

    expected_total_scenes = len(manifest.script.scenes)
    expected_total_shots = sum(len(plan.shots) for plan in manifest.cinematography)
    shots_duration = round(
        sum(sh.duration_sec for plan in manifest.cinematography for sh in plan.shots), 6
    )

    scene_plan_pairs = list(zip(manifest.script.scenes, manifest.cinematography, strict=False))
    scenes_plans_aligned = len(scene_plan_pairs) == len(manifest.cinematography)
    live_axis_pairs = [
        (
            verify_180_degree_rule(
                plan.shots,
                plan.axis,
                scene=scene,
                character_positions=plan.character_positions,
                bind_axis=True,
                expected_initial_axis_side=expected_initial_axis_side,
            ),
            plan.axis_report,
        )
        for scene, plan in scene_plan_pairs
    ]
    axis_reports_consistent = scenes_plans_aligned and all(
        _axis_reports_equal(live, cached) for live, cached in live_axis_pairs
    )
    expected_axis_ok = scenes_plans_aligned and all(live.compliant for live, _ in live_axis_pairs)

    all_shots = [sh for plan in manifest.cinematography for sh in plan.shots]
    live_edit_report = verify_editorial_timeline(
        manifest.editorial,
        shots=all_shots,
        expected_max_saccade_limit=expected_max_saccade_limit,
    )

    edit_reports_consistent = _editorial_reports_equal(
        live_edit_report, manifest.editorial.verification
    )
    expected_edit_ok = live_edit_report.compliant
    expected_eye_ok = live_edit_report.eye_trace_valid

    trajectories_valid = all(
        _shot_trajectory_valid(sh) for plan in manifest.cinematography for sh in plan.shots
    )

    summary_valid = (
        manifest.schema_version == MANIFEST_SCHEMA_VERSION
        and _scene_shot_bijection_valid(manifest)
        and _editorial_summary_valid(manifest.editorial)
        and manifest.total_scenes == expected_total_scenes
        and expected_total_shots > 0
        and manifest.total_shots == expected_total_shots
        and math.isfinite(manifest.total_duration_sec)
        and abs(manifest.total_duration_sec - manifest.editorial.total_duration_sec) <= 1e-6
        and abs(manifest.total_duration_sec - shots_duration) <= 1e-5
        and trajectories_valid
        and axis_reports_consistent
        and edit_reports_consistent
        and expected_axis_ok
        and expected_edit_ok
        and expected_eye_ok
        and manifest.axis_180_compliant is True
        and manifest.editorial_compliant is True
        and manifest.eye_trace_compliant is True
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


def _trusted_source_digest(trusted_source_text: str | bytes) -> str | None:
    """SHA-256 of the *normalized* form of an independently supplied screenplay.

    The digest is computed here, from bytes the caller handed us, and in the same
    normalization the parser applies (``normalize_script_text``) — so it is directly
    comparable with ``ParsedScript.source_sha256`` without the caller ever touching
    a field of the manifest it is asking us to trust. Returns ``None`` (fail-closed)
    for unusable input rather than raising: this is an untrusted-input boundary.
    """
    if isinstance(trusted_source_text, bytes):
        try:
            text = trusted_source_text.decode("utf-8")
        except UnicodeDecodeError:
            return None
    elif isinstance(trusted_source_text, str):
        text = trusted_source_text
    else:
        return None
    if not text:
        return None
    try:
        normalized = normalize_script_text(text)
    except Exception:  # normalize_script_text rejects non-str; never propagate
        return None
    if not normalized.strip():
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def verify_manifest_integrity_strict(
    manifest: CompiledStoryboardManifest,
    *,
    hmac_key: bytes | str | None = None,
    expected_source_sha256: str | None = None,
    trusted_source_text: str | bytes | None = None,
    expected_max_saccade_limit: float | None = None,
    expected_initial_axis_side: int | None = None,
) -> bool:
    """Production/release gate: :func:`verify_manifest_integrity` with the anchor mandatory.

    ``verify_manifest_integrity`` has to stay callable without a key -- it is the
    format-level checker, and a manifest that is merely *internally* consistent is a
    legitimate intermediate state (the compiler's own tests rely on that). The
    consequence is that its default is ``require_trusted_anchor=False``, and a
    caller that forgets to pass an anchor silently accepts a fully self-consistent
    forgery: an attacker who writes a different screenplay, recompiles it honestly
    and recomputes all four digests produces something keyless verification blesses.

    This wrapper exists so the decision to trust cannot be made by omission. It is
    fail-closed by construction: ``require_trusted_anchor=True`` is hard-wired, so
    it refuses unless one of the three anchor routes is supplied:

    * ``hmac_key`` -- the manifest was sealed with a deployment secret;
    * ``trusted_source_text`` -- **the strongest route for source pinning**: the
      caller supplies the screenplay bytes it agreed to compile, and *this function*
      hashes them (after the parser's own normalization) and compares that digest
      with the manifest. Nothing about the manifest is read before the comparison,
      so the manifest cannot supply its own answer;
    * ``expected_source_sha256`` -- a digest the caller holds from elsewhere.

    ``expected_source_sha256`` is the one route the function cannot audit: a caller
    that lifts it straight out of ``manifest.script.source_sha256`` turns the anchor
    into a self-attestation and has re-created exactly the hole this wrapper closes
    (the value always matches, whatever the manifest claims). Prefer
    ``trusted_source_text`` when the screenplay is the thing being pinned, or
    ``hmac_key`` when a secret is available. Passing both a text and a digest is
    allowed only when they agree; a disagreement is a refusal, never a tie-break.
    """
    digest: str | None = None
    if trusted_source_text is not None:
        digest = _trusted_source_digest(trusted_source_text)
        if digest is None:
            return False
        if expected_source_sha256 is not None and not hmac.compare_digest(
            digest, expected_source_sha256
        ):
            return False
    return verify_manifest_integrity(
        manifest,
        hmac_key=hmac_key,
        expected_source_sha256=digest if digest is not None else expected_source_sha256,
        expected_max_saccade_limit=expected_max_saccade_limit,
        expected_initial_axis_side=expected_initial_axis_side,
        require_trusted_anchor=True,
    )


def compile_storyboard(
    script_text: str,
    *,
    config: StoryboardCompilerConfig | None = None,
    hmac_key: bytes | str | None = None,
) -> CompiledStoryboardManifest:
    """Compile a Persian/English screenplay into a verified, SHA-256-sealed storyboard manifest.

    ``hmac_key`` optionally adds the external HMAC trust anchor to the sealed manifest (see
    :func:`build_compiled_manifest`); it is a deployment secret and is never stored in the manifest.
    """
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
    return build_compiled_manifest(parsed, cinema_plans, editorial, hmac_key=hmac_key)
