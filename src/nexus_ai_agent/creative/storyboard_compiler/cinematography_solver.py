"""Mathematical cinematography solver: SE(3) poses, 180-degree rule, and minimum-jerk motion.

Computes physically valid, deterministic 3D camera blocking and motion profiles
for every shot of a parsed screenplay with zero external API calls:

* **SE(3) Camera Poses**: Orthogonal rotation matrices ``R in SO(3)`` satisfying
  ``R @ R.T == I`` and ``det(R) == +1``, paired with world-space translations
  ``t in R^3`` in right-handed world coordinates (``+X`` right, ``+Y`` depth,
  ``+Z`` up; camera optical axis ``+z_c``, right ``+x_c``, down ``+y_c``).
* **180-Degree Axis of Action Law**: Maintains invariant sign of the horizontal
  cross product ``((cam_pos - p1) x (p2 - p1))_z`` across an entire dialogue
  scene (including every sample of moving camera trajectories) unless an
  explicit intentional axis jump is requested.
* **Optical & Compositional Selection**: Deterministic prime lens choice from
  ``(24, 35, 50, 85, 135)`` mm, cinema ``f_stop`` with thin-lens depth-of-field,
  and closed-form Rule-of-Thirds pinhole framing.
* **Quintic Minimum-Jerk Trajectories**: Continuous ``DOLLY``, ``PAN``, ``CRANE``,
  and ``STATIC`` trajectories parameterized by ``s(tau) = 10*tau^3 - 15*tau^4 + 6*tau^5``
  on ``SO(3) x R^3`` with identically zero linear and angular velocity and
  acceleration at ``tau = 0`` and ``tau = 1``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from enum import Enum
from typing import Any, Final, Self

import numpy as np
import numpy.typing as npt

from .bilingual_script_parser import (
    ActionBeat,
    DialogueLine,
    EmotionTag,
    ParsedScene,
    ParsedScript,
    SceneSetting,
    TimeOfDay,
    WeatherCondition,
)

FloatArray = npt.NDArray[np.float64]
Vec3Tuple = tuple[float, float, float]
Mat3Tuple = tuple[Vec3Tuple, Vec3Tuple, Vec3Tuple]

__all__ = [
    "se3_exp",
    "se3_log",
    "verify_minimum_jerk_calculus_invariants",
    "AxisEnforcementTrace",
    "AxisOfAction",
    "AxisVerificationReport",
    "CANONICAL_F_STOPS",
    "CANONICAL_FOCAL_LENGTHS_MM",
    "CIRCLE_OF_CONFUSION_MM",
    "CameraMoveType",
    "CameraPoseSE3",
    "CameraShotSpec",
    "CinematographyError",
    "LensSettings",
    "MinimumJerkTrajectory",
    "RuleOfThirdsFraming",
    "RuleOfThirdsPoint",
    "SENSOR_HEIGHT_MM",
    "SENSOR_WIDTH_MM",
    "SceneCinematographyPlan",
    "ScreenLookDirection",
    "ShotCameraPlan",
    "ShotKind",
    "ShotScale",
    "TrajectorySample",
    "axis_side_sign",
    "build_look_at_pose",
    "build_rule_of_thirds_pose",
    "canonical_axis_of_action",
    "compute_canonical_character_positions",
    "enforce_axis_of_action",
    "enforce_axis_of_action_with_trace",
    "evaluate_trajectory_at",
    "expected_shot_move_type",
    "expected_shot_subject",
    "generate_minimum_jerk_trajectory",
    "horizontal_axis_cross",
    "is_valid_se3",
    "minimum_jerk_profile",
    "minimum_jerk_scalar",
    "project_world_point",
    "select_lens_and_aperture",
    "select_rule_of_thirds",
    "skew_symmetric",
    "so3_exp",
    "so3_log",
    "solve_scene_cinematography",
    "solve_script_cinematography",
    "verify_180_degree_rule",
    "verify_scene_character_blocking",
]

#: Canonical prime focal lengths (mm) on a Full-Frame 36x20.25 mm 16:9 cinema gate.
CANONICAL_FOCAL_LENGTHS_MM: Final[tuple[int, ...]] = (24, 35, 50, 85, 135)
#: Standard one-stop cinema T/f-stop series.
CANONICAL_F_STOPS: Final[tuple[float, ...]] = (1.4, 2.0, 2.8, 4.0, 5.6, 8.0, 11.0)
#: Full-Frame 16:9 sensor dimensions and standard circle of confusion (mm).
SENSOR_WIDTH_MM: Final[float] = 36.0
SENSOR_HEIGHT_MM: Final[float] = 20.25
CIRCLE_OF_CONFUSION_MM: Final[float] = 0.029

#: Canonical subject distance (m) paired with each focal length to deliver natural framing.
_LENS_NOMINAL_DISTANCE_M: Final[dict[int, float]] = {
    24: 4.20,
    35: 3.10,
    50: 2.35,
    85: 1.95,
    135: 2.25,
}


class CinematographyError(ValueError):
    """Raised when camera geometry, SE(3) invariants, or motion bounds are violated."""


class ShotScale(str, Enum):
    """Canonical cinematic shot size tied to focal length and subject distance."""

    WIDE = "WIDE"
    MEDIUM_WIDE = "MEDIUM_WIDE"
    MEDIUM = "MEDIUM"
    CLOSE_UP = "CLOSE_UP"
    EXTREME_CLOSE_UP = "EXTREME_CLOSE_UP"


class ShotKind(str, Enum):
    """Narrative origin of a storyboard shot."""

    DIALOGUE = "DIALOGUE"
    ACTION = "ACTION"


class CameraMoveType(str, Enum):
    """Continuous camera rigging motion class."""

    STATIC = "STATIC"
    DOLLY = "DOLLY"
    PAN = "PAN"
    CRANE = "CRANE"


class RuleOfThirdsPoint(str, Enum):
    """Four canonical Rule-of-Thirds grid intersections."""

    UPPER_LEFT = "UPPER_LEFT"
    UPPER_RIGHT = "UPPER_RIGHT"
    LOWER_LEFT = "LOWER_LEFT"
    LOWER_RIGHT = "LOWER_RIGHT"


class ScreenLookDirection(str, Enum):
    """Horizontal direction of a subject's eye-line in screen space."""

    LEFT = "LEFT"
    RIGHT = "RIGHT"
    CENTER = "CENTER"


_THIRDS_UV: Final[dict[RuleOfThirdsPoint, tuple[float, float]]] = {
    RuleOfThirdsPoint.UPPER_LEFT: (1.0 / 3.0, 1.0 / 3.0),
    RuleOfThirdsPoint.UPPER_RIGHT: (2.0 / 3.0, 1.0 / 3.0),
    RuleOfThirdsPoint.LOWER_LEFT: (1.0 / 3.0, 2.0 / 3.0),
    RuleOfThirdsPoint.LOWER_RIGHT: (2.0 / 3.0, 2.0 / 3.0),
}

_LENS_TO_SCALE: Final[dict[int, ShotScale]] = {
    24: ShotScale.WIDE,
    35: ShotScale.MEDIUM_WIDE,
    50: ShotScale.MEDIUM,
    85: ShotScale.CLOSE_UP,
    135: ShotScale.EXTREME_CLOSE_UP,
}


# --------------------------------------------------------------------------- #
# Vector / Lie Group SO(3) & SE(3) Primitives
# --------------------------------------------------------------------------- #


def _require_finite_scalar(value: object, *, name: str) -> float:
    """Reject ``bool``, non-numeric, ``NaN`` and ``±Inf`` scalars fail-closed.

    ``bool`` is rejected explicitly because ``isinstance(True, int)`` holds in
    Python: without the check a boolean would silently pass as ``1.0``. NaN and
    infinities raise :class:`CinematographyError` (a ``ValueError``).
    """
    if isinstance(value, (bool, np.bool_)):
        raise TypeError(f"{name} must be a finite number, got bool {value!r}")
    if not isinstance(value, (int, float, np.integer, np.floating)):
        raise TypeError(f"{name} must be a finite number, got {type(value).__name__}")
    result = float(value)
    if not math.isfinite(result):
        raise CinematographyError(f"{name} must be finite, got {value!r}")
    return result


def _require_finite_array(values: object, *, name: str) -> FloatArray:
    """Reject ``bool`` elements and non-finite entries in an array fail-closed."""
    raw = values if isinstance(values, np.ndarray) else np.asarray(values, dtype=object)
    if raw.dtype.kind == "b" or (
        raw.dtype.kind == "O" and any(isinstance(item, (bool, np.bool_)) for item in raw.flat)
    ):
        raise TypeError(f"{name} must be a finite numeric array, got bool element")
    arr = np.asarray(values, dtype=np.float64)
    if not np.all(np.isfinite(arr)):
        raise CinematographyError(f"{name} must be finite, got non-finite entries")
    return arr


def _as_vec3(vec: Sequence[float] | FloatArray, *, name: str) -> FloatArray:
    arr = _require_finite_array(vec, name=name)
    if arr.shape != (3,):
        raise CinematographyError(f"{name} must be a finite 3-vector, got shape {arr.shape}")
    return arr


def _to_vec3_tuple(arr: FloatArray) -> Vec3Tuple:
    return (float(arr[0]), float(arr[1]), float(arr[2]))


def _to_mat3_tuple(mat: FloatArray) -> Mat3Tuple:
    return (
        (float(mat[0, 0]), float(mat[0, 1]), float(mat[0, 2])),
        (float(mat[1, 0]), float(mat[1, 1]), float(mat[1, 2])),
        (float(mat[2, 0]), float(mat[2, 1]), float(mat[2, 2])),
    )


def skew_symmetric(omega: Sequence[float] | FloatArray) -> FloatArray:
    """Return the 3x3 skew-symmetric matrix ``[omega]_x in so(3)``."""
    w = _as_vec3(omega, name="omega")
    return np.array(
        [
            [0.0, -w[2], w[1]],
            [w[2], 0.0, -w[0]],
            [-w[1], w[0], 0.0],
        ],
        dtype=np.float64,
    )


def so3_exp(omega: Sequence[float] | FloatArray) -> FloatArray:
    """Rodrigues exponential map ``exp: so(3) -> SO(3)``."""
    w = _as_vec3(omega, name="omega")
    theta = float(np.linalg.norm(w))
    k = skew_symmetric(w)
    eye = np.eye(3, dtype=np.float64)
    if theta < 1e-8:
        c1 = 1.0 - (theta * theta) / 6.0
        c2 = 0.5 - (theta * theta) / 24.0
    else:
        c1 = math.sin(theta) / theta
        c2 = (1.0 - math.cos(theta)) / (theta * theta)
    result: FloatArray = eye + c1 * k + c2 * (k @ k)
    return result


def _validate_so3(rotation: Sequence[Sequence[float]] | FloatArray, *, tol: float) -> FloatArray:
    _require_finite_scalar(tol, name="tol")
    if not 0 < tol <= 1e-5:
        raise CinematographyError("tol must lie in (0, 1e-5]")
    rot = _require_finite_array(rotation, name="rotation")
    if rot.shape != (3, 3):
        raise CinematographyError(
            "Matrix is not a valid SO(3) rotation: expected a finite 3x3 matrix, "
            f"got shape {rot.shape}"
        )
    ortho_err = float(np.linalg.norm(rot.T @ rot - np.eye(3, dtype=np.float64), ord="fro"))
    if ortho_err > tol:
        raise CinematographyError(
            "Matrix is not a valid SO(3) rotation: orthogonality violated "
            f"||R^T @ R - I||_F = {ortho_err:.3e} > {tol:.3e}"
        )
    det = float(np.linalg.det(rot))
    if abs(det - 1.0) > tol:
        raise CinematographyError(
            f"Matrix is not a valid SO(3) rotation: determinant det(R) = {det:.12f} (expected +1.0)"
        )
    return rot


