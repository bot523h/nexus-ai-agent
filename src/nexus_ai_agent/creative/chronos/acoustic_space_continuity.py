"""Metric shoebox-room, image-source RIR, binaural, and shot-continuity kernels."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product

import numpy as np
from numpy.typing import NDArray

from . import acoustics_lipsync

FloatArray = NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class AcousticRoomSpec:
    room_id: str
    dimensions_m: tuple[float, float, float]
    wall_absorption: tuple[float, float, float, float, float, float]
    air_attenuation_per_m: float = 0.0


@dataclass(frozen=True, slots=True)
class RoomAcousticInvariants:
    room_id: str
    volume_m3: float
    surface_area_m2: float
    area_weighted_mean_absorption: float
    rt60_seconds: float
    critical_distance_m: float


@dataclass(frozen=True, slots=True)
class BinauralRIRReceipt:
    room_id: str
    stereo_rir: FloatArray
    path_count: int
    direct_arrival_sample: int
    direct_distance_m: float
    itd_seconds: float
    drr_db: float
    room_rt60_seconds: float


@dataclass(frozen=True, slots=True)
class AcousticConvolutionReceipt:
    stereo_pcm: FloatArray
    input_sample_count: int
    output_sample_count: int
    sample_drift: int


@dataclass(frozen=True, slots=True)
class RoomShotAudit:
    shot_id: str
    camera_distance_m: float
    drr_db: float
    rt60_seconds: float
    direct_arrival_sample: int


@dataclass(frozen=True, slots=True)
class CrossShotAcousticContinuityReceipt:
    room_invariants: RoomAcousticInvariants
    shots: tuple[RoomShotAudit, ...]
    rt60_max_spread_seconds: float
    drr_monotonic_with_proximity: bool


def _room_arrays(room: AcousticRoomSpec) -> tuple[FloatArray, FloatArray, FloatArray]:
    dimensions = np.asarray(room.dimensions_m, dtype=np.float64)
    absorption = np.asarray(room.wall_absorption, dtype=np.float64)
    if (
        not room.room_id
        or dimensions.shape != (3,)
        or np.any(~np.isfinite(dimensions))
        or np.any(dimensions <= 0)
        or absorption.shape != (6,)
        or np.any(~np.isfinite(absorption))
        or np.any((absorption <= 0.01) | (absorption >= 0.99))
        or not np.isfinite(room.air_attenuation_per_m)
        or room.air_attenuation_per_m < 0
    ):
        raise ValueError(
            "room requires positive dimensions, six absorptions in (0.01,0.99), and m_air >= 0"
        )
    areas = np.array(
        [
            dimensions[1] * dimensions[2],
            dimensions[1] * dimensions[2],
            dimensions[0] * dimensions[2],
            dimensions[0] * dimensions[2],
            dimensions[0] * dimensions[1],
            dimensions[0] * dimensions[1],
        ],
        dtype=np.float64,
    )
    if np.any(~np.isfinite(areas)) or np.any(areas <= 0):
        raise ValueError("room wall areas must remain finite and strictly positive")
    return dimensions, absorption, areas


def compute_room_acoustic_invariants(room: AcousticRoomSpec) -> RoomAcousticInvariants:
    """Calculate Sabine-Eyring-Norris RT60 and critical distance for a shoebox room."""
    dimensions, absorption, areas = _room_arrays(room)
    volume = float(np.prod(dimensions))
    surface = float(np.sum(areas))
    mean_absorption = float(np.dot(areas, absorption) / surface)
    denominator = -surface * np.log1p(-mean_absorption) + 4.0 * room.air_attenuation_per_m * volume
    if not np.isfinite(denominator) or denominator <= 0:
        raise ValueError("room reverberation denominator must be finite and positive")
    rt60 = 0.161 * volume / denominator
    critical_distance = 0.057 * float(np.sqrt(volume / rt60))
    return RoomAcousticInvariants(
        room.room_id, volume, surface, mean_absorption, rt60, critical_distance
    )


def _validate_pose_and_positions(
    room_dims: FloatArray,
    source_position_m: FloatArray,
    camera_position_m: FloatArray,
    camera_to_world_rotation: FloatArray,
) -> tuple[FloatArray, FloatArray, FloatArray]:
    source = np.asarray(source_position_m, dtype=np.float64)
    camera = np.asarray(camera_position_m, dtype=np.float64)
    rotation = np.asarray(camera_to_world_rotation, dtype=np.float64)
    if (
        source.shape != (3,)
        or camera.shape != (3,)
        or rotation.shape != (3, 3)
        or np.any(~np.isfinite(source))
        or np.any(~np.isfinite(camera))
        or np.any(~np.isfinite(rotation))
        or np.any(source <= 0)
        or np.any(source >= room_dims)
        or np.any(camera <= 0)
        or np.any(camera >= room_dims)
        or not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-8, rtol=0.0)
        or not np.isclose(np.linalg.det(rotation), 1.0, atol=1e-8, rtol=0.0)
    ):
        raise ValueError(
            "source and microphone must be strictly inside the room with a camera SO(3) pose"
        )
    return source, camera, rotation


def _image_coordinate(source: float, extent: float, order: int) -> float:
    return 2.0 * order * extent + (source if order % 2 == 0 else extent - source)


def _axis_reflection_gain(absorption: FloatArray, axis: int, order: int) -> float:
    if order == 0:
        return 1.0
    low_index, high_index = 2 * axis, 2 * axis + 1
    first = high_index if order > 0 else low_index
    product_gain = 1.0
    for bounce in range(abs(order)):
        wall_index = (
            first if bounce % 2 == 0 else (low_index if first == high_index else high_index)
        )
        product_gain *= 1.0 - float(absorption[wall_index])
    return product_gain


def _add_linear_fractional_impulse(target: FloatArray, delay_samples: float, gain: float) -> None:
    if delay_samples < 0 or not np.isfinite(delay_samples) or not np.isfinite(gain):
        raise ValueError("RIR path delay and gain must be finite and non-negative")
    left = int(np.floor(delay_samples))
    fraction = delay_samples - left
    if left < len(target):
        target[left] += gain * (1.0 - fraction)
    if fraction and left + 1 < len(target):
        target[left + 1] += gain * fraction


def synthesize_binaural_room_impulse_response(
    room: AcousticRoomSpec,
    source_position_m: FloatArray,
    camera_position_m: FloatArray,
    camera_to_world_rotation: FloatArray,
    *,
    sample_rate_hz: int = 48_000,
    max_order: int = 2,
    sound_speed_m_s: float = 343.0,
    head_radius_m: float = 0.0875,
    minimum_distance_m: float = 0.05,
) -> BinauralRIRReceipt:
    """Synthesize six-wall image-source paths and camera-relative Woodworth stereo."""
    dimensions, absorption, _ = _room_arrays(room)
    invariants = compute_room_acoustic_invariants(room)
    if (
        sample_rate_hz <= 0
        or max_order < 1
        or not np.isfinite(sound_speed_m_s)
        or sound_speed_m_s <= 0
        or not np.isfinite(head_radius_m)
        or head_radius_m <= 0
        or not np.isfinite(minimum_distance_m)
        or minimum_distance_m <= 0
    ):
        raise ValueError(
            "RIR rate, reflection order, sound speed, and head geometry must be positive"
        )
    source, camera, rotation = _validate_pose_and_positions(
        dimensions, source_position_m, camera_position_m, camera_to_world_rotation
    )
    right = rotation[:, 0]
    forward = rotation[:, 2]
    ear_positions = (camera - head_radius_m * right, camera + head_radius_m * right)
    for ear in ear_positions:
        if np.any(ear <= 0) or np.any(ear >= dimensions):
            raise ValueError("binaural microphone ears must lie strictly inside the room")

    # A second-order room neighborhood is the 3×3×3 set of direct/first-order
    # mirrored cells (27 virtual rooms), matching the declared image-source budget.
    axis_radius = max_order - 1
    orders = tuple(product(range(-axis_radius, axis_radius + 1), repeat=3))
    image_sources = [
        np.array(
            [_image_coordinate(source[axis], dimensions[axis], order[axis]) for axis in range(3)],
            dtype=np.float64,
        )
        for order in orders
    ]
    largest_distance = max(float(np.linalg.norm(image - camera)) for image in image_sources)
    head_delay_distance = max(2.0 * head_radius_m, head_radius_m * (np.pi / 2.0 + 1.0))
    tail_samples = (
        int(np.ceil((largest_distance + head_delay_distance) * sample_rate_hz / sound_speed_m_s))
        + 3
    )
    stereo = np.zeros((tail_samples, 2), dtype=np.float64)
    direct = np.zeros_like(stereo)
    reverberant = np.zeros_like(stereo)
    direct_distance = float(np.linalg.norm(source - camera))
    direct_arrival = int(round(direct_distance * sample_rate_hz / sound_speed_m_s))
    direct_itd = 0.0

    for order, image in zip(orders, image_sources, strict=True):
        center_vector = image - camera
        center_distance = float(np.linalg.norm(center_vector))
        if center_distance <= 1e-12:
            raise ValueError("coincident image source and camera are undefined")
        azimuth = float(np.arctan2(np.dot(center_vector, right), np.dot(center_vector, forward)))
        itd = acoustics_lipsync.woodworth_itd(azimuth, head_radius_m)
        if order == (0, 0, 0):
            direct_itd = itd
        reflection_gain = 1.0
        for axis in range(3):
            reflection_gain *= _axis_reflection_gain(absorption, axis, order[axis])
        pressure_gain = (
            reflection_gain
            * np.exp(-room.air_attenuation_per_m * center_distance)
            / max(center_distance, minimum_distance_m)
        )
        direct_path = order == (0, 0, 0)
        target = direct if direct_path else reverberant
        shadow_db = 6.0 * abs(float(np.sin(azimuth)))
        nearer_channel = 1 if azimuth >= 0 else 0
        ear_gain = [10.0 ** (-shadow_db / 20.0), 10.0 ** (-shadow_db / 20.0)]
        ear_gain[nearer_channel] = 1.0
        base_delay = center_distance * sample_rate_hz / sound_speed_m_s
        for channel in range(2):
            # Positive Woodworth ITD means the left ear is later than the right.
            if channel == 0:
                delay = base_delay + max(0.0, itd)
            else:
                delay = base_delay + max(0.0, -itd)
            _add_linear_fractional_impulse(
                target[:, channel], delay, float(pressure_gain * ear_gain[channel])
            )
    stereo[:] = direct + reverberant
    direct_energy = float(np.sum(direct**2))
    reverberant_energy = float(np.sum(reverberant**2))
    drr_db = 10.0 * np.log10(max(direct_energy, 1e-30) / max(reverberant_energy, 1e-30))
    return BinauralRIRReceipt(
        room.room_id,
        stereo,
        len(orders),
        direct_arrival,
        direct_distance,
        direct_itd,
        float(drr_db),
        invariants.rt60_seconds,
    )


def convolve_shot_dialogue_with_room_acoustics(
    dialogue_pcm_mono: FloatArray,
    stereo_rir: FloatArray,
    *,
    sample_rate_hz: int = 48_000,
) -> AcousticConvolutionReceipt:
    """FFT-convolve mono dialogue into stereo while preserving its exact sample horizon."""
    dialogue = np.asarray(dialogue_pcm_mono, dtype=np.float64)
    rir = np.asarray(stereo_rir, dtype=np.float64)
    if (
        sample_rate_hz <= 0
        or dialogue.ndim != 1
        or dialogue.size == 0
        or rir.ndim != 2
        or rir.shape[0] == 0
        or rir.shape[1] != 2
        or np.any(~np.isfinite(dialogue))
        or np.any(~np.isfinite(rir))
    ):
        raise ValueError("acoustic convolution needs finite mono PCM and a non-empty stereo RIR")
    nfft = 1 << (dialogue.size + rir.shape[0] - 2).bit_length()
    dry_spectrum = np.fft.rfft(dialogue, n=nfft)
    rir_spectrum = np.fft.rfft(rir, n=nfft, axis=0)
    full = np.fft.irfft(rir_spectrum * dry_spectrum[:, None], n=nfft, axis=0)
    stereo = np.asarray(full[: dialogue.size], dtype=np.float64)
    return AcousticConvolutionReceipt(stereo, len(dialogue), len(stereo), 0)


def audit_cross_shot_acoustic_continuity(
    room: AcousticRoomSpec,
    speaker_position_m: FloatArray,
    shots: tuple[tuple[str, FloatArray, FloatArray], ...],
    *,
    sample_rate_hz: int = 48_000,
    max_order: int = 2,
) -> CrossShotAcousticContinuityReceipt:
    """Verify same-room RT60 invariance and distance-ordered physical DRR receipts."""
    if len(shots) < 2 or len({shot_id for shot_id, _, _ in shots}) != len(shots):
        raise ValueError("cross-shot room audit requires at least two uniquely named shots")
    invariants = compute_room_acoustic_invariants(room)
    audits: list[RoomShotAudit] = []
    for shot_id, camera_position, camera_rotation in shots:
        rir = synthesize_binaural_room_impulse_response(
            room,
            speaker_position_m,
            camera_position,
            camera_rotation,
            sample_rate_hz=sample_rate_hz,
            max_order=max_order,
        )
        audits.append(
            RoomShotAudit(
                shot_id,
                rir.direct_distance_m,
                rir.drr_db,
                rir.room_rt60_seconds,
                rir.direct_arrival_sample,
            )
        )
    rt_values = [audit.rt60_seconds for audit in audits]
    spread = max(rt_values) - min(rt_values)
    ordered_by_distance = sorted(audits, key=lambda audit: audit.camera_distance_m, reverse=True)
    drr_monotonic = all(
        farther.drr_db <= nearer.drr_db + 1e-9
        for farther, nearer in zip(ordered_by_distance, ordered_by_distance[1:], strict=False)
    )
    if spread >= 1e-6:
        raise ValueError("same-room RT60 changed across camera shots")
    if not drr_monotonic:
        raise ValueError("close-up DRR must exceed medium and wide-shot DRR")
    return CrossShotAcousticContinuityReceipt(
        invariants, tuple(audits), float(spread), drr_monotonic
    )
