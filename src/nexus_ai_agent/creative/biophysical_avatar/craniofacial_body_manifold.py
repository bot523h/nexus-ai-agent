"""Procedural face and body geometry with deterministic low-dimensional bases."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from nexus_ai_agent.creative.biophysical_avatar.entity_dna_seal import (
    MORPHOLOGY_DIM,
    canonical_bone_lengths,
)

__all__ = [
    "FACIAL_ACTION_UNITS",
    "FACE_LANDMARK_COUNT",
    "JOINT_NAMES",
    "PARENT_INDICES",
    "BodyMorphology",
    "CriticalDampedDynamics",
    "FaceManifold",
    "Skeleton3D",
    "SkeletonPose",
    "rigid_transform",
]

FloatArray = NDArray[np.float64]
FACE_LANDMARK_COUNT = 64
ACTION_UNIT_COUNT = 46

FACIAL_ACTION_UNITS: tuple[str, ...] = (
    "AU01_inner_brow_raise",
    "AU02_outer_brow_raise",
    "AU04_brow_lowerer",
    "AU05_upper_lid_raise",
    "AU06_cheek_raiser",
    "AU07_lid_tightener",
    "AU09_nose_wrinkler",
    "AU10_upper_lip_raiser",
    "AU11_nasolabial_deepener",
    "AU12_lip_corner_puller",
    "AU13_cheek_puffer",
    "AU14_dimpler",
    "AU15_lip_corner_depressor",
    "AU16_lower_lip_depressor",
    "AU17_chin_raiser",
    "AU18_lip_puckerer",
    "AU20_lip_stretcher",
    "AU22_lip_funneler",
    "AU23_lip_tightener",
    "AU24_lip_pressor",
    "AU25_lips_part",
    "AU26_jaw_drop",
    "AU27_mouth_stretch",
    "AU28_lip_suck",
    "AU29_jaw_thrust",
    "AU30_jaw_sideways",
    "AU31_jaw_clencher",
    "AU32_bite",
    "AU33_cheek_blow",
    "AU34_cheek_puff",
    "AU35_cheek_suck",
    "AU36_tongue_bulge",
    "AU37_lip_wipe",
    "AU38_nostril_dilator",
    "AU39_nostril_compressor",
    "AU40_sniff",
    "AU41_lid_droop",
    "AU42_slit_eyes",
    "AU43_eye_closure",
    "AU44_squint",
    "AU45_blink",
    "AU46_wink",
    "VISEME_A",
    "VISEME_I",
    "VISEME_U",
    "VISEME_FV",
)

JOINT_NAMES: tuple[str, ...] = (
    "pelvis",
    "spine",
    "chest",
    "head",
    "left_shoulder",
    "left_elbow",
    "left_wrist",
    "right_shoulder",
    "right_elbow",
    "right_wrist",
    "left_hip",
    "left_knee",
    "left_ankle",
    "left_foot",
    "right_hip",
    "right_knee",
    "right_ankle",
    "right_foot",
)
PARENT_INDICES: tuple[int, ...] = (
    -1,
    0,
    1,
    2,
    2,
    4,
    5,
    2,
    7,
    8,
    0,
    10,
    11,
    12,
    0,
    14,
    15,
    16,
)
_REST_DIRECTIONS: FloatArray = np.asarray(
    (
        (0.0, 0.0, 0.0),
        (0.0, 0.0, 1.0),
        (0.0, 0.0, 1.0),
        (0.0, 0.0, 1.0),
        (-1.0, 0.0, 0.0),
        (-1.0, 0.0, 0.0),
        (-1.0, 0.0, 0.0),
        (1.0, 0.0, 0.0),
        (1.0, 0.0, 0.0),
        (1.0, 0.0, 0.0),
        (-1.0, 0.0, 0.0),
        (0.0, 0.0, -1.0),
        (0.0, 0.0, -1.0),
        (0.0, 1.0, 0.0),
        (1.0, 0.0, 0.0),
        (0.0, 0.0, -1.0),
        (0.0, 0.0, -1.0),
        (0.0, 1.0, 0.0),
    ),
    dtype=np.float64,
)
_REST_DIRECTIONS.flags.writeable = False


def _numeric_vector(name: str, values: Sequence[float], length: int) -> FloatArray:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be a numeric vector")
    try:
        result = np.asarray(values, dtype=np.float64)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must contain numbers") from exc
    if result.shape != (length,) or not np.isfinite(result).all():
        raise ValueError(f"{name} must have shape ({length},) and finite values")
    return result


def _readonly_array(values: object, shape: tuple[int, ...], name: str) -> FloatArray:
    try:
        array = np.array(values, dtype=np.float64, copy=True)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a numeric array") from exc
    if array.shape != shape or not np.isfinite(array).all():
        raise ValueError(f"{name} must have shape {shape} and finite values")
    array.flags.writeable = False
    return array


def _dct_basis(rows: int, columns: int) -> FloatArray:
    row = np.arange(rows, dtype=np.float64)[:, None]
    frequency = np.arange(columns, dtype=np.float64)[None, :]
    basis = np.cos(np.pi * (row + 0.5) * frequency / rows)
    basis[:, 0] *= math.sqrt(1.0 / rows)
    if columns > 1:
        basis[:, 1:] *= math.sqrt(2.0 / rows)
    return basis


@dataclass(frozen=True, slots=True)
class FaceManifold:
    """64 structural coefficients plus 46 FACS/viseme controls over 64 points."""

    mean_shape: FloatArray
    structural_basis: FloatArray
    action_unit_basis: FloatArray

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "mean_shape",
            _readonly_array(self.mean_shape, (FACE_LANDMARK_COUNT, 3), "mean_shape"),
        )
        object.__setattr__(
            self,
            "structural_basis",
            _readonly_array(
                self.structural_basis, (FACE_LANDMARK_COUNT * 3, 64), "structural_basis"
            ),
        )
        object.__setattr__(
            self,
            "action_unit_basis",
            _readonly_array(
                self.action_unit_basis,
                (FACE_LANDMARK_COUNT * 3, ACTION_UNIT_COUNT),
                "action_unit_basis",
            ),
        )

    @classmethod
    def default(cls) -> FaceManifold:
        """Construct an orthogonal procedural base without loading large assets."""
        horizontal = np.linspace(-0.115, 0.115, 8, dtype=np.float64)
        vertical = np.linspace(-0.12, 0.12, 8, dtype=np.float64)
        x_grid, z_grid = np.meshgrid(horizontal, vertical)
        normalized_x = x_grid / 0.12
        normalized_z = z_grid / 0.14
        depth = -0.055 * np.sqrt(np.maximum(0.0, 1.0 - 0.55 * normalized_x**2))
        depth -= 0.018 * np.exp(-((normalized_x / 0.22) ** 2 + (normalized_z / 0.16) ** 2))
        mean = np.column_stack((x_grid.ravel(), depth.ravel(), z_grid.ravel()))
        orthogonal = _dct_basis(FACE_LANDMARK_COUNT * 3, 110)
        structural = orthogonal[:, :64] * 0.012
        action_units = orthogonal[:, 64:110] * 0.006
        return cls(mean, structural, action_units)

    def evaluate(
        self,
        alpha: Sequence[float],
        action_units: Sequence[float] | None = None,
    ) -> FloatArray:
        """Evaluate ``mean + U alpha + E w`` and return 64 3-D landmarks."""
        structural = _numeric_vector("alpha", alpha, 64)
        expressions = (
            np.zeros(ACTION_UNIT_COUNT, dtype=np.float64)
            if action_units is None
            else _numeric_vector("action_units", action_units, ACTION_UNIT_COUNT)
        )
        flattened = (
            self.mean_shape.reshape(-1)
            + self.structural_basis @ structural
            + self.action_unit_basis @ expressions
        )
        return flattened.reshape(FACE_LANDMARK_COUNT, 3)


@dataclass(slots=True)
class CriticalDampedDynamics:
    """Exact constant-target solution of a critically damped vector oscillator."""

    position: FloatArray
    velocity: FloatArray
    omega0: float = 12.0
    zeta: float = 1.0

    def __post_init__(self) -> None:
        position = np.array(self.position, dtype=np.float64, copy=True)
        velocity = np.array(self.velocity, dtype=np.float64, copy=True)
        if position.ndim != 1 or position.size == 0 or not np.isfinite(position).all():
            raise ValueError("position must be a non-empty finite vector")
        if velocity.shape != position.shape or not np.isfinite(velocity).all():
            raise ValueError("velocity must match position and contain finite values")
        if not math.isfinite(self.omega0) or self.omega0 <= 0.0:
            raise ValueError("omega0 must be finite and positive")
        if self.zeta != 1.0:
            raise ValueError("this solver is defined for critical damping (zeta=1)")
        self.position = position
        self.velocity = velocity

    @classmethod
    def at_rest(
        cls, dimensions: int = ACTION_UNIT_COUNT, *, omega0: float = 12.0
    ) -> CriticalDampedDynamics:
        if dimensions < 1:
            raise ValueError("dimensions must be positive")
        return cls(
            np.zeros(dimensions, dtype=np.float64), np.zeros(dimensions, dtype=np.float64), omega0
        )

    def step(self, target: Sequence[float], dt: float) -> FloatArray:
        """Advance exactly for a piecewise-constant target, avoiding Euler jumps."""
        target_array = _numeric_vector("target", target, self.position.size)
        if not math.isfinite(dt) or dt <= 0.0:
            raise ValueError("dt must be finite and positive")
        error = self.position - target_array
        coupled = self.velocity + self.omega0 * error
        decay = math.exp(-self.omega0 * dt)
        self.position = target_array + (error + coupled * dt) * decay
        self.velocity = (self.velocity - self.omega0 * coupled * dt) * decay
        return self.position.copy()

    def reset(self, position: Sequence[float], velocity: Sequence[float] | None = None) -> None:
        new_position = _numeric_vector("position", position, self.position.size)
        new_velocity = (
            np.zeros_like(new_position)
            if velocity is None
            else _numeric_vector("velocity", velocity, self.position.size)
        )
        self.position = new_position.copy()
        self.velocity = new_velocity.copy()


@dataclass(frozen=True, slots=True)
class BodyMorphology:
    """Semantic readout of the complete 24-value body-morphology vector."""

    coefficients: tuple[float, ...]

    def __post_init__(self) -> None:
        values = _numeric_vector("morphology", self.coefficients, MORPHOLOGY_DIM)
        object.__setattr__(self, "coefficients", tuple(float(value) for value in values))

    @property
    def bone_lengths(self) -> tuple[float, ...]:
        return canonical_bone_lengths(self.coefficients)

    @property
    def stature_m(self) -> float:
        lengths = self.bone_lengths
        return lengths[0] + lengths[1] + lengths[2] + lengths[10] + lengths[11]

    @property
    def shoulder_width_m(self) -> float:
        return self.bone_lengths[3] + self.bone_lengths[6]

    @property
    def muscle_mass_index(self) -> float:
        return 0.5 + 0.5 * math.tanh(self.coefficients[8])

    @property
    def body_fat_index(self) -> float:
        return 0.5 + 0.5 * math.tanh(self.coefficients[9])

    @property
    def spine_curvature_m(self) -> float:
        return 0.04 * math.tanh(self.coefficients[18])

    @property
    def shoulder_slope_radians(self) -> float:
        return 0.18 * math.tanh(self.coefficients[20])

    @property
    def neck_scale(self) -> float:
        return 1.0 + 0.12 * math.tanh(self.coefficients[10])

    @property
    def upper_arm_scale(self) -> float:
        return 1.0 + 0.12 * math.tanh(self.coefficients[11])

    @property
    def forearm_scale(self) -> float:
        return 1.0 + 0.12 * math.tanh(self.coefficients[12])

    @property
    def thigh_scale(self) -> float:
        return 1.0 + 0.12 * math.tanh(self.coefficients[13])

    @property
    def shin_scale(self) -> float:
        return 1.0 + 0.12 * math.tanh(self.coefficients[14])

    @property
    def chest_width_m(self) -> float:
        return 0.44 * (1.0 + 0.08 * math.tanh(self.coefficients[16]))

    @property
    def waist_width_m(self) -> float:
        return 0.32 * (1.0 + 0.08 * math.tanh(self.coefficients[17]))

    @property
    def hip_width_m(self) -> float:
        return 2.0 * self.bone_lengths[9]

    @property
    def stance_angle_radians(self) -> float:
        return 0.12 * math.tanh(self.coefficients[22])

    @property
    def posture_tilt_radians(self) -> float:
        return 0.10 * math.tanh(self.coefficients[23])


def rigid_transform(
    rotation: Sequence[Sequence[float]], translation: Sequence[float]
) -> FloatArray:
    """Build and validate an element of SE(3) as a homogeneous 4x4 matrix."""
    rotation_array = np.asarray(rotation, dtype=np.float64)
    translation_array = _numeric_vector("translation", translation, 3)
    if rotation_array.shape != (3, 3) or not np.isfinite(rotation_array).all():
        raise ValueError("rotation must be a finite 3x3 matrix")
    if not np.allclose(rotation_array.T @ rotation_array, np.eye(3), atol=1e-8):
        raise ValueError("rotation must be orthonormal")
    if not math.isclose(float(np.linalg.det(rotation_array)), 1.0, abs_tol=1e-8):
        raise ValueError("rotation determinant must be +1")
    matrix = np.eye(4, dtype=np.float64)
    matrix[:3, :3] = rotation_array
    matrix[:3, 3] = translation_array
    return matrix


@dataclass(frozen=True, slots=True)
class SkeletonPose:
    """World-space joint positions and per-joint homogeneous transforms."""

    positions: FloatArray
    transforms: FloatArray
    bone_lengths: FloatArray

    def __post_init__(self) -> None:
        positions = np.array(self.positions, dtype=np.float64, copy=True)
        transforms = np.array(self.transforms, dtype=np.float64, copy=True)
        lengths = np.array(self.bone_lengths, dtype=np.float64, copy=True)
        if positions.shape != (18, 3) or not np.isfinite(positions).all():
            raise ValueError("positions must be a finite (18, 3) array")
        if transforms.shape != (18, 4, 4) or not np.isfinite(transforms).all():
            raise ValueError("transforms must be a finite (18, 4, 4) array")
        if lengths.shape != (17,) or not np.isfinite(lengths).all():
            raise ValueError("bone_lengths must be a finite 17-vector")
        positions.flags.writeable = False
        transforms.flags.writeable = False
        lengths.flags.writeable = False
        object.__setattr__(self, "positions", positions)
        object.__setattr__(self, "transforms", transforms)
        object.__setattr__(self, "bone_lengths", lengths)


class Skeleton3D:
    """18-joint kinematic tree with fixed-length SE(3) bone transforms."""

    def __init__(self, morphology: Sequence[float]) -> None:
        values = _numeric_vector("morphology", morphology, MORPHOLOGY_DIM)
        self._bone_lengths = np.asarray(
            canonical_bone_lengths(tuple(float(value) for value in values)), dtype=np.float64
        )
        self._bone_lengths.flags.writeable = False
        self._rest_directions = _REST_DIRECTIONS.copy()
        spine_curve = 0.04 * math.tanh(float(values[18]))
        posture_tilt = 0.10 * math.tanh(float(values[23]))
        shoulder_slope = 0.18 * math.tanh(float(values[20]))
        stance_angle = 0.12 * math.tanh(float(values[22]))
        self._rest_directions[1] = (0.0, spine_curve, 1.0)
        self._rest_directions[2] = (0.0, spine_curve, 1.0)
        self._rest_directions[3] = (0.0, posture_tilt, 1.0)
        self._rest_directions[4] = (-1.0, 0.0, -shoulder_slope)
        self._rest_directions[7] = (1.0, 0.0, -shoulder_slope)
        self._rest_directions[10] = (-1.0, 0.0, -stance_angle)
        self._rest_directions[14] = (1.0, 0.0, -stance_angle)
        for joint in range(1, 18):
            self._rest_directions[joint] /= np.linalg.norm(self._rest_directions[joint])
        self._rest_directions.flags.writeable = False

    @property
    def bone_lengths(self) -> FloatArray:
        return self._bone_lengths.copy()

    def pose(
        self,
        local_rotations: Sequence[Sequence[Sequence[float]]] | None = None,
        *,
        root_translation: Sequence[float] = (0.0, 0.0, 0.0),
    ) -> SkeletonPose:
        """Compose local rigid transforms while preserving every segment length."""
        root = _numeric_vector("root_translation", root_translation, 3)
        if local_rotations is None:
            rotations = np.repeat(np.eye(3, dtype=np.float64)[None, :, :], 18, axis=0)
        else:
            try:
                rotations = np.asarray(local_rotations, dtype=np.float64)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("local_rotations must be numeric") from exc
            if rotations.shape != (18, 3, 3) or not np.isfinite(rotations).all():
                raise ValueError("local_rotations must have shape (18, 3, 3)")
            for rotation in rotations:
                if not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-8):
                    raise ValueError("each local rotation must be orthonormal")
                if not math.isclose(float(np.linalg.det(rotation)), 1.0, abs_tol=1e-8):
                    raise ValueError("each local rotation determinant must be +1")
        positions = np.zeros((18, 3), dtype=np.float64)
        world_rotations = np.zeros((18, 3, 3), dtype=np.float64)
        positions[0] = root
        world_rotations[0] = rotations[0]
        for joint in range(1, 18):
            parent = PARENT_INDICES[joint]
            world_rotations[joint] = world_rotations[parent] @ rotations[joint]
            direction = world_rotations[parent] @ self._rest_directions[joint]
            positions[joint] = positions[parent] + direction * self._bone_lengths[joint - 1]
        transforms = np.zeros((18, 4, 4), dtype=np.float64)
        transforms[:, 3, 3] = 1.0
        transforms[:, :3, :3] = world_rotations
        transforms[:, :3, 3] = positions
        measured = np.asarray(
            [
                np.linalg.norm(positions[joint] - positions[PARENT_INDICES[joint]])
                for joint in range(1, 18)
            ],
            dtype=np.float64,
        )
        return SkeletonPose(positions, transforms, measured)