def so3_log(rotation: Sequence[Sequence[float]] | FloatArray, *, tol: float = 1e-5) -> FloatArray:
    """Inverse Rodrigues logarithm map ``log: SO(3) -> so(3)`` returning a 3-vector.

    The input must be a proper rotation: shape ``(3, 3)``, all entries finite,
    ``||R^T @ R - I||_F <= 1e-5`` and ``|det(R) - 1| <= 1e-5``, otherwise
    ``ValueError("Matrix is not a valid SO(3) rotation")`` is raised. The
    antipodal singularity at ``theta -> pi`` (where ``R - R^T -> 0`` and the
    classical Rodrigues formula divides by ``sin(theta) -> 0``) is resolved by
    extracting the rotation axis from the exact rank-1 symmetric tensor
    ``S = 0.5*(R + R^T) - cos(theta)*I = (1 - cos(theta)) * u u^T`` (which at
    ``theta = pi`` equals ``2 * B`` with ``B = 0.5*(R + I) = u u^T``), so the
    round trip ``||so3_exp(so3_log(R)) - R||_F < 1e-6`` holds on all of SO(3).
    """
    rot = _validate_so3(rotation, tol=tol)
    vee = np.array(
        [rot[2, 1] - rot[1, 2], rot[0, 2] - rot[2, 0], rot[1, 0] - rot[0, 1]],
        dtype=np.float64,
    )
    sin_theta = 0.5 * float(np.linalg.norm(vee))
    cos_theta = float(np.clip(0.5 * (float(np.trace(rot)) - 1.0), -1.0, 1.0))
    theta = math.atan2(sin_theta, cos_theta)
    if theta < 1e-7:
        # Identity neighbourhood: Taylor coefficient of theta/(2 sin theta).
        return (0.5 + theta * theta / 12.0 + 7.0 * theta**4 / 720.0) * vee
    if cos_theta < -0.99:
        # Antipodal region (covers pi - theta < 1e-4 and beyond): vee(R - R^T)
        # vanishes with sin(theta), so the axis is read from the exact symmetric
        # tensor S = (1 - cos(theta)) * u u^T instead of the skew part.
        sym = 0.5 * (rot + rot.T) - cos_theta * np.eye(3, dtype=np.float64)
        col_norms = np.linalg.norm(sym, axis=0)
        best = int(np.argmax(col_norms))
        axis: FloatArray = sym[:, best] / float(col_norms[best])
        if sin_theta > 1e-12 and float(np.dot(axis, vee)) < 0.0:
            axis = -axis
        return theta * axis
    scale = theta / (2.0 * sin_theta)
    return scale * vee


def se3_exp(twist: Sequence[float] | FloatArray) -> FloatArray:
    """Exponential of a twist ordered as (translation velocity, rotation vector)."""
    xi = _require_finite_array(twist, name="twist")
    if xi.shape != (6,):
        raise CinematographyError("twist must have shape (6,)")
    omega = xi[3:]
    theta = float(np.linalg.norm(omega))
    k = skew_symmetric(omega)
    if theta < 1e-4:
        a = 0.5 - theta**2 / 24.0 + theta**4 / 720.0
        b = 1.0 / 6.0 - theta**2 / 120.0 + theta**4 / 5040.0
    else:
        a = (1.0 - math.cos(theta)) / theta**2
        b = (theta - math.sin(theta)) / theta**3
    v = np.eye(3) + a * k + b * (k @ k)
    result = np.eye(4, dtype=np.float64)
    result[:3, :3] = so3_exp(omega)
    result[:3, 3] = v @ xi[:3]
    return result


def se3_log(transform: Sequence[Sequence[float]] | FloatArray) -> FloatArray:
    """Logarithm of SE(3), regular at identity and at a half-turn."""
    t = _require_finite_array(transform, name="transform")
    if t.shape != (4, 4) or not np.allclose(t[3], [0, 0, 0, 1], atol=1e-10, rtol=0):
        raise CinematographyError("transform must be a valid SE(3) matrix")
    omega = so3_log(t[:3, :3])
    theta = float(np.linalg.norm(omega))
    k = skew_symmetric(omega)
    if theta < 1e-4:
        coefficient = 1.0 / 12.0 + theta**2 / 720.0 + theta**4 / 30240.0
    else:
        # Half-angle form stays regular at pi, unlike division by sin(theta).
        coefficient = 1.0 / theta**2 - 1.0 / (2.0 * theta * math.tan(theta / 2.0))
    inverse_v = np.eye(3) - 0.5 * k + coefficient * (k @ k)
    return np.concatenate((inverse_v @ t[:3, 3], omega))


def is_valid_se3(matrix: Sequence[Sequence[float]] | FloatArray, *, tol: float = 1e-9) -> bool:
    """Return True iff ``matrix`` is a valid 4x4 homogeneous transform in ``SE(3)``."""
    arr = np.asarray(matrix, dtype=np.float64)
    if arr.shape != (4, 4) or not np.all(np.isfinite(arr)):
        return False
    bottom_err = float(np.max(np.abs(arr[3, :] - np.array([0.0, 0.0, 0.0, 1.0]))))
    if bottom_err > tol:
        return False
    rot = arr[:3, :3]
    ortho_err = float(np.linalg.norm(rot @ rot.T - np.eye(3, dtype=np.float64), ord="fro"))
    det_err = abs(float(np.linalg.det(rot)) - 1.0)
    return ortho_err <= tol and det_err <= tol


@dataclass(frozen=True)
class CameraPoseSE3:
    """Immutable 3D camera-to-world pose ``(R, t) in SE(3)``."""

    rotation: Mat3Tuple
    translation: Vec3Tuple

    def __post_init__(self) -> None:
        rotation = _require_finite_array(self.rotation, name="rotation")
        if rotation.shape != (3, 3):
            raise CinematographyError("rotation must have shape (3, 3)")
        _as_vec3(self.translation, name="translation")

    def validate_strict(self) -> Self:
        """Validate SO(3) membership when promoting an audit record to geometry."""
        _validate_so3(self.rotation, tol=1e-5)
        _as_vec3(self.translation, name="translation")
        return self

    @classmethod
    def from_rt(
        cls,
        rotation: Sequence[Sequence[float]] | FloatArray,
        translation: Sequence[float] | FloatArray,
        *,
        tol: float = 1e-9,
    ) -> CameraPoseSE3:
        """Construct and validate a ``CameraPoseSE3`` from ``(R, t)``."""
        rot = _validate_so3(rotation, tol=tol)
        trans = _as_vec3(translation, name="translation")
        return cls(rotation=_to_mat3_tuple(rot), translation=_to_vec3_tuple(trans))

    @property
    def rotation_matrix(self) -> FloatArray:
        """3x3 proper orthogonal rotation matrix ``R`` (columns: right, down, forward)."""
        return np.array(self.rotation, dtype=np.float64)

    @property
    def translation_vector(self) -> FloatArray:
        """World-space camera optical-center coordinates ``t`` (meters)."""
        return np.array(self.translation, dtype=np.float64)

    @property
    def matrix(self) -> FloatArray:
        """4x4 homogeneous camera-to-world transform ``T_wc in SE(3)``."""
        mat = np.eye(4, dtype=np.float64)
        mat[:3, :3] = self.rotation_matrix
        mat[:3, 3] = self.translation_vector
        return mat

    @property
    def forward_axis(self) -> FloatArray:
        """World-space unit optical axis ``+z_c``."""
        return self.rotation_matrix[:, 2]

    def inverse(self) -> CameraPoseSE3:
        """Exact Lie-group inverse ``T_cw = (R^T, -R^T t) in SE(3)``."""
        r_t = self.rotation_matrix.T
        t_inv = -(r_t @ self.translation_vector)
        return CameraPoseSE3.from_rt(r_t, t_inv)

    def compose(self, other: CameraPoseSE3) -> CameraPoseSE3:
        """Exact Lie-group product ``self @ other in SE(3)``."""
        r1 = self.rotation_matrix
        r_prod = r1 @ other.rotation_matrix
        t_prod = r1 @ other.translation_vector + self.translation_vector
        return CameraPoseSE3.from_rt(r_prod, t_prod)


def _apply_optical_roll(rotation: FloatArray, roll_rad: float) -> FloatArray:
    _require_finite_scalar(roll_rad, name="roll_rad")
    if not math.isfinite(roll_rad):
        raise CinematographyError(f"roll_rad must be finite, got {roll_rad!r}")
    if abs(roll_rad) == 0.0:
        return rotation
    c = math.cos(roll_rad)
    s = math.sin(roll_rad)
    roll_mat = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]], dtype=np.float64)
    out: FloatArray = rotation @ roll_mat
    return out


def build_look_at_pose(
    camera_pos: Sequence[float] | FloatArray,
    target_pos: Sequence[float] | FloatArray,
    *,
    world_up: Sequence[float] | FloatArray = (0.0, 0.0, 1.0),
    roll_rad: float = 0.0,
) -> CameraPoseSE3:
    """Build a right-handed ``SE(3)`` pose looking from ``camera_pos`` at ``target_pos``."""
    cam = _as_vec3(camera_pos, name="camera_pos")
    tgt = _as_vec3(target_pos, name="target_pos")
    up = _as_vec3(world_up, name="world_up")

    up_norm = float(np.linalg.norm(up))
    if up_norm < 1e-9:
        raise CinematographyError("world_up must be non-zero")
    up_unit = up / up_norm

    delta = tgt - cam
    dist = float(np.linalg.norm(delta))
    if dist < 1e-6:
        raise CinematographyError("camera_pos and target_pos must be distinct")
    z_cam = delta / dist

    right_raw = np.cross(z_cam, up_unit)
    right_norm = float(np.linalg.norm(right_raw))
    if right_norm < 1e-6:
        # Degenerate vertical look (zenith/nadir): choose canonical horizontal fallback.
        fallback = (
            np.array([0.0, 1.0, 0.0], dtype=np.float64)
            if abs(float(z_cam[1])) < 0.9
            else np.array([1.0, 0.0, 0.0], dtype=np.float64)
        )
        right_raw = np.cross(z_cam, fallback)
        right_norm = float(np.linalg.norm(right_raw))
    x_cam = right_raw / right_norm
    y_cam = np.cross(z_cam, x_cam)
    y_cam = y_cam / float(np.linalg.norm(y_cam))

    rotation = np.column_stack((x_cam, y_cam, z_cam))
    rotation = _apply_optical_roll(rotation, roll_rad)
    return CameraPoseSE3.from_rt(rotation, cam)


def project_world_point(
    pose: CameraPoseSE3,
    point_world: Sequence[float] | FloatArray,
    focal_length_mm: float,
    *,
    sensor_width_mm: float = SENSOR_WIDTH_MM,
    sensor_height_mm: float = SENSOR_HEIGHT_MM,
) -> tuple[float, float]:
    """Project a 3D world point onto normalized sensor coordinates ``(u, v)``."""
    _require_finite_scalar(focal_length_mm, name="focal_length_mm")
    for name, value in (
        ("sensor_width_mm", sensor_width_mm),
        ("sensor_height_mm", sensor_height_mm),
    ):
        if _require_finite_scalar(value, name=name) <= 0:
            raise CinematographyError(f"{name} must be positive")
    if not math.isfinite(focal_length_mm) or focal_length_mm <= 0.0:
        raise CinematographyError(f"focal_length_mm must be positive, got {focal_length_mm!r}")
    pt = _as_vec3(point_world, name="point_world")
    p_cam = pose.rotation_matrix.T @ (pt - pose.translation_vector)
    if float(p_cam[2]) <= 1e-6:
        raise CinematographyError("point_world lies behind or on the camera principal plane")
    u = 0.5 + (focal_length_mm * float(p_cam[0])) / (sensor_width_mm * float(p_cam[2]))
    v = 0.5 + (focal_length_mm * float(p_cam[1])) / (sensor_height_mm * float(p_cam[2]))
    return (u, v)


