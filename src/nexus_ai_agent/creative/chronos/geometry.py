"""Metric pinhole/thin-lens camera geometry (SI world units, mm optics)."""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from .contracts import KernelError

FloatArray = NDArray[np.float64]


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
