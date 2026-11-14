"""Continuous locomotion, analytic foot locking, and bounded Verlet cloth physics."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

import numpy as np
from numpy.typing import NDArray

__all__ = [
    "DEFAULT_GARMENT_ARCHIVE",
    "ClothMesh",
    "FootLock",
    "GarmentArchive",
    "GarmentCategory",
    "GarmentDefinition",
    "IKResult",
    "LocomotionMode",
    "LocomotionSample",
    "ParametricLocomotionManifold",
    "VerletClothSolver",
    "make_cloth_grid",
    "solve_two_bone_ik",
]

FloatArray = NDArray[np.float64]


class LocomotionMode(str, Enum):
    """Named motion families supported by the parametric locomotion manifold."""

    STAND = "standing_breathing"
    WALK = "walk"
    MARCH = "military_march"
    SPRINT = "sprint"
    SLOW_MOTION = "slow_motion"
    TURN_360 = "turn_360"
    SIT = "sit_down"
    RISE = "stand_up"
    JUMP = "jump"
    DODGE = "dodge"
    COMBAT = "combat"


class GarmentCategory(str, Enum):
    """Cultural, historical, contemporary, tactical, and science-fiction groups."""

    IRANIAN_TRADITIONAL = "iranian_traditional"
    HISTORICAL = "historical"
    REGIONAL = "regional"
    FORMAL = "formal"
    MODERN = "modern"
    TACTICAL = "tactical"
    ACTION = "action"
    SCI_FI = "sci_fi"


def _vec3(name: str, value: Sequence[float] | NDArray[np.float64]) -> FloatArray:
    if isinstance(value, (str, bytes)):
        raise ValueError(f"{name} must be a finite 3-vector")
    try:
        result = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite 3-vector") from exc
    if result.shape != (3,) or not np.isfinite(result).all():
        raise ValueError(f"{name} must be a finite 3-vector")
    return result


def _tuple3(vector: Sequence[float] | NDArray[np.float64]) -> tuple[float, float, float]:
    return (float(vector[0]), float(vector[1]), float(vector[2]))


def _unit(vector: FloatArray, fallback: FloatArray | None = None) -> FloatArray:
    length = float(np.linalg.norm(vector))
    if length > 1e-12:
        return vector / length
    return np.asarray((0.0, 0.0, -1.0) if fallback is None else fallback, dtype=np.float64)


@dataclass(frozen=True, slots=True)
class IKResult:
    """Knee/elbow location and reachable endpoint from the cosine-law solver."""

    joint: tuple[float, float, float]
    end: tuple[float, float, float]
    reached: bool
    endpoint_error: float


def solve_two_bone_ik(
    root: Sequence[float] | NDArray[np.float64],
    target: Sequence[float] | NDArray[np.float64],
    upper_length: float,
    lower_length: float,
    *,
    pole_direction: Sequence[float] = (0.0, 1.0, 0.0),
) -> IKResult:
    """Analytical two-link IK using the law of cosines and a pole plane.

    Reachable targets are reproduced to floating-point precision.  An
    unreachable target is projected onto the nearest legal reach sphere.
    """
    origin = _vec3("root", root)
    destination = _vec3("target", target)
    pole = _vec3("pole_direction", pole_direction)
    if (
        not math.isfinite(upper_length)
        or not math.isfinite(lower_length)
        or upper_length <= 0.0
        or lower_length <= 0.0
    ):
        raise ValueError("bone lengths must be finite and positive")
    vector = destination - origin
    distance = float(np.linalg.norm(vector))
    direction = _unit(vector)
    minimum = abs(upper_length - lower_length) + 1e-12
    maximum = upper_length + lower_length - 1e-12
    reachable_distance = min(max(distance, minimum), maximum)
    along = (upper_length**2 - lower_length**2 + reachable_distance**2) / (2.0 * reachable_distance)
    perpendicular_distance = math.sqrt(max(upper_length**2 - along**2, 0.0))
    perpendicular = pole - float(np.dot(pole, direction)) * direction
    if float(np.linalg.norm(perpendicular)) <= 1e-12:
        alternate = np.asarray((1.0, 0.0, 0.0), dtype=np.float64)
        if abs(float(np.dot(alternate, direction))) > 0.9:
            alternate = np.asarray((0.0, 1.0, 0.0), dtype=np.float64)
        perpendicular = alternate - float(np.dot(alternate, direction)) * direction
    perpendicular = _unit(perpendicular, np.asarray((0.0, 1.0, 0.0), dtype=np.float64))
    reachable_end = origin + direction * reachable_distance
    joint = origin + direction * along + perpendicular * perpendicular_distance
    error = float(np.linalg.norm(reachable_end - destination))
    return IKResult(
        joint=_tuple3(joint),
        end=_tuple3(reachable_end),
        reached=error <= 1e-9,
        endpoint_error=error,
    )


class FootLock:
    """Stateful zero-skate contact anchor for one foot during a stance phase."""

    def __init__(self) -> None:
        self._anchor: FloatArray | None = None
        self._in_stance = False

    @property
    def anchor(self) -> tuple[float, float, float] | None:
        if self._anchor is None:
            return None
        return _tuple3(self._anchor)

    def reset(self) -> None:
        self._anchor = None
        self._in_stance = False

    def update(
        self,
        *,
        in_stance: bool,
        touchdown_position: Sequence[float] | NDArray[np.float64],
        swing_position: Sequence[float] | NDArray[np.float64],
    ) -> tuple[float, float, float]:
        """Capture a world-space heel point on touchdown and hold it exactly."""
        touchdown = _vec3("touchdown_position", touchdown_position)
        swing = _vec3("swing_position", swing_position)
        if in_stance:
            if not self._in_stance or self._anchor is None:
                self._anchor = touchdown.copy()
            self._in_stance = True
            selected = self._anchor
        else:
            self._in_stance = False
            self._anchor = None
            selected = swing
        return _tuple3(selected)


@dataclass(frozen=True, slots=True)
class LocomotionSample:
    """One time sample from a motion mode, including analytical leg targets."""

    mode: LocomotionMode
    time_seconds: float
    root_position: tuple[float, float, float]
    heading_radians: float
    left_foot: tuple[float, float, float]
    right_foot: tuple[float, float, float]
    left_knee: tuple[float, float, float]
    right_knee: tuple[float, float, float]
    left_in_stance: bool
    right_in_stance: bool
    gait_phase: float
    body_bob_m: float


_MODE_SPEEDS: dict[LocomotionMode, float] = {
    LocomotionMode.STAND: 0.0,
    LocomotionMode.WALK: 1.35,
    LocomotionMode.MARCH: 1.15,
    LocomotionMode.SPRINT: 5.8,
    LocomotionMode.SLOW_MOTION: 0.55,
    LocomotionMode.TURN_360: 0.0,
    LocomotionMode.SIT: 0.0,
    LocomotionMode.RISE: 0.0,
    LocomotionMode.JUMP: 0.0,
    LocomotionMode.DODGE: 0.0,
    LocomotionMode.COMBAT: 0.8,
}
_MODE_FREQUENCIES: dict[LocomotionMode, float] = {
    LocomotionMode.WALK: 1.65,
    LocomotionMode.MARCH: 1.45,
    LocomotionMode.SPRINT: 2.65,
    LocomotionMode.SLOW_MOTION: 0.72,
    LocomotionMode.COMBAT: 1.25,
}


class ParametricLocomotionManifold:
    """Compact deterministic gait synthesizer for cinematic/game-style actions."""

    def __init__(
        self, *, stride_length: float = 1.25, leg_lengths: tuple[float, float] = (0.43, 0.42)
    ) -> None:
        if not math.isfinite(stride_length) or stride_length <= 0.0:
            raise ValueError("stride_length must be finite and positive")
        if len(leg_lengths) != 2 or any(
            not math.isfinite(length) or length <= 0.0 for length in leg_lengths
        ):
            raise ValueError("leg_lengths must contain two finite positive lengths")
        self.stride_length = stride_length
        self.upper_leg_length, self.lower_leg_length = leg_lengths
        self._foot_locks = {"left": FootLock(), "right": FootLock()}
        self._last_gait_mode: LocomotionMode | None = None

    def reset(self) -> None:
        """Clear support contacts when beginning a new, discontinuous take."""
        for lock in self._foot_locks.values():
            lock.reset()
        self._last_gait_mode = None

    def sample(
        self,
        mode: LocomotionMode | str,
        time_seconds: float,
        *,
        speed: float | None = None,
        heading_radians: float = 0.0,
        phase_offset: float = 0.0,
        root_height: float = 0.92,
    ) -> LocomotionSample:
        """Evaluate a continuous action and locked foot contacts at time ``t``."""
        try:
            selected_mode = mode if isinstance(mode, LocomotionMode) else LocomotionMode(mode)
        except ValueError as exc:
            raise ValueError(f"unsupported locomotion mode: {mode}") from exc
        values = (time_seconds, heading_radians, phase_offset, root_height)
        if not all(math.isfinite(value) for value in values) or time_seconds < 0.0:
            raise ValueError("time and pose parameters must be finite; time must be non-negative")
        movement_speed = _MODE_SPEEDS[selected_mode] if speed is None else speed
        if not math.isfinite(movement_speed) or movement_speed < 0.0:
            raise ValueError("speed must be finite and non-negative")
        effective_time = time_seconds * (
            0.35 if selected_mode is LocomotionMode.SLOW_MOTION else 1.0
        )
        heading = heading_radians
        if selected_mode is LocomotionMode.TURN_360:
            heading += 2.0 * math.pi * time_seconds / 4.0
        forward = np.asarray((math.cos(heading), math.sin(heading), 0.0), dtype=np.float64)
        lateral = np.asarray((-math.sin(heading), math.cos(heading), 0.0), dtype=np.float64)
        travel = movement_speed * effective_time
        root = forward * travel
        root[2] = root_height
        if selected_mode is LocomotionMode.SIT:
            root[2] -= 0.36 * _smoothstep(min(time_seconds, 1.0))
        elif selected_mode is LocomotionMode.RISE:
            root[2] -= 0.36 * (1.0 - _smoothstep(min(time_seconds, 1.0)))
        elif selected_mode is LocomotionMode.JUMP:
            jump_phase = (time_seconds % 1.2) / 1.2
            root[2] += 0.78 * math.sin(math.pi * jump_phase)
        elif selected_mode is LocomotionMode.DODGE:
            dodge_phase = min(time_seconds / 0.55, 1.0)
            root += lateral * (0.48 * math.sin(math.pi * dodge_phase))

        frequency = _MODE_FREQUENCIES.get(selected_mode, 0.0)
        gait_phase = (2.0 * math.pi * frequency * effective_time + phase_offset) % (2.0 * math.pi)
        gait_active = frequency > 0.0
        if gait_active and selected_mode is not self._last_gait_mode:
            self.reset()
            self._last_gait_mode = selected_mode
        elif not gait_active:
            self.reset()

        stride = self.stride_length
        duty = 0.62 if selected_mode is not LocomotionMode.SPRINT else 0.48
        left_phase = gait_phase
        right_phase = (gait_phase + math.pi) % (2.0 * math.pi)
        left_foot, left_stance = self._foot_target(
            "left", left_phase, duty, root, forward, lateral, stride, gait_active
        )
        right_foot, right_stance = self._foot_target(
            "right", right_phase, duty, root, forward, lateral, stride, gait_active
        )
        left_hip = root + np.asarray((-0.055, 0.0, -0.06), dtype=np.float64)
        right_hip = root + np.asarray((0.055, 0.0, -0.06), dtype=np.float64)
        left_ik = solve_two_bone_ik(
            left_hip, left_foot, self.upper_leg_length, self.lower_leg_length
        )
        right_ik = solve_two_bone_ik(
            right_hip, right_foot, self.upper_leg_length, self.lower_leg_length
        )
        if selected_mode is LocomotionMode.STAND:
            bob = 0.004 * math.sin(2.0 * math.pi * 0.25 * time_seconds)
        elif gait_active:
            bob = 0.025 * abs(math.sin(gait_phase))
        else:
            bob = 0.0
        return LocomotionSample(
            mode=selected_mode,
            time_seconds=time_seconds,
            root_position=_tuple3(root),
            heading_radians=heading,
            left_foot=left_foot,
            right_foot=right_foot,
            left_knee=left_ik.joint,
            right_knee=right_ik.joint,
            left_in_stance=left_stance,
            right_in_stance=right_stance,
            gait_phase=gait_phase,
            body_bob_m=bob,
        )

    def _foot_target(
        self,
        side: str,
        phase: float,
        duty: float,
        root: FloatArray,
        forward: FloatArray,
        lateral: FloatArray,
        stride: float,
        gait_active: bool,
    ) -> tuple[tuple[float, float, float], bool]:
        if not gait_active:
            self._foot_locks[side].reset()
            lateral_offset = -0.10 if side == "left" else 0.10
            foot = root + lateral * lateral_offset
            foot[2] = 0.0
            return _tuple3(foot), True
        cycle = phase / (2.0 * math.pi)
        in_stance = cycle < duty
        touchdown = root + forward * (stride * 0.5)
        touchdown += lateral * (-0.10 if side == "left" else 0.10)
        touchdown[2] = 0.0
        if in_stance:
            swing = touchdown
        else:
            progress = (cycle - duty) / (1.0 - duty)
            swing = root + forward * (stride * (progress - 0.5))
            swing += lateral * (-0.10 if side == "left" else 0.10)
            swing[2] = 0.12 * math.sin(math.pi * progress)
        target = self._foot_locks[side].update(
            in_stance=in_stance,
            touchdown_position=touchdown,
            swing_position=swing,
        )
        return target, in_stance


def _smoothstep(value: float) -> float:
    clamped = min(max(value, 0.0), 1.0)
    return clamped * clamped * (3.0 - 2.0 * clamped)


@dataclass(frozen=True, slots=True)
class GarmentDefinition:
    """Small physical/material descriptor; no heavyweight mesh is bundled."""

    name: str
    category: GarmentCategory
    width_m: float
    length_m: float
    damping: float
    wind_drag: float
    stiffness_iterations: int
    color_rgb: tuple[int, int, int]

    def __post_init__(self) -> None:
        dimensions = (self.width_m, self.length_m)
        if not self.name or not all(math.isfinite(value) and value > 0.0 for value in dimensions):
            raise ValueError("garment name and dimensions must be valid")
        if (
            not math.isfinite(self.damping)
            or not 0.0 <= self.damping < 1.0
            or not math.isfinite(self.wind_drag)
            or self.wind_drag < 0.0
        ):
            raise ValueError("garment damping or wind drag is invalid")
        if self.stiffness_iterations < 1:
            raise ValueError("stiffness_iterations must be positive")
        if len(self.color_rgb) != 3 or any(
            not isinstance(channel, int) or channel < 0 or channel > 255
            for channel in self.color_rgb
        ):
            raise ValueError("color_rgb must contain three byte channels")


_DEFAULT_GARMENTS: tuple[GarmentDefinition, ...] = (
    GarmentDefinition(
        "qabaye_shahnameh", GarmentCategory.HISTORICAL, 0.85, 1.30, 0.035, 0.018, 8, (116, 40, 37)
    ),
    GarmentDefinition(
        "iranian_traditional",
        GarmentCategory.IRANIAN_TRADITIONAL,
        0.78,
        1.05,
        0.055,
        0.025,
        7,
        (43, 96, 78),
    ),
    GarmentDefinition(
        "regional_balochi", GarmentCategory.REGIONAL, 0.82, 1.10, 0.06, 0.03, 7, (193, 151, 71)
    ),
    GarmentDefinition(
        "formal_suit", GarmentCategory.FORMAL, 0.72, 0.92, 0.12, 0.012, 9, (32, 43, 61)
    ),
    GarmentDefinition(
        "modern_coat", GarmentCategory.MODERN, 0.90, 1.18, 0.09, 0.02, 9, (70, 78, 86)
    ),
    GarmentDefinition(
        "service_uniform", GarmentCategory.FORMAL, 0.78, 1.00, 0.10, 0.016, 8, (52, 71, 59)
    ),
    GarmentDefinition(
        "tactical_armor", GarmentCategory.TACTICAL, 0.84, 0.86, 0.20, 0.009, 10, (53, 61, 52)
    ),
    GarmentDefinition(
        "action_cape", GarmentCategory.ACTION, 1.05, 1.42, 0.025, 0.055, 8, (94, 31, 39)
    ),
    GarmentDefinition(
        "orbital_suit", GarmentCategory.SCI_FI, 0.88, 1.08, 0.16, 0.014, 10, (116, 173, 190)
    ),
    GarmentDefinition(
        "sci_fi_cloak", GarmentCategory.SCI_FI, 1.12, 1.48, 0.02, 0.07, 9, (77, 59, 130)
    ),
)


class GarmentArchive:
    """In-memory parametric archive covering cultural and futuristic clothing."""

    def __init__(self, definitions: Sequence[GarmentDefinition] = _DEFAULT_GARMENTS) -> None:
        catalog = {definition.name: definition for definition in definitions}
        if not catalog:
            raise ValueError("garment archive must not be empty")
        if len(catalog) != len(definitions):
            raise ValueError("garment names must be unique")
        self._catalog = catalog

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._catalog))

    def get(self, name: str) -> GarmentDefinition:
        try:
            return self._catalog[name]
        except KeyError as exc:
            raise KeyError(f"unknown garment: {name}") from exc

    def by_category(self, category: GarmentCategory | str) -> tuple[GarmentDefinition, ...]:
        try:
            selected = (
                category if isinstance(category, GarmentCategory) else GarmentCategory(category)
            )
        except ValueError as exc:
            raise ValueError(f"unknown garment category: {category}") from exc
        return tuple(
            definition for definition in self._catalog.values() if definition.category is selected
        )

    def mesh(
        self,
        name: str,
        *,
        columns: int = 8,
        rows: int = 12,
        origin: Sequence[float] = (0.0, 0.0, 1.6),
    ) -> ClothMesh:
        definition = self.get(name)
        return make_cloth_grid(
            definition.width_m,
            definition.length_m,
            columns=columns,
            rows=rows,
            origin=origin,
        )


DEFAULT_GARMENT_ARCHIVE = GarmentArchive()


@dataclass(frozen=True, slots=True)
class ClothMesh:
    """Regular cloth grid, surface triangles, constraints, and fixed top edge."""

    vertices: FloatArray
    triangles: tuple[tuple[int, int, int], ...]
    constraints: tuple[tuple[int, int, float], ...]
    pinned_indices: tuple[int, ...]
    columns: int
    rows: int

    def __post_init__(self) -> None:
        vertices = np.array(self.vertices, dtype=np.float64, copy=True)
        if vertices.ndim != 2 or vertices.shape[1] != 3 or vertices.shape[0] < 4:
            raise ValueError("cloth vertices must have shape (n>=4, 3)")
        if not np.isfinite(vertices).all():
            raise ValueError("cloth vertices must be finite")
        count = vertices.shape[0]
        for triangle in self.triangles:
            if len(triangle) != 3 or any(index < 0 or index >= count for index in triangle):
                raise ValueError("cloth triangle index is out of bounds")
        for first, second, rest in self.constraints:
            if first < 0 or second < 0 or first >= count or second >= count or first == second:
                raise ValueError("cloth constraint index is invalid")
            if not math.isfinite(rest) or rest <= 0.0:
                raise ValueError("cloth rest length must be finite and positive")
        if any(index < 0 or index >= count for index in self.pinned_indices):
            raise ValueError("pinned vertex index is out of bounds")
        if self.columns < 2 or self.rows < 2 or self.columns * self.rows != count:
            raise ValueError("cloth grid dimensions do not match its vertices")
        vertices.flags.writeable = False
        object.__setattr__(self, "vertices", vertices)


def make_cloth_grid(
    width_m: float,
    length_m: float,
    *,
    columns: int = 8,
    rows: int = 12,
    origin: Sequence[float] = (0.0, 0.0, 1.6),
) -> ClothMesh:
    """Create a lightweight hanging grid with structural and shear constraints."""
    if (
        not math.isfinite(width_m)
        or not math.isfinite(length_m)
        or width_m <= 0.0
        or length_m <= 0.0
    ):
        raise ValueError("cloth width and length must be finite and positive")
    if columns < 2 or rows < 2:
        raise ValueError("cloth grid must have at least two rows and columns")
    anchor = _vec3("origin", origin)
    vertices: list[tuple[float, float, float]] = []
    for row in range(rows):
        z = anchor[2] - length_m * row / (rows - 1)
        for column in range(columns):
            x = anchor[0] - width_m / 2.0 + width_m * column / (columns - 1)
            vertices.append((float(x), float(anchor[1]), float(z)))
    triangles: list[tuple[int, int, int]] = []
    constraints: list[tuple[int, int, float]] = []

    def add_constraint(first: int, second: int) -> None:
        a = np.asarray(vertices[first], dtype=np.float64)
        b = np.asarray(vertices[second], dtype=np.float64)
        constraints.append((first, second, float(np.linalg.norm(b - a))))

    for row in range(rows):
        for column in range(columns):
            index = row * columns + column
            if column + 1 < columns:
                add_constraint(index, index + 1)
            if row + 1 < rows:
                below = index + columns
                add_constraint(index, below)
                if column + 1 < columns:
                    add_constraint(index, below + 1)
                    add_constraint(index + 1, below)
                    triangles.extend(((index, index + 1, below + 1), (index, below + 1, below)))
    pinned = tuple(range(columns))
    return ClothMesh(
        np.asarray(vertices, dtype=np.float64),
        tuple(triangles),
        tuple(constraints),
        pinned,
        columns,
        rows,
    )


class VerletClothSolver:
    """Position-based Verlet solver with pinning and Gauss-Seidel projection."""

    def __init__(
        self,
        positions: Sequence[Sequence[float]] | NDArray[np.float64],
        constraints: Sequence[tuple[int, int, float]],
        *,
        fixed_indices: Sequence[int] = (),
        damping: float = 0.03,
        gravity: Sequence[float] = (0.0, 0.0, -9.81),
        wind_drag: float = 0.02,
        iterations: int = 8,
        max_wind_acceleration: float = 250.0,
    ) -> None:
        try:
            current = np.asarray(positions, dtype=np.float64)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("positions must be a numeric (n, 3) array") from exc
        if current.ndim != 2 or current.shape[1] != 3 or current.shape[0] < 1:
            raise ValueError("positions must have shape (n>=1, 3)")
        if not np.isfinite(current).all():
            raise ValueError("positions must be finite")
        if not math.isfinite(damping) or not 0.0 <= damping < 1.0:
            raise ValueError("damping must be in [0, 1)")
        if not math.isfinite(wind_drag) or wind_drag < 0.0:
            raise ValueError("wind_drag must be finite and non-negative")
        if iterations < 1:
            raise ValueError("iterations must be positive")
        if not math.isfinite(max_wind_acceleration) or max_wind_acceleration <= 0.0:
            raise ValueError("max_wind_acceleration must be finite and positive")
        count = current.shape[0]
        pins = frozenset(int(index) for index in fixed_indices)
        if any(index < 0 or index >= count for index in pins):
            raise ValueError("fixed vertex index is out of bounds")
        checked_constraints: list[tuple[int, int, float]] = []
        for first, second, rest in constraints:
            if first < 0 or second < 0 or first >= count or second >= count or first == second:
                raise ValueError("constraint index is invalid")
            if not math.isfinite(rest) or rest <= 0.0:
                raise ValueError("constraint rest length must be finite and positive")
            checked_constraints.append((int(first), int(second), float(rest)))
        self._positions = current.copy()
        self._previous = current.copy()
        self._pins = pins
        self._pin_positions = current.copy()
        self._constraints = tuple(checked_constraints)
        self._damping = damping
        self._gravity = _vec3("gravity", gravity)
        self._wind_drag = wind_drag
        self._iterations = iterations
        self._max_wind_acceleration = max_wind_acceleration

    @classmethod
    def from_mesh(
        cls,
        mesh: ClothMesh,
        garment: GarmentDefinition,
        *,
        gravity: Sequence[float] = (0.0, 0.0, -9.81),
    ) -> VerletClothSolver:
        return cls(
            mesh.vertices,
            mesh.constraints,
            fixed_indices=mesh.pinned_indices,
            damping=garment.damping,
            gravity=gravity,
            wind_drag=garment.wind_drag,
            iterations=garment.stiffness_iterations,
        )

    @property
    def positions(self) -> FloatArray:
        """Copy current particle positions so callers cannot mutate solver state."""
        return self._positions.copy()

    @property
    def previous_positions(self) -> FloatArray:
        return self._previous.copy()

    @property
    def max_constraint_error(self) -> float:
        if not self._constraints:
            return 0.0
        return max(
            abs(float(np.linalg.norm(self._positions[second] - self._positions[first])) - rest)
            for first, second, rest in self._constraints
        )

    def update_pins(self, anchors: Mapping[int, Sequence[float] | NDArray[np.float64]]) -> None:
        """Move body-attached cloth vertices without injecting artificial velocity."""
        checked: dict[int, FloatArray] = {}
        for index, position in anchors.items():
            if isinstance(index, bool) or index not in self._pins:
                raise ValueError("only fixed cloth vertices may be moved as anchors")
            checked[index] = _vec3("anchor position", position).copy()
        for index, position in checked.items():
            self._pin_positions[index] = position
            self._positions[index] = position
            self._previous[index] = position

    def step(
        self,
        dt: float,
        *,
        wind: Sequence[float] = (0.0, 0.0, 0.0),
        body_acceleration: Sequence[float] = (0.0, 0.0, 0.0),
    ) -> FloatArray:
        """Integrate one stable step; wind acceleration is capped at a safe bound."""
        if not math.isfinite(dt) or dt <= 0.0 or dt > 0.1:
            raise ValueError("dt must be finite and in the interval (0, 0.1]")
        wind_vector = _vec3("wind", wind)
        body_force = _vec3("body_acceleration", body_acceleration)
        wind_speed = float(np.linalg.norm(wind_vector))
        wind_acceleration = wind_vector * (self._wind_drag * wind_speed)
        acceleration_size = float(np.linalg.norm(wind_acceleration))
        if acceleration_size > self._max_wind_acceleration:
            wind_acceleration *= self._max_wind_acceleration / acceleration_size
        total_acceleration = self._gravity + body_force + wind_acceleration

        current = self._positions.copy()
        velocity = self._positions - self._previous
        updated = (
            self._positions + (1.0 - self._damping) * velocity + total_acceleration * (dt * dt)
        )
        for index in self._pins:
            updated[index] = self._pin_positions[index]
        self._previous = current
        self._positions = updated
        wind_load = min(acceleration_size / self._max_wind_acceleration, 1.0)
        projection_iterations = self._iterations + int(math.ceil(40.0 * wind_load))
        for _ in range(projection_iterations):
            self._project_constraints()
        for index in self._pins:
            self._positions[index] = self._pin_positions[index]
            self._previous[index] = self._pin_positions[index]
        if not np.isfinite(self._positions).all():
            raise FloatingPointError("cloth integration produced non-finite positions")
        return self._positions.copy()

    def _project_constraints(self) -> None:
        for first, second, rest in self._constraints:
            delta = self._positions[second] - self._positions[first]
            distance = float(np.linalg.norm(delta))
            first_fixed = first in self._pins
            second_fixed = second in self._pins
            if distance <= 1e-12:
                if first_fixed and second_fixed:
                    continue
                direction = np.asarray((1.0, 0.0, 0.0), dtype=np.float64)
                if first_fixed:
                    self._positions[second] = self._positions[first] + direction * rest
                elif second_fixed:
                    self._positions[first] = self._positions[second] - direction * rest
                else:
                    self._positions[first] -= direction * (rest * 0.5)
                    self._positions[second] += direction * (rest * 0.5)
                continue
            correction = delta * ((distance - rest) / distance)
            if first_fixed and second_fixed:
                continue
            if first_fixed:
                self._positions[second] -= correction
            elif second_fixed:
                self._positions[first] += correction
            else:
                self._positions[first] += correction * 0.5
                self._positions[second] -= correction * 0.5
