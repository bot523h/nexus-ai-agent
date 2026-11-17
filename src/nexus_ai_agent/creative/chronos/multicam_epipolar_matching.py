"""Calibrated multi-camera epipolar, focus-breathing, and sensor-color continuity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import numpy as np
from numpy.typing import NDArray

from . import color_science, weather_and_grading

FloatArray = NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class EpipolarRigReceipt:
    """Essential/fundamental matrices and the essential singular-value audit."""

    essential_matrix: FloatArray
    fundamental_matrix: FloatArray
    essential_singular_values: tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class CorrespondenceAuditReceipt:
    """Original and first-order corrected image correspondences with Sampson errors."""

    points_a_px: FloatArray
    points_b_px: FloatArray
    repaired_points_a_px: FloatArray
    repaired_points_b_px: FloatArray
    sampson_distance_squared_before: FloatArray
    sampson_distance_squared_after: FloatArray
    correction_iterations: int


@dataclass(frozen=True, slots=True)
class FocusBreathingReceipt:
    frame: FloatArray
    breathing_scale: float
    reference_focus_distance_mm: float
    focus_distance_mm: float


@dataclass(frozen=True, slots=True)
class MultiCameraColorReceipt:
    harmonized_rgb: FloatArray
    sensor_matrix_b_to_a: FloatArray
    sensor_offset_b_to_a: tuple[float, float, float]
    cdl_slope: tuple[float, float, float]
    cdl_offset: tuple[float, float, float]
    cdl_power: tuple[float, float, float]
    cdl_saturation: float
    delta_e00_before_max: float
    delta_e00_after_max: float


def _skew(vector: FloatArray) -> FloatArray:
    x, y, z = vector
    return np.array([[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]], dtype=np.float64)


def _validate_intrinsics(matrix: FloatArray, label: str) -> FloatArray:
    intrinsics = np.asarray(matrix, dtype=np.float64)
    if (
        intrinsics.shape != (3, 3)
        or np.any(~np.isfinite(intrinsics))
        or intrinsics[0, 0] <= 0
        or intrinsics[1, 1] <= 0
        or abs(float(np.linalg.det(intrinsics))) <= 1e-15
    ):
        raise ValueError(
            f"{label} intrinsics must be finite, invertible, and have positive focal lengths"
        )
    return intrinsics


def compute_fundamental_matrix_from_rig(
    intrinsics_a: FloatArray,
    intrinsics_b: FloatArray,
    rotation_ab: FloatArray,
    translation_ab: FloatArray,
) -> EpipolarRigReceipt:
    """Construct ``E=[t]x R`` and ``F=K_B^-T E K_A^-1`` from a calibrated rig."""
    camera_a = _validate_intrinsics(intrinsics_a, "A-camera")
    camera_b = _validate_intrinsics(intrinsics_b, "B-camera")
    rotation = np.asarray(rotation_ab, dtype=np.float64)
    translation = np.asarray(translation_ab, dtype=np.float64)
    if (
        rotation.shape != (3, 3)
        or np.any(~np.isfinite(rotation))
        or not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-9, rtol=0.0)
        or not np.isclose(np.linalg.det(rotation), 1.0, atol=1e-9, rtol=0.0)
        or translation.shape != (3,)
        or np.any(~np.isfinite(translation))
        or float(np.linalg.norm(translation)) <= 1e-12
    ):
        raise ValueError("relative camera pose must be finite SO(3) plus a nonzero translation")
    essential = _skew(translation) @ rotation
    singular_values = np.linalg.svd(essential, compute_uv=False)
    if (
        singular_values[1] <= 1e-12
        or not np.isclose(singular_values[0], singular_values[1], rtol=1e-10, atol=1e-12)
        or singular_values[2] > singular_values[0] * 1e-10
    ):
        raise ValueError("essential matrix failed its rank-2 equal-singular-value invariant")
    fundamental = np.linalg.solve(camera_b.T, essential) @ np.linalg.inv(camera_a)
    if np.any(~np.isfinite(fundamental)):
        raise ValueError("fundamental matrix construction produced non-finite values")
    return EpipolarRigReceipt(
        np.asarray(essential, dtype=np.float64),
        np.asarray(fundamental, dtype=np.float64),
        cast(tuple[float, float, float], tuple(float(value) for value in singular_values)),
    )


def _sampson_terms(
    points_a_px: FloatArray, points_b_px: FloatArray, fundamental: FloatArray
) -> tuple[FloatArray, FloatArray, FloatArray, FloatArray, FloatArray]:
    points_a = np.asarray(points_a_px, dtype=np.float64)
    points_b = np.asarray(points_b_px, dtype=np.float64)
    matrix = np.asarray(fundamental, dtype=np.float64)
    if (
        points_a.ndim != 2
        or points_a.shape[1] != 2
        or points_b.shape != points_a.shape
        or len(points_a) == 0
        or matrix.shape != (3, 3)
        or np.any(~np.isfinite(points_a))
        or np.any(~np.isfinite(points_b))
        or np.any(~np.isfinite(matrix))
        or float(np.linalg.norm(matrix)) <= 1e-15
    ):
        raise ValueError("Sampson audit requires matching finite Nx2 points and a nonzero 3x3 F")
    homogeneous_a = np.column_stack((points_a, np.ones(len(points_a))))
    homogeneous_b = np.column_stack((points_b, np.ones(len(points_b))))
    line_b = homogeneous_a @ matrix.T
    line_a = homogeneous_b @ matrix
    residual = np.einsum("ni,ij,nj->n", homogeneous_b, matrix, homogeneous_a)
    gradient_a = line_a[:, :2]
    gradient_b = line_b[:, :2]
    denominator = np.sum(gradient_a**2, axis=1) + np.sum(gradient_b**2, axis=1)
    if np.any(denominator <= 1e-20):
        raise ValueError("Sampson distance is undefined at an epipolar singularity")
    distance_squared = residual**2 / denominator
    return points_a, points_b, residual, denominator, distance_squared


def sampson_epipolar_distance_squared(
    points_a_px: FloatArray, points_b_px: FloatArray, fundamental_matrix: FloatArray
) -> FloatArray:
    """Return the first-order Sampson epipolar distance in squared pixels."""
    return _sampson_terms(points_a_px, points_b_px, fundamental_matrix)[-1]


def audit_and_project_epipolar_correspondences(
    points_a_px: FloatArray,
    points_b_px: FloatArray,
    fundamental_matrix: FloatArray,
    *,
    tolerance_px2: float = 1e-10,
    max_iterations: int = 12,
) -> CorrespondenceAuditReceipt:
    """Iteratively apply the first-order Sampson correction onto the epipolar manifold."""
    if not np.isfinite(tolerance_px2) or tolerance_px2 <= 0 or max_iterations <= 0:
        raise ValueError("Sampson projection requires a positive tolerance and iteration bound")
    original_a = np.asarray(points_a_px, dtype=np.float64).copy()
    original_b = np.asarray(points_b_px, dtype=np.float64).copy()
    points_a, points_b, _, _, before = _sampson_terms(original_a, original_b, fundamental_matrix)
    repaired_a, repaired_b = points_a.copy(), points_b.copy()
    iterations = 0
    for _ in range(max_iterations):
        _, _, residual, denominator, after = _sampson_terms(
            repaired_a, repaired_b, fundamental_matrix
        )
        if np.all(after < tolerance_px2):
            break
        homogeneous_a = np.column_stack((repaired_a, np.ones(len(repaired_a))))
        homogeneous_b = np.column_stack((repaired_b, np.ones(len(repaired_b))))
        gradient_a = (homogeneous_b @ np.asarray(fundamental_matrix))[:, :2]
        gradient_b = (homogeneous_a @ np.asarray(fundamental_matrix).T)[:, :2]
        correction_scale = residual / denominator
        repaired_a -= correction_scale[:, None] * gradient_a
        repaired_b -= correction_scale[:, None] * gradient_b
        iterations += 1
    after = _sampson_terms(repaired_a, repaired_b, fundamental_matrix)[-1]
    if np.any(after >= tolerance_px2):
        raise ValueError("Sampson projection did not converge to the requested epipolar tolerance")
    return CorrespondenceAuditReceipt(
        points_a,
        points_b,
        repaired_a,
        repaired_b,
        before,
        after,
        iterations,
    )


def _validate_lens_domain(
    focal_length_mm: float, focus_distance_mm: float, reference_distance_mm: float | None = None
) -> None:
    values = (focal_length_mm, focus_distance_mm) + (
        () if reference_distance_mm is None else (reference_distance_mm,)
    )
    if any(not np.isfinite(value) for value in values) or focal_length_mm <= 0:
        raise ValueError("lens focal length and focus distances must be finite and positive")
    if focus_distance_mm <= focal_length_mm or (
        reference_distance_mm is not None and reference_distance_mm <= focal_length_mm
    ):
        raise ValueError("thin-lens focus distances must exceed focal length")


def _breathing_scale(
    focal_length_mm: float, focus_distance_mm: float, reference_distance_mm: float
) -> float:
    _validate_lens_domain(focal_length_mm, focus_distance_mm, reference_distance_mm)
    scale = (
        reference_distance_mm
        * (focus_distance_mm - focal_length_mm)
        / (focus_distance_mm * (reference_distance_mm - focal_length_mm))
    )
    if not np.isfinite(scale) or scale <= 0:
        raise ValueError("thin-lens breathing scale must be finite and positive")
    return float(scale)


def compensate_focus_breathing_frame(
    frame_rgb: FloatArray,
    *,
    focal_length_mm: float,
    reference_focus_distance_mm: float,
    focus_distance_mm: float,
    principal_point_px: tuple[float, float],
) -> FocusBreathingReceipt:
    """Center-locked bilinear warp that removes the thin-lens focus magnification jump."""
    frame = np.asarray(frame_rgb, dtype=np.float64)
    if (
        frame.ndim not in (2, 3)
        or frame.shape[0] == 0
        or frame.shape[1] == 0
        or (frame.ndim == 3 and frame.shape[2] not in (1, 3, 4))
        or np.any(~np.isfinite(frame))
    ):
        raise ValueError("focus-breathing compensation requires a finite image buffer")
    scale = _breathing_scale(focal_length_mm, focus_distance_mm, reference_focus_distance_mm)
    cx, cy = principal_point_px
    height, width = frame.shape[:2]
    if (
        not np.isfinite(cx)
        or not np.isfinite(cy)
        or not 0 <= cx <= width - 1
        or not 0 <= cy <= height - 1
    ):
        raise ValueError("principal point must lie inside the image buffer")
    rows, columns = np.mgrid[0:height, 0:width]
    source_x = np.clip(cx + (columns - cx) / scale, 0.0, width - 1.0)
    source_y = np.clip(cy + (rows - cy) / scale, 0.0, height - 1.0)
    x0, y0 = np.floor(source_x).astype(np.intp), np.floor(source_y).astype(np.intp)
    x1, y1 = np.minimum(x0 + 1, width - 1), np.minimum(y0 + 1, height - 1)
    wx, wy = source_x - x0, source_y - y0
    if frame.ndim == 3:
        wx, wy = wx[..., None], wy[..., None]
    top = frame[y0, x0] * (1.0 - wx) + frame[y0, x1] * wx
    bottom = frame[y1, x0] * (1.0 - wx) + frame[y1, x1] * wx
    rectified = np.asarray(top * (1.0 - wy) + bottom * wy, dtype=np.float64)
    return FocusBreathingReceipt(rectified, scale, reference_focus_distance_mm, focus_distance_mm)


def compute_physical_circle_of_confusion_mm(
    subject_distance_mm: float,
    focus_distance_mm: float,
    focal_length_mm: float,
    t_stop: float,
) -> float:
    """Thin-lens image-plane circle-of-confusion diameter in millimetres."""
    values = (subject_distance_mm, focus_distance_mm, focal_length_mm, t_stop)
    if any(not np.isfinite(value) for value in values):
        raise ValueError("circle-of-confusion inputs must be finite")
    if subject_distance_mm <= focal_length_mm or t_stop <= 0:
        raise ValueError("subject distance must exceed focal length and T-stop must be positive")
    _validate_lens_domain(focal_length_mm, focus_distance_mm)
    return float(
        abs(subject_distance_mm - focus_distance_mm)
        / subject_distance_mm
        * focal_length_mm**2
        / (t_stop * (focus_distance_mm - focal_length_mm))
    )


def _chart_delta_e00(source: FloatArray, target: FloatArray) -> FloatArray:
    source_lab = color_science.rgb_to_lab(color_science.srgb_encode(np.clip(source, 0.0, 1.0)))
    target_lab = color_science.rgb_to_lab(color_science.srgb_encode(np.clip(target, 0.0, 1.0)))
    return color_science.delta_e_2000(source_lab, target_lab)


def harmonize_multicam_sensor_color(
    source_sensor_chart_rgb: FloatArray,
    reference_chart_rgb: FloatArray,
    source_frame_rgb: FloatArray,
    *,
    ridge: float = 1e-8,
) -> MultiCameraColorReceipt:
    """Ridge-fit B→A linear sensor matrix, then fit/apply ASC-CDL channel trims."""
    source = np.asarray(source_sensor_chart_rgb, dtype=np.float64)
    reference = np.asarray(reference_chart_rgb, dtype=np.float64)
    frame = np.asarray(source_frame_rgb, dtype=np.float64)
    if (
        source.ndim != 2
        or source.shape[1] != 3
        or reference.shape != source.shape
        or len(source) < 4
        or frame.ndim not in (3, 4)
        or frame.shape[-1] != 3
        or np.any(~np.isfinite(source))
        or np.any(~np.isfinite(reference))
        or np.any(~np.isfinite(frame))
        or any(np.any((array < 0) | (array > 1)) for array in (source, reference, frame))
        or not np.isfinite(ridge)
        or ridge <= 0
    ):
        raise ValueError("sensor color calibration requires finite [0,1] RGB chart/frame buffers")
    if np.linalg.matrix_rank(source, tol=1e-10) < 3:
        raise ValueError("sensor chart must span three independent color channels")
    affine_design = np.column_stack((source, np.ones(len(source))))
    if np.linalg.matrix_rank(affine_design, tol=1e-10) < 4:
        raise ValueError("sensor chart must span three colors plus an independent affine offset")
    regularizer = np.diag((ridge, ridge, ridge, 0.0))
    affine_coefficients = np.linalg.solve(
        affine_design.T @ affine_design + regularizer,
        affine_design.T @ reference,
    )
    sensor_matrix = affine_coefficients[:3]
    sensor_offset = affine_coefficients[3]
    calibrated_chart = source @ sensor_matrix + sensor_offset
    design = np.column_stack((calibrated_chart, np.ones(len(source))))
    cdl_coefficients = np.linalg.lstsq(design, reference, rcond=None)[0]
    slope = np.maximum(cdl_coefficients[:3].T.diagonal(), 1e-8)
    offset = cdl_coefficients[3]
    if np.any(~np.isfinite(slope)) or np.any(~np.isfinite(offset)):
        raise ValueError("fitted ASC-CDL coefficients are not finite")
    mapped_chart = weather_and_grading.apply_asc_cdl_and_3d_lut(
        calibrated_chart,
        slope=tuple(float(v) for v in slope),
        offset=tuple(float(v) for v in offset),
    )
    mapped_frame = weather_and_grading.apply_asc_cdl_and_3d_lut(
        frame @ sensor_matrix + sensor_offset,
        slope=tuple(float(v) for v in slope),
        offset=tuple(float(v) for v in offset),
    )
    before = _chart_delta_e00(source, reference)
    after = _chart_delta_e00(mapped_chart, reference)
    return MultiCameraColorReceipt(
        np.asarray(mapped_frame, dtype=np.float64),
        np.asarray(sensor_matrix, dtype=np.float64),
        cast(tuple[float, float, float], tuple(float(value) for value in sensor_offset)),
        cast(tuple[float, float, float], tuple(float(value) for value in slope)),
        cast(tuple[float, float, float], tuple(float(value) for value in offset)),
        (1.0, 1.0, 1.0),
        1.0,
        float(np.max(before)),
        float(np.max(after)),
    )