def build_rule_of_thirds_pose(
    camera_pos: Sequence[float] | FloatArray,
    subject_pos: Sequence[float] | FloatArray,
    *,
    focal_length_mm: float,
    target_uv: tuple[float, float],
    sensor_width_mm: float = SENSOR_WIDTH_MM,
    sensor_height_mm: float = SENSOR_HEIGHT_MM,
    roll_rad: float = 0.0,
) -> CameraPoseSE3:
    """Solve closed-form horizon-level ``SE(3)`` pose placing ``subject_pos`` at ``target_uv``."""
    _require_finite_scalar(focal_length_mm, name="focal_length_mm")
    for name, value in (
        ("sensor_width_mm", sensor_width_mm),
        ("sensor_height_mm", sensor_height_mm),
    ):
        if _require_finite_scalar(value, name=name) <= 0:
            raise CinematographyError(f"{name} must be positive")
    if not math.isfinite(focal_length_mm) or focal_length_mm <= 0.0:
        raise CinematographyError(f"focal_length_mm must be positive, got {focal_length_mm!r}")
    _require_finite_array(target_uv, name="target_uv")
    u_star, v_star = target_uv
    if not (0.0 < u_star < 1.0 and 0.0 < v_star < 1.0):
        raise CinematographyError(f"target_uv must lie strictly in (0, 1)^2, got {target_uv!r}")

    cam = _as_vec3(camera_pos, name="camera_pos")
    subj = _as_vec3(subject_pos, name="subject_pos")
    delta = subj - cam
    dist = float(np.linalg.norm(delta))
    if dist < 1e-6:
        raise CinematographyError("camera_pos and subject_pos must be distinct")

    ray = delta / dist
    r_x, r_y, r_z = float(ray[0]), float(ray[1]), float(ray[2])
    r_h = math.hypot(r_x, r_y)

    _require_finite_scalar(roll_rad, name="roll_rad")
    if not math.isfinite(roll_rad):
        raise CinematographyError(f"roll_rad must be finite, got {roll_rad!r}")

    alpha_x = ((u_star - 0.5) * sensor_width_mm) / focal_length_mm
    alpha_y = ((v_star - 0.5) * sensor_height_mm) / focal_length_mm
    z_c = 1.0 / math.sqrt(1.0 + alpha_x * alpha_x + alpha_y * alpha_y)
    # Map target camera-space ray through R_roll so R_final^T @ ray hits target_uv exactly.
    x_rolled = alpha_x * z_c
    y_rolled = alpha_y * z_c
    cos_r, sin_r = math.cos(roll_rad), math.sin(roll_rad)
    x_c = cos_r * x_rolled - sin_r * y_rolled
    y_c = sin_r * x_rolled + cos_r * y_rolled

    if r_h <= abs(x_c) + 1e-6:
        # Steeply vertical viewpoint fallback: align optical axis from look-at basis.
        base = build_look_at_pose(cam, subj)
        aim_cam = np.array([-x_c / z_c, -y_c / z_c, 1.0], dtype=np.float64)
        aim_world = cam + dist * (base.rotation_matrix @ (aim_cam / float(np.linalg.norm(aim_cam))))
        return build_look_at_pose(cam, aim_world, roll_rad=roll_rad)

    psi_0 = math.atan2(r_y, r_x)
    delta_psi = math.asin(max(-1.0, min(1.0, -x_c / r_h)))
    psi = psi_0 - delta_psi

    q = math.sqrt(max(0.0, r_h * r_h - x_c * x_c))
    beta = math.atan2(r_z * z_c + q * y_c, q * z_c - r_z * y_c)

    cos_b, sin_b = math.cos(beta), math.sin(beta)
    cos_p, sin_p = math.cos(psi), math.sin(psi)

    x_axis = np.array([sin_p, -cos_p, 0.0], dtype=np.float64)
    y_axis = np.array([sin_b * cos_p, sin_b * sin_p, -cos_b], dtype=np.float64)
    z_axis = np.array([cos_b * cos_p, cos_b * sin_p, sin_b], dtype=np.float64)

    rotation = np.column_stack((x_axis, y_axis, z_axis))
    rotation = _apply_optical_roll(rotation, roll_rad)
    return CameraPoseSE3.from_rt(rotation, cam)


# --------------------------------------------------------------------------- #
# 180-Degree Axis of Action Geometry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AxisOfAction:
    """Directed 180-degree line of action between two staged subjects ``p1 -> p2``."""

    p1: Vec3Tuple
    p2: Vec3Tuple
    initial_side: int = 1
    character_a: str = "CHARACTER_A"
    character_b: str = "CHARACTER_B"

    def __post_init__(self) -> None:
        _require_finite_array(self.p1, name="p1")
        _require_finite_array(self.p2, name="p2")
        _require_finite_scalar(self.initial_side, name="initial_side")
        v1 = _as_vec3(self.p1, name="p1")
        v2 = _as_vec3(self.p2, name="p2")
        horiz = math.hypot(float(v2[0] - v1[0]), float(v2[1] - v1[1]))
        if horiz < 1e-6:
            raise CinematographyError("p1 and p2 must have distinct horizontal (X, Y) coordinates")
        if self.initial_side not in (-1, 1):
            raise CinematographyError(f"initial_side must be +1 or -1, got {self.initial_side!r}")

    @property
    def p1_vector(self) -> FloatArray:
        return np.array(self.p1, dtype=np.float64)

    @property
    def p2_vector(self) -> FloatArray:
        return np.array(self.p2, dtype=np.float64)

    @property
    def axis_vector(self) -> FloatArray:
        """3D action vector ``p2 - p1``."""
        return self.p2_vector - self.p1_vector

    @property
    def horizontal_length(self) -> float:
        """Euclidean distance between ``p1`` and ``p2`` projected onto the XY floor."""
        return math.hypot(self.p2[0] - self.p1[0], self.p2[1] - self.p1[1])

    @property
    def midpoint(self) -> FloatArray:
        """3D midpoint ``0.5 * (p1 + p2)``."""
        return 0.5 * (self.p1_vector + self.p2_vector)


def horizontal_axis_cross(
    camera_pos: Sequence[float] | FloatArray,
    p1: Sequence[float] | FloatArray,
    p2: Sequence[float] | FloatArray,
) -> float:
    """Compute ``((cam_pos - p1) x (p2 - p1))_z`` on the horizontal XY plane."""
    c = _as_vec3(camera_pos, name="camera_pos")
    a = _as_vec3(p1, name="p1")
    b = _as_vec3(p2, name="p2")
    return float((c[0] - a[0]) * (b[1] - a[1]) - (c[1] - a[1]) * (b[0] - a[0]))


def axis_side_sign(
    camera_pos: Sequence[float] | FloatArray,
    p1: Sequence[float] | FloatArray,
    p2: Sequence[float] | FloatArray,
    *,
    eps: float = 1e-9,
) -> int:
    """Return ``+1``, ``-1``, or ``0`` for camera placement relative to ``p1 -> p2``."""
    _require_finite_scalar(eps, name="eps")
    if eps < 0:
        raise CinematographyError("eps must be nonnegative")
    cross_z = horizontal_axis_cross(camera_pos, p1, p2)
    if cross_z > eps:
        return 1
    if cross_z < -eps:
        return -1
    return 0


@dataclass(frozen=True)
class AxisEnforcementTrace:
    """Explicit geometric audit record of 180-degree axis enforcement on a 3D point."""

    adjusted_position: Vec3Tuple
    original_position: Vec3Tuple
    original_axis_side: int
    enforced_axis_side: int
    original_signed_distance_m: float
    enforced_signed_distance_m: float
    was_axis_reflected: bool

    def validate_strict(self) -> Self:
        """Validate trusted geometry, while the raw constructor remains audit-safe."""
        for item in fields(self):
            value = getattr(self, item.name)
            if item.type in ("float", "int"):
                _require_finite_scalar(value, name=item.name)
            elif item.type in ("Vec3Tuple", "Mat3Tuple"):
                _require_finite_array(value, name=item.name)
        return self

    @classmethod
    def from_validated(cls, **kwargs: Any) -> Self:
        """Construct validated geometry, not an untrusted audit record."""
        return cls(**kwargs).validate_strict()


def enforce_axis_of_action_with_trace(
    camera_pos: Sequence[float] | FloatArray,
    axis: AxisOfAction,
    *,
    required_side: int,
    min_clearance_m: float = 0.35,
    strict: bool = False,
) -> tuple[FloatArray, AxisEnforcementTrace]:
    """Enforce ``required_side`` of ``axis`` and return ``(adjusted_pos, trace)``.

    When ``camera_pos`` lies on the opposite half-plane or closer than
    ``min_clearance_m`` to the 180-degree line:

    * If ``strict=True``, raises :class:`CinematographyError` (a ``ValueError``
      subclass) rather than silently reflecting/clamping the camera.
    * If ``strict=False`` (default), reflects/clamps ``camera_pos`` orthogonally
      into the valid half-plane and sets ``trace.was_axis_reflected = True`` along
      with ``trace.original_axis_side`` (``-1``, ``0``, or ``+1``).
    """
    _require_finite_scalar(required_side, name="required_side")
    if required_side not in (-1, 1):
        raise CinematographyError(f"required_side must be +1 or -1, got {required_side!r}")
    _require_finite_scalar(min_clearance_m, name="min_clearance_m")
    if not math.isfinite(min_clearance_m) or min_clearance_m <= 0.0:
        raise CinematographyError(f"min_clearance_m must be positive, got {min_clearance_m!r}")

    cam = _as_vec3(camera_pos, name="camera_pos")
    orig_side = axis_side_sign(cam, axis.p1, axis.p2)
    p1_xy = axis.p1_vector[:2]
    d_xy = axis.axis_vector[:2]
    axis_unit = d_xy / axis.horizontal_length
    # Unit normal pointing strictly into the positive horizontal-cross-product half-plane:
    # (n_plus x axis_unit)_z = a_y^2 - (-a_x)*a_x = +1.
    n_plus = np.array([axis_unit[1], -axis_unit[0]], dtype=np.float64)

    rel_xy = cam[:2] - p1_xy
    along = float(np.dot(rel_xy, axis_unit))
    signed_dist = float(np.dot(rel_xy, n_plus))

    if required_side * signed_dist >= min_clearance_m:
        unchanged = cam.copy()
        return unchanged, AxisEnforcementTrace(
            adjusted_position=_to_vec3_tuple(unchanged),
            original_position=_to_vec3_tuple(cam),
            original_axis_side=orig_side,
            enforced_axis_side=required_side,
            original_signed_distance_m=signed_dist,
            enforced_signed_distance_m=signed_dist,
            was_axis_reflected=False,
        )

    if strict:
        raise CinematographyError(
            f"camera_pos {_to_vec3_tuple(cam)} violates 180-degree axis in strict mode "
            f"(original_axis_side={orig_side}, required_side={required_side}, "
            f"signed_distance_m={signed_dist:.6f}, min_clearance_m={min_clearance_m:.6f})"
        )

    corrected_dist = float(required_side) * max(abs(signed_dist), min_clearance_m)
    new_xy = p1_xy + along * axis_unit + corrected_dist * n_plus
    adjusted = np.array([float(new_xy[0]), float(new_xy[1]), float(cam[2])], dtype=np.float64)
    return adjusted, AxisEnforcementTrace(
        adjusted_position=_to_vec3_tuple(adjusted),
        original_position=_to_vec3_tuple(cam),
        original_axis_side=orig_side,
        enforced_axis_side=required_side,
        original_signed_distance_m=signed_dist,
        enforced_signed_distance_m=corrected_dist,
        was_axis_reflected=True,
    )


def enforce_axis_of_action(
    camera_pos: Sequence[float] | FloatArray,
    axis: AxisOfAction,
    *,
    required_side: int,
    min_clearance_m: float = 0.35,
    strict: bool = False,
) -> FloatArray:
    """Guarantee ``camera_pos`` lies strictly on ``required_side`` of ``axis``.

    If ``camera_pos`` crosses or sits too close to the 180-degree line, raises
    :class:`CinematographyError` (``ValueError``) when ``strict=True``; otherwise
    reflects/clamps it orthogonally across the line of action in the XY plane.
    Use :func:`enforce_axis_of_action_with_trace` to inspect reflection metadata.
    """
    adjusted, _ = enforce_axis_of_action_with_trace(
        camera_pos,
        axis,
        required_side=required_side,
        min_clearance_m=min_clearance_m,
        strict=strict,
    )
    return adjusted


# --------------------------------------------------------------------------- #
# Lens, Aperture (f-stop) & Rule-of-Thirds Selection
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LensSettings:
    """Selected prime cinema lens, aperture, and thin-lens optical metrics."""

    focal_length_mm: int
    f_stop: float
    shot_scale: ShotScale
    horizontal_fov_deg: float
    vertical_fov_deg: float
    focus_distance_m: float
    hyperfocal_distance_m: float
    dof_near_m: float
    dof_far_m: float

    def __post_init__(self) -> None:
        _require_finite_scalar(self.focal_length_mm, name="focal_length_mm")
        if self.focal_length_mm <= 0:
            raise CinematographyError("focal_length_mm must be positive")
        _require_finite_scalar(self.f_stop, name="f_stop")
        if self.f_stop <= 0:
            raise CinematographyError("f_stop must be positive")
        _require_finite_scalar(self.horizontal_fov_deg, name="horizontal_fov_deg")
        _require_finite_scalar(self.vertical_fov_deg, name="vertical_fov_deg")
        _require_finite_scalar(self.focus_distance_m, name="focus_distance_m")
        if self.focus_distance_m <= 0:
            raise CinematographyError("focus_distance_m must be positive")
        _require_finite_scalar(self.hyperfocal_distance_m, name="hyperfocal_distance_m")
        if self.hyperfocal_distance_m <= 0:
            raise CinematographyError("hyperfocal_distance_m must be positive")
        _require_finite_scalar(self.dof_near_m, name="dof_near_m")
        _require_finite_scalar(self.dof_far_m, name="dof_far_m")


