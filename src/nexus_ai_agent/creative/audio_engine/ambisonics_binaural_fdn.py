"""K1: real ACN/SN3D spatial audio and a passive eight-line acoustic FDN.

Coordinates are right-handed: X front, Y left, Z up; azimuth is counter-
clockwise from X, elevation above XY. FOA is [W,Y,Z,X]. Euler rotations are
Rz(yaw) Ry(pitch) Rx(roll); camera-coordinate rotations use the inverse.

The binaural model is an analytic spherical-head/pinna approximation, NOT a
measured or personalized HRTF. Woodworth's lateral ITD, a frequency-dependent
Schlosberg-style head-shadow shelf, and an elevation-dependent causal pinna
notch are projected over a quadrature-exact virtual loudspeaker array. There
is no model download, anthropometric inference, or external AI API.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

SOUND_SPEED = 343.0
HEAD_RADIUS = 0.0875


def _order(order: int) -> int:
    if not isinstance(order, int) or isinstance(order, bool) or order not in (0, 1, 2, 3):
        raise ValueError("ACN/SN3D order must be an integer in [0, 3]")
    return order


def _signal(samples: np.ndarray, *, matrix: bool = False) -> np.ndarray:
    data = np.asarray(samples, dtype=np.float64)
    if data.ndim not in ((1, 2) if matrix else (1,)) or not np.isfinite(data).all():
        raise ValueError("audio must be finite, channel-first PCM")
    return data


def sn3d_harmonics(order: int, azimuth: np.ndarray, elevation: np.ndarray) -> np.ndarray:
    """Real spherical harmonics (channels, directions), no Condon-Shortley phase.

    Associated Legendre recurrence and Schmidt semi-normalization yield
    sum_m Y_lm(u)^2 = 1 for every degree and unit direction u.
    """
    _order(order)
    az, el = np.broadcast_arrays(np.asarray(azimuth, float), np.asarray(elevation, float))
    if not np.isfinite(az).all() or not np.isfinite(el).all() or np.any(np.abs(el) > np.pi / 2):
        raise ValueError("directions must be finite; elevation must be in [-pi/2, pi/2]")
    az, z = az.reshape(-1), np.sin(el.reshape(-1))
    result = np.zeros(((order + 1) ** 2, az.size), dtype=np.float64)
    for m in range(order + 1):
        pmm = np.ones_like(z)
        for k in range(1, m + 1):
            pmm *= (2 * k - 1) * np.sqrt(np.maximum(0.0, 1.0 - z * z))
        previous, current = np.zeros_like(z), pmm
        for degree in range(m, order + 1):
            if degree > m:
                previous, current = (
                    current,
                    ((2 * degree - 1) * z * current - (degree + m - 1) * previous) / (degree - m),
                )
            normalization = math.sqrt(
                (1 if m == 0 else 2) * math.factorial(degree - m) / math.factorial(degree + m)
            )
            base = normalization * current
            result[degree * (degree + 1) + m] = base * np.cos(m * az)
            if m:
                result[degree * (degree + 1) - m] = base * np.sin(m * az)
    return result


def encode_ambisonics(
    samples: np.ndarray,
    *,
    azimuth: np.ndarray | float,
    elevation: np.ndarray | float = 0.0,
    order: int = 1,
) -> np.ndarray:
    """Encode and sum independent point sources; input is (sources, samples)."""
    data = np.atleast_2d(_signal(samples, matrix=True))
    az = np.broadcast_to(np.asarray(azimuth, float), (data.shape[0],))
    el = np.broadcast_to(np.asarray(elevation, float), (data.shape[0],))
    return np.asarray(sn3d_harmonics(order, az, el) @ data, dtype=np.float64)


def _quadrature(order: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    z, weights = np.polynomial.legendre.leggauss(order + 2)
    count = 2 * order + 5
    az = np.tile(2 * np.pi * np.arange(count) / count, z.size)
    el = np.repeat(np.arcsin(z), count)
    return az, el, np.repeat(weights * (2 * np.pi / count), count)


def euler_so3(yaw: float, pitch: float, roll: float) -> np.ndarray:
    if not np.isfinite([yaw, pitch, roll]).all():
        raise ValueError("rotation angles must be finite radians")
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cr, sr = math.cos(roll), math.sin(roll)
    return np.asarray(
        np.array([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]])
        @ np.array([[cp, 0, sp], [0, 1, 0], [-sp, 0, cp]])
        @ np.array([[1, 0, 0], [0, cr, -sr], [0, sr, cr]]),
        dtype=np.float64,
    )


def ambisonic_rotation_matrix(
    order: int,
    *,
    yaw: float = 0.0,
    pitch: float = 0.0,
    roll: float = 0.0,
    camera_coordinates: bool = True,
) -> np.ndarray:
    """SO(3) representation by exact spherical quadrature, degree by degree.

    Polar orthogonalization removes only round-off (~1e-15); it is not an
    arbitrary energy normalization of the signal. Inverse rotations and group
    composition retain their directional meaning, including HOA orders 2/3.
    """
    _order(order)
    rotation = euler_so3(yaw, pitch, roll)
    if camera_coordinates:
        rotation = rotation.T
    az, el, weights = _quadrature(order)
    vectors = np.vstack((np.cos(el) * np.cos(az), np.cos(el) * np.sin(az), np.sin(el)))
    turned = rotation @ vectors
    target = sn3d_harmonics(
        order, np.arctan2(turned[1], turned[0]), np.arcsin(np.clip(turned[2], -1, 1))
    )
    basis = sn3d_harmonics(order, az, el)
    matrix = np.zeros(((order + 1) ** 2, (order + 1) ** 2), dtype=np.float64)
    for degree in range(order + 1):
        band = slice(degree * degree, (degree + 1) ** 2)
        block = (target[band] * weights) @ basis[band].T * ((2 * degree + 1) / (4 * np.pi))
        left, _, right = np.linalg.svd(block)
        matrix[band, band] = left @ right
    return matrix


def rotate_ambisonics(
    samples: np.ndarray,
    *,
    yaw: float = 0.0,
    pitch: float = 0.0,
    roll: float = 0.0,
    camera_coordinates: bool = True,
) -> np.ndarray:
    data = _signal(samples, matrix=True)
    if data.ndim != 2 or data.shape[0] not in (1, 4, 9, 16):
        raise ValueError("B-format must have 1, 4, 9 or 16 ACN channels")
    order = math.isqrt(data.shape[0]) - 1
    rotation = ambisonic_rotation_matrix(
        order, yaw=yaw, pitch=pitch, roll=roll, camera_coordinates=camera_coordinates
    )
    return np.asarray(rotation @ data, dtype=np.float64)


def woodworth_itd(
    azimuth: float,
    elevation: float = 0.0,
    *,
    head_radius: float = HEAD_RADIUS,
    sound_speed: float = SOUND_SPEED,
) -> float:
    """Signed ear-to-ear delay, seconds; positive source Y (left) delays right.

    Rear-hemisphere directions fold to the lateral great-circle angle. This
    preserves front/back ITD ambiguity; the pinna notch is an independent cue.
    """
    if not np.isfinite([azimuth, elevation, head_radius, sound_speed]).all():
        raise ValueError("head/direction parameters must be finite")
    if head_radius <= 0 or sound_speed <= 0 or abs(elevation) > math.pi / 2:
        raise ValueError("head geometry and sound speed must be positive")
    lateral = math.asin(math.sin(azimuth) * math.cos(elevation))
    return head_radius / sound_speed * (math.sin(lateral) + lateral)


def lagrange_fractional_delay(
    samples: np.ndarray, delay_samples: float, *, order: int = 3
) -> np.ndarray:
    """Causal Lagrange fractional delay with ZERO extension (never circular wrap)."""
    data = _signal(samples)
    if not math.isfinite(delay_samples) or delay_samples < 0 or not 1 <= order <= 8:
        raise ValueError("delay must be finite/nonnegative; interpolation order must be 1..8")
    integer = math.floor(delay_samples)
    fraction = delay_samples - integer
    coefficients = np.ones(order + 1, dtype=np.float64)
    for k in range(order + 1):
        for j in range(order + 1):
            if j != k:
                coefficients[k] *= (fraction - j) / (k - j)
    output = np.zeros(data.size, dtype=np.float64)
    if integer < data.size:
        output[integer:] = np.convolve(data, coefficients)[: data.size - integer]
    return output


def _hrtf_impulse(azimuth: float, elevation: float, sample_rate: int) -> np.ndarray:
    length = 512
    bins = np.exp(-2j * np.pi * np.fft.rfftfreq(length))
    lateral = math.sin(azimuth) * math.cos(elevation)
    itd = woodworth_itd(azimuth, elevation) * sample_rate
    notch_hz = min(0.42 * sample_rate, 7500.0 + 2500.0 * math.sin(elevation))
    cosine = math.cos(2 * math.pi * notch_hz / sample_rate)
    radius = math.exp(-math.pi * 1600.0 / sample_rate)
    notch = (1 - 2 * cosine * bins + bins**2) / (
        1 - 2 * radius * cosine * bins + radius**2 * bins**2
    )
    notch *= (1 - 2 * radius * cosine + radius**2) / (2 - 2 * cosine)
    pinna = 0.25 + 0.75 * notch
    result = np.zeros((2, length), dtype=np.float64)
    for ear in range(2):
        contralateral = (ear == 1 and lateral > 0) or (ear == 0 and lateral < 0)
        high_gain = 10 ** (-18.0 * abs(lateral) / 20) if contralateral else 1.0
        tau = HEAD_RADIUS / SOUND_SPEED
        k = 2 * sample_rate * tau
        shadow = ((1 + bins) + high_gain * k * (1 - bins)) / ((1 + bins) + k * (1 - bins))
        impulse = np.fft.irfft(pinna * shadow, n=length)
        delay = max(0.0, itd if ear == 1 else -itd)
        result[ear] = lagrange_fractional_delay(impulse, delay, order=5)
    return result


def binaural_decode(samples: np.ndarray, *, sample_rate: int = 48000) -> np.ndarray:
    """Virtual-loudspeaker quadrature projection into phase-coherent (2,N)."""
    data = _signal(samples, matrix=True)
    if data.ndim != 2 or data.shape[0] not in (1, 4, 9, 16):
        raise ValueError("B-format must have 1, 4, 9 or 16 ACN channels")
    if not 22050 <= sample_rate <= 192000:
        raise ValueError("analytic pinna rendering requires 22050..192000 Hz")
    if data.shape[1] == 0:
        return np.zeros((2, 0), dtype=np.float64)
    order = math.isqrt(data.shape[0]) - 1
    az, el, weights = _quadrature(order)
    harmonics = sn3d_harmonics(order, az, el)
    degrees = np.repeat(np.arange(order + 1), 2 * np.arange(order + 1) + 1)
    decoder = harmonics.T * ((2 * degrees + 1) / (4 * np.pi)) * weights[:, None]
    virtual = decoder @ data
    fft_size = 1 << (data.shape[1] + 511).bit_length()
    output = np.zeros((2, fft_size // 2 + 1), dtype=np.complex128)
    for index, (azimuth, elevation) in enumerate(zip(az, el, strict=True)):
        hrtf = _hrtf_impulse(float(azimuth), float(elevation), sample_rate)
        output += np.fft.rfft(hrtf, n=fft_size, axis=1) * np.fft.rfft(virtual[index], n=fft_size)
    return np.asarray(
        np.fft.irfft(output, n=fft_size, axis=1)[:, : data.shape[1]], dtype=np.float64
    )


def householder_matrix(lines: int = 8) -> np.ndarray:
    if isinstance(lines, bool) or lines < 2 or lines > 32:
        raise ValueError("Householder line count must be in [2, 32]")
    return np.eye(lines) - (2.0 / lines) * np.ones((lines, lines))


def _next_prime(value: int) -> int:
    candidate = max(2, value)
    while any(candidate % divisor == 0 for divisor in range(2, math.isqrt(candidate) + 1)):
        candidate += 1
    return candidate


@dataclass(frozen=True)
class FdnRoom:
    volume_m3: float = 120.0
    surface_area_m2: float = 150.0
    rt60_low: float = 0.8
    rt60_mid: float = 0.6
    rt60_high: float = 0.4

    def __post_init__(self) -> None:
        if not all(
            math.isfinite(value) and value > 0
            for value in (
                self.volume_m3,
                self.surface_area_m2,
                self.rt60_low,
                self.rt60_mid,
                self.rt60_high,
            )
        ):
            raise ValueError("room geometry and RT60 must be finite and positive")
        if not 0.05 <= self.rt60_high <= self.rt60_mid <= self.rt60_low <= 10:
            raise ValueError("passive absorption requires 0.05 <= high <= mid <= low <= 10 seconds")
        if 4 * self.volume_m3 / self.surface_area_m2 > 100:
            raise ValueError("room mean-free path exceeds the bounded acoustic model")

    @property
    def eyring_absorption(self) -> tuple[float, float, float]:
        values = [
            1 - math.exp(-0.161 * self.volume_m3 / (self.surface_area_m2 * rt))
            for rt in (self.rt60_low, self.rt60_mid, self.rt60_high)
        ]
        return values[0], values[1], values[2]


@dataclass(frozen=True)
class FdnResult:
    samples: np.ndarray
    delays: tuple[int, ...]
    feedback: np.ndarray
    absorption: tuple[float, float, float]
    maximum_feedback_gain: float


def fdn_reverb(
    samples: np.ndarray,
    *,
    sample_rate: int = 48000,
    room: FdnRoom | None = None,
    tail_seconds: float | None = None,
) -> FdnResult:
    """Eight mutually prime delays, unitary mixing and contractive band damping.

    Feedback gain per line/band is 10^(-3*delay/(fs*RT60)). The two one-pole
    absorption states interpolate low/mid/high gains with nonnegative weights;
    their transfer magnitude cannot exceed the low-band gain (<1). Eyring's
    absorption is derived from V/S/RT; no unmeasured decay is reported as fact.
    """
    data = _signal(samples)
    room = room or FdnRoom()
    if not 8000 <= sample_rate <= 192000:
        raise ValueError("FDN sample_rate must be in [8000, 192000]")
    tail = room.rt60_low * 1.3 if tail_seconds is None else tail_seconds
    if not math.isfinite(tail) or not 0 <= tail <= 20:
        raise ValueError("FDN tail must be finite and in [0, 20] seconds")
    mean_free_time = 4 * room.volume_m3 / (room.surface_area_m2 * SOUND_SPEED)
    delays: list[int] = []
    for multiplier in np.linspace(0.65, 1.85, 8):
        candidate = _next_prime(round(sample_rate * mean_free_time * float(multiplier)))
        while candidate in delays:
            candidate = _next_prime(candidate + 1)
        delays.append(candidate)
    buffers = [np.zeros(delay, dtype=np.float64) for delay in delays]
    positions = np.zeros(8, dtype=int)
    feedback = householder_matrix()
    gains = 10 ** (
        -3
        * np.array(delays)[:, None]
        / (sample_rate * np.array([room.rt60_low, room.rt60_mid, room.rt60_high]))
    )
    lp_low, lp_high, previous_delayed = np.zeros(8), np.zeros(8), np.zeros(8)
    warped_low = math.tan(math.pi * 250 / sample_rate)
    warped_high = math.tan(math.pi * min(3000, sample_rate * 0.35) / sample_rate)
    pole_low = (1 - warped_low) / (1 + warped_low)
    pole_high = (1 - warped_high) / (1 + warped_high)
    injection = np.ones(8) / math.sqrt(8)
    stereo = np.vstack((injection, np.array([1, -1, 1, -1, -1, 1, -1, 1]) / math.sqrt(8)))
    output = np.zeros((2, data.size + round(tail * sample_rate)), dtype=np.float64)
    for n in range(output.shape[1]):
        delayed = np.array([buffers[k][positions[k]] for k in range(8)])
        output[:, n] = stereo @ delayed
        lp_low = pole_low * lp_low + (1 - pole_low) * 0.5 * (delayed + previous_delayed)
        lp_high = pole_high * lp_high + (1 - pole_high) * 0.5 * (delayed + previous_delayed)
        previous_delayed = delayed
        damped = (
            gains[:, 2] * delayed
            + (gains[:, 1] - gains[:, 2]) * lp_high
            + (gains[:, 0] - gains[:, 1]) * lp_low
        )
        excitation = data[n] if n < data.size else 0.0
        mixed = feedback @ damped + injection * excitation
        for k in range(8):
            buffers[k][positions[k]] = mixed[k]
            positions[k] = (positions[k] + 1) % delays[k]
    return FdnResult(output, tuple(delays), feedback, room.eyring_absorption, float(gains.max()))


def schroeder_rt60(samples: np.ndarray, *, sample_rate: int) -> float:
    """Backward energy integration, -5..-35 dB regression extrapolated to -60 dB."""
    data = _signal(samples, matrix=True)
    if sample_rate <= 0 or data.size == 0:
        raise ValueError("decay measurement requires samples and a positive sample rate")
    power = np.sum(np.atleast_2d(data) ** 2, axis=0)
    energy = np.cumsum(power[::-1])[::-1]
    if energy[0] <= 0:
        raise ValueError("silence has no measurable RT60")
    decay = 10 * np.log10(np.maximum(energy / energy[0], 1e-30))
    selected = (decay <= -5) & (decay >= -35)
    if np.count_nonzero(selected) < 20:
        raise ValueError("insufficient -5..-35 dB decay evidence")
    times = np.arange(power.size)[selected] / sample_rate
    slope = float(np.polyfit(times, decay[selected], 1)[0])
    if slope >= 0:
        raise ValueError("the measured decay is not stable")
    return -60 / slope


__all__ = [
    "FdnResult",
    "FdnRoom",
    "ambisonic_rotation_matrix",
    "binaural_decode",
    "encode_ambisonics",
    "euler_so3",
    "fdn_reverb",
    "householder_matrix",
    "lagrange_fractional_delay",
    "rotate_ambisonics",
    "schroeder_rt60",
    "sn3d_harmonics",
    "woodworth_itd",
]
