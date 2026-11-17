"""Metric landmark identity consistency and shot/reverse-shot geometry checks."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import cast

import numpy as np
from numpy.typing import NDArray

from .contracts import KernelError
from .geometry import undistort_normalized_point

Array = NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class EyelineVerificationReceipt:
    accepted: bool
    gaze_ray_miss_distance_m: float
    eyeline_angle_error_deg: float
    skew_ray_closest_distance_m: float = 0.0

    def __bool__(self) -> bool:
        return self.accepted


@dataclass(frozen=True, slots=True)
class IdentityVerificationReceipt:
    scope: str
    rotation_so3: tuple[tuple[float, float, float], ...]
    translation_m: tuple[float, float, float]
    scale: float
    rms_residual_mm: float
    mahalanobis_distance: float
    identity_locked: bool
    downweighted_landmark_count: int = 0
    world_space_scale_m: float = 0.0
    image_space_span_px: float = 0.0


@dataclass(frozen=True, slots=True)
class CranialBaselineReceipt:
    reference_baseline_m: float
    observed_baseline_m: float
    relative_drift: float
    within_tolerance: bool


@dataclass(frozen=True, slots=True)
class CranialIdentityRestorationReceipt:
    restored_landmarks_m: Array
    rotation_so3: Array
    fitted_scale: float
    scale_drift_before: float
    scale_drift_after: float
    downweighted_landmark_count: int
    max_fit_residual_m: float


def restore_cranial_landmarks_to_anchor(
    canonical_landmarks_m: Array,
    observed_landmarks_m: Array,
    *,
    left_index: int = 0,
    right_index: int = 1,
    huber_delta_m: float = 0.005,
    iterations: int = 8,
) -> CranialIdentityRestorationReceipt:
    """Robust Umeyama/Kabsch fit and inverse-map 3D landmarks to a metric anchor.

    Grossly corrupted landmarks are identified by their final Huber influence and
    snapped to the canonical anchor after the global similarity correction.
    """
    reference = np.asarray(canonical_landmarks_m, dtype=np.float64)
    observed = np.asarray(observed_landmarks_m, dtype=np.float64)
    if (
        reference.ndim != 2
        or reference.shape[1] != 3
        or len(reference) < 4
        or observed.shape != reference.shape
        or not 0 <= left_index < len(reference)
        or not 0 <= right_index < len(reference)
        or left_index == right_index
        or not np.isfinite(huber_delta_m)
        or huber_delta_m <= 0
        or iterations < 1
        or np.any(~np.isfinite(reference))
        or np.any(~np.isfinite(observed))
    ):
        raise KernelError("invalid 3D identity anchor or restoration controls")
    reference_baseline = float(np.linalg.norm(reference[left_index] - reference[right_index]))
    if reference_baseline <= 1e-12:
        raise KernelError("canonical cranial baseline must be nonzero")
    observed_baseline = float(np.linalg.norm(observed[left_index] - observed[right_index]))
    scale_drift_before = abs(observed_baseline - reference_baseline) / reference_baseline
    weights = np.ones(len(reference), dtype=np.float64)
    rotation = np.eye(3, dtype=np.float64)
    scale = 1.0
    translation = np.zeros(3, dtype=np.float64)
    residual_lengths = np.zeros(len(reference), dtype=np.float64)
    for _ in range(iterations):
        total_weight = float(weights.sum())
        reference_center = np.sum(reference * weights[:, None], axis=0) / total_weight
        observed_center = np.sum(observed * weights[:, None], axis=0) / total_weight
        reference_zero = reference - reference_center
        observed_zero = observed - observed_center
        covariance = (reference_zero * weights[:, None]).T @ observed_zero
        try:
            u, singular, vt = np.linalg.svd(covariance)
        except np.linalg.LinAlgError as exc:
            raise KernelError("3D identity SVD failed") from exc
        correction = np.eye(3, dtype=np.float64)
        correction[-1, -1] = 1.0 if np.linalg.det(u @ vt) >= 0 else -1.0
        rotation = u @ correction @ vt
        denominator = float(np.sum(weights[:, None] * reference_zero**2))
        if denominator <= 1e-20:
            raise KernelError("canonical landmarks have degenerate scale")
        scale = float(np.sum(singular * np.diag(correction)) / denominator)
        if not np.isfinite(scale) or scale <= 0:
            raise KernelError("3D identity fit produced a non-positive scale")
        translation = observed_center - scale * reference_center @ rotation
        fitted = scale * reference @ rotation + translation
        residual_lengths = np.linalg.norm(observed - fitted, axis=1)
        weights = np.minimum(1.0, huber_delta_m / np.maximum(residual_lengths, 1e-12))

    outliers = residual_lengths > max(4.0 * huber_delta_m, 1e-8)
    if np.any(outliers):
        weights[outliers] = 0.0
        total_weight = float(weights.sum())
        reference_center = np.sum(reference * weights[:, None], axis=0) / total_weight
        observed_center = np.sum(observed * weights[:, None], axis=0) / total_weight
        reference_zero = reference - reference_center
        observed_zero = observed - observed_center
        covariance = (reference_zero * weights[:, None]).T @ observed_zero
        try:
            u, singular, vt = np.linalg.svd(covariance)
        except np.linalg.LinAlgError as exc:
            raise KernelError("3D identity inlier refit failed") from exc
        correction = np.eye(3, dtype=np.float64)
        correction[-1, -1] = 1.0 if np.linalg.det(u @ vt) >= 0 else -1.0
        rotation = u @ correction @ vt
        denominator = float(np.sum(weights[:, None] * reference_zero**2))
        if denominator <= 1e-20:
            raise KernelError("too few non-outlier landmarks for identity refit")
        scale = float(np.sum(singular * np.diag(correction)) / denominator)
        translation = observed_center - scale * reference_center @ rotation
        residual_lengths = np.linalg.norm(
            observed - (scale * reference @ rotation + translation), axis=1
        )

    restored = (observed - translation) @ rotation.T / scale
    outliers = residual_lengths > max(4.0 * huber_delta_m, 1e-8)
    restored[outliers] = reference[outliers]
    restored_baseline = float(np.linalg.norm(restored[left_index] - restored[right_index]))
    scale_drift_after = abs(restored_baseline - reference_baseline) / reference_baseline
    return CranialIdentityRestorationReceipt(
        restored,
        rotation,
        scale,
        scale_drift_before,
        scale_drift_after,
        int(np.count_nonzero(outliers)),
        float(np.max(residual_lengths)),
    )


def verify_cranial_baseline(
    reference_landmarks_m: Array,
    observed_landmarks_m: Array,
    *,
    left_index: int,
    right_index: int,
    relative_tolerance: float = 0.05,
) -> CranialBaselineReceipt:
    """Measure an explicitly indexed cranial/inter-pupillary baseline in metres."""
    reference = np.asarray(reference_landmarks_m, dtype=np.float64)
    observed = np.asarray(observed_landmarks_m, dtype=np.float64)
    if (
        reference.ndim != 2
        or reference.shape[1] != 3
        or observed.shape != reference.shape
        or not 0 <= left_index < len(reference)
        or not 0 <= right_index < len(reference)
        or left_index == right_index
        or not np.isfinite(relative_tolerance)
        or relative_tolerance < 0
        or np.any(~np.isfinite(reference))
        or np.any(~np.isfinite(observed))
    ):
        raise KernelError("invalid cranial baseline landmarks, indices, or tolerance")
    reference_m = float(np.linalg.norm(reference[left_index] - reference[right_index]))
    observed_m = float(np.linalg.norm(observed[left_index] - observed[right_index]))
    if reference_m <= 1e-12:
        raise KernelError("reference cranial baseline must be nonzero")
    drift = abs(observed_m - reference_m) / reference_m
    return CranialBaselineReceipt(reference_m, observed_m, drift, drift <= relative_tolerance)


def _brown_conrady_jacobian(
    x: float, y: float, distortion: tuple[float, float, float, float, float]
) -> Array:
    """Analytical Jacobian of the Brown-Conrady forward distortion map."""
    k1, k2, k3, p1, p2 = distortion
    r2 = x * x + y * y
    radial = 1 + k1 * r2 + k2 * r2**2 + k3 * r2**3
    radial_slope = k1 + 2 * k2 * r2 + 3 * k3 * r2**2
    dr_dx, dr_dy = 2 * x * radial_slope, 2 * y * radial_slope
    return np.array(
        [
            [radial + x * dr_dx + 2 * p1 * y + 6 * p2 * x, x * dr_dy + 2 * p1 * x + 2 * p2 * y],
            [y * dr_dx + 2 * p1 * x + 2 * p2 * y, radial + y * dr_dy + 6 * p1 * y + 2 * p2 * x],
        ],
        dtype=np.float64,
    )


def verify_pose_invariant_identity(
    canonical_m: Array,
    observed_px: Array,
    intrinsics: Array,
    depth_m: float,
    *,
    distortion: tuple[float, float, float, float, float] = (0, 0, 0, 0, 0),
    sigma_mm: float = 2.0,
    tolerance_mm: float = 5.0,
    huber_delta_mm: float = 3.0,
    pixel_sigma_px: float = 0.5,
    depth_sigma_m: float = 0.01,
) -> IdentityVerificationReceipt:
    """Back-project image landmarks at fixed depth and fit a proper rotation/scale."""
    p, q, k = (
        np.asarray(canonical_m, float),
        np.asarray(observed_px, float),
        np.asarray(intrinsics, float),
    )
    if p.ndim != 2 or p.shape[1] != 3 or len(p) < 4 or q.shape != (len(p), 2) or k.shape != (3, 3):
        raise KernelError("identity requires N>=4 3D and matching 2D landmarks, plus 3x3 K")
    if (
        depth_m <= 0
        or sigma_mm <= 0
        or huber_delta_mm <= 0
        or pixel_sigma_px < 0
        or depth_sigma_m < 0
        or not np.all(np.isfinite(p))
        or not np.all(np.isfinite(q))
        or not np.all(np.isfinite(k))
    ):
        raise KernelError("invalid depth, uncertainty, or landmark values")
    camera_2x2 = k[:2, :2]
    if min(k[0, 0], k[1, 1]) <= 0 or abs(float(np.linalg.det(camera_2x2))) <= 1e-15:
        raise KernelError("camera focal lengths and pixel transform must be invertible")
    try:
        pixel_to_distorted = np.linalg.inv(camera_2x2)
    except np.linalg.LinAlgError as exc:
        raise KernelError("camera pixel transform must be invertible") from exc
    normalized_points: list[tuple[float, float]] = []
    jacobians: list[Array] = []
    for u, v in q:
        distorted_xy = pixel_to_distorted @ np.array([u - k[0, 2], v - k[1, 2]])
        x, y, _ = undistort_normalized_point(
            float(distorted_xy[0]), float(distorted_xy[1]), distortion
        )
        normalized_points.append((x, y))
        try:
            undistortion_jacobian = np.linalg.inv(_brown_conrady_jacobian(x, y, distortion))
        except np.linalg.LinAlgError as exc:
            raise KernelError("singular Brown-Conrady covariance Jacobian") from exc
        normalized_pixel_jacobian = undistortion_jacobian @ pixel_to_distorted
        jacobian = np.zeros((3, 3), dtype=np.float64)
        jacobian[:2, :2] = depth_m * normalized_pixel_jacobian
        jacobian[:2, 2] = (x, y)
        jacobian[2, 2] = 1.0
        jacobians.append(jacobian)
    normalized = np.asarray(normalized_points, dtype=np.float64)
    observed = np.column_stack((normalized * depth_m, np.full(len(p), depth_m)))
    weights = np.ones(len(p), dtype=np.float64)
    rotation = np.eye(3)
    scale = 1.0
    translation = np.zeros(3)
    for _ in range(6):
        total = float(weights.sum())
        pc = np.sum(p * weights[:, None], axis=0) / total
        qc = np.sum(observed * weights[:, None], axis=0) / total
        a, b = p - pc, observed - qc
        u, singular, vt = np.linalg.svd((a * weights[:, None]).T @ b)
        fix = np.eye(3)
        fix[-1, -1] = np.linalg.det(u @ vt)
        rotation = u @ fix @ vt
        denom = float(np.sum(weights[:, None] * a * a))
        scale = float(np.sum(singular * np.diag(fix)) / denom) if denom > 1e-20 else 0.0
        translation = qc - scale * pc @ rotation
        residual_points = scale * p @ rotation + translation - observed
        residual_mm = np.linalg.norm(residual_points, axis=1) * 1000
        weights = np.minimum(1.0, huber_delta_mm / np.maximum(residual_mm, 1e-12))
    rms = float(np.sqrt(np.mean(residual_points**2)) * 1000)
    # GUM propagation: each landmark has its own 3x3 covariance because the
    # pixel-to-ray Jacobian depends on its normalized image coordinates.
    input_covariance = np.diag([pixel_sigma_px**2, pixel_sigma_px**2, depth_sigma_m**2])
    squared_mahalanobis: list[float] = []
    for residual, jacobian in zip(residual_points, jacobians, strict=True):
        covariance = jacobian @ input_covariance @ jacobian.T
        try:
            whitened = np.linalg.solve(covariance, residual)
        except np.linalg.LinAlgError:
            # Zero-valued caller uncertainty is permitted for legacy inputs;
            # use the Moore-Penrose inverse on the observable subspace.
            whitened = np.linalg.pinv(covariance) @ residual
        squared_mahalanobis.append(max(0.0, float(residual @ whitened)))
    mahal = float(np.sqrt(np.mean(squared_mahalanobis)))
    span_px = float(np.linalg.norm(q.max(axis=0) - q.min(axis=0)))
    return IdentityVerificationReceipt(
        "GEOMETRIC_CRANIOFACIAL_INVARIANCE",
        cast(
            tuple[tuple[float, float, float], ...],
            tuple(tuple(float(v) for v in row) for row in rotation.tolist()),
        ),
        cast(tuple[float, float, float], tuple(translation.tolist())),
        scale,
        rms,
        mahal,
        rms <= tolerance_mm,
        int(np.count_nonzero(weights < 0.999)),
        float(np.ptp(p, axis=0).max()),
        span_px,
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
    head_radius_m: float = 0.12,
) -> EyelineVerificationReceipt:
    """Check camera side-of-axis signs and each actor's gaze toward the other."""
    ca, cb, aa, ab = (np.asarray(x, float) for x in (camera_a, camera_b, actor_a, actor_b))
    ga, gb = np.asarray(gaze_a, float), np.asarray(gaze_b, float)
    if (
        any(x.shape != (3,) for x in (ca, cb, aa, ab, ga, gb))
        or not 0 < angular_tolerance_deg <= 180
        or head_radius_m <= 0
    ):
        raise KernelError("invalid shot/reverse-shot vectors or tolerance")
    axis = ab - aa

    # Closest approach of the two 3D gaze lines. In the parallel limit the
    # cross-product formula is indeterminate, so use point-to-line distance.
    direction_cross = np.cross(ga, gb)
    cross_norm = float(np.linalg.norm(direction_cross))
    origin_delta = ab - aa
    norm_a, norm_b = float(np.linalg.norm(ga)), float(np.linalg.norm(gb))
    if cross_norm > 1e-12:
        skew_distance = abs(float(np.dot(origin_delta, direction_cross))) / cross_norm
    elif norm_a > 1e-12:
        skew_distance = float(np.linalg.norm(np.cross(origin_delta, ga)) / norm_a)
    elif norm_b > 1e-12:
        skew_distance = float(np.linalg.norm(np.cross(origin_delta, gb)) / norm_b)
    else:
        skew_distance = float(np.linalg.norm(origin_delta))

    side_a = float(np.dot(np.cross(axis, ca - aa), np.cross(axis, cb - aa)))
    if side_a < 0:
        return EyelineVerificationReceipt(False, float("inf"), 180.0, skew_distance)

    def angle(x: Array, y: Array) -> float:
        den = float(np.linalg.norm(x) * np.linalg.norm(y))
        return math.degrees(math.acos(float(np.clip(np.dot(x, y) / den, -1, 1)))) if den else 180.0

    def miss(origin: Array, direction: Array, target: Array) -> float:
        norm = float(np.linalg.norm(direction))
        if norm <= 1e-12:
            return float("inf")
        unit = direction / norm
        along = max(0.0, float(np.dot(target - origin, unit)))
        return float(np.linalg.norm(origin + along * unit - target))

    miss_distance = max(miss(aa, ga, ab), miss(ab, gb, aa))
    angle_error = max(angle(ga, ab - aa), angle(gb, aa - ab))
    accepted = angle_error <= angular_tolerance_deg and miss_distance <= head_radius_m
    return EyelineVerificationReceipt(accepted, miss_distance, angle_error, skew_distance)