@dataclass(frozen=True)
class RuleOfThirdsFraming:
    """Rule-of-Thirds composition target and verified pinhole projection."""

    power_point: RuleOfThirdsPoint
    target_uv: tuple[float, float]
    projected_start_uv: tuple[float, float]
    projected_end_uv: tuple[float, float]
    framing_error_uv: float
    look_direction: ScreenLookDirection

    def __post_init__(self) -> None:
        _require_finite_array(self.target_uv, name="target_uv")
        _require_finite_array(self.projected_start_uv, name="projected_start_uv")
        _require_finite_array(self.projected_end_uv, name="projected_end_uv")
        _require_finite_scalar(self.framing_error_uv, name="framing_error_uv")


def select_lens_and_aperture(
    dramatic_tension: float,
    *,
    is_establishing: bool = False,
    is_two_shot: bool = False,
    setting: SceneSetting = SceneSetting.INT,
    time_of_day: TimeOfDay = TimeOfDay.DAY,
    focus_distance_m: float | None = None,
) -> LensSettings:
    """Select focal length, f-stop, and depth-of-field from dramatic tension."""
    _require_finite_scalar(dramatic_tension, name="dramatic_tension")
    if not math.isfinite(dramatic_tension) or not (0.0 <= dramatic_tension <= 1.0):
        raise CinematographyError(
            f"dramatic_tension must be within [0.0, 1.0], got {dramatic_tension!r}"
        )
    if focus_distance_m is not None:
        _require_finite_scalar(focus_distance_m, name="focus_distance_m")
    if focus_distance_m is not None and (
        not math.isfinite(focus_distance_m) or focus_distance_m <= 0.0
    ):
        raise CinematographyError(f"focus_distance_m must be positive, got {focus_distance_m!r}")

    if is_establishing or dramatic_tension < 0.25:
        focal = 24
    elif is_two_shot or dramatic_tension < 0.45:
        focal = 35
    elif dramatic_tension < 0.65:
        focal = 50
    elif dramatic_tension < 0.85:
        focal = 85
    else:
        focal = 135

    # Higher tension isolates the subject via shallower depth of field (lower f-stop index).
    if is_establishing:
        stop_idx = 5  # f/8.0 deep focus for geography
    elif dramatic_tension >= 0.88:
        stop_idx = 0  # f/1.4 razor-thin isolation
    elif dramatic_tension >= 0.70:
        stop_idx = 1  # f/2.0
    elif dramatic_tension >= 0.50:
        stop_idx = 2  # f/2.8
    elif dramatic_tension >= 0.30:
        stop_idx = 3  # f/4.0
    else:
        stop_idx = 4  # f/5.6

    # Physical ambient-light adjustment: open aperture at night/twilight, close in exterior sun.
    if time_of_day == TimeOfDay.NIGHT:
        stop_idx = max(0, stop_idx - 1)
    elif setting == SceneSetting.EXT and time_of_day == TimeOfDay.DAY:
        stop_idx = min(len(CANONICAL_F_STOPS) - 1, stop_idx + 1)

    f_stop = CANONICAL_F_STOPS[stop_idx]
    dist_m = focus_distance_m if focus_distance_m is not None else _LENS_NOMINAL_DISTANCE_M[focal]

    h_fov = math.degrees(2.0 * math.atan(SENSOR_WIDTH_MM / (2.0 * focal)))
    v_fov = math.degrees(2.0 * math.atan(SENSOR_HEIGHT_MM / (2.0 * focal)))

    # Thin-lens hyperfocal distance H = f^2 / (N * c) + f (converted from mm to meters).
    f_mm = float(focal)
    hyperfocal_m = ((f_mm * f_mm) / (f_stop * CIRCLE_OF_CONFUSION_MM) + f_mm) / 1000.0
    f_m = f_mm / 1000.0
    dof_near = (hyperfocal_m * dist_m) / (hyperfocal_m + max(0.0, dist_m - f_m))
    if dist_m >= hyperfocal_m:
        dof_far = 1_000.0  # optical infinity cap for finite JSON serialization
    else:
        dof_far = min(1_000.0, (hyperfocal_m * dist_m) / (hyperfocal_m - (dist_m - f_m)))

    return LensSettings(
        focal_length_mm=focal,
        f_stop=f_stop,
        shot_scale=_LENS_TO_SCALE[focal],
        horizontal_fov_deg=round(h_fov, 6),
        vertical_fov_deg=round(v_fov, 6),
        focus_distance_m=round(dist_m, 6),
        hyperfocal_distance_m=round(hyperfocal_m, 6),
        dof_near_m=round(dof_near, 6),
        dof_far_m=round(dof_far, 6),
    )


def select_rule_of_thirds(
    subject_slot: int,
    *,
    axis_side: int = 1,
    dramatic_tension: float = 0.5,
    is_establishing: bool = False,
    weather: WeatherCondition = WeatherCondition.CLEAR,
) -> tuple[RuleOfThirdsPoint, tuple[float, float], ScreenLookDirection]:
    """Choose Rule-of-Thirds intersection and screen look-room direction.

    ``subject_slot == 0`` represents ``character_a`` at ``p1`` (screen-left when
    ``axis_side == +1``), ``subject_slot == 1`` represents ``character_b`` at
    ``p2`` (screen-right when ``axis_side == +1``), and ``subject_slot >= 2``
    represents a neutral/two-shot/establishing anchor.
    """
    if axis_side not in (-1, 1):
        raise CinematographyError(f"axis_side must be +1 or -1, got {axis_side!r}")

    if is_establishing and weather != WeatherCondition.CLEAR:
        # Place ground subject on lower third so atmospheric sky occupies upper two-thirds.
        point = RuleOfThirdsPoint.LOWER_LEFT if axis_side == 1 else RuleOfThirdsPoint.LOWER_RIGHT
        return point, _THIRDS_UV[point], ScreenLookDirection.CENTER

    if subject_slot == 0:
        on_left = axis_side == 1
        point = RuleOfThirdsPoint.UPPER_LEFT if on_left else RuleOfThirdsPoint.UPPER_RIGHT
        look = ScreenLookDirection.RIGHT if on_left else ScreenLookDirection.LEFT
        return point, _THIRDS_UV[point], look

    if subject_slot == 1:
        on_right = axis_side == 1
        point = RuleOfThirdsPoint.UPPER_RIGHT if on_right else RuleOfThirdsPoint.UPPER_LEFT
        look = ScreenLookDirection.LEFT if on_right else ScreenLookDirection.RIGHT
        return point, _THIRDS_UV[point], look

    # Action / two-shot beat: keep eye-line on upper third, biased by dramatic tension.
    use_right = dramatic_tension >= 0.65
    if axis_side == -1:
        use_right = not use_right
    point = RuleOfThirdsPoint.UPPER_RIGHT if use_right else RuleOfThirdsPoint.UPPER_LEFT
    return point, _THIRDS_UV[point], ScreenLookDirection.CENTER


# --------------------------------------------------------------------------- #
# Quintic Minimum-Jerk Trajectory (s(tau) = 10*tau^3 - 15*tau^4 + 6*tau^5)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TrajectorySample:
    """Kinematic and SE(3) state of a moving camera at normalized time ``tau``."""

    tau: float
    time_sec: float
    s: float
    ds_dtau: float
    d2s_dtau2: float
    d3s_dtau3: float
    pose: CameraPoseSE3
    linear_velocity: Vec3Tuple
    linear_acceleration: Vec3Tuple
    linear_jerk: Vec3Tuple
    angular_velocity: Vec3Tuple
    angular_acceleration: Vec3Tuple
    angular_jerk: Vec3Tuple = (0.0, 0.0, 0.0)

    def __post_init__(self) -> None:
        _require_finite_scalar(self.tau, name="tau")
        _require_finite_scalar(self.time_sec, name="time_sec")
        _require_finite_scalar(self.s, name="s")
        _require_finite_scalar(self.ds_dtau, name="ds_dtau")
        _require_finite_scalar(self.d2s_dtau2, name="d2s_dtau2")
        _require_finite_scalar(self.d3s_dtau3, name="d3s_dtau3")
        _require_finite_array(self.linear_velocity, name="linear_velocity")
        _require_finite_array(self.linear_acceleration, name="linear_acceleration")
        _require_finite_array(self.linear_jerk, name="linear_jerk")
        _require_finite_array(self.angular_velocity, name="angular_velocity")
        _require_finite_array(self.angular_acceleration, name="angular_acceleration")
        _require_finite_array(self.angular_jerk, name="angular_jerk")


@dataclass(frozen=True)
class MinimumJerkTrajectory:
    """Sampled minimum-jerk camera trajectory on ``SO(3) x R^3``."""

    move_type: CameraMoveType
    duration_sec: float
    start_pose: CameraPoseSE3
    end_pose: CameraPoseSE3
    crane_arc_m: float
    total_translation_m: float
    total_rotation_deg: float
    samples: tuple[TrajectorySample, ...]

    def __post_init__(self) -> None:
        _require_finite_scalar(self.duration_sec, name="duration_sec")
        if self.duration_sec <= 0:
            raise CinematographyError("duration_sec must be positive")
        _require_finite_scalar(self.crane_arc_m, name="crane_arc_m")
        _require_finite_scalar(self.total_translation_m, name="total_translation_m")
        _require_finite_scalar(self.total_rotation_deg, name="total_rotation_deg")


def minimum_jerk_scalar(tau: float) -> tuple[float, float, float, float]:
    """Evaluate ``s(tau) = 10*tau^3 - 15*tau^4 + 6*tau^5`` and its first three derivatives.

    Returns ``(s, ds/dtau, d2s/dtau2, d3s/dtau3)``. Guarantees exact boundary
    conditions: ``s(0) = 0``, ``s(1) = 1``, ``s'(0) = s'(1) = 0``, and
    ``s''(0) = s''(1) = 0``.
    """
    _require_finite_scalar(tau, name="tau")
    if not math.isfinite(tau) or not (0.0 <= tau <= 1.0):
        raise CinematographyError(f"tau must be within [0.0, 1.0], got {tau!r}")
    one_minus = 1.0 - tau
    tau2 = tau * tau
    tau3 = tau2 * tau
    s = tau3 * (10.0 + tau * (-15.0 + 6.0 * tau))
    ds = 30.0 * tau2 * (one_minus * one_minus)
    d2s = 60.0 * tau * one_minus * (1.0 - 2.0 * tau)
    d3s = 60.0 - 360.0 * tau + 360.0 * tau2
    return (s, ds, d2s, d3s)


def minimum_jerk_profile(taus: Sequence[float] | FloatArray) -> FloatArray:
    """Vectorized minimum-jerk evaluation returning array of shape ``(N, 4)``."""
    arr = _require_finite_array(taus, name="taus")
    if arr.ndim != 1 or arr.size == 0 or not np.all(np.isfinite(arr)):
        raise CinematographyError("taus must be a non-empty 1-D array of finite values")
    if float(np.min(arr)) < 0.0 or float(np.max(arr)) > 1.0:
        raise CinematographyError("all tau values must lie within [0.0, 1.0]")
    om = 1.0 - arr
    t2 = arr * arr
    t3 = t2 * arr
    s = t3 * (10.0 + arr * (-15.0 + 6.0 * arr))
    ds = 30.0 * t2 * (om * om)
    d2s = 60.0 * arr * om * (1.0 - 2.0 * arr)
    d3s = 60.0 - 360.0 * arr + 360.0 * t2
    return np.column_stack((s, ds, d2s, d3s))


def _crane_bell_scalar(tau: float) -> tuple[float, float, float, float]:
    """Zero-boundary-velocity/acceleration crane arc bell ``b(tau) = 64*tau^3*(1-tau)^3``."""
    om = 1.0 - tau
    b = 64.0 * (tau**3) * (om**3)
    db = 192.0 * (tau * tau) * (om * om) * (1.0 - 2.0 * tau)
    d2b = 384.0 * tau * om * (1.0 - 5.0 * tau + 5.0 * tau * tau)
    d3b = 384.0 * (1.0 - 12.0 * tau + 30.0 * tau * tau - 20.0 * (tau**3))
    return (b, db, d2b, d3b)


