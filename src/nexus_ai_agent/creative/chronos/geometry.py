"""Metric pinhole/thin-lens camera geometry (SI world units, mm optics)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import numpy as np
from numpy.typing import NDArray

from .contracts import KernelError

FloatArray = NDArray[np.float64]


def rotation_matrix_from_axis_angle(axis: FloatArray, angle_rad: float) -> FloatArray:
    """Construct a proper SO(3) rotation with Rodrigues' formula."""
    vector = np.asarray(axis, dtype=np.float64)
    if vector.shape != (3,) or not np.all(np.isfinite(vector)) or not np.isfinite(angle_rad):
        raise KernelError("axis-angle rotation requires a finite 3-vector and angle")
    magnitude = float(np.linalg.norm(vector))
    if magnitude <= 1e-15:
        raise KernelError("axis-angle rotation requires a nonzero axis")
    x, y, z = vector / magnitude
    skew = np.array([[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]], dtype=np.float64)
    return cast(
        FloatArray,
        np.eye(3) + np.sin(angle_rad) * skew + (1.0 - np.cos(angle_rad)) * (skew @ skew),
    )


def intrinsics(
    f_mm: float, width_px: int, height_px: int, sx_mm: float, sy_mm: float
) -> FloatArray:
    if min(f_mm, width_px, height_px, sx_mm, sy_mm) <= 0:
        raise KernelError("camera dimensions and focal length must be positive")
    return np.array(
        [
            [f_mm * width_px / sx_mm, 0.0, width_px / 2],
            [0.0, f_mm * height_px / sy_mm, height_px / 2],
            [0.0, 0.0, 1.0],
        ]
    )


def project(
    points: FloatArray,
    rotation: FloatArray,
    translation: FloatArray,
    k: FloatArray,
    distortion: tuple[float, float, float, float, float] = (0, 0, 0, 0, 0),
) -> FloatArray:
    if (
        rotation.shape != (3, 3)
        or not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-8)
        or not np.isclose(np.linalg.det(rotation), 1.0, atol=1e-8)
    ):
        raise KernelError("rotation must belong to SO(3)")
    camera = (rotation @ points.T).T + translation
    if np.any(camera[:, 2] <= 0):
        raise KernelError("points behind camera")
    x, y = camera[:, 0] / camera[:, 2], camera[:, 1] / camera[:, 2]
    k1, k2, k3, p1, p2 = distortion
    r2 = x * x + y * y
    radial = 1 + k1 * r2 + k2 * r2 * r2 + k3 * r2 * r2 * r2
    xd = x * radial + 2 * p1 * x * y + p2 * (r2 + 2 * x * x)
    yd = y * radial + p1 * (r2 + 2 * y * y) + 2 * p2 * x * y
    pixels = (k @ np.stack((xd, yd, np.ones_like(x)), axis=0)).T
    return pixels[:, :2] / pixels[:, 2, None]


def coc_diameter_px(
    f_mm: float, f_number: float, focus_mm: float, subject_mm: float, sensor_pitch_mm: float
) -> float:
    if (
        min(f_mm, f_number, focus_mm, subject_mm, sensor_pitch_mm) <= 0
        or focus_mm <= f_mm
        or subject_mm <= f_mm
    ):
        raise KernelError("invalid thin-lens domain")
    # Sensor-plane blur circle, converted to pixels.
    image_focus = f_mm * focus_mm / (focus_mm - f_mm)
    image_subject = f_mm * subject_mm / (subject_mm - f_mm)
    return abs(f_mm / f_number * (image_focus - image_subject) / image_subject) / sensor_pitch_mm


def projected_height_jacobian(f_px: float, height_m: float, depth_m: float) -> float:
    if depth_m <= 0:
        raise KernelError("depth must be positive")
    return -f_px * height_m / depth_m**2


@dataclass(frozen=True, slots=True)
class WorldScaleReceipt:
    world_space_contract: str
    image_space_contract: str
    world_height_m: float
    projected_height_px: float
    gum_uncertainty_m: float
    undistortion_residual: float


