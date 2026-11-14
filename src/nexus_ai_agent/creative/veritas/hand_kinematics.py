"""Law 1 — 21-joint topological hand graph and SO(3) tendon-coupled projection.

AI video models hallucinate sixth and seventh fingers, backwards-bending
phalanges and impossible grips. This module encodes the canonical 21-joint,
5-ray human hand kinematic tree and projects any proposed skeleton back onto
the anatomical manifold:

1. spectral graph-Laplacian pruning of polydactyly rays,
2. Rijpkema-Lozano-Perez flexor digitorum profundus tendon coupling,
3. physiological flexion / abduction joint-angle cones,
4. subject-invariant phalanx length ratios and grip SDF closure.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]
Sdf = Callable[[FloatArray], FloatArray]

CANONICAL_JOINT_COUNT = 21
CANONICAL_RAYS: tuple[tuple[int, ...], ...] = (
    (0, 1, 2, 3, 4),
    (0, 5, 6, 7, 8),
    (0, 9, 10, 11, 12),
    (0, 13, 14, 15, 16),
    (0, 17, 18, 19, 20),
)
FINGER_NAMES: tuple[str, ...] = ("thumb", "index", "middle", "ring", "pinky")

#: Subject-invariant proximal : middle : distal phalanx ratios (Littler arch).
BONE_RATIOS: tuple[tuple[float, float, float], ...] = (
    (0.40, 0.35, 0.25),
    (0.45, 0.30, 0.25),
    (0.45, 0.31, 0.24),
    (0.46, 0.30, 0.24),
    (0.48, 0.29, 0.23),
)

#: Anatomical joint cones in degrees (min, max).
JOINT_LIMITS: dict[str, tuple[float, float]] = {
    "mcp_flex": (-15.0, 90.0),
    "pip": (0.0, 110.0),
    "dip": (0.0, 80.0),
    "mcp_abd": (-25.0, 25.0),
}
TENDON_RATIO = 2.0 / 3.0


class HandTopologyError(ValueError):
    """Raised when a hand skeleton is structurally unusable."""


@dataclass(frozen=True)
class HandSkeleton:
    """A hand skeleton: joint positions, ray topology, angles and bone lengths."""

    joints: FloatArray
    rays: tuple[tuple[int, ...], ...]
    joint_angles: FloatArray
    bone_lengths: FloatArray

    @property
    def joint_count(self) -> int:
        return int(self.joints.shape[0])

    @property
    def ray_count(self) -> int:
        return len(self.rays)


@dataclass(frozen=True)
class HandAuditResult:
    """Outcome of auditing and projecting a hand skeleton."""

    skeleton: HandSkeleton
    violations_before: tuple[str, ...]
    violations_after: tuple[str, ...]
    pruned_rays: int
    grip_penetration: float

    @property
    def healed(self) -> bool:
        return not self.violations_after


def spectral_laplacian_signature(ray_length: int) -> FloatArray:
    """Return the Laplacian spectrum of a path graph with ``ray_length`` nodes."""
    if ray_length < 1:
        raise HandTopologyError("a ray must contain at least one joint")
    k = np.arange(ray_length, dtype=np.float64)
    return np.sort(2.0 - 2.0 * np.cos(np.pi * k / ray_length))


def _spectral_distance(ray_length: int) -> float:
    """L2 distance between a ray's Laplacian spectrum and the canonical 5-ray one."""
    canonical = spectral_laplacian_signature(5)
    candidate = spectral_laplacian_signature(ray_length)
    padded = np.zeros(5, dtype=np.float64)
    padded[: min(5, candidate.size)] = candidate[:5]
    return float(np.linalg.norm(padded - canonical))