def evaluate_trajectory_at(
    start_pose: CameraPoseSE3,
    end_pose: CameraPoseSE3,
    tau: float,
    *,
    duration_sec: float,
    crane_arc_m: float = 0.0,
) -> TrajectorySample:
    """Evaluate exact pose, velocity, acceleration, and jerk at normalized time ``tau``."""
    _require_finite_scalar(duration_sec, name="duration_sec")
    if not math.isfinite(duration_sec) or duration_sec <= 0.0:
        raise CinematographyError(f"duration_sec must be positive, got {duration_sec!r}")
    _require_finite_scalar(crane_arc_m, name="crane_arc_m")
    if not math.isfinite(crane_arc_m):
        raise CinematographyError(f"crane_arc_m must be finite, got {crane_arc_m!r}")

    s, ds, d2s, d3s = minimum_jerk_scalar(tau)
    t0 = start_pose.translation_vector
    delta_t = end_pose.translation_vector - t0

    r0 = start_pose.rotation_matrix
    r1 = end_pose.rotation_matrix
    omega_rel = so3_log(r0.T @ r1)

    pos = t0 + s * delta_t
    dpos_dtau = ds * delta_t
    d2pos_dtau2 = d2s * delta_t
    d3pos_dtau3 = d3s * delta_t

    if crane_arc_m != 0.0:
        b, db, d2b, d3b = _crane_bell_scalar(tau)
        z_unit = np.array([0.0, 0.0, crane_arc_m], dtype=np.float64)
        pos = pos + b * z_unit
        dpos_dtau = dpos_dtau + db * z_unit
        d2pos_dtau2 = d2pos_dtau2 + d2b * z_unit
        d3pos_dtau3 = d3pos_dtau3 + d3b * z_unit

    inv_t = 1.0 / duration_sec
    inv_t2 = inv_t * inv_t
    inv_t3 = inv_t2 * inv_t

    lin_vel = dpos_dtau * inv_t
    lin_acc = d2pos_dtau2 * inv_t2
    lin_jerk = d3pos_dtau3 * inv_t3

    rot = r0 @ so3_exp(s * omega_rel)
    # World-frame angular velocity/acceleration/jerk along the geodesic axis R0 @ omega_rel.
    omega_world = r0 @ omega_rel
    ang_vel = (ds * inv_t) * omega_world
    ang_acc = (d2s * inv_t2) * omega_world
    ang_jerk = (d3s * inv_t3) * omega_world

    return TrajectorySample(
        tau=float(tau),
        time_sec=float(tau * duration_sec),
        s=s,
        ds_dtau=ds,
        d2s_dtau2=d2s,
        d3s_dtau3=d3s,
        pose=CameraPoseSE3.from_rt(rot, pos),
        linear_velocity=_to_vec3_tuple(lin_vel),
        linear_acceleration=_to_vec3_tuple(lin_acc),
        linear_jerk=_to_vec3_tuple(lin_jerk),
        angular_velocity=_to_vec3_tuple(ang_vel),
        angular_acceleration=_to_vec3_tuple(ang_acc),
        angular_jerk=_to_vec3_tuple(ang_jerk),
    )


def generate_minimum_jerk_trajectory(
    start_pose: CameraPoseSE3,
    end_pose: CameraPoseSE3,
    *,
    duration_sec: float,
    move_type: CameraMoveType = CameraMoveType.DOLLY,
    num_samples: int = 11,
    crane_arc_m: float = 0.0,
) -> MinimumJerkTrajectory:
    """Generate a smooth minimum-jerk ``SE(3)`` camera trajectory across ``[0, duration_sec]``."""
    if not isinstance(num_samples, int) or isinstance(num_samples, bool) or num_samples < 2:
        raise CinematographyError(f"num_samples must be an integer >= 2, got {num_samples!r}")
    _require_finite_scalar(duration_sec, name="duration_sec")
    if not math.isfinite(duration_sec) or duration_sec <= 0.0:
        raise CinematographyError(f"duration_sec must be positive, got {duration_sec!r}")

    taus = np.linspace(0.0, 1.0, num_samples, dtype=np.float64)
    # Pin exact floating-point endpoints.
    taus[0] = 0.0
    taus[-1] = 1.0

    samples = tuple(
        evaluate_trajectory_at(
            start_pose,
            end_pose,
            float(tau),
            duration_sec=duration_sec,
            crane_arc_m=crane_arc_m,
        )
        for tau in taus
    )

    # Compute arc-length translation and geodesic rotation excursion.
    arc_len = sum(
        float(
            np.linalg.norm(
                samples[i + 1].pose.translation_vector - samples[i].pose.translation_vector
            )
        )
        for i in range(len(samples) - 1)
    )
    rot_deg = math.degrees(
        float(np.linalg.norm(so3_log(start_pose.rotation_matrix.T @ end_pose.rotation_matrix)))
    )

    return MinimumJerkTrajectory(
        move_type=move_type,
        duration_sec=float(duration_sec),
        start_pose=start_pose,
        end_pose=end_pose,
        crane_arc_m=float(crane_arc_m),
        total_translation_m=round(arc_len, 6),
        total_rotation_deg=round(rot_deg, 6),
        samples=samples,
    )


# --------------------------------------------------------------------------- #
# Scene Shot Blocking, Solving & 180-Degree Verification
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CameraShotSpec:
    """Complete mathematical cinematography specification for one shot."""

    shot_index: int
    scene_index: int
    shot_in_scene: int
    element_index: int
    shot_kind: ShotKind
    subject_label: str
    subject_position: Vec3Tuple
    duration_sec: float
    dramatic_tension: float
    emotion: EmotionTag
    pose: CameraPoseSE3
    end_pose: CameraPoseSE3
    axis_side: int
    axis_cross_z: float
    end_axis_cross_z: float
    intentional_axis_jump: bool
    lens: LensSettings
    framing: RuleOfThirdsFraming
    trajectory: MinimumJerkTrajectory
    start_axis_trace: AxisEnforcementTrace
    end_axis_trace: AxisEnforcementTrace
    start_original_axis_side: int = 1
    end_original_axis_side: int = 1
    start_was_reflected: bool = False
    end_was_reflected: bool = False
    original_axis_side: int = 1
    was_axis_reflected: bool = False

    def validate_strict(self) -> Self:
        """Validate trusted geometry, while the raw constructor remains audit-safe."""
        for item in fields(self):
            value = getattr(self, item.name)
            if item.type in ("float", "int"):
                _require_finite_scalar(value, name=item.name)
            elif item.type in ("Vec3Tuple", "Mat3Tuple"):
                _require_finite_array(value, name=item.name)
        if self.duration_sec <= 0:
            raise CinematographyError("duration_sec must be positive")
        for pose in (self.pose, self.end_pose):
            _validate_so3(pose.rotation, tol=1e-5)
            _as_vec3(pose.translation, name="translation")
        for trace in (self.start_axis_trace, self.end_axis_trace):
            trace.validate_strict()
        self.lens.__post_init__()
        return self

    @classmethod
    def from_validated(cls, **kwargs: Any) -> Self:
        """Construct validated geometry, not an untrusted audit record."""
        return cls(**kwargs).validate_strict()


#: Alias for per-shot camera plan carrying 180-degree reflection provenance.
ShotCameraPlan = CameraShotSpec


@dataclass(frozen=True)
class AxisVerificationReport:
    """Mathematical audit certificate for the 180-degree rule and SE(3) validity."""

    compliant: bool
    initial_side: int
    cross_products: tuple[float, ...]
    intentional_jumps: int
    violations: tuple[str, ...]
    reflected_shot_indices: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        _require_finite_scalar(self.initial_side, name="initial_side")
        _require_finite_scalar(self.intentional_jumps, name="intentional_jumps")


@dataclass(frozen=True)
class SceneCinematographyPlan:
    """Solved blocking, axis of action, shots, and 180-degree verification for one scene."""

    scene_index: int
    axis: AxisOfAction
    character_positions: tuple[tuple[str, Vec3Tuple], ...]
    shots: tuple[CameraShotSpec, ...]
    axis_report: AxisVerificationReport

    def __post_init__(self) -> None:
        _require_finite_scalar(self.scene_index, name="scene_index")


def _axis_trace_matches_geometry(
    trace: AxisEnforcementTrace,
    axis: AxisOfAction,
    *,
    expected_side: int,
    min_clearance_m: float,
    pose_translation: Vec3Tuple,
    tol: float = 1e-6,
) -> bool:
    """Independently replay 180-degree axis enforcement and verify all 7 trace fields."""
    replayed = (
        enforce_axis_of_action_with_trace(
            trace.original_position,
            axis,
            required_side=expected_side,
            min_clearance_m=min_clearance_m,
            strict=False,
        )[1]
        if len(trace.original_position) == 3
        and all(math.isfinite(c) for c in trace.original_position)
        else None
    )
    return (
        replayed is not None
        and all(
            abs(trace.original_position[i] - replayed.original_position[i]) <= tol for i in range(3)
        )
        and all(
            abs(trace.adjusted_position[i] - replayed.adjusted_position[i]) <= tol for i in range(3)
        )
        and all(abs(trace.adjusted_position[i] - pose_translation[i]) <= tol for i in range(3))
        and trace.original_axis_side == replayed.original_axis_side
        and trace.enforced_axis_side == expected_side
        and abs(trace.original_signed_distance_m - replayed.original_signed_distance_m) <= tol
        and abs(trace.enforced_signed_distance_m - replayed.enforced_signed_distance_m) <= tol
        and trace.was_axis_reflected is replayed.was_axis_reflected
    )


def expected_shot_move_type(shot: CameraShotSpec) -> CameraMoveType:
    """Return the canonical ``CameraMoveType`` prescribed by shot grammar."""
    is_establishing = (
        shot.shot_kind == ShotKind.ACTION
        and shot.shot_in_scene == 0
        and shot.dramatic_tension < 0.45
    )
    return _choose_camera_move(
        shot_kind=shot.shot_kind,
        is_establishing=is_establishing,
        emotion=shot.emotion,
        dramatic_tension=shot.dramatic_tension,
        duration_sec=shot.duration_sec,
        shot_in_scene=shot.shot_in_scene,
    )


def _canonical_shot_geometry_valid(
    shot: CameraShotSpec,
    axis: AxisOfAction,
    *,
    expected_side: int,
    start_override: Sequence[float] | FloatArray | None = None,
    end_override: Sequence[float] | FloatArray | None = None,
    tol: float = 1e-6,
) -> bool:
    """Verify move_type, clearance policy, pinhole framing, and pre-reflection positions."""
    base_move = expected_shot_move_type(shot)
    expected_move = (
        CameraMoveType.DOLLY
        if end_override is not None and base_move in (CameraMoveType.STATIC, CameraMoveType.PAN)
        else base_move
    )
    dist = _LENS_NOMINAL_DISTANCE_M.get(shot.lens.focal_length_mm, 0.0)
    look = shot.framing.look_direction
    slot = (
        (0 if ((look == ScreenLookDirection.RIGHT) == (expected_side == 1)) else 1)
        if look != ScreenLookDirection.CENTER
        else (0 if all(abs(shot.subject_position[i] - axis.p1[i]) <= tol for i in range(3)) else 2)
    )
    d_xy = axis.axis_vector[:2] / axis.horizontal_length
    n_plus = np.array([d_xy[1], -d_xy[0]], dtype=np.float64)
    cam_normal = float(expected_side) * n_plus
    variation = 0.06 * ((shot.shot_in_scene % 3) - 1)
    azimuth = (
        math.radians(52.0) + variation
        if slot == 0
        else (
            math.radians(-52.0) + variation
            if slot == 1
            else math.radians(10.0 * ((shot.shot_in_scene % 3) - 1))
        )
    )
    horiz_offset = dist * (math.sin(azimuth) * d_xy + math.cos(azimuth) * cam_normal)
    z_offset = (
        0.45
        if shot.lens.focal_length_mm == 24
        else (-0.08 if shot.dramatic_tension >= 0.82 else 0.0)
    )
    canonical_start: Vec3Tuple = (
        shot.subject_position[0] + float(horiz_offset[0]),
        shot.subject_position[1] + float(horiz_offset[1]),
        shot.subject_position[2] + z_offset,
    )
    expected_raw_start = (
        _to_vec3_tuple(_as_vec3(start_override, name="camera_override"))
        if start_override is not None
        else canonical_start
    )

    push_frac = 0.14 + 0.10 * shot.dramatic_tension
    canonical_dolly_end: Vec3Tuple = (
        shot.pose.translation[0]
        + push_frac * (shot.subject_position[0] - shot.pose.translation[0]),
        shot.pose.translation[1]
        + push_frac * (shot.subject_position[1] - shot.pose.translation[1]),
        shot.pose.translation[2]
        + push_frac * (shot.subject_position[2] - shot.pose.translation[2]),
    )
    vertical = -0.30 if shot.lens.focal_length_mm == 24 else 0.32
    canonical_crane_end: Vec3Tuple = (
        shot.pose.translation[0] + 0.35 * float(d_xy[0]),
        shot.pose.translation[1] + 0.35 * float(d_xy[1]),
        shot.pose.translation[2] + vertical,
    )
    canonical_end = (
        expected_raw_start
        if expected_move in (CameraMoveType.STATIC, CameraMoveType.PAN)
        else (canonical_dolly_end if expected_move == CameraMoveType.DOLLY else canonical_crane_end)
    )
    expected_raw_end = (
        _to_vec3_tuple(_as_vec3(end_override, name="end_camera_override"))
        if end_override is not None
        and expected_move in (CameraMoveType.DOLLY, CameraMoveType.CRANE)
        else canonical_end
    )

    expected_crane_arc = 0.12 if expected_move == CameraMoveType.CRANE else 0.0
    subj_vec = np.asarray(shot.subject_position, dtype=np.float64)
    poses_projectable = (
        dist > 0.0
        and is_valid_se3(shot.pose.matrix)
        and is_valid_se3(shot.end_pose.matrix)
        and float((shot.pose.rotation_matrix.T @ (subj_vec - shot.pose.translation_vector))[2])
        > 1e-6
        and float(
            (shot.end_pose.rotation_matrix.T @ (subj_vec - shot.end_pose.translation_vector))[2]
        )
        > 1e-6
    )
    proj_ok = poses_projectable and (
        lambda ps, pe: (
            abs(shot.framing.projected_start_uv[0] - ps[0]) <= tol
            and abs(shot.framing.projected_start_uv[1] - ps[1]) <= tol
            and abs(shot.framing.projected_end_uv[0] - pe[0]) <= tol
            and abs(shot.framing.projected_end_uv[1] - pe[1]) <= tol
        )
    )(
        project_world_point(shot.pose, shot.subject_position, float(shot.lens.focal_length_mm)),
        project_world_point(shot.end_pose, shot.subject_position, float(shot.lens.focal_length_mm)),
    )
    return (
        shot.trajectory.move_type == expected_move
        and abs(shot.trajectory.crane_arc_m - expected_crane_arc) <= tol
        and all(
            abs(shot.start_axis_trace.original_position[i] - expected_raw_start[i]) <= tol
            for i in range(3)
        )
        and all(
            abs(shot.end_axis_trace.original_position[i] - expected_raw_end[i]) <= tol
            for i in range(3)
        )
        and (
            shot.end_axis_trace == shot.start_axis_trace
            if expected_move in (CameraMoveType.STATIC, CameraMoveType.PAN)
            else True
        )
        and proj_ok
    )


