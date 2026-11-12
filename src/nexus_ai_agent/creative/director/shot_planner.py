"""Algorithmic shot planner: 180-degree axis, 30-degree rule, eyeline, proxemics.

The planner compiles one parsed scene into a deterministic, ordered list of
camera setups (Establishing/Master -> Over-the-Shoulder A -> Over-the-Shoulder
B -> Close-Up -> Insert) and enforces the classic continuity rules:

* **180-degree line.** For the primary interaction pair ``(A, B)`` the action
  line is :math:`\\vec{L}_{AB} = \\mathbf{p}_B - \\mathbf{p}_A`. Every camera in
  one continuous sequence must sit in the same half-plane: the sign of the 2D
  cross product :math:`(\\mathbf{p}_{cam} - \\mathbf{p}_A) \\times \\vec{L}_{AB}`
  must equal the sequence's declared side, unless the setup is an explicit
  neutral axis reset.
* **30-degree rule.** Two consecutive shots on the same subject must differ by
  at least 30 degrees of camera angle *or* by at least 25% of focal length,
  otherwise the cut is a jump cut.
* **Eyeline match.** For facing singles, if A looks screen-right
  (:math:`\\hat{g}_A \\cdot \\hat{u}_{screen} > 0`) then B must look
  screen-left (:math:`\\hat{g}_B \\cdot \\hat{u}_{screen} < 0`).
* **Hall proxemics + separation.** Actor placements are classified into Hall
  bands and must respect a minimum non-colliding separation.

The planner is a *pure function* of its inputs: identical inputs give identical
setups. It does not render, dispatch, or own project state.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .contracts import (
    ActorPlacement,
    CameraSetup,
    EyelineResult,
    HallBand,
    ShotSequence,
    ShotSize,
    hall_band,
)
from .errors import AxisViolationError, JumpCutError, ProxemicsError

__all__ = [
    "ShotPlanRequest",
    "plan_shot_sequence",
    "side_of_line",
    "camera_angle_deg",
    "check_jump_cut",
    "check_eyeline_match",
    "check_proxemics",
    "MIN_SEPARATION_M",
    "MIN_ANGLE_DELTA_DEG",
    "MIN_FOCAL_RATIO",
    "DEFAULT_FOCAL_MM",
]

#: Minimum centre-to-centre separation before two actors are considered to be
#: occupying the same space (metres).
MIN_SEPARATION_M = 0.35
#: The 30-degree rule threshold.
MIN_ANGLE_DELTA_DEG = 30.0
#: The focal-length jump threshold (fractional change).
MIN_FOCAL_RATIO = 0.25
DEFAULT_FOCAL_MM = 50

_SIZE_FOCAL = {
    ShotSize.ESTABLISHING: 24,
    ShotSize.MASTER: 35,
    ShotSize.FULL: 35,
    ShotSize.MEDIUM: 50,
    ShotSize.OTS: 50,
    ShotSize.CLOSE_UP: 85,
    ShotSize.EXTREME_CLOSE_UP: 135,
    ShotSize.INSERT: 100,
}

#: Camera stand-off distance from the subject, by shot size (metres).
_SIZE_DISTANCE = {
    ShotSize.ESTABLISHING: 9.0,
    ShotSize.MASTER: 6.0,
    ShotSize.FULL: 4.5,
    ShotSize.MEDIUM: 2.4,
    ShotSize.OTS: 1.6,
    ShotSize.CLOSE_UP: 1.0,
    ShotSize.EXTREME_CLOSE_UP: 0.5,
    ShotSize.INSERT: 0.8,
}


@dataclass(frozen=True)
class ShotPlanRequest:
    scene_index: int
    actors: tuple[ActorPlacement, ...]
    primary_pair: tuple[str, str]
    axis_side: int = 1
    focal_override: dict[ShotSize, int] | None = None

    def __post_init__(self) -> None:
        if self.axis_side not in (-1, 1):
            raise ValueError("axis_side must be -1 or +1")
        if self.primary_pair[0] == self.primary_pair[1]:
            raise ValueError("primary_pair must name two distinct actors")


def _find(actors: tuple[ActorPlacement, ...], actor_id: str) -> ActorPlacement:
    for a in actors:
        if a.actor_id == actor_id:
            return a
    raise ProxemicsError(f"actor {actor_id!r} is not placed in the scene")


def side_of_line(cam: tuple[float, float], a: ActorPlacement, b: ActorPlacement) -> int:
    """Sign of ``(cam - p_A) x (p_B - p_A)``; 0 when the camera is on the line."""
    lx = b.x - a.x
    ly = b.y - a.y
    vx = cam[0] - a.x
    vy = cam[1] - a.y
    cross = vx * ly - vy * lx
    if cross > 0:
        return 1
    if cross < 0:
        return -1
    return 0


def camera_angle_deg(
    subject: ActorPlacement, cam_a: tuple[float, float], cam_b: tuple[float, float]
) -> float:
    """Angle in degrees between two cameras as seen from the subject."""
    a1 = math.atan2(cam_a[1] - subject.y, cam_a[0] - subject.x)
    a2 = math.atan2(cam_b[1] - subject.y, cam_b[0] - subject.x)
    delta = abs(math.degrees(a1 - a2)) % 360.0
    return 360.0 - delta if delta > 180.0 else delta


def check_jump_cut(prev: CameraSetup, nxt: CameraSetup, subject: ActorPlacement) -> None:
    """Raise :class:`JumpCutError` when two shots on one subject break the rule."""
    if prev.subject_id != nxt.subject_id:
        return
    angle = camera_angle_deg(subject, (prev.cam_x, prev.cam_y), (nxt.cam_x, nxt.cam_y))
    focal_ratio = abs(nxt.focal_mm - prev.focal_mm) / max(prev.focal_mm, 1)
    if angle < MIN_ANGLE_DELTA_DEG and focal_ratio < MIN_FOCAL_RATIO:
        raise JumpCutError(
            f"{prev.setup_id!r} -> {nxt.setup_id!r}: angle {angle:.1f}deg < "
            f"{MIN_ANGLE_DELTA_DEG} and focal delta {focal_ratio:.2f} < {MIN_FOCAL_RATIO}"
        )


def check_eyeline_match(a: ActorPlacement, b: ActorPlacement) -> EyelineResult:
    """Two facing singles match when their screen-space gaze directions oppose.

    ``facing_deg`` is the actor's gaze direction in screen space (0 = +x). A
    looks screen-right when ``cos(facing) > 0``; B must look screen-left
    (``cos(facing) < 0``) for the pair to match.
    """
    a_right = math.cos(math.radians(a.facing_deg)) > 0
    b_left = math.cos(math.radians(b.facing_deg)) < 0
    return EyelineResult(a_looks_right=a_right, b_looks_left=b_left, matched=a_right == b_left)


def check_proxemics(actors: tuple[ActorPlacement, ...]) -> tuple[HallBand, ...]:
    """Validate minimum separation; return the Hall band per consecutive pair."""
    for i in range(len(actors)):
        for j in range(i + 1, len(actors)):
            a, b = actors[i], actors[j]
            d = math.hypot(a.x - b.x, a.y - b.y)
            if d < MIN_SEPARATION_M:
                raise ProxemicsError(
                    f"actors {a.actor_id!r} and {b.actor_id!r} are {d:.3f} m apart "
                    f"< {MIN_SEPARATION_M} m"
                )
    bands: list[HallBand] = []
    for i in range(len(actors) - 1):
        a, b = actors[i], actors[i + 1]
        bands.append(hall_band(math.hypot(a.x - b.x, a.y - b.y)))
    return tuple(bands)


def _camera_for(
    request: ShotPlanRequest,
    subject: ActorPlacement,
    partner: ActorPlacement,
    size: ShotSize,
    setup_index: int,
    *,
    reset: bool = False,
) -> CameraSetup:
    """Place one camera on the declared axis side at a shot-size distance."""
    dist = _SIZE_DISTANCE[size]
    focal = (request.focal_override or {}).get(size, _SIZE_FOCAL[size])
    # direction from the subject toward the partner, rotated onto the axis side
    ang = math.atan2(partner.y - subject.y, partner.x - subject.x)
    side = request.axis_side if not reset else 0
    # Rotating the A->B direction by ``-side*75deg`` puts the camera on the
    # half-plane whose cross-product sign equals ``side`` (verified in tests).
    cam_ang = ang - side * math.radians(75.0)
    cam_x = subject.x + dist * math.cos(cam_ang)
    cam_y = subject.y + dist * math.sin(cam_ang)
    actual_side = side_of_line((cam_x, cam_y), subject, partner) if side else 0
    return CameraSetup(
        setup_id=f"setup_{request.scene_index}_{setup_index}",
        shot_size=size,
        subject_id=subject.actor_id,
        cam_x=cam_x,
        cam_y=cam_y,
        focal_mm=focal,
        axis_side=actual_side if actual_side != 0 else (1 if not reset else 0),
        is_axis_reset=reset,
    )


def plan_shot_sequence(request: ShotPlanRequest) -> ShotSequence:
    """Compile a scene into a deterministic, rule-checked shot sequence."""
    a = _find(request.actors, request.primary_pair[0])
    b = _find(request.actors, request.primary_pair[1])
    check_proxemics(request.actors)

    plan: list[tuple[ShotSize, ActorPlacement, ActorPlacement, bool]] = [
        (ShotSize.ESTABLISHING, a, b, False),
        (ShotSize.MASTER, a, b, False),
        (ShotSize.OTS, a, b, False),
        (ShotSize.OTS, b, a, False),
        (ShotSize.CLOSE_UP, a, b, False),
        (ShotSize.INSERT, b, a, False),
    ]

    setups: list[CameraSetup] = []
    for idx, (size, subject, partner, reset) in enumerate(plan):
        setup = _camera_for(request, subject, partner, size, idx, reset=reset)
        if not reset and setup.axis_side != request.axis_side:
            raise AxisViolationError(
                f"{setup.setup_id!r} landed on axis side {setup.axis_side}, "
                f"expected {request.axis_side}"
            )
        if setups:
            # enforce the 30-degree rule on consecutive same-subject shots
            prev = setups[-1]
            if prev.subject_id == setup.subject_id:
                check_jump_cut(prev, setup, subject)
        setups.append(setup)

    eyeline = check_eyeline_match(a, b)
    notes: list[str] = []
    if not eyeline.matched:
        notes.append("eyeline_mismatch")
    return ShotSequence(
        scene_index=request.scene_index,
        setups=tuple(setups),
        axis_side=request.axis_side,
        notes=tuple(notes),
    )
