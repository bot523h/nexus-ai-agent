"""Metric landmark identity consistency and shot/reverse-shot geometry checks."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from .contracts import KernelError
from .geometry import undistort_normalized_point

Array = NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class IdentityVerificationReceipt:
    scope: str
    rotation_so3: tuple[tuple[float, float, float], ...]
    translation_m: tuple[float, float, float]
    scale: float
    rms_residual_mm: float
    mahalanobis_distance: float
    identity_locked: bool


def verify_pose_invariant_identity(
    canonical_m: Array,
    observed_px: Array,
    intrinsics: Array,
    depth_m: float,
    *,
    distortion: tuple[float, float, float, float, float] = (0, 0, 0, 0, 0),
    sigma_mm: float = 2.0,
    tolerance_mm: float = 5.0,
) -> IdentityVerificationReceipt:
    """Back-project image landmarks at fixed depth and fit a proper rotation/scale."""
    p, q, k = (
        np.asarray(canonical_m, float),
        np.asarray(observed_px, float),
        np.asarray(intrinsics, float),
    )
    if p.ndim != 2 or p.shape[1] != 3 or len(p) < 4 or q.shape != (len(p), 2) or k.shape != (3, 3):
        raise KernelError("identity requires N>=4 3D and matching 2D landmarks, plus 3x3 K")
    if depth_m <= 0 or sigma_mm <= 0 or not np.all(np.isfinite(p)) or not np.all(np.isfinite(q)):
        raise KernelError("invalid depth, uncertainty, or landmark values")
    if min(k[0, 0], k[1, 1]) <= 0:
        raise KernelError("camera focal lengths must be positive")
    normalized = np.array(
        [
            undistort_normalized_point(
                (u - k[0, 2]) / k[0, 0], (v - k[1, 2]) / k[1, 1], distortion
            )[:2]
            for u, v in q
        ]
    )
    observed = np.column_stack((normalized * depth_m, np.full(len(p), depth_m)))
    pc, qc = p.mean(axis=0), observed.mean(axis=0)
    a, b = p - pc, observed - qc
    u, singular, vt = np.linalg.svd(a.T @ b)
    fix = np.eye(3)
    fix[-1, -1] = np.linalg.det(u @ vt)
    rotation = u @ fix @ vt
    denom = float(np.sum(a * a))
    scale = float(np.sum(singular * np.diag(fix)) / denom) if denom > 1e-20 else 0.0
    aligned = scale * a @ rotation + qc
    residual = (aligned - observed).reshape(-1)
    rms = float(np.sqrt(np.mean(residual**2)) * 1000)
    mahal = float(np.linalg.norm(residual * 1000 / sigma_mm))
    return IdentityVerificationReceipt(
        "GEOMETRIC_CRANIOFACIAL_INVARIANCE",
        tuple(map(tuple, rotation.tolist())),
        tuple(qc.tolist()),
        scale,
        rms,
        mahal,
        rms <= tolerance_mm,
    )


def verify_eyeline_and_180_rule(
    camera_a: Array,
    camera_b: Array,
    actor_a: Array,
    actor_b: Array,
    gaze_a: Array,
    gaze_b: Array,
    *,
    angular_tolerance_deg: float = 3.5,
) -> bool:
    """Check camera side-of-axis signs and each actor's gaze toward the other."""
    ca, cb, aa, ab = (np.asarray(x, float) for x in (camera_a, camera_b, actor_a, actor_b))
    ga, gb = np.asarray(gaze_a, float), np.asarray(gaze_b, float)
    if (
        any(x.shape != (3,) for x in (ca, cb, aa, ab, ga, gb))
        or not 0 < angular_tolerance_deg <= 180
    ):
        raise KernelError("invalid shot/reverse-shot vectors or tolerance")
    axis = ab - aa
    side_a = float(np.dot(np.cross(axis, ca - aa), np.cross(axis, cb - aa)))
    if side_a < 0:
        return False

    def angle(x: Array, y: Array) -> float:
        den = float(np.linalg.norm(x) * np.linalg.norm(y))
        return math.degrees(math.acos(float(np.clip(np.dot(x, y) / den, -1, 1)))) if den else 180.0

    return (
        angle(ga, ab - aa) <= angular_tolerance_deg and angle(gb, aa - ab) <= angular_tolerance_deg
    )