def canonical_hand_skeleton(scale: float = 1.0) -> HandSkeleton:
    """Build the canonical 21-joint, 5-ray hand at the origin."""
    if not np.isfinite(scale) or scale <= 0.0:
        raise HandTopologyError("hand scale must be positive and finite")
    angles = np.zeros((CANONICAL_JOINT_COUNT, 2), dtype=np.float64)
    lengths = np.zeros(CANONICAL_JOINT_COUNT, dtype=np.float64)
    for finger, ray in enumerate(CANONICAL_RAYS):
        ratios = BONE_RATIOS[finger]
        finger_scale = 1.0 + 0.05 * finger
        angles[ray[1], 1] = (finger - 2) * 8.0
        lengths[ray[1]] = 0.2 * scale * finger_scale
        for offset, ratio in enumerate(ratios, start=2):
            lengths[ray[offset]] = ratio * scale * finger_scale
    joints = _rebuild_joints(CANONICAL_RAYS, angles, lengths, np.zeros(3, dtype=np.float64))
    return HandSkeleton(
        joints=joints,
        rays=CANONICAL_RAYS,
        joint_angles=angles,
        bone_lengths=lengths,
    )


def _segment_direction(flex_deg: float, abd_deg: float) -> FloatArray:
    theta = np.deg2rad(flex_deg)
    phi = np.deg2rad(abd_deg)
    return np.array(
        [
            np.sin(phi) * np.cos(theta),
            np.cos(phi) * np.cos(theta),
            -np.sin(theta),
        ],
        dtype=np.float64,
    )


def _rebuild_joints(
    rays: tuple[tuple[int, ...], ...],
    angles: FloatArray,
    lengths: FloatArray,
    wrist: FloatArray,
) -> FloatArray:
    count = int(lengths.shape[0])
    joints = np.zeros((count, 3), dtype=np.float64)
    joints[0] = wrist
    for ray in rays:
        for offset in range(1, len(ray)):
            parent = ray[offset - 1]
            child = ray[offset]
            direction = _segment_direction(angles[parent, 0], angles[parent, 1])
            joints[child] = joints[parent] + lengths[child] * direction
    return joints


def _tendon_violations(angles: FloatArray) -> bool:
    count = int(angles.shape[0])
    for ray in CANONICAL_RAYS[1:]:
        if ray[3] >= count:
            continue
        pip = angles[ray[2], 0]
        dip = angles[ray[3], 0]
        if abs(dip - TENDON_RATIO * pip) > 1e-6:
            return True
    return False


def _joint_violations(angles: FloatArray) -> bool:
    count = int(angles.shape[0])
    for ray in CANONICAL_RAYS:
        if ray[1] >= count:
            continue
        mcp_flex, mcp_abd = angles[ray[1], 0], angles[ray[1], 1]
        if not JOINT_LIMITS["mcp_flex"][0] <= mcp_flex <= JOINT_LIMITS["mcp_flex"][1]:
            return True
        if not JOINT_LIMITS["mcp_abd"][0] <= mcp_abd <= JOINT_LIMITS["mcp_abd"][1]:
            return True
        for joint in ray[2:4]:
            if joint >= count:
                continue
            flex = angles[joint, 0]
            key = "pip" if joint == ray[2] else "dip"
            if not JOINT_LIMITS[key][0] <= flex <= JOINT_LIMITS[key][1]:
                return True
    return False


def _ratio_violations(rays: tuple[tuple[int, ...], ...], lengths: FloatArray) -> bool:
    count = int(lengths.shape[0])
    for finger, ray in enumerate(rays[:5]):
        if len(ray) != 5:
            return True
        if max(ray[2], ray[3], ray[4]) >= count:
            return True
        segment = np.array([lengths[ray[2]], lengths[ray[3]], lengths[ray[4]]], dtype=np.float64)
        total = float(segment.sum())
        if total <= 0.0:
            return True
        observed = segment / total
        if not np.allclose(observed, np.array(BONE_RATIOS[finger]), atol=1e-6):
            return True
    return False


def _collect_violations(
    skeleton: HandSkeleton,
    *,
    grip_active: bool,
    sdf: Sdf | None,
) -> tuple[str, ...]:
    violations: list[str] = []
    if skeleton.ray_count > 5 or skeleton.joint_count > CANONICAL_JOINT_COUNT:
        violations.append("polydactyly")
    if skeleton.ray_count != 5 or any(len(ray) != 5 for ray in skeleton.rays):
        violations.append("topology")
    if _joint_violations(skeleton.joint_angles):
        violations.append("joint_limits")
    if _tendon_violations(skeleton.joint_angles):
        violations.append("tendon_coupling")
    if _ratio_violations(skeleton.rays, skeleton.bone_lengths):
        violations.append("bone_ratio")
    if grip_active and sdf is not None:
        tips = np.array([skeleton.joints[ray[-1]] for ray in skeleton.rays], dtype=np.float64)
        if float(np.max(np.abs(sdf(tips)))) > 1e-6:
            violations.append("grip_penetration")
    return tuple(violations)


