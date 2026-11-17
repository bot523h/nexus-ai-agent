"""Deterministic character IK, stance pinning, Verlet chains, and shutter blur."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import cast

import numpy as np
from numpy.typing import NDArray

from .contracts import KernelError, RationalTimestamp

FloatArray = NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class IKReceipt:
    joint_positions: tuple[tuple[float, float, float], ...]
    joint_angles_radians: tuple[float, ...]
    target_residual_m: float
    reachable: bool
    clamped: bool
    iterations: int


@dataclass(frozen=True, slots=True)
class FootLockReceipt:
    positions: tuple[tuple[float, float, float], ...]
    max_foot_slip_mm_before: float
    max_foot_slip_mm_after: float
    locked_frame_count: int


@dataclass(frozen=True, slots=True)
class VerletReceipt:
    positions: tuple[tuple[tuple[float, float, float], ...], ...]
    max_segment_strain: float
    rest_lengths_m: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class ShutterBlurReceipt:
    image: FloatArray
    shutter_fraction: float
    energy_before: float
    energy_after: float
    energy_relative_error: float


def _vector3(value: object, name: str) -> FloatArray:
    result = np.asarray(value, dtype=np.float64)
    if result.shape != (3,) or not np.all(np.isfinite(result)):
        raise KernelError(f"{name} must be a finite 3D vector")
    return result


def solve_two_bone_or_chain_ik(
    root_position: FloatArray,
    segment_lengths_m: tuple[float, ...] | list[float],
    target_position: FloatArray,
    *,
    joint_angle_limits_rad: tuple[float, float] | tuple[tuple[float, float], ...] = (
        -math.pi,
        math.pi,
    ),
    clamp_unreachable: bool = False,
    damping: float = 1e-3,
    max_iterations: int = 200,
    tolerance_m: float = 1e-7,
) -> IKReceipt:
    """Solve a planar articulated chain with bounded DLS updates in a target plane.

    Joint variables are relative bend angles. A single pair of limits applies to
    every joint; alternatively provide one ``(min, max)`` pair per joint.
    """
    root, target = (
        _vector3(root_position, "root_position"),
        _vector3(target_position, "target_position"),
    )
    lengths = np.asarray(segment_lengths_m, dtype=np.float64)
    if (
        lengths.ndim != 1
        or lengths.size < 2
        or np.any(~np.isfinite(lengths))
        or np.any(lengths <= 0)
        or damping <= 0
        or max_iterations < 1
        or tolerance_m <= 0
    ):
        raise KernelError("IK requires positive bone lengths and valid solver controls")
    if len(joint_angle_limits_rad) == 2 and isinstance(joint_angle_limits_rad[0], (int, float)):
        bounds = np.broadcast_to(np.asarray(joint_angle_limits_rad, dtype=float), (len(lengths), 2))
    else:
        bounds = np.asarray(joint_angle_limits_rad, dtype=float)
    if (
        bounds.shape != (len(lengths), 2)
        or np.any(~np.isfinite(bounds))
        or np.any(bounds[:, 0] > bounds[:, 1])
    ):
        raise KernelError("joint angle limits must be ordered finite pairs")

    displacement = target - root
    target_distance = float(np.linalg.norm(displacement))
    reach = float(lengths.sum())
    clamped = target_distance > reach
    if clamped and not clamp_unreachable:
        raise KernelError("effector target exceeds total chain reach")
    direction = (
        displacement / target_distance if target_distance > 1e-12 else np.array([1.0, 0.0, 0.0])
    )
    effective_distance = min(target_distance, reach * (1.0 - 1e-12))
    effective_target = direction * effective_distance
    plane_normal = np.array([0.0, 0.0, 1.0])
    if abs(float(np.dot(direction, plane_normal))) > 0.95:
        plane_normal = np.array([0.0, 1.0, 0.0])
    side = np.cross(plane_normal, direction)
    side /= max(float(np.linalg.norm(side)), 1e-15)
    axis = direction
    target_2d = np.array(
        [float(np.dot(effective_target, axis)), float(np.dot(effective_target, side))]
    )

    angles = np.full(len(lengths), 0.025, dtype=np.float64)
    if len(lengths) == 2:
        cosine = np.clip(
            (target_2d @ target_2d - lengths[0] ** 2 - lengths[1] ** 2)
            / (2 * lengths[0] * lengths[1]),
            -1.0,
            1.0,
        )
        second = math.acos(float(cosine))
        first = math.atan2(target_2d[1], target_2d[0]) - math.atan2(
            lengths[1] * math.sin(second), lengths[0] + lengths[1] * math.cos(second)
        )
        angles[:] = (first, second)
    angles = np.clip(angles, bounds[:, 0], bounds[:, 1])

    def forward(q: FloatArray) -> tuple[FloatArray, FloatArray]:
        absolute = np.cumsum(q)
        endpoints = np.cumsum(
            np.column_stack((lengths * np.cos(absolute), lengths * np.sin(absolute))), axis=0
        )
        jacobian = np.zeros((2, len(lengths)), dtype=np.float64)
        for joint in range(len(lengths)):
            for bone in range(joint, len(lengths)):
                jacobian[:, joint] += lengths[bone] * np.array(
                    [-math.sin(absolute[bone]), math.cos(absolute[bone])]
                )
        return endpoints[-1], jacobian

    iterations = 0
    for iteration in range(1, max_iterations + 1):
        iterations = iteration
        effector, jacobian = forward(angles)
        error = target_2d - effector
        if float(np.linalg.norm(error)) <= tolerance_m:
            break
        update = jacobian.T @ np.linalg.solve(jacobian @ jacobian.T + damping**2 * np.eye(2), error)
        update = np.clip(update, -0.25, 0.25)
        angles = np.clip(angles + update, bounds[:, 0], bounds[:, 1])
    effector, _ = forward(angles)
    joints = [root.copy()]
    absolute_angles = np.cumsum(angles)
    for length, angle_value in zip(lengths, absolute_angles, strict=True):
        joints.append(
            joints[-1]
            + length * math.cos(angle_value) * axis
            + length * math.sin(angle_value) * side
        )
    actual_effector = joints[-1]
    residual = float(np.linalg.norm(target - actual_effector))
    return IKReceipt(
        cast(
            tuple[tuple[float, float, float], ...],
            tuple(tuple(float(v) for v in point) for point in joints),
        ),
        cast(tuple[float, ...], tuple(float(v) for v in angles)),
        residual,
        residual <= tolerance_m,
        clamped,
        iterations,
    )


def enforce_stance_foot_lock(
    timestamps: tuple[RationalTimestamp, ...] | list[RationalTimestamp],
    ankle_positions_m: FloatArray,
    stance_contact: NDArray[np.bool_],
    *,
    transition_frames: int = 2,
) -> FootLockReceipt:
    """Pin horizontal XY foot coordinates through stance with smooth swing ramps."""
    points = np.asarray(ankle_positions_m, dtype=np.float64)
    stance = np.asarray(stance_contact, dtype=bool)
    if (
        points.ndim != 2
        or points.shape[1] != 3
        or points.shape[0] != len(timestamps)
        or stance.shape != (len(timestamps),)
        or len(timestamps) == 0
        or transition_frames < 0
        or np.any(~np.isfinite(points))
    ):
        raise KernelError("foot lock requires matching finite N x 3 positions and contacts")
    seconds = [stamp.seconds for stamp in timestamps]
    if any(current <= previous for previous, current in zip(seconds, seconds[1:], strict=False)):
        raise KernelError("foot lock timestamps must be strictly increasing")
    if any(stamp.time_base != timestamps[0].time_base for stamp in timestamps):
        raise KernelError("foot lock timestamps must share a time base")

    result = points.copy()
    max_before = 0.0
    index = 0
    while index < len(stance):
        if not stance[index]:
            index += 1
            continue
        start = index
        while index + 1 < len(stance) and stance[index + 1]:
            index += 1
        end = index
        anchor = points[start, :2].copy()
        if end > start:
            slips = np.linalg.norm(np.diff(points[start : end + 1, :2], axis=0), axis=1)
            max_before = max(max_before, float(slips.max(initial=0.0)))
        result[start : end + 1, :2] = anchor
        if transition_frames:
            before_start = max(0, start - transition_frames)
            for frame in range(before_start, start):
                t = (frame - before_start + 1) / (start - before_start + 1)
                weight = 3 * t**2 - 2 * t**3
                result[frame, :2] = (1 - weight) * points[frame, :2] + weight * anchor
            after_end = min(len(stance) - 1, end + transition_frames)
            for frame in range(end + 1, after_end + 1):
                t = (frame - end) / (after_end - end + 1)
                weight = 3 * t**2 - 2 * t**3
                result[frame, :2] = (1 - weight) * anchor + weight * points[frame, :2]
        index += 1
    max_after = 0.0
    for frame in range(len(stance) - 1):
        if stance[frame] and stance[frame + 1]:
            slip = float(np.linalg.norm(result[frame + 1, :2] - result[frame, :2]))
            max_after = max(max_after, slip)
    return FootLockReceipt(
        cast(
            tuple[tuple[float, float, float], ...],
            tuple(tuple(float(v) for v in p) for p in result),
        ),
        max_before * 1000.0,
        max_after * 1000.0,
        int(np.count_nonzero(stance)),
    )


def simulate_secondary_verlet_chain(
    timestamps: tuple[RationalTimestamp, ...] | list[RationalTimestamp],
    root_positions_m: FloatArray,
    initial_chain_positions_m: FloatArray,
    *,
    rest_lengths_m: tuple[float, ...] | list[float] | None = None,
    gravity_m_s2: tuple[float, ...] = (0.0, 0.0, -9.81),
    wind_m_s2: tuple[float, ...] = (0.0, 0.0, 0.0),
    drag: float = 0.02,
    constraint_iterations: int = 8,
) -> VerletReceipt:
    """Integrate a tethered chain with Verlet prediction and exact PBD projection."""
    roots = np.asarray(root_positions_m, dtype=np.float64)
    initial = np.asarray(initial_chain_positions_m, dtype=np.float64)
    if (
        roots.shape != (len(timestamps), 3)
        or initial.ndim != 2
        or initial.shape[1] != 3
        or len(initial) < 2
        or len(timestamps) < 1
        or np.any(~np.isfinite(roots))
        or np.any(~np.isfinite(initial))
        or not 0 <= drag < 1
        or constraint_iterations < 1
    ):
        raise KernelError("invalid Verlet chain, root path, or solver controls")
    if any(
        current.seconds <= previous.seconds
        for previous, current in zip(timestamps, timestamps[1:], strict=False)
    ):
        raise KernelError("Verlet timestamps must be strictly increasing")
    lengths = (
        np.linalg.norm(np.diff(initial, axis=0), axis=1)
        if rest_lengths_m is None
        else np.asarray(rest_lengths_m, dtype=np.float64)
    )
    if (
        lengths.shape != (len(initial) - 1,)
        or np.any(~np.isfinite(lengths))
        or np.any(lengths <= 0)
    ):
        raise KernelError("one positive rest length is required for every chain segment")
    gravity = _vector3(gravity_m_s2, "gravity_m_s2")
    wind = _vector3(wind_m_s2, "wind_m_s2")
    shift = roots[0] - initial[0]
    current = initial + shift
    current[0] = roots[0]
    for segment, rest_length in enumerate(lengths):
        delta = current[segment + 1] - current[segment]
        distance = float(np.linalg.norm(delta))
        if distance <= 1e-12:
            delta = initial[segment + 1] - initial[segment]
            distance = float(np.linalg.norm(delta))
        if distance <= 1e-12:
            delta, distance = np.array([0.0, 0.0, 1.0]), 1.0
        current[segment + 1] = current[segment] + delta / distance * rest_length
    previous = current.copy()
    history = [current.copy()]

    for frame in range(1, len(timestamps)):
        dt = float(timestamps[frame].seconds - timestamps[frame - 1].seconds)
        if dt <= 0:
            raise KernelError("Verlet frame duration must be positive")
        velocity = (current - previous) * (1 - drag)
        predicted = current + velocity + (gravity + wind) * dt**2
        predicted[0] = roots[frame]
        previous, current = current, predicted
        for _ in range(constraint_iterations):
            for segment in range(len(lengths)):
                delta = current[segment + 1] - current[segment]
                distance = float(np.linalg.norm(delta))
                if distance <= 1e-12:
                    delta = history[-1][segment + 1] - history[-1][segment]
                    distance = float(np.linalg.norm(delta))
                if distance <= 1e-12:
                    delta, distance = np.array([1.0, 0.0, 0.0]), 1.0
                correction = delta * ((distance - lengths[segment]) / distance)
                if segment == 0:
                    current[segment + 1] -= correction
                else:
                    current[segment] += 0.5 * correction
                    current[segment + 1] -= 0.5 * correction
            current[0] = roots[frame]
        # A final forward projection makes each segment length exact in the
        # anchored chain, avoiding a solver-iteration-dependent strain claim.
        for segment, rest_length in enumerate(lengths):
            delta = current[segment + 1] - current[segment]
            distance = float(np.linalg.norm(delta))
            if distance <= 1e-12:
                delta, distance = history[-1][segment + 1] - history[-1][segment], 1.0
            if float(np.linalg.norm(delta)) <= 1e-12:
                delta = np.array([1.0, 0.0, 0.0])
            else:
                delta = delta / distance
            current[segment + 1] = current[segment] + delta * rest_length
        history.append(current.copy())

    strain = 0.0
    for frame_points in history:
        lengths_actual = np.linalg.norm(np.diff(frame_points, axis=0), axis=1)
        strain = max(strain, float(np.max(np.abs(lengths_actual - lengths) / lengths)))
    nested = tuple(
        cast(
            tuple[tuple[float, float, float], ...], tuple(tuple(float(v) for v in p) for p in frame)
        )
        for frame in history
    )
    return VerletReceipt(nested, strain, cast(tuple[float, ...], tuple(float(v) for v in lengths)))


def _periodic_fractional_shift(image: FloatArray, dx: float, dy: float) -> FloatArray:
    """Bilinearly translate an image on a periodic domain (sum preserving)."""
    height, width, _ = image.shape
    y, x = np.mgrid[0:height, 0:width]
    source_x, source_y = (x - dx) % width, (y - dy) % height
    x0, y0 = np.floor(source_x).astype(int), np.floor(source_y).astype(int)
    x1, y1 = (x0 + 1) % width, (y0 + 1) % height
    wx, wy = source_x - x0, source_y - y0
    return np.asarray(
        image[y0, x0] * ((1 - wx) * (1 - wy))[..., None]
        + image[y0, x1] * (wx * (1 - wy))[..., None]
        + image[y1, x0] * ((1 - wx) * wy)[..., None]
        + image[y1, x1] * (wx * wy)[..., None],
        dtype=np.float64,
    )


def render_shutter_motion_blur_linear(
    scene_linear_layer: FloatArray,
    velocity_px_per_frame: tuple[float, float],
    *,
    shutter_angle_deg: float = 180.0,
    subframe_samples: int = 9,
) -> ShutterBlurReceipt:
    """Average scene-linear, sub-frame shifted radiance over a rotary shutter."""
    source = np.asarray(scene_linear_layer, dtype=np.float64)
    velocity = np.asarray(velocity_px_per_frame, dtype=np.float64)
    if (
        source.ndim != 3
        or source.shape[2] not in (1, 3, 4)
        or velocity.shape != (2,)
        or np.any(~np.isfinite(source))
        or np.any(source < 0)
        or np.any(~np.isfinite(velocity))
        or not 0 < shutter_angle_deg <= 360
        or not np.isfinite(shutter_angle_deg)
        or subframe_samples < 1
    ):
        raise KernelError("invalid shutter-blur layer, motion, or exposure controls")
    fraction = shutter_angle_deg / 360.0
    offsets = np.linspace(-fraction / 2, fraction / 2, subframe_samples)
    accumulated = np.zeros_like(source)
    for offset in offsets:
        accumulated += _periodic_fractional_shift(
            source, float(velocity[0] * offset), float(velocity[1] * offset)
        )
    blurred = accumulated / subframe_samples
    energy_before = float(source.sum())
    energy_after = float(blurred.sum())
    relative_error = abs(energy_after - energy_before) / max(abs(energy_before), 1e-15)
    return ShutterBlurReceipt(blurred, fraction, energy_before, energy_after, relative_error)
