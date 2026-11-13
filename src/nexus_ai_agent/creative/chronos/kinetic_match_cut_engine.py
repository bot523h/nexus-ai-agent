"""Sub-pixel motion, CMOS rolling-shutter, and match-on-action cut continuity."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from . import temporal_optics

FloatArray = NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class RollingShutterFrameReceipt:
    rectified_frame: FloatArray
    max_warp_px: float
    readout_fraction: float


@dataclass(frozen=True, slots=True)
class KineticCutReceipt:
    outgoing_frame_index: int
    incoming_frame_index: int
    selected_cost: float
    unaligned_cut_cost: float
    cost_reduction: float
    cost_matrix: FloatArray
    bridge_landmarks_3d: FloatArray
    bridge_velocities_3d_per_s: FloatArray
    bridge_duration_s: float


def estimate_subpixel_phase_translation(
    reference_luminance: FloatArray,
    moving_luminance: FloatArray,
    *,
    epsilon: float = 1e-12,
) -> temporal_optics.PhaseCorrelationReceipt:
    """Hann-windowed FFT phase correlation with three-point peak interpolation."""
    return temporal_optics.estimate_subpixel_translation_phase_corr(
        reference_luminance, moving_luminance, epsilon=epsilon
    )


def rectify_rolling_shutter_frame(
    frame: FloatArray,
    camera_velocity_px_per_frame: tuple[float, float],
    *,
    readout_fraction: float,
    frame_period_s: float,
) -> RollingShutterFrameReceipt:
    """Undo CMOS scanline skew from velocity-per-frame and normalized readout time."""
    velocity = np.asarray(camera_velocity_px_per_frame, dtype=np.float64)
    if (
        velocity.shape != (2,)
        or np.any(~np.isfinite(velocity))
        or not np.isfinite(readout_fraction)
        or not 0.0 <= readout_fraction <= 1.0
        or not np.isfinite(frame_period_s)
        or frame_period_s <= 0
    ):
        raise ValueError(
            "rolling-shutter controls require finite velocity, gamma in [0,1], and period > 0"
        )
    receipt = temporal_optics.rectify_rolling_shutter_skew(
        frame,
        (float(velocity[0] / frame_period_s), float(velocity[1] / frame_period_s)),
        readout_duration_s=readout_fraction * frame_period_s,
        frame_period_s=frame_period_s,
    )
    return RollingShutterFrameReceipt(receipt.frame, receipt.max_warp_px, readout_fraction)


def _validate_trajectory_pair(
    positions: FloatArray, velocities: FloatArray, label: str
) -> tuple[FloatArray, FloatArray]:
    pose = np.asarray(positions, dtype=np.float64)
    velocity = np.asarray(velocities, dtype=np.float64)
    if (
        pose.ndim != 3
        or pose.shape[0] == 0
        or pose.shape[1] == 0
        or pose.shape[2] != 3
        or velocity.shape != (pose.shape[0], pose.shape[1], 2)
        or np.any(~np.isfinite(pose))
        or np.any(~np.isfinite(velocity))
    ):
        raise ValueError(f"{label} trajectory requires finite TxLx3 landmarks and TxLx2 velocities")
    return pose, velocity


def _hermite_bridge(
    start_position: FloatArray,
    start_velocity: FloatArray,
    end_position: FloatArray,
    end_velocity: FloatArray,
    *,
    duration_s: float,
    sample_count: int,
) -> tuple[FloatArray, FloatArray]:
    if (
        duration_s <= 0
        or not np.isfinite(duration_s)
        or sample_count < 2
        or start_position.shape != end_position.shape
        or start_position.shape != start_velocity.shape
        or start_position.shape != end_velocity.shape
    ):
        raise ValueError(
            "Hermite bridge requires matching endpoints and positive duration/sample count"
        )
    tau = np.linspace(0.0, 1.0, sample_count, dtype=np.float64)
    h00 = 2 * tau**3 - 3 * tau**2 + 1
    h10 = tau**3 - 2 * tau**2 + tau
    h01 = -2 * tau**3 + 3 * tau**2
    h11 = tau**3 - tau**2
    shape = (sample_count,) + (1,) * start_position.ndim
    h00 = np.asarray(h00.reshape(shape), dtype=np.float64)
    h10 = np.asarray(h10.reshape(shape), dtype=np.float64)
    h01 = np.asarray(h01.reshape(shape), dtype=np.float64)
    h11 = np.asarray(h11.reshape(shape), dtype=np.float64)
    positions = (
        h00 * start_position
        + h10 * duration_s * start_velocity
        + h01 * end_position
        + h11 * duration_s * end_velocity
    )
    dh00 = 6 * tau**2 - 6 * tau
    dh10 = 3 * tau**2 - 4 * tau + 1
    dh01 = -6 * tau**2 + 6 * tau
    dh11 = 3 * tau**2 - 2 * tau
    derivative_shape = (sample_count,) + (1,) * start_position.ndim
    velocities = (
        dh00.reshape(derivative_shape) * start_position / duration_s
        + dh10.reshape(derivative_shape) * start_velocity
        + dh01.reshape(derivative_shape) * end_position / duration_s
        + dh11.reshape(derivative_shape) * end_velocity
    )
    return np.asarray(positions, dtype=np.float64), np.asarray(velocities, dtype=np.float64)


def optimize_match_on_action_cut(
    outgoing_landmarks_3d: FloatArray,
    outgoing_screen_velocity_px_s: FloatArray,
    incoming_landmarks_3d: FloatArray,
    incoming_screen_velocity_px_s: FloatArray,
    *,
    pose_weight: float = 1.0,
    velocity_weight: float = 1.0,
    speed_weight: float = 0.25,
    bridge_duration_s: float = 0.12,
    bridge_sample_count: int = 8,
    source_frame_rate_fps: float = 24.0,
) -> KineticCutReceipt:
    """Choose the global minimum pose/velocity/speed cut and build a 3D Hermite handoff."""
    outgoing, outgoing_velocity = _validate_trajectory_pair(
        outgoing_landmarks_3d, outgoing_screen_velocity_px_s, "outgoing"
    )
    incoming, incoming_velocity = _validate_trajectory_pair(
        incoming_landmarks_3d, incoming_screen_velocity_px_s, "incoming"
    )
    if outgoing.shape[1] != incoming.shape[1]:
        raise ValueError("match-on-action trajectories must track the same number of landmarks")
    controls = (pose_weight, velocity_weight, speed_weight, source_frame_rate_fps)
    if any(not np.isfinite(value) or value < 0 for value in controls) or source_frame_rate_fps == 0:
        raise ValueError("kinetic weights must be non-negative and frame rate positive")
    if pose_weight + velocity_weight + speed_weight <= 0:
        raise ValueError("at least one kinetic cost weight must be positive")
    if not np.isfinite(bridge_duration_s) or bridge_duration_s <= 0 or bridge_sample_count < 2:
        raise ValueError("kinetic bridge requires positive duration and at least two samples")

    with np.errstate(over="ignore", invalid="ignore"):
        pose_delta = outgoing[:, None, :, :] - incoming[None, :, :, :]
        velocity_delta = outgoing_velocity[:, None, :, :] - incoming_velocity[None, :, :, :]
        pose_cost = np.sum(pose_delta**2, axis=(2, 3))
        velocity_cost = np.sum(velocity_delta**2, axis=(2, 3))
        outgoing_speed = np.linalg.norm(outgoing_velocity, axis=-1).mean(axis=1)
        incoming_speed = np.linalg.norm(incoming_velocity, axis=-1).mean(axis=1)
        speed_cost = (outgoing_speed[:, None] - incoming_speed[None, :]) ** 2
        costs = (
            pose_weight * pose_cost + velocity_weight * velocity_cost + speed_weight * speed_cost
        )
    if np.any(~np.isfinite(costs)):
        raise ValueError("global match-on-action cost matrix overflowed finite precision")
    outgoing_index_raw, incoming_index_raw = np.unravel_index(int(np.argmin(costs)), costs.shape)
    outgoing_index, incoming_index = int(outgoing_index_raw), int(incoming_index_raw)
    selected_cost = float(costs[outgoing_index, incoming_index])
    unaligned_cost = float(costs[-1, 0])

    def endpoint_velocity(trajectory: FloatArray, index: int, incoming_side: bool) -> FloatArray:
        if incoming_side:
            if index + 1 < len(trajectory):
                return (trajectory[index + 1] - trajectory[index]) * source_frame_rate_fps
            if index > 0:
                return (trajectory[index] - trajectory[index - 1]) * source_frame_rate_fps
        else:
            if index > 0:
                return (trajectory[index] - trajectory[index - 1]) * source_frame_rate_fps
            if index + 1 < len(trajectory):
                return (trajectory[index + 1] - trajectory[index]) * source_frame_rate_fps
        return np.zeros_like(trajectory[index])

    bridge_positions, bridge_velocities = _hermite_bridge(
        outgoing[outgoing_index],
        endpoint_velocity(outgoing, outgoing_index, False),
        incoming[incoming_index],
        endpoint_velocity(incoming, incoming_index, True),
        duration_s=bridge_duration_s,
        sample_count=bridge_sample_count,
    )
    return KineticCutReceipt(
        int(outgoing_index),
        int(incoming_index),
        selected_cost,
        unaligned_cost,
        unaligned_cost - selected_cost,
        np.asarray(costs, dtype=np.float64),
        bridge_positions,
        bridge_velocities,
        bridge_duration_s,
    )