def _positions_match(
    pairs: Sequence[tuple[str, Vec3Tuple]],
    canonical: Mapping[str, Vec3Tuple],
    *,
    tol: float,
    ordered: bool,
    expected_names: Sequence[str],
) -> bool:
    """True iff ``pairs`` names exactly the scene's cast and matches ``canonical`` within ``tol``.

    ``expected_names`` is the parsed scene's cast, so the ledger is bound to the screenplay itself
    and not merely to the plan's own staging. With ``ordered`` (a sequence-of-pairs ledger) the name
    *sequence* must match that cast exactly; without it (a :class:`~collections.abc.Mapping` ledger)
    the key *set* plus the entry *count* are bound, so dict insertion order is irrelevant while a
    short or duplicated ledger is not. The cast itself is validated before this comparison, so a
    non-string or blank ledger key can never line up with it, and every ``canonical`` lookup below
    is total.
    """
    names = [name for name, _ in pairs]
    if ordered:
        if names != list(expected_names) or set(names) != set(canonical):
            return False
    elif (
        len(names) != len(expected_names)  # cardinality, not just membership
        or set(names) != set(expected_names)
        or set(names) != set(canonical)
    ):
        # A Mapping ledger is order-free but never cardinality-free: comparing only key *sets*
        # would let a duplicated cast (``scene.characters == ("SARA", "SARA")``) collapse and be
        # "covered" by a single-entry dict, which the sequence-of-pairs path already refuses.
        return False
    return all(
        all(abs(pos[i] - canonical[name][i]) <= tol for i in range(3)) for name, pos in pairs
    )


def _cast_names_well_formed(cast: object) -> bool:
    """True iff a scene's declared cast is a duplicate-free tuple of non-blank names.

    The parser emits deduplicated casts, so a repeated, empty, or non-string entry can only come
    from a hand-forged or deserialized scene graph. Rejecting it up front keeps ``set`` based
    ledger comparisons from hiding a collapsed duplicate cast behind an apparently complete ledger.
    """
    if not isinstance(cast, tuple) or not all(
        isinstance(name, str) and bool(name.strip()) for name in cast
    ):
        return False
    return len(cast) == len(set(cast))


def _axis_side_pin_valid(axis: AxisOfAction, expected_initial_axis_side: object) -> bool:
    """True iff an optional opening-side policy pin is legal *and* satisfied.

    Only ``-1``/``+1`` are meaningful. Non-integers and ``bool`` are rejected explicitly (``True``
    would otherwise slip through because ``True == 1``), and any other value is treated as a
    configuration error and refused fail-closed rather than silently ignored.
    """
    if expected_initial_axis_side is None:
        return True
    if (
        not isinstance(expected_initial_axis_side, int)
        or isinstance(expected_initial_axis_side, bool)
        or expected_initial_axis_side not in (-1, 1)
    ):
        return False
    return axis.initial_side == expected_initial_axis_side


def _scene_blocking_valid(
    shots: Sequence[CameraShotSpec],
    axis: AxisOfAction,
    scene: ParsedScene,
    pairs: Sequence[tuple[str, Vec3Tuple]] | None,
    *,
    bind_axis: bool,
    ordered: bool,
    tol: float,
) -> bool:
    """Verify declared blocking and every shot subject against deterministic scene blocking.

    Recomputes the canonical staging and the per-element ``(subject_label, subject_position)`` from
    ``scene`` alone, so a tampered manifest cannot re-stage its actors around forged coordinates
    even if it re-seals every SHA-256 digest. With ``bind_axis`` the plan's ``AxisOfAction`` itself
    must also equal the scene-derived canonical axis (used when auditing sealed manifests).
    """
    try:
        canonical = compute_canonical_character_positions(
            scene, axis=axis, initial_axis_side=axis.initial_side
        )
        if bind_axis:
            expected_axis = canonical_axis_of_action(scene, axis.initial_side)
            if not (
                axis.character_a == expected_axis.character_a
                and axis.character_b == expected_axis.character_b
                and all(
                    abs(axis.p1[i] - expected_axis.p1[i]) <= tol
                    and abs(axis.p2[i] - expected_axis.p2[i]) <= tol
                    for i in range(3)
                )
            ):
                return False
        # The ledger must cover exactly this scene's parsed cast -- order-bound for a
        # sequence-of-pairs ledger, key-set-bound for a Mapping.
        if pairs is not None and not _positions_match(
            pairs, canonical, tol=tol, ordered=ordered, expected_names=scene.characters
        ):
            return False
        if len(shots) != len(scene.elements):
            return False
        for idx, shot in enumerate(shots):
            label, position = expected_shot_subject(
                scene,
                scene.elements[idx],
                canonical,
                axis=axis,
                initial_axis_side=axis.initial_side,
            )
            if shot.subject_label != label or any(
                abs(shot.subject_position[i] - position[i]) > tol for i in range(3)
            ):
                return False
        return True
    except (
        AttributeError,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
        ZeroDivisionError,
        FloatingPointError,
    ):
        return False


def verify_scene_character_blocking(
    shots: Sequence[CameraShotSpec],
    axis: AxisOfAction,
    *,
    scene: ParsedScene | None,
    character_positions: Sequence[tuple[str, Vec3Tuple]] | Mapping[str, Vec3Tuple] | None,
    bind_axis: bool = False,
    expected_initial_axis_side: int | None = None,
    position_tolerance: float = 1e-6,
) -> bool:
    """Verify ``character_positions`` and every shot subject against deterministic scene blocking.

    With ``scene`` supplied, the canonical ``(subject_label, subject_position)`` of every scene
    element -- and, when ``bind_axis`` is set, the canonical :class:`AxisOfAction` itself -- is
    recomputed from the parsed script alone, and the plan must match it within
    ``position_tolerance``. ``expected_initial_axis_side`` optionally pins the opening camera side
    (only ``-1``/``+1``; anything else, including ``bool``, fails closed). A :class:`Mapping`
    ledger is bound by key *set* (never by insertion order) but also by *cardinality*, so a
    duplicate-cast scene cannot be "covered" by a shortened ledger. ``position_tolerance`` must be
    a real finite non-negative number: a ``bool``, a string, ``None``, ``NaN``/``Inf``, or a
    negative value raises :class:`ValueError` instead of leaking a ``TypeError``. Everything after
    that policy check -- including ledger coordinates and scene attributes that a deserialized
    manifest could have replaced with a string, ``None``, or a short tuple -- is total: malformed
    input fails closed as ``False`` and never propagates an exception. The same routine backs
    ``verify_180_degree_rule`` (solver and cached report) and manifest verification, so solver and
    auditor cannot drift apart.
    """
    if (
        isinstance(position_tolerance, bool)
        or not isinstance(position_tolerance, (int, float))
        or not math.isfinite(position_tolerance)
        or position_tolerance < 0.0
    ):
        raise ValueError(
            f"position_tolerance must be a finite, non-negative float, got {position_tolerance!r}"
        )
    try:
        # Every actor name the scene claims must exist exactly once; a forged or deserialized scene
        # with a repeated, blank, non-string, or non-tuple cast is refused before any comparison.
        if scene is not None and not _cast_names_well_formed(getattr(scene, "characters", None)):
            return False
        tol = position_tolerance
        if not _axis_side_pin_valid(axis, expected_initial_axis_side):
            return False
        ordered_ledger = not isinstance(character_positions, Mapping)
        pairs = (
            list(character_positions.items())
            if isinstance(character_positions, Mapping)
            else (list(character_positions) if character_positions is not None else None)
        )
        char_map = dict(pairs) if pairs is not None else None
        pairs_ok = pairs is None or (
            len(char_map or {}) == len(pairs)
            and all(len(pos) == 3 and all(math.isfinite(c) for c in pos) for _, pos in pairs)
        )
        shots_match_chars = char_map is None or all(
            (
                sh.subject_label in char_map
                and all(
                    abs(sh.subject_position[i] - char_map[sh.subject_label][i]) <= tol
                    for i in range(3)
                )
            )
            for sh in shots
            if sh.shot_kind == ShotKind.DIALOGUE or sh.subject_label in char_map
        )
        if scene is None:
            return pairs_ok and shots_match_chars
        return (
            pairs_ok
            and shots_match_chars
            and _scene_blocking_valid(
                shots,
                axis,
                scene,
                pairs,
                bind_axis=bind_axis,
                ordered=ordered_ledger,
                tol=tol,
            )
        )
    except Exception:
        # Fail closed on *anything*: a deserialized manifest can hand this auditor objects whose
        # comparisons or arithmetic raise arbitrary exceptions, and a crashing gate is worse than a
        # refusing one. BaseException (KeyboardInterrupt, SystemExit) is deliberately not caught.
        return False