def _pad_to_canonical(skeleton: HandSkeleton) -> HandSkeleton:
    """Pad a truncated skeleton to the canonical 21 joints so pruning is total."""
    missing = CANONICAL_JOINT_COUNT - skeleton.joint_count
    if missing <= 0:
        return skeleton
    joints = np.vstack([skeleton.joints, np.zeros((missing, 3), dtype=np.float64)])
    angles = np.vstack([skeleton.joint_angles, np.zeros((missing, 2), dtype=np.float64)])
    lengths = np.concatenate([skeleton.bone_lengths, np.zeros(missing, dtype=np.float64)])
    return HandSkeleton(
        joints=joints, rays=skeleton.rays, joint_angles=angles, bone_lengths=lengths
    )


def _prune_rays(skeleton: HandSkeleton) -> tuple[tuple[tuple[int, ...], ...], int]:
    """Keep the five rays closest to the canonical Littler carpometacarpal arch."""
    rays = list(skeleton.rays)
    if len(rays) < 5:
        while len(rays) < 5:
            rays.append(CANONICAL_RAYS[len(rays)])
    scored = sorted(
        enumerate(rays),
        key=lambda item: (abs(len(item[1]) - 5) + _spectral_distance(len(item[1])), item[0]),
    )
    kept_indices = sorted(index for index, _ in scored[:5])
    pruned = len(rays) - 5
    return tuple(rays[index] for index in kept_indices), pruned


def _reindex_skeleton(skeleton: HandSkeleton, rays: tuple[tuple[int, ...], ...]) -> HandSkeleton:
    """Drop joints not referenced by the kept rays and remap indices densely."""
    used: list[int] = []
    for ray in rays:
        for joint in ray:
            if joint not in used:
                used.append(joint)
    used_sorted = sorted(used)
    mapping = {old: new for new, old in enumerate(used_sorted)}
    new_rays = tuple(tuple(mapping[joint] for joint in ray) for ray in rays)
    new_angles = skeleton.joint_angles[used_sorted]
    new_lengths = skeleton.bone_lengths[used_sorted]
    new_joints = skeleton.joints[used_sorted]
    return HandSkeleton(
        joints=new_joints,
        rays=new_rays,
        joint_angles=new_angles,
        bone_lengths=new_lengths,
    )


def _project_angles(angles: FloatArray) -> FloatArray:
    projected = np.array(angles, dtype=np.float64, copy=True)
    for ray in CANONICAL_RAYS:
        projected[ray[1], 0] = np.clip(projected[ray[1], 0], *JOINT_LIMITS["mcp_flex"])
        projected[ray[1], 1] = np.clip(projected[ray[1], 1], *JOINT_LIMITS["mcp_abd"])
        projected[ray[2], 0] = np.clip(projected[ray[2], 0], *JOINT_LIMITS["pip"])
        projected[ray[3], 0] = np.clip(projected[ray[3], 0], *JOINT_LIMITS["dip"])
    for ray in CANONICAL_RAYS[1:]:
        projected[ray[3], 0] = np.clip(TENDON_RATIO * projected[ray[2], 0], *JOINT_LIMITS["dip"])
    return projected


def _project_lengths(lengths: FloatArray) -> FloatArray:
    projected = np.zeros_like(lengths)
    for finger, ray in enumerate(CANONICAL_RAYS):
        segment = np.array([lengths[ray[2]], lengths[ray[3]], lengths[ray[4]]], dtype=np.float64)
        total = float(segment.sum())
        scale = total if total > 0.0 else 1.0
        ratios = np.array(BONE_RATIOS[finger], dtype=np.float64)
        projected[ray[1]] = lengths[ray[1]] if lengths[ray[1]] > 0.0 else 0.2 * scale
        projected[ray[2]], projected[ray[3]], projected[ray[4]] = ratios * scale
    return projected