def undistort_normalized_point(
    distorted_x: float,
    distorted_y: float,
    distortion: tuple[float, float, float, float, float] = (0, 0, 0, 0, 0),
    tolerance: float = 1e-10,
    max_iterations: int = 100,
) -> tuple[float, float, float]:
    """Invert Brown-Conrady distortion by Newton iterations; return x,y,residual."""
    target = np.array([distorted_x, distorted_y], dtype=np.float64)
    if not np.all(np.isfinite(target)) or tolerance <= 0 or max_iterations <= 0:
        raise KernelError("invalid undistortion inputs")

    def distort(point: FloatArray) -> FloatArray:
        x, y = point
        k1, k2, k3, p1, p2 = distortion
        r2 = x * x + y * y
        radial = 1 + k1 * r2 + k2 * r2 * r2 + k3 * r2 * r2 * r2
        return np.array(
            [
                x * radial + 2 * p1 * x * y + p2 * (r2 + 2 * x * x),
                y * radial + p1 * (r2 + 2 * y * y) + 2 * p2 * x * y,
            ]
        )

    estimate = target.copy()
    for _ in range(max_iterations):
        residual_vector = distort(estimate) - target
        residual = float(np.linalg.norm(residual_vector))
        if residual < tolerance:
            return float(estimate[0]), float(estimate[1]), residual
        step = 1e-6
        jacobian = np.column_stack(
            [
                (distort(estimate + np.eye(2)[i] * step) - distort(estimate - np.eye(2)[i] * step))
                / (2 * step)
                for i in range(2)
            ]
        )
        try:
            estimate -= np.linalg.solve(jacobian, residual_vector)
        except np.linalg.LinAlgError as exc:
            raise KernelError("singular lens-distortion inversion") from exc
        if not np.all(np.isfinite(estimate)):
            raise KernelError("undistortion diverged")
    raise KernelError("undistortion did not converge to requested residual")


def backproject_character_height_m(
    top_px: tuple[float, float],
    bottom_px: tuple[float, float],
    depth_m: float,
    k: FloatArray,
    distortion: tuple[float, float, float, float, float] = (0, 0, 0, 0, 0),
    pixel_uncertainty: float = 0.5,
) -> WorldScaleReceipt:
    """Recover metric vertical character height from calibrated image endpoints."""
    if depth_m <= 0 or k.shape != (3, 3) or pixel_uncertainty < 0:
        raise KernelError("invalid metric back-projection inputs")
    inv_k = np.linalg.solve(k, np.eye(3))
    recovered: list[tuple[float, float, float]] = []
    for u, v in (top_px, bottom_px):
        normalized = inv_k @ np.array([u, v, 1.0])
        if abs(normalized[2]) < 1e-12:
            raise KernelError("singular pixel ray")
        x_d, y_d = normalized[0] / normalized[2], normalized[1] / normalized[2]
        x, y, residual = undistort_normalized_point(x_d, y_d, distortion)
        recovered.append((x, y, residual))
    height = abs(recovered[0][1] - recovered[1][1]) * depth_m
    fy = float(k[1, 1])
    uncertainty = abs(depth_m / fy) * np.sqrt(2) * pixel_uncertainty
    projected = float(np.linalg.norm(np.asarray(top_px) - np.asarray(bottom_px)))
    return WorldScaleReceipt(
        "WORLD_SPACE_CHARACTER_SCALE",
        "IMAGE_SPACE_PROJECTED_SCALE",
        float(height),
        projected,
        float(uncertainty),
        max(recovered[0][2], recovered[1][2]),
    )


def circle_of_confusion_px(
    depth_m: float,
    focus_distance_m: float,
    focal_length_mm: float,
    f_number: float,
    sensor_width_mm: float,
    image_width_px: int,
) -> float:
    """Thin-lens defocus disk diameter on sensor, returned in pixels."""
    if (
        min(depth_m, focus_distance_m, focal_length_mm, f_number, sensor_width_mm, image_width_px)
        <= 0
    ):
        raise KernelError("thin-lens parameters must be positive")
    f = focal_length_mm / 1000.0
    coc_sensor_m = (
        f * f / f_number * abs(depth_m - focus_distance_m) / (depth_m * (focus_distance_m - f))
    )
    return coc_sensor_m * 1000 / sensor_width_mm * image_width_px