def verify_180_degree_rule(
    shots: Sequence[CameraShotSpec],
    axis: AxisOfAction,
    *,
    scene: ParsedScene | None = None,
    character_positions: Sequence[tuple[str, Vec3Tuple]] | Mapping[str, Vec3Tuple] | None = None,
    bind_axis: bool = False,
    expected_initial_axis_side: int | None = None,
    se3_tol: float = 1e-9,
    camera_overrides: Mapping[int, Sequence[float] | FloatArray] | None = None,
    end_camera_overrides: Mapping[int, Sequence[float] | FloatArray] | None = None,
) -> AxisVerificationReport:
    """Verify 180-degree axis compliance and ``SE(3)`` membership for all shots & samples.

    With ``scene`` supplied the plan's blocking ledger and every shot subject are re-derived from
    the parsed script; ``bind_axis=True`` additionally requires the axis itself to be the canonical
    scene-derived one, and ``expected_initial_axis_side`` pins the opening side. Builder and
    auditor pass the same flags, so ``axis_180_compliant`` means exactly what verification checks.
    """
    expected_side = axis.initial_side
    crosses: list[float] = []
    violations: list[str] = []
    jumps = 0

    if not verify_scene_character_blocking(
        shots,
        axis,
        scene=scene,
        character_positions=character_positions,
        bind_axis=bind_axis,
        expected_initial_axis_side=expected_initial_axis_side,
    ):
        violations.append("scene character_positions or shot subject blocking inconsistent")

    for shot in shots:
        if shot.intentional_axis_jump:
            expected_side = -expected_side
            jumps += 1

        if not is_valid_se3(shot.pose.matrix, tol=se3_tol) or not is_valid_se3(
            shot.end_pose.matrix, tol=se3_tol
        ):
            violations.append(f"shot {shot.shot_index}: start or end pose is not valid SE(3)")

        c_z = horizontal_axis_cross(shot.pose.translation_vector, axis.p1, axis.p2)
        end_c_z = horizontal_axis_cross(shot.end_pose.translation_vector, axis.p1, axis.p2)
        crosses.append(c_z)
        sign = axis_side_sign(shot.pose.translation_vector, axis.p1, axis.p2)
        end_sign = axis_side_sign(shot.end_pose.translation_vector, axis.p1, axis.p2)
        if sign != expected_side or end_sign != expected_side:
            violations.append(
                f"shot {shot.shot_index}: start or end pose crossed 180-degree axis "
                f"(start_cross_z={c_z:.6f}, start_sign={sign}, "
                f"end_cross_z={end_c_z:.6f}, end_sign={end_sign}, expected={expected_side})"
            )

        start_ovr = (
            camera_overrides[shot.shot_in_scene]
            if camera_overrides is not None and shot.shot_in_scene in camera_overrides
            else None
        )
        end_ovr = (
            end_camera_overrides[shot.shot_in_scene]
            if end_camera_overrides is not None and shot.shot_in_scene in end_camera_overrides
            else None
        )
        end_clearance = 0.50 if shot.trajectory.move_type == CameraMoveType.DOLLY else 0.55
        traces_consistent = (
            shot.axis_side == expected_side
            and abs(shot.axis_cross_z - c_z) <= 1e-6
            and abs(shot.end_axis_cross_z - end_c_z) <= 1e-6
            and _axis_trace_matches_geometry(
                shot.start_axis_trace,
                axis,
                expected_side=expected_side,
                min_clearance_m=0.55,
                pose_translation=shot.pose.translation,
            )
            and _axis_trace_matches_geometry(
                shot.end_axis_trace,
                axis,
                expected_side=expected_side,
                min_clearance_m=end_clearance,
                pose_translation=shot.end_pose.translation,
            )
            and _canonical_shot_geometry_valid(
                shot,
                axis,
                expected_side=expected_side,
                start_override=start_ovr,
                end_override=end_ovr,
            )
            and shot.start_original_axis_side == shot.start_axis_trace.original_axis_side
            and shot.end_original_axis_side == shot.end_axis_trace.original_axis_side
            and shot.start_was_reflected is shot.start_axis_trace.was_axis_reflected
            and shot.end_was_reflected is shot.end_axis_trace.was_axis_reflected
            and shot.original_axis_side == shot.start_original_axis_side
            and shot.was_axis_reflected is (shot.start_was_reflected or shot.end_was_reflected)
        )
        if not traces_consistent:
            violations.append(
                f"shot {shot.shot_index}: start/end axis enforcement trace inconsistent with poses"
            )

        for sample in shot.trajectory.samples:
            if not is_valid_se3(sample.pose.matrix, tol=se3_tol):
                violations.append(
                    f"shot {shot.shot_index} tau={sample.tau:.3f}: sample pose invalid SE(3)"
                )
            s_sign = axis_side_sign(sample.pose.translation_vector, axis.p1, axis.p2)
            if s_sign != expected_side:
                s_cz = horizontal_axis_cross(sample.pose.translation_vector, axis.p1, axis.p2)
                violations.append(
                    f"shot {shot.shot_index} tau={sample.tau:.3f}: trajectory crossed axis "
                    f"(cross_z={s_cz:.6f}, sign={s_sign}, expected={expected_side})"
                )

    return AxisVerificationReport(
        compliant=len(violations) == 0,
        initial_side=axis.initial_side,
        cross_products=tuple(crosses),
        intentional_jumps=jumps,
        violations=tuple(violations),
        reflected_shot_indices=tuple(sh.shot_index for sh in shots if sh.was_axis_reflected),
    )


def _stage_scene_characters(
    scene: ParsedScene,
    axis: AxisOfAction | None,
    initial_axis_side: int,
) -> tuple[AxisOfAction, dict[str, tuple[int, FloatArray]]]:
    """Deterministically stage scene characters around a canonical 180-degree axis."""
    chars = list(scene.characters)
    if axis is None:
        char_a = chars[0] if len(chars) >= 1 else "SUBJECT_A"
        char_b = chars[1] if len(chars) >= 2 else "SUBJECT_B"
        resolved_axis = AxisOfAction(
            p1=(-1.10, 0.0, 1.60),
            p2=(1.10, 0.0, 1.60),
            initial_side=initial_axis_side,
            character_a=char_a,
            character_b=char_b,
        )
    else:
        resolved_axis = axis

    positions: dict[str, tuple[int, FloatArray]] = {}
    if len(chars) >= 1:
        positions[chars[0]] = (0, resolved_axis.p1_vector)
    if len(chars) >= 2:
        positions[chars[1]] = (1, resolved_axis.p2_vector)

    # Place any supporting characters (3rd, 4th, ...) behind the axis (opposite camera side)
    # so they face into the conversation without forcing the camera across the 180-degree line.
    if len(chars) >= 3:
        d_xy = resolved_axis.axis_vector[:2] / resolved_axis.horizontal_length
        n_plus = np.array([d_xy[1], -d_xy[0]], dtype=np.float64)
        mid = resolved_axis.midpoint
        for extra_idx, extra_name in enumerate(chars[2:]):
            spread = 0.65 * (extra_idx - 0.5 * (len(chars) - 3))
            depth_back = -resolved_axis.initial_side * (0.75 + 0.25 * (extra_idx % 2))
            xy = mid[:2] + spread * d_xy + depth_back * n_plus
            slot = extra_idx % 2
            positions[extra_name] = (
                slot,
                np.array([float(xy[0]), float(xy[1]), float(mid[2])], dtype=np.float64),
            )

    return resolved_axis, positions


def compute_canonical_character_positions(
    scene: ParsedScene,
    *,
    axis: AxisOfAction | None = None,
    initial_axis_side: int = 1,
) -> dict[str, Vec3Tuple]:
    """Deterministically compute canonical 3D character blocking for ``scene``."""
    _, staged = _stage_scene_characters(scene, axis, initial_axis_side)
    return {name: _to_vec3_tuple(vec) for name, (_, vec) in staged.items()}


def canonical_axis_of_action(scene: ParsedScene, initial_axis_side: int = 1) -> AxisOfAction:
    """Return the deterministic, scene-derived 180-degree axis the solver stages characters on.

    Any sealed plan whose ``axis`` deviates from this value (endpoint ``p1``/``p2`` or the
    ``character_a``/``character_b`` labels taken from ``scene.characters``) was not produced by
    the canonical blocking pass and must be rejected.
    """
    return _stage_scene_characters(scene, None, initial_axis_side)[0]


def expected_shot_subject(
    scene: ParsedScene,
    action: DialogueLine | ActionBeat,
    char_positions: Mapping[str, Vec3Tuple],
    *,
    axis: AxisOfAction | None = None,
    initial_axis_side: int = 1,
) -> tuple[str, Vec3Tuple]:
    """Return the canonical ``(subject_label, subject_position)`` for one scene element."""
    resolved_axis, _ = _stage_scene_characters(scene, axis, initial_axis_side)
    midpoint = _to_vec3_tuple(resolved_axis.midpoint)
    if isinstance(action, DialogueLine):
        return (action.character, char_positions[action.character])
    if len(action.focus_characters) == 1 and action.focus_characters[0] in char_positions:
        focus_name = action.focus_characters[0]
        return (focus_name, char_positions[focus_name])
    if len(action.focus_characters) >= 2:
        return (f"{action.focus_characters[0]} & {action.focus_characters[1]}", midpoint)
    return (scene.heading.location, midpoint)


def _choose_camera_move(
    *,
    shot_kind: ShotKind,
    is_establishing: bool,
    emotion: EmotionTag,
    dramatic_tension: float,
    duration_sec: float,
    shot_in_scene: int,
) -> CameraMoveType:
    """Select cinematically motivated camera movement class for a shot."""
    if duration_sec < 0.85:
        return CameraMoveType.STATIC
    if is_establishing:
        return CameraMoveType.CRANE if duration_sec >= 1.8 else CameraMoveType.PAN
    if shot_kind == ShotKind.ACTION:
        return CameraMoveType.CRANE if dramatic_tension >= 0.65 else CameraMoveType.PAN
    if emotion in (EmotionTag.ANGRY, EmotionTag.FEARFUL, EmotionTag.URGENT, EmotionTag.WHISPER):
        return CameraMoveType.DOLLY
    if dramatic_tension >= 0.65:
        return CameraMoveType.DOLLY
    if shot_in_scene % 3 == 2 and duration_sec >= 1.5:
        return CameraMoveType.PAN
    return CameraMoveType.STATIC


def _compute_camera_endpoints(
    *,
    subject_pos: FloatArray,
    subject_slot: int,
    axis: AxisOfAction,
    active_side: int,
    lens: LensSettings,
    target_uv: tuple[float, float],
    move_type: CameraMoveType,
    dramatic_tension: float,
    shot_in_scene: int,
    camera_override: Sequence[float] | FloatArray | None = None,
    end_camera_override: Sequence[float] | FloatArray | None = None,
    strict_axis: bool = False,
) -> tuple[CameraPoseSE3, CameraPoseSE3, float, AxisEnforcementTrace, AxisEnforcementTrace]:
    """Compute 180-degree-compliant start/end SE(3) poses, crane lift, and both axis traces."""
    d_xy = axis.axis_vector[:2] / axis.horizontal_length
    n_plus = np.array([d_xy[1], -d_xy[0]], dtype=np.float64)
    cam_normal = float(active_side) * n_plus  # points strictly into valid camera half-plane

    dist = _LENS_NOMINAL_DISTANCE_M[lens.focal_length_mm]
    # Classic reverse-shot angle: camera sits near the listener's shoulder looking across
    # toward the speaker (~25-38 deg to the axis of action), strictly on active_side.
    variation = 0.06 * ((shot_in_scene % 3) - 1)
    if subject_slot == 0:
        azimuth_from_normal = math.radians(52.0) + variation  # toward p2 looking back at p1
    elif subject_slot == 1:
        azimuth_from_normal = math.radians(-52.0) + variation  # toward p1 looking back at p2
    else:
        azimuth_from_normal = math.radians(10.0 * ((shot_in_scene % 3) - 1))

    horiz_offset = dist * (
        math.sin(azimuth_from_normal) * d_xy + math.cos(azimuth_from_normal) * cam_normal
    )

    # Subtle camera-height grammar: establishing slightly elevated, high tension slightly lower.
    if lens.focal_length_mm == 24:
        z_offset = 0.45
    elif dramatic_tension >= 0.82:
        z_offset = -0.08
    else:
        z_offset = 0.0

    raw_start = (
        _as_vec3(camera_override, name="camera_override")
        if camera_override is not None
        else np.array(
            [
                float(subject_pos[0] + horiz_offset[0]),
                float(subject_pos[1] + horiz_offset[1]),
                float(subject_pos[2] + z_offset),
            ],
            dtype=np.float64,
        )
    )
    cam_start, start_trace = enforce_axis_of_action_with_trace(
        raw_start,
        axis,
        required_side=active_side,
        min_clearance_m=0.55,
        strict=strict_axis,
    )

    # Mild Dutch roll only on extreme peak tension shots (keeps SO(3) exact).
    roll_rad = math.radians(2.5) if dramatic_tension >= 0.95 else 0.0
    start_pose = build_rule_of_thirds_pose(
        cam_start,
        subject_pos,
        focal_length_mm=float(lens.focal_length_mm),
        target_uv=target_uv,
        roll_rad=roll_rad,
    )

    crane_arc = 0.0
    if move_type == CameraMoveType.STATIC:
        return (start_pose, start_pose, crane_arc, start_trace, start_trace)

    if move_type == CameraMoveType.DOLLY:
        # Smooth push-in toward subject (clamped to keep safe axis clearance >= 0.50 m).
        to_subject = subject_pos - cam_start
        push_frac = 0.14 + 0.10 * dramatic_tension
        raw_end = (
            _as_vec3(end_camera_override, name="end_camera_override")
            if end_camera_override is not None
            else cam_start + push_frac * to_subject
        )
        cam_end, end_trace = enforce_axis_of_action_with_trace(
            raw_end,
            axis,
            required_side=active_side,
            min_clearance_m=0.50,
            strict=strict_axis,
        )
        end_pose = build_rule_of_thirds_pose(
            cam_end,
            subject_pos,
            focal_length_mm=float(lens.focal_length_mm),
            target_uv=target_uv,
            roll_rad=roll_rad,
        )
        return (start_pose, end_pose, crane_arc, start_trace, end_trace)

    if move_type == CameraMoveType.PAN:
        # Fixed pedestal (zero translation), subtle horizontal lead reframe within safe margins.
        pan_shift = -0.04 if target_uv[0] > 0.5 else 0.04
        end_uv = (min(max(target_uv[0] + pan_shift, 0.20), 0.80), target_uv[1])
        end_pose = build_rule_of_thirds_pose(
            cam_start,
            subject_pos,
            focal_length_mm=float(lens.focal_length_mm),
            target_uv=end_uv,
            roll_rad=roll_rad,
        )
        return (start_pose, end_pose, crane_arc, start_trace, start_trace)

    # CameraMoveType.CRANE: jib descent/rise + gentle lateral glide + smooth arc bell.
    lateral = 0.35 * d_xy
    vertical = -0.30 if lens.focal_length_mm == 24 else 0.32
    raw_end = (
        _as_vec3(end_camera_override, name="end_camera_override")
        if end_camera_override is not None
        else np.array(
            [
                float(cam_start[0] + lateral[0]),
                float(cam_start[1] + lateral[1]),
                float(cam_start[2] + vertical),
            ],
            dtype=np.float64,
        )
    )
    cam_end, end_trace = enforce_axis_of_action_with_trace(
        raw_end,
        axis,
        required_side=active_side,
        min_clearance_m=0.55,
        strict=strict_axis,
    )
    crane_arc = 0.12
    end_pose = build_rule_of_thirds_pose(
        cam_end,
        subject_pos,
        focal_length_mm=float(lens.focal_length_mm),
        target_uv=target_uv,
        roll_rad=roll_rad,
    )
    return (start_pose, end_pose, crane_arc, start_trace, end_trace)