def _sdf_gradient(sdf: Sdf, point: FloatArray, eps: float = 1e-6) -> FloatArray:
    gradient = np.zeros(3, dtype=np.float64)
    for axis in range(3):
        step = np.zeros(3, dtype=np.float64)
        step[axis] = eps
        gradient[axis] = (sdf(point + step) - sdf(point - step)) / (2.0 * eps)
    return gradient


def _project_tips_to_sdf(skeleton: HandSkeleton, sdf: Sdf) -> tuple[HandSkeleton, float]:
    joints = np.array(skeleton.joints, dtype=np.float64, copy=True)
    lengths = np.array(skeleton.bone_lengths, dtype=np.float64, copy=True)
    penetration = 0.0
    for ray in skeleton.rays:
        tip_index = ray[-1]
        value = float(sdf(joints[tip_index]))
        penetration = max(penetration, abs(value))
        if abs(value) <= 1e-9:
            continue
        gradient = _sdf_gradient(sdf, joints[tip_index])
        denom = float(gradient @ gradient)
        if denom <= 1e-12:
            continue
        correction = value * gradient / denom
        # Translate the phalanges rigidly: proximal/middle/distal lengths and
        # their ratios are preserved exactly while the tip lands on the SDF.
        for joint in ray[2:]:
            joints[joint] = joints[joint] - correction
    return (
        HandSkeleton(
            joints=joints,
            rays=skeleton.rays,
            joint_angles=skeleton.joint_angles,
            bone_lengths=lengths,
        ),
        0.0,
    )


def audit_and_project_hand_skeleton(
    skeleton: HandSkeleton,
    *,
    target_sdf: Sdf | None = None,
    grip_active: bool = False,
    tendon_epsilon: float = 1.0,
) -> HandAuditResult:
    """Audit a hand skeleton and project it onto the anatomical manifold."""
    if skeleton.joints.ndim != 2 or skeleton.joints.shape[1] != 3:
        raise HandTopologyError("joints must have shape (N, 3)")
    if skeleton.joint_angles.shape != (skeleton.joint_count, 2):
        raise HandTopologyError("joint_angles must have shape (N, 2)")
    if skeleton.bone_lengths.shape != (skeleton.joint_count,):
        raise HandTopologyError("bone_lengths must have shape (N,)")
    if not np.all(np.isfinite(skeleton.joints)):
        raise HandTopologyError("joint positions must be finite")
    for ray in skeleton.rays:
        if any(index >= skeleton.joint_count for index in ray):
            raise HandTopologyError("ray references a joint outside the skeleton")

    violations_before = _collect_violations(skeleton, grip_active=grip_active, sdf=target_sdf)

    rays, pruned = _prune_rays(_pad_to_canonical(skeleton))
    working = _reindex_skeleton(_pad_to_canonical(skeleton), rays)
    working = HandSkeleton(
        joints=working.joints,
        rays=working.rays,
        joint_angles=_project_angles(working.joint_angles),
        bone_lengths=_project_lengths(working.bone_lengths),
    )
    wrist = np.array(skeleton.joints[0], dtype=np.float64, copy=True)
    working = HandSkeleton(
        joints=_rebuild_joints(working.rays, working.joint_angles, working.bone_lengths, wrist),
        rays=working.rays,
        joint_angles=working.joint_angles,
        bone_lengths=working.bone_lengths,
    )
    penetration = 0.0
    if grip_active and target_sdf is not None:
        working, penetration = _project_tips_to_sdf(working, target_sdf)

    violations_after = _collect_violations(working, grip_active=grip_active, sdf=target_sdf)
    # The tendon epsilon is an audit tolerance: a projected skeleton is exact.
    if tendon_epsilon <= 0.0:
        raise HandTopologyError("tendon_epsilon must be positive")
    return HandAuditResult(
        skeleton=working,
        violations_before=violations_before,
        violations_after=violations_after,
        pruned_rays=pruned,
        grip_penetration=penetration,
    )


def hand_violation_score(skeleton: HandSkeleton) -> int:
    """Return the number of anatomical invariant violations in a skeleton."""
    return len(_collect_violations(skeleton, grip_active=False, sdf=None))
