"""Deterministic shoebox acoustics and rational-time critically damped jaw model."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from .contracts import KernelError, RationalTimestamp

FloatArray = NDArray[np.float64]
SOUND_SPEED = 343.0


@dataclass(frozen=True, slots=True)
class TimbreResult:
    status: str
    metric_scope: str
    mean_distance_db: float | None
    uncertainty_type_a_db: float | None
    reference_voiced_frames: int
    candidate_voiced_frames: int


@dataclass(frozen=True, slots=True)
class RoomTelemetry:
    drr_db: float
    rt60_seconds: float
    itd_seconds: float


def woodworth_itd(azimuth_radians: float, head_radius_m: float = 0.0875) -> float:
    """Signed Woodworth-Schlosberg interaural delay in seconds."""
    angle = float(np.clip(azimuth_radians, -np.pi, np.pi))
    magnitude = abs(angle)
    path = (
        magnitude + np.sin(magnitude)
        if magnitude <= np.pi / 2
        else np.pi - magnitude + np.sin(magnitude)
    )
    return float(np.sign(angle) * head_radius_m / SOUND_SPEED * path)


def _voiced_envelopes(pcm: FloatArray, sample_rate: int) -> list[FloatArray]:
    signal = np.asarray(pcm, dtype=np.float64).reshape(-1)
    width, hop = round(sample_rate * 0.025), round(sample_rate * 0.010)
    if width <= 1 or len(signal) < width:
        return []
    window = np.hanning(width)
    nfft = 1 << (width - 1).bit_length()
    qmax = max(1, min(nfft // 2, sample_rate // 500))
    frequency = np.fft.rfftfreq(nfft, 1 / sample_rate)
    keep = (frequency >= 100) & (frequency <= 6000)
    envelopes: list[FloatArray] = []
    for start in range(0, len(signal) - width + 1, hop):
        frame = signal[start : start + width]
        if float(np.sqrt(np.mean(frame * frame))) < 1e-4:
            continue
        centered = frame - frame.mean()
        corr = np.correlate(centered, centered, mode="full")[width - 1 :]
        low, high = max(1, sample_rate // 450), min(width - 1, sample_rate // 60)
        if high <= low or float(np.max(corr[low : high + 1])) / max(float(corr[0]), 1e-12) < 0.3:
            continue
        spectrum = np.log(np.abs(np.fft.rfft(centered * window, nfft)) + 1e-12)
        cepstrum = np.fft.irfft(spectrum, nfft)
        lifter = np.zeros(nfft)
        taper = 0.5 * (1 + np.cos(np.pi * np.arange(qmax) / qmax))
        lifter[1 : qmax + 1] = taper
        cepstrum *= lifter
        envelope = (20 / np.log(10)) * np.fft.rfft(cepstrum, nfft).real
        envelopes.append(np.asarray(envelope[keep], dtype=np.float64))
    return envelopes


def measure_cepstral_timbre_consistency(
    ref_pcm: FloatArray,
    candidate_pcm: FloatArray,
    sample_rate: int,
    minimum_voiced_frames: int = 3,
) -> TimbreResult:
    """Compare liftered vocal-tract envelopes; this is not speaker identification."""
    if sample_rate <= 0 or minimum_voiced_frames < 2:
        raise KernelError("invalid sample rate or voiced-frame threshold")
    reference = _voiced_envelopes(ref_pcm, sample_rate)
    candidate = _voiced_envelopes(candidate_pcm, sample_rate)
    scope = "ACOUSTIC_TIMBRE_CONSISTENCY"
    if len(reference) < minimum_voiced_frames or len(candidate) < minimum_voiced_frames:
        return TimbreResult(
            "INSUFFICIENT_VOICED_FRAMES", scope, None, None, len(reference), len(candidate)
        )
    reference_mean = np.mean(np.stack(reference), axis=0)
    distances = np.asarray([np.sqrt(np.mean((frame - reference_mean) ** 2)) for frame in candidate])
    uncertainty = float(np.std(distances, ddof=1) / np.sqrt(len(distances)))
    return TimbreResult(
        "OK", scope, float(np.mean(distances)), uncertainty, len(reference), len(candidate)
    )


def sabine_rt60(
    volume_m3: float, surface_area_m2: float, absorption: float, *, eyring: bool = False
) -> float:
    if min(volume_m3, surface_area_m2) <= 0 or not 0 <= absorption < 1:
        raise KernelError("invalid room dimensions or absorption")
    denominator = surface_area_m2 * (-np.log1p(-absorption) if eyring else absorption)
    if denominator <= 0:
        raise KernelError("zero room absorption")
    return float(0.161 * volume_m3 / denominator)


def fractional_impulse(delay_samples: float, length: int, radius: int = 8) -> FloatArray:
    if delay_samples < 0 or length <= 0 or radius <= 0:
        raise KernelError("invalid fractional delay")
    result = np.zeros(length, dtype=np.float64)
    center = int(np.floor(delay_samples))
    indices = np.arange(max(0, center - radius), min(length, center + radius + 1))
    delta = indices - delay_samples
    result[indices] = np.sinc(delta) * np.kaiser(len(indices), 8.6)
    return result


def shoebox_rir(
    source: FloatArray,
    receiver: FloatArray,
    dimensions: FloatArray,
    absorption: float,
    sample_rate: int,
    order: int = 1,
) -> FloatArray:
    if (
        source.shape != (3,)
        or receiver.shape != (3,)
        or dimensions.shape != (3,)
        or np.any(dimensions <= 0)
        or not 0 <= absorption <= 1
        or sample_rate <= 0
        or order < 0
    ):
        raise KernelError("invalid shoebox geometry")
    max_distance = float(np.linalg.norm(dimensions) * (2 * order + 1))
    rir = np.zeros(int(np.ceil(max_distance / SOUND_SPEED * sample_rate)) + 1)
    for ix in range(-order, order + 1):
        for iy in range(-order, order + 1):
            for iz in range(-order, order + 1):
                image = np.array(
                    [
                        2 * ix * dimensions[0]
                        + (source[0] if ix % 2 == 0 else dimensions[0] - source[0]),
                        2 * iy * dimensions[1]
                        + (source[1] if iy % 2 == 0 else dimensions[1] - source[1]),
                        2 * iz * dimensions[2]
                        + (source[2] if iz % 2 == 0 else dimensions[2] - source[2]),
                    ]
                )
                distance = float(np.linalg.norm(image - receiver))
                if distance <= 1e-9:
                    continue
                delay = distance / SOUND_SPEED * sample_rate
                decay = absorption ** (abs(ix) + abs(iy) + abs(iz))
                rir += fractional_impulse(delay, len(rir)) * decay / distance
    return rir


def jaw_aperture(
    samples: list[tuple[RationalTimestamp, float]], natural_frequency_hz: float
) -> FloatArray:
    """Integrate a critically damped jaw driven by piecewise-constant viseme input."""
    if natural_frequency_hz <= 0 or not samples:
        raise KernelError("positive natural frequency and samples required")
    omega = 2 * np.pi * natural_frequency_hz
    y = velocity = 0.0
    out = np.zeros(len(samples))
    for i, (stamp, target) in enumerate(samples):
        if not 0 <= target <= 1 or (i and stamp.seconds <= samples[i - 1][0].seconds):
            raise KernelError("invalid viseme or non-monotonic timestamp")
        dt = float(stamp.seconds - samples[i - 1][0].seconds) if i else float(stamp.time_base)
        # Semi-implicit stable substeps, deterministic in rationally specified intervals.
        steps = max(1, int(np.ceil(dt * omega / 0.2)))
        h = dt / steps
        for _ in range(steps):
            acceleration = omega * omega * (target - y) - 2 * omega * velocity
            velocity += acceleration * h
            y = min(1.0, max(0.0, y + velocity * h))
        out[i] = y
    return out


def synthesize_stereo_room_ir(
    room_dims_m: FloatArray,
    speaker_pos_m: FloatArray,
    camera_pos_m: FloatArray,
    camera_right_unit: FloatArray,
    sample_rate: int,
    absorption: float,
    max_order: int = 2,
    head_radius_m: float = 0.0875,
) -> tuple[FloatArray, RoomTelemetry]:
    """Create camera-oriented stereo image-source RIRs with head-shadow gains."""
    right = np.asarray(camera_right_unit, dtype=np.float64)
    if right.shape != (3,) or not np.isclose(np.linalg.norm(right), 1.0, atol=1e-6):
        raise KernelError("camera right vector must be a unit 3-vector")
    delta = np.asarray(speaker_pos_m) - np.asarray(camera_pos_m)
    forward = np.array([-right[2], 0.0, right[0]])
    norm_forward = float(np.linalg.norm(forward))
    if norm_forward <= 1e-12:
        raise KernelError("camera right vector cannot be vertical")
    forward /= norm_forward
    distance = float(np.linalg.norm(delta))
    if distance <= 0:
        raise KernelError("speaker and camera positions must differ")
    theta = float(np.arctan2(np.dot(delta, right), np.dot(delta, forward)))
    itd = woodworth_itd(theta, head_radius_m)
    ear_positions = [camera_pos_m - head_radius_m * right, camera_pos_m + head_radius_m * right]
    impulses = [
        shoebox_rir(
            np.asarray(speaker_pos_m),
            np.asarray(ear),
            room_dims_m,
            absorption,
            sample_rate,
            max_order,
        )
        for ear in ear_positions
    ]
    length = max(map(len, impulses))
    stereo = np.zeros((length, 2), dtype=np.float64)
    for channel, impulse in enumerate(impulses):
        stereo[: len(impulse), channel] = impulse
    # Apply the requested Woodworth differential delay around the common center.
    shift = itd * sample_rate / 2
    for channel, delay in enumerate((max(0.0, -shift), max(0.0, shift))):
        if delay:
            kernel = fractional_impulse(delay, min(length, 33), 8)
            stereo[:, channel] = np.convolve(stereo[:, channel], kernel, mode="same")
    stereo[:, 0] *= 1.0 - 0.15 * np.sin(theta)
    stereo[:, 1] *= 1.0 + 0.15 * np.sin(theta)
    direct = int(round(distance * sample_rate / SOUND_SPEED))
    window = max(1, int(0.0025 * sample_rate))
    direct_energy = float(np.sum(stereo[max(0, direct - window) : direct + window + 1] ** 2))
    total_energy = float(np.sum(stereo**2))
    tail_energy = max(total_energy - direct_energy, 1e-15)
    drr = 10 * np.log10(max(direct_energy, 1e-15) / tail_energy)
    volume = float(np.prod(room_dims_m))
    surface = float(
        2
        * (
            room_dims_m[0] * room_dims_m[1]
            + room_dims_m[0] * room_dims_m[2]
            + room_dims_m[1] * room_dims_m[2]
        )
    )
    rt60 = sabine_rt60(volume, surface, absorption)
    return stereo, RoomTelemetry(float(drr), rt60, itd)


def render_spatial_dialogue(
    dry_mono_pcm: FloatArray,
    stereo_rir: FloatArray,
    block_size: int = 4096,
) -> tuple[FloatArray, RoomTelemetry]:
    """Bounded block FFT convolution of mono dialogue through a stereo RIR."""
    dry = np.asarray(dry_mono_pcm, dtype=np.float64).reshape(-1)
    rir = np.asarray(stereo_rir, dtype=np.float64)
    if dry.size == 0 or rir.ndim != 2 or rir.shape[1] != 2 or block_size <= 0:
        raise KernelError("invalid dry signal, stereo RIR, or block size")
    output = np.zeros((dry.size + rir.shape[0] - 1, 2), dtype=np.float64)
    nfft = 1 << (block_size + rir.shape[0] - 2).bit_length()
    for channel in range(2):
        response = np.fft.rfft(rir[:, channel], nfft)
        for start in range(0, dry.size, block_size):
            segment = dry[start : start + block_size]
            transformed = np.fft.rfft(segment, nfft)
            convolved = np.fft.irfft(transformed * response, nfft)
            count = min(len(segment) + len(rir) - 1, len(output) - start)
            output[start : start + count, channel] += convolved[:count]
    energy = float(np.sum(output**2))
    telemetry = RoomTelemetry(float(10 * np.log10(max(energy, 1e-15))), 0.0, 0.0)
    return output, telemetry