def solve_scene_cinematography(
    scene: ParsedScene,
    *,
    axis: AxisOfAction | None = None,
    initial_axis_side: int = 1,
    starting_shot_index: int = 0,
    num_trajectory_samples: int = 11,
    camera_overrides: Mapping[int, Sequence[float] | FloatArray] | None = None,
    end_camera_overrides: Mapping[int, Sequence[float] | FloatArray] | None = None,
    strict_axis: bool = False,
) -> SceneCinematographyPlan:
    """Solve camera poses, 180-degree blocking, optics, and trajectories for one scene."""
    _require_finite_scalar(starting_shot_index, name="starting_shot_index")
    _require_finite_scalar(initial_axis_side, name="initial_axis_side")
    if starting_shot_index < 0:
        raise CinematographyError(f"starting_shot_index must be >= 0, got {starting_shot_index!r}")
    if (
        not isinstance(num_trajectory_samples, int)
        or isinstance(num_trajectory_samples, bool)
        or num_trajectory_samples < 2
    ):
        raise CinematographyError(
            f"num_trajectory_samples must be an integer >= 2, got {num_trajectory_samples!r}"
        )

    resolved_axis, char_map = _stage_scene_characters(scene, axis, initial_axis_side)
    char_tuple_map = {name: _to_vec3_tuple(vec) for name, (_, vec) in char_map.items()}
    active_side = resolved_axis.initial_side
    shots: list[CameraShotSpec] = []

    for local_idx, elem in enumerate(scene.elements):
        is_jump = elem.intentional_axis_jump
        if is_jump:
            active_side = -active_side

        subject_label, subj_tuple = expected_shot_subject(
            scene,
            elem,
            char_tuple_map,
            axis=resolved_axis,
            initial_axis_side=initial_axis_side,
        )
        subj_pos = np.array(subj_tuple, dtype=np.float64)
        if isinstance(elem, DialogueLine):
            shot_kind = ShotKind.DIALOGUE
            emotion = elem.emotion
            duration = elem.timing.total_duration_sec
            slot = char_map[elem.character][0]
            is_establishing = False
            is_two_shot = False
        else:
            assert isinstance(elem, ActionBeat)
            shot_kind = ShotKind.ACTION
            emotion = EmotionTag.NEUTRAL
            duration = elem.duration_sec
            is_establishing = local_idx == 0 and elem.dramatic_tension < 0.45
            if len(elem.focus_characters) == 1 and elem.focus_characters[0] in char_map:
                slot = char_map[subject_label][0]
                is_two_shot = False
            elif len(elem.focus_characters) >= 2:
                slot = 2
                is_two_shot = not is_establishing
            else:
                slot = 2
                is_two_shot = not is_establishing and elem.dramatic_tension < 0.50

        prelim_lens = select_lens_and_aperture(
            elem.dramatic_tension,
            is_establishing=is_establishing,
            is_two_shot=is_two_shot,
            setting=scene.heading.setting,
            time_of_day=scene.heading.time_of_day,
        )
        power_point, target_uv, look_dir = select_rule_of_thirds(
            slot,
            axis_side=active_side,
            dramatic_tension=elem.dramatic_tension,
            is_establishing=is_establishing,
            weather=scene.heading.weather,
        )
        move_type = _choose_camera_move(
            shot_kind=shot_kind,
            is_establishing=is_establishing,
            emotion=emotion,
            dramatic_tension=elem.dramatic_tension,
            duration_sec=duration,
            shot_in_scene=local_idx,
        )
        shot_override = (
            camera_overrides[local_idx]
            if camera_overrides is not None and local_idx in camera_overrides
            else None
        )
        end_override = (
            end_camera_overrides[local_idx]
            if end_camera_overrides is not None and local_idx in end_camera_overrides
            else None
        )
        effective_move = (
            CameraMoveType.DOLLY
            if end_override is not None and move_type in (CameraMoveType.STATIC, CameraMoveType.PAN)
            else move_type
        )

        (
            start_pose,
            end_pose,
            crane_arc,
            start_trace,
            end_trace,
        ) = _compute_camera_endpoints(
            subject_pos=subj_pos,
            subject_slot=slot,
            axis=resolved_axis,
            active_side=active_side,
            lens=prelim_lens,
            target_uv=target_uv,
            move_type=effective_move,
            dramatic_tension=elem.dramatic_tension,
            shot_in_scene=local_idx,
            camera_override=shot_override,
            end_camera_override=end_override,
            strict_axis=strict_axis,
        )

        # Refine optical focus distance and DoF to exact 3D camera-to-subject distance.
        exact_dist = float(np.linalg.norm(subj_pos - start_pose.translation_vector))
        lens = select_lens_and_aperture(
            elem.dramatic_tension,
            is_establishing=is_establishing,
            is_two_shot=is_two_shot,
            setting=scene.heading.setting,
            time_of_day=scene.heading.time_of_day,
            focus_distance_m=exact_dist,
        )

        proj_start = project_world_point(start_pose, subj_pos, float(lens.focal_length_mm))
        proj_end = project_world_point(end_pose, subj_pos, float(lens.focal_length_mm))
        framing_err = math.hypot(proj_start[0] - target_uv[0], proj_start[1] - target_uv[1])

        framing = RuleOfThirdsFraming(
            power_point=power_point,
            target_uv=target_uv,
            projected_start_uv=proj_start,
            projected_end_uv=proj_end,
            framing_error_uv=framing_err,
            look_direction=look_dir,
        )

        trajectory = generate_minimum_jerk_trajectory(
            start_pose,
            end_pose,
            duration_sec=duration,
            move_type=effective_move,
            num_samples=num_trajectory_samples,
            crane_arc_m=crane_arc,
        )

        cross_z = horizontal_axis_cross(
            start_pose.translation_vector, resolved_axis.p1, resolved_axis.p2
        )
        end_cross_z = horizontal_axis_cross(
            end_pose.translation_vector, resolved_axis.p1, resolved_axis.p2
        )
        was_reflected = start_trace.was_axis_reflected or end_trace.was_axis_reflected
        shots.append(
            CameraShotSpec(
                shot_index=starting_shot_index + local_idx,
                scene_index=scene.scene_index,
                shot_in_scene=local_idx,
                element_index=elem.element_index,
                shot_kind=shot_kind,
                subject_label=subject_label,
                subject_position=_to_vec3_tuple(subj_pos),
                duration_sec=duration,
                dramatic_tension=elem.dramatic_tension,
                emotion=emotion,
                pose=start_pose,
                end_pose=end_pose,
                axis_side=active_side,
                axis_cross_z=cross_z,
                end_axis_cross_z=end_cross_z,
                intentional_axis_jump=is_jump,
                lens=lens,
                framing=framing,
                trajectory=trajectory,
                start_axis_trace=start_trace,
                end_axis_trace=end_trace,
                start_original_axis_side=start_trace.original_axis_side,
                end_original_axis_side=end_trace.original_axis_side,
                start_was_reflected=start_trace.was_axis_reflected,
                end_was_reflected=end_trace.was_axis_reflected,
                original_axis_side=start_trace.original_axis_side,
                was_axis_reflected=was_reflected,
            )
        )

    staged_pairs = tuple((name, _to_vec3_tuple(vec)) for name, (_, vec) in char_map.items())
    axis_report = verify_180_degree_rule(
        shots,
        resolved_axis,
        scene=scene,
        character_positions=staged_pairs,
        camera_overrides=camera_overrides,
        end_camera_overrides=end_camera_overrides,
    )
    return SceneCinematographyPlan(
        scene_index=scene.scene_index,
        axis=resolved_axis,
        character_positions=staged_pairs,
        shots=tuple(shots),
        axis_report=axis_report,
    )


def solve_script_cinematography(
    script: ParsedScript,
    *,
    initial_axis_side: int = 1,
    num_trajectory_samples: int = 11,
) -> tuple[SceneCinematographyPlan, ...]:
    """Solve cinematography plans for every scene in ``script`` with contiguous shot indices."""
    plans: list[SceneCinematographyPlan] = []
    next_shot_idx = 0
    for scene in script.scenes:
        plan = solve_scene_cinematography(
            scene,
            initial_axis_side=initial_axis_side,
            starting_shot_index=next_shot_idx,
            num_trajectory_samples=num_trajectory_samples,
        )
        plans.append(plan)
        next_shot_idx += len(plan.shots)
    return tuple(plans)


def verify_minimum_jerk_calculus_invariants(num_steps: int = 1000) -> dict[str, float | bool]:
    """Composite Simpson quadrature and a boundary-preserving action variation.

    The perturbation is epsilon*t^3*(1-t)^3. Its third derivative is orthogonal
    to the extremal jerk, hence every nonzero epsilon increases the action.
    """
    if isinstance(num_steps, bool) or not isinstance(num_steps, int):
        raise TypeError("num_steps must be an integer")
    if num_steps < 100:
        raise ValueError("num_steps must be at least 100")
    n = num_steps + num_steps % 2
    t: FloatArray = np.linspace(0.0, 1.0, n + 1)
    profile: FloatArray = np.array([minimum_jerk_scalar(float(x)) for x in t], dtype=np.float64)

    def integrate(y: FloatArray) -> float:
        return float((y[0] + y[-1] + 4 * y[1:-1:2].sum() + 2 * y[2:-1:2].sum()) / (3 * n))

    velocity_integral = integrate(profile[:, 1])
    action = integrate(profile[:, 3] ** 2)
    perturbation_jerk = 6 - 72 * t + 180 * t**2 - 120 * t**3
    perturbed_action = integrate((profile[:, 3] + 0.1 * perturbation_jerk) ** 2)
    boundary_ok = bool(
        np.allclose(profile[[0, -1], :3], [[0, 0, 0], [1, 0, 0]], atol=1e-12, rtol=0)
    )
    # Independent nine-point central differences on position alone. The
    # stencil is exact through degree eight, avoiding cancellation/truncation
    # tradeoffs in the third derivative of this quintic.
    offsets = np.arange(-4.0, 5.0)
    moments = np.vstack([offsets**power for power in range(9)])
    h = 0.01
    errors = []
    for derivative in (1, 2, 3):
        rhs = np.zeros(9)
        rhs[derivative] = math.factorial(derivative)
        weights = np.linalg.solve(moments, rhs) / h**derivative
        error = 0.0
        for tau in np.linspace(0.05, 0.95, min(n, 101)):
            positions = np.array([minimum_jerk_scalar(float(tau + h * x))[0] for x in offsets])
            numerical = float(weights @ positions)
            error = max(error, abs(numerical - minimum_jerk_scalar(float(tau))[derivative]))
        errors.append(error)
    return {
        "derivative_1_max_error": errors[0],
        "derivative_2_max_error": errors[1],
        "derivative_3_max_error": errors[2],
        "boundary_ok": boundary_ok,
        "velocity_integral": velocity_integral,
        "action": action,
        "perturbed_action": perturbed_action,
        "verified": boundary_ok
        and max(errors) < 1e-5
        and abs(velocity_integral - 1) < 1e-6
        and abs(action - 720) < 1e-3
        and perturbed_action > action,
    }
