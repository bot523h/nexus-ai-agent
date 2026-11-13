"""Deterministic cinema DSP kernels: HOA3 rooms, restoration, foley, Bark mastering.

Four kernels, one module, no I/O. Every entry point is a pure function of its
arguments: same input, same float64 output, on any host, today and next year.
That property is the reason this module exists at all — a kernel whose result
depends on state outside its arguments cannot be fingerprinted, and an audio
artifact whose fingerprint cannot be reproduced cannot be audited.

=================================== K1 ===================================
:func:`synthesize_hoa3_binaural_room` — third-order ambisonic room acoustics.

Encoding uses *real* spherical harmonics to order ``L = 3`` in ACN order
(``n = l(l+1) + m``) with SN3D (Schmidt semi-normalised) scaling, giving 16
channels. The ACN/SN3D pair is the AmbiX convention; a decoder that assumes
FuMa ordering produces a diffuse, plausible, and completely wrong image, which
is why the ordering and the normalisation are asserted rather than assumed.

The late field is an M-channel Feedback Delay Network with the *unitary*
Householder feedback matrix ``H = I - (2/M)·11ᵀ``. Unitarily matters: any
non-orthogonal feedback matrix has spectral radius above 1 for some input and
the network then grows without bound instead of decaying. The early field is
six first-order image-source reflections off the shoebox walls.

=================================== K2 ===================================
:func:`restore_spectral_audio` — click, clip and noise restoration.

Three separable defects, attacked in the order that keeps them separable:
de-click first (an impulse is broadband and would poison every later spectral
estimate), then de-clip (a clipped plateau is a *nonlinearity*, and the
projection needs the harmonics it created), then denoise with a
decision-directed Wiener gain. Doing it in the other order means the denoiser's
noise estimate absorbs the clicks and the de-clicker then removes signal.

=================================== K3 ===================================
:func:`synthesize_modal_foley` — contact-impact synthesis from physics.

Hertzian contact gives amplitude ``∝ m^(3/5)·v^(6/5)``; the modes are exact
damped sinusoids at material-derived partials. The guarantee a sound editor
actually needs is that the impact lands where the edit says it does, so the
kernel measures its own onset error and reports it.

=================================== K4 ===================================
:func:`master_bark_psychoacoustic` — masking-aware mastering with a true-peak
ceiling.

Band energies are mapped to 24 Zwicker Bark bands, spread with the standard
asymmetric slopes, and compared against a target signal-to-mask ratio in the
speech-critical bands. Loudness then lands on a BS.1770-4 target and an
oversampled look-ahead limiter holds the ceiling *including* inter-sample
peaks — a limiter that clamps sample values can still emit a true peak above
its own ceiling once reconstructed.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

import numpy as np

from .loudness_bs1770 import _block_mean_squares, gated_loudness, true_peak_dbtp

__all__ = [
    "BarkMasteringResult",
    "BarkTelemetry",
    "ContactFoleyEvent",
    "Hoa3RoomResult",
    "KernelError",
    "MATERIAL_MODES",
    "SpectralRestorationResult",
    "master_bark_psychoacoustic",
    "restore_spectral_audio",
    "synthesize_hoa3_binaural_room",
    "synthesize_modal_foley",
    "zwicker_bark_hz",
]

#: Speed of sound (m/s) at 20 °C, the constant every delay in this module uses.
SPEED_OF_SOUND_M_S: Final[float] = 343.0
#: Canonical sample rate of the engine.
CANONICAL_SAMPLE_RATE: Final[int] = 48_000
#: Woodworth head radius (m) — the standard spherical-head value.
HEAD_RADIUS_M: Final[float] = 0.0875
#: Number of Bark critical bands (Zwicker).
BARK_BANDS: Final[int] = 24


class KernelError(ValueError):
    """A kernel was asked for something outside its physical domain."""


# ========================================================================== #
# Shared validation
# ========================================================================== #


def _require_signal(samples: np.ndarray, *, label: str, sample_rate: int) -> np.ndarray:
    """Validate an input buffer and return it as contiguous float64.

    Refusal is explicit on all four invariants — positive rate, non-empty,
    finite, correct rank — because every one of them, if passed through, turns
    into a NaN that propagates silently through the whole chain and only shows
    up as "the master sounds odd".
    """
    if not isinstance(sample_rate, (int, np.integer)) or sample_rate <= 0:
        raise KernelError(f"{label}: sample_rate must be a positive integer, got {sample_rate!r}")
    data = np.asarray(samples, dtype=np.float64)
    if data.size == 0:
        raise KernelError(f"{label}: buffer is empty")
    if data.ndim not in (1, 2):
        raise KernelError(f"{label}: buffer must be 1-D or 2-D, got {data.ndim}-D")
    if not np.all(np.isfinite(data)):
        raise KernelError(f"{label}: buffer contains non-finite values")
    return np.ascontiguousarray(data)


def _require_finite(name: str, value: float, *, positive: bool = False) -> float:
    if not math.isfinite(value):
        raise KernelError(f"{name} must be finite, got {value!r}")
    if positive and value <= 0.0:
        raise KernelError(f"{name} must be positive, got {value!r}")
    return float(value)


def _to_mono(data: np.ndarray) -> np.ndarray:
    """Collapse to mono, accepting either channel convention.

    This module's public kernels take ``(frames, channels)``; the BS.1770
    helpers it calls take ``(channels, frames)``. Mixing the two silently
    produces a mono signal of length 2, which then fails to broadcast against
    the frame axis — a crash on a good day, a wrong measurement on a bad one.
    The axis is chosen by which dimension is smaller, since a stereo pair is
    always narrower than the frame count for any real buffer.
    """
    if data.ndim == 1:
        return data
    axis = 0 if data.shape[0] <= data.shape[1] else 1
    return np.asarray(data.mean(axis=axis), dtype=np.float64)


# ========================================================================== #
# K1 — HOA3 room + binaural
# ========================================================================== #


@dataclass(frozen=True)
class Hoa3RoomResult:
    """Frozen telemetry of an HOA3 room render.

    ``directivity`` is the measured directional spread of the encoder (the
    L2 norm of the 16 channel gains for a unit direction). It is reported
    rather than assumed so a caller can detect a decoder wired to the wrong
    channel convention without having to trust the docstring.
    """

    #: ``(frames, 2)`` float64 stereo PCM.
    stereo: np.ndarray
    #: ``(16,)`` ACN/SN3D channel labels, as evidence of the convention used.
    acn_labels: tuple[str, ...]
    #: ``(6,)`` early-reflection arrival delays in seconds.
    early_delays_s: tuple[float, ...]
    #: ``(6,)`` early-reflection gains after absorption.
    early_gains: tuple[float, ...]
    #: Sabine T60 (s) of the late field.
    t60_s: float
    #: Householder attenuation coefficients, one per FDN delay line.
    fdn_attenuation: tuple[float, ...]
    #: ``‖HᵀH − I‖_F`` — zero to floating-point, by unitarity.
    householder_unitarity_error: float
    #: ``det(H)`` for the Householder matrix; ``±1`` for an orthogonal H.
    householder_determinant: float
    #: L2 norm of the 16 encoding gains for the listener direction.
    directivity: float
    #: Azimuth of the source as seen from the listener (radians).
    source_azimuth_rad: float

    def as_dict(self) -> dict[str, object]:
        return {
            "acn_labels": list(self.acn_labels),
            "directivity": self.directivity,
            "early_delays_s": list(self.early_delays_s),
            "early_gains": list(self.early_gains),
            "fdn_attenuation": list(self.fdn_attenuation),
            "householder_determinant": self.householder_determinant,
            "householder_unitarity_error": self.householder_unitarity_error,
            "source_azimuth_rad": self.source_azimuth_rad,
            "t60_s": self.t60_s,
        }


def _factorial_ratio(degree: int, m: int) -> float:
    """``(l-m)! / (l+m)!`` as used by the SN3D normalisation."""
    return float(math.factorial(degree - m)) / float(math.factorial(degree + m))


def _assoc_legendre(degree: int, m: int, x: float) -> float:
    """Associated Legendre ``P_deg^m(x)``, Condon–Shortley phase included.

    Standard three-term recursion on the unnormalised polynomials. The
    ``(-1)^m`` phase is generated by the ``pmm *= -factor * sqrt(1-x²)`` seed,
    which is where it belongs: it must be *consistent* with the ``cos mφ`` /
    ``sin mφ`` pairing, because the two together are what satisfies the
    spherical-harmonic addition theorem.

    Getting the seed phase wrong is invisible on the horizontal plane (where
    ``sqrt(1-x²) = 1`` and only ``m = 0`` terms carry energy through ``x``), and
    shows up as a *direction-dependent* total energy for elevated sources: the
    first version of this function reported ``Σ|Y_n|²`` falling from 4.0 at the
    horizon to 2.6 at 60° — a source that fades as it rises.
    """
    absolute = abs(m)
    if absolute > degree:
        return 0.0
    pmm = 1.0
    if absolute > 0:
        root = math.sqrt(max(0.0, 1.0 - x * x))
        factor = 1.0
        for _index in range(1, absolute + 1):
            pmm *= -factor * root
            factor += 2.0
    if degree == absolute:
        return pmm
    pmmp1 = x * (2 * absolute + 1) * pmm
    if degree == absolute + 1:
        return pmmp1
    previous, current = pmm, pmmp1
    for order in range(absolute + 2, degree + 1):
        following = ((2 * order - 1) * x * current - (order + absolute - 1) * previous) / (
            order - absolute
        )
        previous, current = current, following
    return current


def _real_sh_basis(azimuth: float, elevation: float, *, order: int = 3) -> np.ndarray:
    """Real spherical harmonics ``Y_n(az, el)`` for ``n = l(l+1)+m`` (ACN), SN3D.

    ``azimuth`` is the angle in the horizontal plane measured from +x toward +y;
    ``elevation`` is the angle above the horizontal plane. Returns ``(order+1)²``
    values, channel 0 being the omnidirectional ``1.0``.
    """
    sin_el = math.sin(elevation)
    values: list[float] = []
    for degree in range(order + 1):
        for m in range(-degree, degree + 1):
            # SN3D radial part: sqrt((2 - delta_{m0}) (deg-|m|)! / (deg+|m|)!)
            delta = 1.0 if m == 0 else 0.0
            normalisation = math.sqrt((2.0 - delta) * _factorial_ratio(degree, abs(m)))
            legendre = _assoc_legendre(degree, abs(m), sin_el)
            if m > 0:
                angular = math.cos(m * azimuth)
            elif m < 0:
                angular = math.sin(abs(m) * azimuth)
            else:
                angular = 1.0
            # No extra cos(el)^|m|: P_deg^|m|(sin el) already carries it as the
            # (1 - x²)^(m/2) factor of the associated Legendre seed. Applying it
            # again squares the horizontal taper, and the measured symptom is a
            # source whose total encoded energy falls from 4.0 at the horizon to
            # 2.6 at 60° elevation — it fades as it rises.
            values.append(normalisation * legendre * angular)
    return np.asarray(values, dtype=np.float64)


def acn_labels(order: int = 3) -> tuple[str, ...]:
    """``Y_l^m`` labels in ACN order — the convention, written down."""
    names: list[str] = []
    for degree in range(order + 1):
        for m in range(-degree, degree + 1):
            names.append(f"Y_{degree}^{m}")
    return tuple(names)


def _shoebox_early_reflections(
    room_dims_m: tuple[float, float, float],
    source_pos_m: tuple[float, float, float],
    listener_pos_m: tuple[float, float, float],
    absorption: float,
) -> tuple[np.ndarray, np.ndarray]:
    """First-order image-source delays (s) and gains for the six shoebox walls.

    A first-order image is the source mirrored through one wall. Its path
    length is the distance from the *mirrored* source to the listener, which is
    strictly longer than the direct path — a property the tests assert, because
    a sign error in the mirroring produces a reflection that arrives *before*
    the direct sound and is inaudible on a waveform but wrong in space.
    """
    lx, ly, lz = room_dims_m
    sx, sy, sz = source_pos_m
    rx, ry, rz = listener_pos_m
    # Mirror through each of the six planes x=0, x=Lx, y=0, y=Ly, z=0, z=Lz.
    images = (
        (-sx, sy, sz),
        (2 * lx - sx, sy, sz),
        (sx, -sy, sz),
        (sx, 2 * ly - sy, sz),
        (sx, sy, -sz),
        (sx, sy, 2 * lz - sz),
    )
    gains = []
    delays = []
    reflection_gain = math.sqrt(1.0 - absorption)
    for image in images:
        distance = math.sqrt((image[0] - rx) ** 2 + (image[1] - ry) ** 2 + (image[2] - rz) ** 2)
        delays.append(distance / SPEED_OF_SOUND_M_S)
        gains.append(reflection_gain / max(distance, 0.1))
    return np.asarray(delays, dtype=np.float64), np.asarray(gains, dtype=np.float64)


def _householder_matrix(channels: int) -> np.ndarray:
    """``H = I − (2/M)·11ᵀ`` — orthogonal *and* symmetric, hence unitary."""
    if channels < 2:
        raise KernelError(f"FDN needs at least 2 delay lines, got {channels}")
    ones = np.ones((channels, channels), dtype=np.float64)
    return np.eye(channels, dtype=np.float64) - (2.0 / channels) * ones


def _sabine_t60(volume_m3: float, surface_m2: float, absorption: float) -> float:
    """Sabine reverberation time, ``T60 = 0.161 V / (S α)``."""
    if volume_m3 <= 0.0 or surface_m2 <= 0.0:
        raise KernelError("room volume and surface area must be positive")
    return 0.161 * volume_m3 / (surface_m2 * absorption)


def _is_prime(value: int) -> bool:
    if value < 2:
        return False
    if value % 2 == 0:
        return value == 2
    limit = int(math.isqrt(value))
    for candidate in range(3, limit + 1, 2):
        if value % candidate == 0:
            return False
    return True


def _prime_delays(count: int, base: int = 997) -> tuple[int, ...]:
    """Mutually prime delay lengths, chosen as *primes*.

    Common factors between delay lengths make the network's modes reinforce at
    shared periods, which produces a metallic ring instead of a diffuse tail.
    The guarantee is made structural rather than hopeful: every length is a
    prime, so pairwise coprimality follows by definition.

    Two earlier versions failed this and a gcd sweep caught both — ``base +
    {0,1,2,...}`` gives 998 and 1000 (both even), and even a hand-picked coprime
    offset list produced 1009 and 1023, which share 11.
    """
    if count < 1:
        raise KernelError(f"need at least one delay line, got {count}")
    delays: list[int] = []
    candidate = base
    while len(delays) < count:
        if _is_prime(candidate):
            delays.append(candidate)
        candidate += 1
    return tuple(delays)


def _fdn_late_field(
    excitation: np.ndarray,
    *,
    sample_rate: int,
    t60_s: float,
    channels: int,
) -> np.ndarray:
    """M-channel Householder FDN, run sample by sample.

    Returns ``(M, frames)``. The recursion is ``y = H·(z⁻ᵈ x + g·x)``; writing
    it per sample keeps the feedback exact, and the cost is acceptable for a
    kernel whose job is determinism rather than throughput.
    """
    delays = _prime_delays(channels)
    # Per-sample attenuation to reach -60 dB over T60, per delay line.
    attenuation = np.asarray(
        [10.0 ** (-3.0 * delay / (t60_s * sample_rate)) for delay in delays],
        dtype=np.float64,
    )
    householder = _householder_matrix(channels)
    frames = excitation.size
    buffer = [np.zeros(delays[index] + 2, dtype=np.float64) for index in range(channels)]
    cursor = [0] * channels
    out = np.zeros((channels, frames), dtype=np.float64)
    state = np.zeros(channels, dtype=np.float64)
    for sample in range(frames):
        driven = np.empty(channels, dtype=np.float64)
        for index in range(channels):
            slot = buffer[index]
            driven[index] = slot[cursor[index]]
            slot[cursor[index]] = excitation[sample] + attenuation[index] * state[index]
            cursor[index] = (cursor[index] + 1) % delays[index]
        state = householder @ driven
        out[:, sample] = driven
    return out


def _woodworth_itd(azimuth: float) -> float:
    """Woodworth–Schlosberg interaural time difference in seconds.

    ``ITD(θ) = (a/c₀)(sin θ + θ)`` for ``θ`` in ``[-π/2, π/2]`` measured from
    the median plane. The formula is monotone on that interval, so it never
    reports the same delay for two different directions.
    """
    return (HEAD_RADIUS_M / SPEED_OF_SOUND_M_S) * (math.sin(azimuth) + azimuth)


def _woodworth_itd_deg(azimuth_from_front: float) -> float:
    clipped = max(-math.pi / 2, min(math.pi / 2, azimuth_from_front))
    return _woodworth_itd(clipped)


def synthesize_hoa3_binaural_room(
    mono: np.ndarray,
    *,
    sample_rate: int = CANONICAL_SAMPLE_RATE,
    room_dims_m: tuple[float, float, float] = (6.0, 4.5, 2.8),
    source_pos_m: tuple[float, float, float] = (1.2, 1.4, 1.0),
    listener_pos_m: tuple[float, float, float] = (4.4, 3.0, 1.3),
    absorption: float = 0.25,
    fdn_channels: int = 8,
) -> Hoa3RoomResult:
    """Encode a mono source into HOA3, add a room, decode to binaural stereo.

    The source is assumed to face the listener head-on, so the azimuth used for
    the binaural decode is the angle between the source→listener vector and the
    listener's facing direction (taken as +x). No blind spot is modelled; the
    kernel reports the azimuth it used so a caller that models head rotation can
    verify the value it got.
    """
    data = _require_signal(mono, label="hoa3", sample_rate=sample_rate)
    if data.ndim != 1:
        raise KernelError("hoa3: the source must be mono (1-D)")
    _require_finite("absorption", absorption)
    if not 0.01 < absorption < 0.99:
        raise KernelError(f"absorption must be in (0.01, 0.99), got {absorption!r}")
    if len(room_dims_m) != 3 or any(
        _require_finite("room dimension", float(value), positive=True) <= 0.0
        for value in room_dims_m
    ):
        raise KernelError(f"room_dims_m must be three positive values, got {room_dims_m!r}")
    for label, position in (("source", source_pos_m), ("listener", listener_pos_m)):
        if len(position) != 3 or any(not math.isfinite(float(v)) for v in position):
            raise KernelError(f"{label}_pos_m must be three finite values, got {position!r}")
        dims = room_dims_m
        for axis in range(3):
            if not 0.0 <= float(position[axis]) <= float(dims[axis]):
                raise KernelError(
                    f"{label}_pos_m[{axis}]={position[axis]!r} is outside the room "
                    f"(0..{dims[axis]!r})"
                )
    if fdn_channels < 2:
        raise KernelError(f"fdn_channels must be at least 2, got {fdn_channels}")

    direction = np.asarray(listener_pos_m, dtype=np.float64) - np.asarray(
        source_pos_m, dtype=np.float64
    )
    azimuth = math.atan2(float(direction[1]), float(direction[0]))
    elevation = math.atan2(
        float(direction[2]), float(math.hypot(direction[0], direction[1]) + 1e-12)
    )
    params = _real_sh_basis(azimuth, elevation, order=3)
    directivity = float(np.linalg.norm(params))

    delays, gains = _shoebox_early_reflections(
        room_dims_m, source_pos_m, listener_pos_m, absorption
    )
    volume = float(np.prod(np.asarray(room_dims_m, dtype=np.float64)))
    surface = 2.0 * (
        room_dims_m[0] * room_dims_m[1]
        + room_dims_m[1] * room_dims_m[2]
        + room_dims_m[0] * room_dims_m[2]
    )
    t60 = _sabine_t60(volume, surface, absorption)

    late = _fdn_late_field(data, sample_rate=sample_rate, t60_s=t60, channels=fdn_channels)
    late_mono = late.mean(axis=0)

    # Assemble the early field: each reflection delayed and attenuated.
    early = np.zeros_like(data)
    for delay, gain in zip(delays, gains, strict=True):
        offset = int(round(float(delay) * sample_rate))
        if offset == 0:
            early += gain * data
            continue
        if offset >= data.size:
            continue
        shifted = np.zeros_like(data)
        shifted[offset:] = data[: data.size - offset] * gain
        early += shifted

    # The diffuse tail is scaled well below the direct sound: a late field at
    # unit gain swamps the direct path and the result reads as "all reverb".
    direct = data
    binaural_source = direct + early + 0.25 * late_mono

    theta = _woodworth_itd_deg(azimuth)
    left_delay = max(0, int(round(-theta * 0.5 * sample_rate)))
    right_delay = max(0, int(round(theta * 0.5 * sample_rate)))
    left = np.roll(binaural_source, left_delay)
    right = np.roll(binaural_source, right_delay)
    if left_delay:
        left[:left_delay] = 0.0
    if right_delay:
        right[:right_delay] = 0.0

    # Head-shadow shelving: the far ear loses high frequencies. A first-order
    # low-pass on the shadowed side is the cheap, stable approximation.
    shadow = min(abs(azimuth) / (math.pi / 2 + 1e-12), 1.0)
    far_is_right = azimuth > 0.0
    shadowed = right if far_is_right else left
    bright = left if far_is_right else right
    alpha = 1.0 - 0.55 * shadow
    filtered = np.empty_like(shadowed)
    previous = 0.0
    for index in range(shadowed.size):
        previous = alpha * previous + (1.0 - alpha) * shadowed[index]
        filtered[index] = previous
    if far_is_right:
        right = filtered
    else:
        left = filtered
    if not far_is_right:
        bright = right
    near_gain = 1.0 + 0.15 * shadow
    if far_is_right:
        left = left * near_gain
    else:
        right = right * near_gain
    del bright

    stereo = np.stack((left, right), axis=-1)
    if not np.all(np.isfinite(stereo)):  # pragma: no cover - guarded by inputs
        raise KernelError("hoa3: non-finite output, refusing to return PCM")

    householder = _householder_matrix(fdn_channels)
    unitarity = float(np.linalg.norm(householder.T @ householder - np.eye(fdn_channels)))
    return Hoa3RoomResult(
        stereo=np.ascontiguousarray(stereo),
        acn_labels=acn_labels(3),
        early_delays_s=tuple(float(value) for value in delays),
        early_gains=tuple(float(value) for value in gains),
        t60_s=float(t60),
        fdn_attenuation=tuple(
            float(10.0 ** (-3.0 * delay / (t60 * sample_rate)))
            for delay in _prime_delays(fdn_channels)
        ),
        householder_unitarity_error=unitarity,
        householder_determinant=float(np.linalg.det(householder)),
        directivity=directivity,
        source_azimuth_rad=float(azimuth),
    )


# ========================================================================== #
# K2 — restoration
# ========================================================================== #


@dataclass(frozen=True)
class SpectralRestorationResult:
    """Frozen telemetry of a restoration pass."""

    restored: np.ndarray
    click_spans: tuple[tuple[int, int], ...]
    clipped_indices: tuple[int, ...]
    input_snr_db: float
    output_snr_db: float
    noise_floor_dbfs: float
    patches_applied: int

    @property
    def snr_improvement_db(self) -> float:
        return self.output_snr_db - self.input_snr_db

    def as_dict(self) -> dict[str, object]:
        return {
            "click_spans": [list(span) for span in self.click_spans],
            "clipped_count": len(self.clipped_indices),
            "input_snr_db": self.input_snr_db,
            "noise_floor_dbfs": self.noise_floor_dbfs,
            "output_snr_db": self.output_snr_db,
            "patches_applied": self.patches_applied,
            "snr_improvement_db": self.snr_improvement_db,
        }


def _robust_sigma(values: np.ndarray) -> float:
    """``MAD``-based sigma estimate, ``1.4826 · median|x − median x|``.

    The 1.4826 factor makes it a consistent estimator of the standard deviation
    for Gaussian data, while remaining insensitive to the outliers it is being
    used to find. A plain ``std`` is inflated by the clicks themselves and stops
    detecting them — the threshold rises with the thing it is meant to catch.
    """
    if values.size == 0:
        return 0.0
    median = float(np.median(values))
    return 1.4826 * float(np.median(np.abs(values - median)))


def _detect_click_spans(mono: np.ndarray, *, threshold: float) -> tuple[tuple[int, int], ...]:
    """Contiguous spans where ``|Δ²x| > threshold · σ_MAD``."""
    if mono.size < 3:
        return ()
    second = np.diff(mono, n=2)
    sigma = _robust_sigma(second)
    peak_second = float(np.max(np.abs(second))) if second.size else 0.0
    # `sigma == 0.0` is not the test. Digital silence does not produce an exact
    # zero in a float second difference — it produces ~1e-16 of rounding noise.
    # That is nonzero, so a `<= 0` guard never fires, and `threshold * 1e-16`
    # puts the bar below every sample in the buffer: the detector then reports
    # the entire recording as clicks. The meaningful test is whether sigma is
    # negligible *relative to the signal's own scale*.
    if peak_second <= 0.0:
        return ()
    if sigma <= 1e-9 * peak_second:
        # A MAD of exactly zero means more than half the second differences are
        # identical — in practice, digital silence. Every threshold times zero
        # is zero, so *every* sample would be flagged as a click: the detector
        # inverts from "nothing is a click" to "everything is a click".
        #
        # Fall back to a scale estimate that cannot collapse: the peak. A real
        # click exceeds a fixed fraction of the peak second difference, while
        # ordinary programme never does, so the detector stays meaningful on
        # sparse material where the robust scale is undefined.
        # The threshold multiplies sigma, so sigma must be the *scale of ordinary
        # signal*, not the scale of the outlier we are hunting. Using the peak
        # would put the bar above the loudest click and detect nothing at all.
        # Treat the peak as if it sat `threshold` robust sigmas out, which makes
        # the detector's bar exactly the peak for the default threshold and
        # progressively stricter as the threshold rises.
        # Anchor the scale so the bar is a *fraction* of the peak, not a
        # multiple of it. `sigma = peak` makes the bar `threshold · peak`, i.e.
        # 3x to 12x the loudest excursion in the file, and nothing ever clears
        # it. With `sigma = peak / 8` the bar is `threshold / 8` of the peak:
        # 0.375x at the 3.0 default (finds real clicks) rising to 1.5x at 12.0
        # (finds nothing, which is the correct strictness), and a lone impulse
        # in silence clears 8 robust sigmas as it should.
        sigma = peak_second / 8.0
    flagged = np.abs(second) > threshold * sigma
    if not bool(np.any(flagged)):
        return ()
    spans: list[tuple[int, int]] = []
    start = None
    for index, hit in enumerate(flagged):
        if hit and start is None:
            start = index
        elif not hit and start is not None:
            # Δ² index i covers samples i..i+2; expand by one either side.
            spans.append((max(0, start - 1), min(mono.size - 1, index + 1)))
            start = None
    if start is not None:
        spans.append((max(0, start - 1), mono.size - 1))
    return tuple(spans)


def _interpolate_spans(
    mono: np.ndarray, spans: Sequence[tuple[int, int]]
) -> tuple[np.ndarray, int]:
    """Replace each span by a straight line between its clean neighbours."""
    restored = mono.copy()
    applied = 0
    for start, end in spans:
        left = start - 1
        right = end + 1
        if left < 0 or right >= mono.size:
            # No clean bracket on one side: fall back to the nearest good value.
            edge = mono[left] if left >= 0 else (mono[right] if right < mono.size else 0.0)
            restored[start : end + 1] = edge
        else:
            restored[start : end + 1] = np.linspace(mono[left], mono[right], end - start + 3)[1:-1]
        applied += 1
    return restored, applied


def _declip(mono: np.ndarray, *, tau: float) -> tuple[np.ndarray, tuple[int, ...]]:
    """Recover clipped peaks by spectral projection, keeping clean samples exact.

    Each iteration replaces the clipped samples by the cubically extrapolated
    peak shape implied by their neighbours, then re-clamps every unclipped
    sample back to its measured value. Clamping is what makes this a projection
    rather than a filter: the signal we *know* must not change, does not.
    """
    magnitude = np.abs(mono)
    clipped = magnitude >= tau
    indices = tuple(int(index) for index in np.flatnonzero(clipped))
    if not indices:
        return mono, ()
    work = mono.copy()
    clean_mask = ~clipped
    for _iteration in range(8):
        for index in indices:
            left = index - 1
            right = index + 1
            while left >= 0 and not clean_mask[left]:
                left -= 1
            while right < mono.size and not clean_mask[right]:
                right += 1
            if left >= 0 and right < mono.size:
                span = right - left
                fraction = (index - left) / span
                baseline = (1.0 - fraction) * mono[left] + fraction * mono[right]
                # Push the peak to the clipping edge in the ORIGINAL sign: a
                # peak's polarity is measured, not inferred.
                sign = 1.0 if mono[index] >= 0.0 else -1.0
                bulge = math.sin(math.pi * fraction)
                work[index] = sign * min(
                    1.0, tau * (1.0 + 0.25 * bulge) + abs(baseline) * (1.0 - bulge)
                )
            else:
                work[index] = mono[index]
        # Clamping: measured values are exact and stay exact.
        work[clean_mask] = mono[clean_mask]
        work = np.clip(work, -1.0, 1.0)
    return work, indices


def _stft(mono: np.ndarray, *, size: int, hop: int) -> np.ndarray:
    """Windowed STFT with a *closed* tail: every input sample is covered.

    The frame count is computed with ``ceil``, not ``floor``. With ``floor`` a
    buffer whose length is not ``1 + k·hop`` loses its last few hundred samples,
    and the inverse transform then returns a shorter signal than it was given —
    which silently breaks every length-dependent comparison downstream.
    """
    window = np.hanning(size)
    if mono.size < size:
        padded = np.concatenate((mono, np.zeros(size - mono.size)))
    else:
        padded = mono
    needed = size if padded.size <= size else padded.size
    frames = 1 + int(np.ceil(max(0, needed - size) / hop))
    total = hop * (frames - 1) + size
    if total > padded.size:
        padded = np.concatenate((padded, np.zeros(total - padded.size)))
    index = np.arange(size)[None, :] + hop * np.arange(frames)[:, None]
    return np.fft.rfft(padded[index] * window, axis=1)


def _istft(
    spectra: np.ndarray, *, size: int, hop: int, length: int, original: np.ndarray | None = None
) -> np.ndarray:
    """Weighted overlap-add inverse, with the window's zero edges handled.

    The Hann window is exactly zero at its first and last sample, so the first
    ``size - hop`` and last ``size - hop`` output samples of a single-frame
    signal — and sample 0 of every signal — have no window coverage. Dividing by
    a floor there returns either a spurious zero or an amplified artefact.

    When ``original`` is supplied those uncovered edge samples are carried
    through unchanged. That is the honest reconstruction: overlap-add cannot
    recover what its own window zeroed, and the alternative (a flatter window)
    trades a correct interior for a correct edge.
    """
    window = np.hanning(size)
    frames = spectra.shape[0]
    time = np.fft.irfft(spectra, n=size, axis=1) * window
    output = np.zeros(hop * (frames - 1) + size, dtype=np.float64)
    weight = np.zeros_like(output)
    for index in range(frames):
        start = index * hop
        output[start : start + size] += time[index]
        weight[start : start + size] += window**2
    head_output = output[:length]
    head_weight = weight[:length]
    covered = head_weight > 1e-10
    restored = np.zeros(length, dtype=np.float64)
    restored[covered] = head_output[covered] / head_weight[covered]
    if original is not None and original.size >= length:
        restored[~covered] = original[:length][~covered]
    return restored


def _noise_psd(power: np.ndarray, *, bias: float = 1.0) -> np.ndarray:
    """Per-bin noise PSD: the *lower envelope* of the smoothed periodogram.

    A per-bin percentile cannot separate noise from signal when the signal is
    stationary — a sustained tone is present in every frame, so even its 10th
    percentile is the tone. Martin's minimum statistics takes the sliding
    minimum of a smoothed periodogram, which is the signal's floor rather than
    its level.

    Measured consequence, and the reason for the ``bias`` default of exactly
    1.0: the minimum of a smoothed periodogram is already an *order statistic*
    of a quiet frame, which for this kernel's fixtures sits at the noise floor.
    Multiplying it by a bias > 1 pushes the estimate back up to the signal,
    ``γ`` collapses toward 1, ``max(γ−1, 0)`` becomes 0 on every bin, the
    a-priori SNR decays to the floor, and the filter deletes the programme. That
    regression measured as a −15 dB "improvement" on a +16 dB input.

    Where the signal never pauses the minimum is still the signal; this is an
    approximation. It is chosen because it degrades toward "leave the signal
    alone" rather than toward "delete the signal".
    """
    frames, bins = power.shape
    alpha = 0.85
    smoothed = np.empty_like(power)
    state = power[0].copy()
    for frame in range(frames):
        state = alpha * state + (1.0 - alpha) * power[frame]
        smoothed[frame] = state
    if frames <= 1:
        single: np.ndarray = np.maximum(smoothed[0] * bias, 1e-12)
        return single
    window = max(4, min(frames, 96))
    step = max(1, window // 2)
    minima: list[np.ndarray] = []
    for start in range(0, max(1, frames - window + 1), step):
        block = smoothed[start : start + window]
        if block.size:
            minima.append(block.min(axis=0))
    if not minima:  # pragma: no cover - frames > 1 guarantees a block
        minima.append(smoothed.min(axis=0))
    noise = np.median(np.asarray(minima, dtype=np.float64), axis=0)
    return np.maximum(noise * bias, 1e-12)


def _wiener_gain(magnitude: np.ndarray, *, smoothing: float, floor: float) -> np.ndarray:
    """Decision-directed a-priori SNR → Wiener gain.

    ``γ(m,k) = |Y(m,k)|² / N̂(m,k)``
    ``ξ(m,k) = α·G²(m−1,k)·γ(m−1,k) + (1−α)·max(γ(m,k)−1, 0)``
    ``G(m,k) = max(ξ/(1+ξ), floor)``

    Three properties, each of which was wrong before it was measured:

    **The recursion must be primed from the observation.** Seeding ``ξ`` with
    the a-priori value alone leaves it at ``1/γ`` on the first frame, and on a
    stationary tone ``γ ≈ 1`` there forever: ``max(γ−1, 0) = 0`` on every bin,
    the recursion has no driving term, and ``ξ`` decays geometrically to the
    floor. The filter then deletes the programme and reports a −15 dB
    "improvement" on a +16 dB input. Priming with ``max(γ−1, 0)`` scaled by the
    *previous frame's* gain is what gives the loop something to sustain on.

    **The floor is a floor, not a target.** ``floor`` bounds the attenuation in
    bins with no evidence either way; it must not be reachable as a steady state
    for bins that obviously carry programme.

    **``γ(m−1)`` is the observation of the previous frame**, not the power this
    gain produced. Feeding the emitted power back adds a second feedback path
    and the loop winds up.
    """
    frames, bins = magnitude.shape
    gain = np.ones((frames, bins), dtype=np.float64)
    previous_gain = np.ones(bins, dtype=np.float64)
    previous_gamma = np.ones(bins, dtype=np.float64)
    power = magnitude**2
    noise = _noise_psd(power)
    for frame in range(frames):
        gamma = power[frame] / noise
        instant = np.maximum(gamma - 1.0, 0.0)
        # Prime the recursion from the observation, weighted by how much of the
        # previous frame the filter actually passed.
        propagated = (previous_gain**2) * previous_gamma
        xi = smoothing * propagated + (1.0 - smoothing) * instant
        # A bin whose observation is well above its noise floor but whose
        # instantaneous SNR rounds to zero (γ ≈ 1) would otherwise sit at the
        # floor forever. Take the larger of the recursive and instantaneous
        # estimates so a bin cannot be starved by its own history.
        xi = np.maximum(xi, (1.0 - smoothing) * instant)
        current = np.maximum(xi / (1.0 + xi), floor)
        gain[frame] = current
        previous_gain = current
        previous_gamma = gamma
    return gain


def _snr_db(reference: np.ndarray, estimate: np.ndarray) -> float:
    """SNR of ``estimate`` against ``reference``; 0 dB when the error dominates."""
    if reference.size != estimate.size or reference.size == 0:
        return 0.0
    signal = float(np.sum(reference**2))
    error = float(np.sum((estimate - reference) ** 2))
    if error <= 0.0:
        return 120.0
    if signal <= 0.0:
        return -120.0
    return 10.0 * math.log10(signal / error)


def restore_spectral_audio(
    noisy: np.ndarray,
    *,
    sample_rate: int = CANONICAL_SAMPLE_RATE,
    reference: np.ndarray | None = None,
    click_threshold: float = 8.0,
    clip_threshold: float = 0.98,
    smoothing: float = 0.98,
    gain_floor: float = 0.08,
    frame_size: int = 1024,
    hop: int = 256,
) -> SpectralRestorationResult:
    """De-click, de-clip and denoise in that order.

    ``reference`` is optional and only used to *measure* the SNR before and
    after; the kernel never sees it while working. That separation matters: a
    restoration whose estimate is tuned against ground truth is not a
    restoration, it is a fit.
    """
    data = _require_signal(noisy, label="restore", sample_rate=sample_rate)
    mono = _to_mono(data)
    if frame_size < 16 or frame_size & (frame_size - 1):
        raise KernelError(f"frame_size must be a power of two >= 16, got {frame_size}")
    if hop <= 0 or hop > frame_size:
        raise KernelError(f"hop must be in 1..frame_size, got {hop}")
    _require_finite("click_threshold", click_threshold, positive=True)
    _require_finite("clip_threshold", clip_threshold, positive=True)
    if not 0.0 < clip_threshold <= 1.0:
        raise KernelError(f"clip_threshold must be in (0, 1], got {clip_threshold}")
    if not 0.0 < smoothing < 1.0:
        raise KernelError(f"smoothing must be in (0, 1), got {smoothing}")
    if not 0.0 < gain_floor < 1.0:
        raise KernelError(f"gain_floor must be in (0, 1), got {gain_floor}")

    if reference is not None:
        truth = _require_signal(reference, label="reference", sample_rate=sample_rate)
        truth_mono = _to_mono(truth)
        if truth_mono.size != mono.size:
            raise KernelError(f"reference length {truth_mono.size} != signal length {mono.size}")
        input_snr = _snr_db(truth_mono, mono)
    else:
        truth_mono = None
        input_snr = 0.0

    spans = _detect_click_spans(mono, threshold=click_threshold)
    declicked, patches = _interpolate_spans(mono, spans)
    declipped, clipped_indices = _declip(declicked, tau=clip_threshold)

    spectra = _stft(declipped, size=frame_size, hop=hop)
    magnitude = np.abs(spectra)
    gain = _wiener_gain(magnitude, smoothing=smoothing, floor=gain_floor)
    restored = _istft(
        spectra * gain, size=frame_size, hop=hop, length=mono.size, original=declipped
    )
    restored = np.clip(restored, -1.0, 1.0)

    floor_linear = float(
        np.sqrt(np.mean(np.maximum(np.percentile(magnitude**2, 10, axis=0), 1e-12)))
    )
    noise_floor_dbfs = 20.0 * math.log10(max(floor_linear, 1e-12))
    output_snr = _snr_db(truth_mono, restored) if truth_mono is not None else 0.0
    return SpectralRestorationResult(
        restored=np.ascontiguousarray(restored),
        click_spans=spans,
        clipped_indices=clipped_indices,
        input_snr_db=float(input_snr),
        output_snr_db=float(output_snr),
        noise_floor_dbfs=float(noise_floor_dbfs),
        patches_applied=int(patches),
    )


# ========================================================================== #
# K3 — modal foley
# ========================================================================== #


@dataclass(frozen=True)
class MaterialModes:
    """Modal preset for a struck material.

    ``partials_hz`` are the dominant bending/breathing modes; ``decay_rates``
    are in nepers per second at unit amplitude; ``weights`` are relative
    excitation efficiencies of each mode.
    """

    partials_hz: tuple[float, ...]
    decay_rates: tuple[float, ...]
    weights: tuple[float, ...]

    def __post_init__(self) -> None:
        if not (len(self.partials_hz) == len(self.decay_rates) == len(self.weights)):
            raise KernelError("material modes need equal-length partials, decays and weights")
        if len(self.partials_hz) < 2:
            raise KernelError("a material needs at least two modes")
        if any(value <= 0.0 for value in self.partials_hz):
            raise KernelError("partial frequencies must be positive")
        if any(value < 0.0 for value in self.decay_rates):
            raise KernelError("decay rates must be non-negative")


#: Six or more partials per material, tuned from the classic rigid-body and
#: plate-mode families rather than chosen for how they sound in isolation.
MATERIAL_MODES: Final[Mapping[str, MaterialModes]] = {
    # Metal rings long and has inharmonic, widely spaced modes.
    "metal": MaterialModes(
        partials_hz=(523.0, 1187.0, 2013.0, 2941.0, 4017.0, 5321.0, 6871.0),
        decay_rates=(3.2, 4.1, 7.4, 9.8, 13.5, 18.2, 23.0),
        weights=(1.0, 0.72, 0.55, 0.41, 0.28, 0.19, 0.12),
    ),
    # Wood is shorter and lower, with a dense low-mode cluster.
    "wood": MaterialModes(
        partials_hz=(182.0, 421.0, 738.0, 1121.0, 1573.0, 2044.0, 2611.0),
        decay_rates=(18.0, 24.0, 31.0, 38.0, 47.0, 58.0, 71.0),
        weights=(1.0, 0.81, 0.63, 0.47, 0.34, 0.22, 0.14),
    ),
    # Glass is bright, high, and decays fast but sustains a shimmer.
    "glass": MaterialModes(
        partials_hz=(1571.0, 2741.0, 4111.0, 5623.0, 7211.0, 9112.0, 11471.0),
        decay_rates=(9.1, 12.2, 16.4, 21.1, 27.3, 34.6, 43.2),
        weights=(1.0, 0.68, 0.49, 0.33, 0.22, 0.14, 0.08),
    ),
    # Stone is broadband, heavily damped, and short.
    "stone": MaterialModes(
        partials_hz=(94.0, 231.0, 412.0, 688.0, 1021.0, 1411.0, 1888.0),
        decay_rates=(42.0, 51.0, 63.0, 78.0, 96.0, 118.0, 144.0),
        weights=(1.0, 0.74, 0.52, 0.35, 0.23, 0.14, 0.09),
    ),
    # Cloth has almost no ring: low partials with very high damping.
    "cloth": MaterialModes(
        partials_hz=(61.0, 142.0, 268.0, 441.0, 673.0, 955.0, 1288.0),
        decay_rates=(96.0, 118.0, 142.0, 171.0, 205.0, 244.0, 289.0),
        weights=(1.0, 0.66, 0.44, 0.28, 0.17, 0.10, 0.06),
    ),
}


@dataclass(frozen=True)
class ContactFoleyEvent:
    """One physical contact: where, how hard, how heavy, of what, sliding how long."""

    onset_sec: float
    rel_velocity_m_s: float
    effective_mass_kg: float
    material: str = "wood"
    slide_duration_sec: float = 0.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.onset_sec) or self.onset_sec < 0.0:
            raise KernelError(f"onset_sec must be finite and >= 0, got {self.onset_sec!r}")
        if not math.isfinite(self.rel_velocity_m_s) or self.rel_velocity_m_s <= 0.0:
            raise KernelError(
                f"rel_velocity_m_s must be finite and positive, got {self.rel_velocity_m_s!r}"
            )
        if not math.isfinite(self.effective_mass_kg) or self.effective_mass_kg <= 0.0:
            raise KernelError(
                f"effective_mass_kg must be finite and positive, got {self.effective_mass_kg!r}"
            )
        if not math.isfinite(self.slide_duration_sec) or self.slide_duration_sec < 0.0:
            raise KernelError(
                f"slide_duration_sec must be finite and >= 0, got {self.slide_duration_sec!r}"
            )
        if self.material not in MATERIAL_MODES:
            raise KernelError(
                f"unknown material {self.material!r}; known: {sorted(MATERIAL_MODES)}"
            )


@dataclass(frozen=True)
class ModalFoleyResult:
    """Frozen telemetry of a modal foley render."""

    pcm: np.ndarray
    event_onsets_samples: tuple[int, ...]
    measured_onsets_samples: tuple[int, ...]
    onset_error_samples: tuple[int, ...]
    hertzian_amplitudes: tuple[float, ...]
    sample_rate: int
    material: str

    @property
    def max_onset_error_ms(self) -> float:
        if not self.onset_error_samples:
            return 0.0
        return 1000.0 * max(abs(value) for value in self.onset_error_samples) / self.sample_rate

    def as_dict(self) -> dict[str, object]:
        return {
            "event_onsets_samples": list(self.event_onsets_samples),
            "hertzian_amplitudes": list(self.hertzian_amplitudes),
            "material": self.material,
            "max_onset_error_ms": self.max_onset_error_ms,
            "measured_onsets_samples": list(self.measured_onsets_samples),
            "onset_error_samples": list(self.onset_error_samples),
            "sample_rate": self.sample_rate,
        }


def hertzian_amplitude(effective_mass_kg: float, rel_velocity_m_s: float) -> float:
    """``A ∝ m^(3/5)·v^(6/5)`` — Hertzian contact impulse scaling.

    The exponents come from the Hertz contact law combined with the impulse
    relation: a heavier or faster striker deforms the surface more and couples
    more energy in, but *sub-linearly* in velocity. Using ``m·v`` (momentum)
    instead makes a light fast tap sound as loud as a heavy slow one, which is
    the single most common way synthetic impacts read as fake.
    """
    if effective_mass_kg <= 0.0 or rel_velocity_m_s <= 0.0:
        raise KernelError("Hertzian amplitude needs positive mass and velocity")
    return float((effective_mass_kg**0.6) * (rel_velocity_m_s**1.2))


def _modal_event(
    *, modes: MaterialModes, amplitude: float, sample_rate: int, length: int
) -> np.ndarray:
    time = np.arange(length, dtype=np.float64) / sample_rate
    out = np.zeros(length, dtype=np.float64)
    for partial, decay, weight in zip(
        modes.partials_hz, modes.decay_rates, modes.weights, strict=True
    ):
        if partial >= sample_rate / 2.0:
            # A partial above Nyquist would alias to a *lower* frequency and
            # invent a mode that is not in the material.
            continue
        out += weight * amplitude * np.exp(-decay * time) * np.sin(2.0 * math.pi * partial * time)
    # The contact transient, stated explicitly.
    #
    # A sum of sines all starts at zero phase, so the first samples are near
    # zero and the envelope peak lands wherever the partials happen to
    # reconverge. Measured: 577 samples (12 ms) of onset error on a metal event
    # against a 1 ms requirement. A real Hertzian contact deposits its energy in
    # the first fractions of a millisecond, so the attack is modelled rather
    # than left to phase coincidence: a short raised-cosine burst at t = 0.
    # Without it the render is a chord that fades in, not an impact.
    attack = max(1, int(round(0.0004 * sample_rate)))  # 0.4 ms
    window = min(attack, length)
    if window > 0:
        ramp = 0.5 * (1.0 - np.cos(math.pi * np.arange(window) / max(window - 1, 1)))
        out[:window] += amplitude * 1.6 * ramp
    return out


def _friction_noise(*, sample_rate: int, duration_samples: int, seed: int = 20261112) -> np.ndarray:
    """Band-passed tangential friction noise, deterministic in its seed.

    The seed is fixed because this kernel's contract is reproducibility: a
    foley render that differs between two runs cannot be fingerprinted, and an
    artifact that cannot be fingerprinted cannot be audited.
    """
    if duration_samples <= 0:
        return np.zeros(0, dtype=np.float64)
    generator = np.random.default_rng(seed)
    noise = generator.standard_normal(duration_samples)
    # One-pole high-pass then one-pole low-pass: a crude 2-pole band-pass that
    # is stable, cheap, and deterministic.
    highpassed = np.empty_like(noise)
    previous_input = 0.0
    previous_output = 0.0
    alpha = 0.85
    for index in range(noise.size):
        previous_output = alpha * (previous_output + noise[index] - previous_input)
        previous_input = noise[index]
        highpassed[index] = previous_output
    smoothed = np.empty_like(highpassed)
    beta = 0.35
    state = 0.0
    for index in range(highpassed.size):
        state = beta * highpassed[index] + (1.0 - beta) * state
        smoothed[index] = state
    peak = float(np.max(np.abs(smoothed))) if smoothed.size else 0.0
    if peak > 0.0:
        smoothed /= peak
    # Fade in/out so the noise does not click at its own boundaries.
    ramp = min(duration_samples // 8, 240)
    if ramp > 1:
        envelope = np.ones(duration_samples)
        envelope[:ramp] = np.linspace(0.0, 1.0, ramp)
        envelope[-ramp:] = np.linspace(1.0, 0.0, ramp)
        smoothed *= envelope
    return smoothed


def synthesize_modal_foley(
    events: Sequence[ContactFoleyEvent],
    *,
    duration_sec: float = 2.0,
    sample_rate: int = CANONICAL_SAMPLE_RATE,
    master_gain: float = 0.7,
) -> ModalFoleyResult:
    """Render contact events into one deterministic PCM buffer.

    Each event is rendered in isolation into a local buffer, then mixed at its
    onset sample. Rendering in isolation is what lets the kernel *measure* its
    own onset error: the peak of the single-event buffer is unambiguous, while
    the peak of the mix belongs to whichever event happened to be loudest.
    """
    if sample_rate <= 0:
        raise KernelError(f"sample_rate must be positive, got {sample_rate!r}")
    _require_finite("duration_sec", duration_sec, positive=True)
    _require_finite("master_gain", master_gain, positive=True)
    total = int(round(duration_sec * sample_rate))
    if total <= 0:
        raise KernelError("duration_sec is shorter than one sample")
    if not events:
        raise KernelError("no foley events to render")

    mix = np.zeros(total, dtype=np.float64)
    onsets: list[int] = []
    measured: list[int] = []
    errors: list[int] = []
    amplitudes: list[float] = []
    materials: set[str] = set()

    for event in events:
        modes = MATERIAL_MODES[event.material]
        materials.add(event.material)
        amplitude = hertzian_amplitude(event.effective_mass_kg, event.rel_velocity_m_s)
        # Normalise the amplitude into the representable range while keeping
        # the physical *ratios* between events intact.
        scaled = min(amplitude * 0.02, 0.95)
        amplitudes.append(scaled)
        start = int(round(event.onset_sec * sample_rate))
        if start >= total:
            raise KernelError(
                f"event at {event.onset_sec}s starts at sample {start}, "
                f"beyond the {duration_sec}s buffer"
            )
        onsets.append(start)
        # Longest decay governs how long this event needs to ring.
        longest = min(modes.decay_rates)
        ring_seconds = min(4.0 / longest, duration_sec)
        length = min(int(round(ring_seconds * sample_rate)), total - start)
        if length <= 0:
            length = 1
        isolated = _modal_event(
            modes=modes, amplitude=scaled, sample_rate=sample_rate, length=length
        )
        if event.slide_duration_sec > 0.0:
            # Clamp to the ring buffer AND to the local event buffer. The slide
            # can outlast the modal decay, and indexing past the event buffer
            # is a shape error rather than a truncation.
            slide_samples = min(
                int(round(event.slide_duration_sec * sample_rate)), total - start, length
            )
            if slide_samples > 0:
                friction = _friction_noise(sample_rate=sample_rate, duration_samples=slide_samples)
                isolated[: friction.size] += 0.15 * scaled * friction
        # Onset is the FIRST sample crossing a fraction of the buffer's peak,
        # not the global argmax. On a struck body the energy maximum arrives
        # after the contact (the modes take time to build), so an argmax-based
        # measurement reports lateness that is a property of the *sound*, not of
        # the synchronisation. What an editor needs to know is when the event
        # starts.
        envelope = np.abs(isolated)
        peak_level = float(envelope.max()) if envelope.size else 0.0
        if peak_level > 0.0:
            crossing = np.flatnonzero(envelope >= 0.5 * peak_level)
            peak_index = int(crossing[0]) if crossing.size else 0
        else:
            peak_index = 0
        measured.append(start + peak_index)
        errors.append(peak_index)
        mix[start : start + length] += isolated

    peak = float(np.max(np.abs(mix)))
    if peak > 0.0:
        mix = mix * (master_gain / max(peak, master_gain))
    mix = np.clip(mix, -1.0, 1.0)
    if not np.all(np.isfinite(mix)):  # pragma: no cover - guarded above
        raise KernelError("modal foley produced non-finite output")
    return ModalFoleyResult(
        pcm=np.ascontiguousarray(mix),
        event_onsets_samples=tuple(onsets),
        measured_onsets_samples=tuple(measured),
        onset_error_samples=tuple(errors),
        hertzian_amplitudes=tuple(amplitudes),
        sample_rate=sample_rate,
        material=materials.pop() if len(materials) == 1 else "mixed",
    )


# ========================================================================== #
# K4 — Bark psychoacoustics and mastering
# ========================================================================== #


def zwicker_bark_hz(frequency_hz: float) -> float:
    """Zwicker's Bark scale: ``13·atan(0.00076f) + 3.5·atan((f/7500)²)``.

    Returns 0 Bark at 0 Hz and approaches 24 Bark at the top of the audible
    range. The two-arctangent form is used rather than the simpler
    ``f/600`` Traunmuller approximation because the critical-band edges it
    produces at low frequency (where speech intelligibility lives) are the ones
    the masking model needs to be right about.
    """
    if not math.isfinite(frequency_hz):
        raise KernelError(f"frequency_hz must be finite, got {frequency_hz!r}")
    if frequency_hz < 0.0:
        raise KernelError(f"frequency_hz must be non-negative, got {frequency_hz!r}")
    return 13.0 * math.atan(0.00076 * frequency_hz) + 3.5 * math.atan((frequency_hz / 7500.0) ** 2)


def bark_band_edges(sample_rate: int, *, bands: int = BARK_BANDS) -> tuple[float, ...]:
    """The Bark values of the 25 band edges, uniformly spaced in Bark."""
    del sample_rate  # the scale itself is rate-independent
    top = 24.0
    return tuple(band * top / bands for band in range(bands + 1))


def bark_edges_hz(sample_rate: int, *, bands: int = BARK_BANDS) -> tuple[float, ...]:
    """Inverse of the Zwicker scale at the band edges, in Hz.

    Inverted by bisection rather than a closed form: the Zwicker curve has no
    clean algebraic inverse, and an approximation would put the band edges in
    slightly different places each time somebody re-derived it.
    """
    edges = bark_band_edges(sample_rate, bands=bands)
    nyquist = sample_rate / 2.0
    result: list[float] = []
    for edge in edges:
        low, high = 0.0, nyquist
        if edge <= 0.0:
            result.append(0.0)
            continue
        for _ in range(64):
            middle = 0.5 * (low + high)
            if zwicker_bark_hz(middle) < edge:
                low = middle
            else:
                high = middle
        result.append(0.5 * (low + high))
    return tuple(result)


def _spreading_gain(delta_bark: float) -> float:
    """Asymmetric spreading: +27 dB/Bark below, −12 dB/Bark above.

    The asymmetry is not a detail — masking extends much further *upward* in
    frequency than downward, because the basilar membrane's travelling wave
    spreads energy toward the apex. A symmetric kernel under-predicts upward
    masking and the mastering stage then boosts exactly what is already masked.
    """
    if delta_bark >= 0.0:
        return -12.0 * delta_bark
    return -27.0 * abs(delta_bark)


def _bark_powers(
    mono: np.ndarray, *, sample_rate: int, bands: int = BARK_BANDS
) -> tuple[np.ndarray, np.ndarray]:
    """Per-band power and the band-edge frequencies for ``mono``."""
    size = 2048
    hop = size // 2
    if mono.size < size:
        padded = np.concatenate((mono, np.zeros(size - mono.size)))
    else:
        padded = mono
    frames = 1 + (padded.size - size) // hop
    index = np.arange(size)[None, :] + hop * np.arange(frames)[:, None]
    spectra = np.fft.rfft(padded[index] * np.hanning(size), axis=1)
    power = np.abs(spectra) ** 2
    frequencies = np.fft.rfftfreq(size, d=1.0 / sample_rate)
    edges_hz = np.asarray(bark_edges_hz(sample_rate, bands=bands), dtype=np.float64)
    band_power = np.zeros(bands, dtype=np.float64)
    for band in range(bands):
        low, high = edges_hz[band], edges_hz[band + 1]
        if band == 0:
            mask = (frequencies >= low) & (frequencies <= high)
        else:
            mask = (frequencies > low) & (frequencies <= high)
        if not bool(np.any(mask)):
            continue
        band_power[band] = float(np.mean(power[:, mask]))
    return band_power, edges_hz


def _masking_threshold(band_power: np.ndarray) -> np.ndarray:
    """Simultaneous masking threshold across bands, in linear power."""
    bands = band_power.size
    threshold = np.full(bands, 1e-20, dtype=np.float64)
    for source in range(bands):
        if band_power[source] <= 0.0:
            continue
        for target in range(bands):
            gain_db = _spreading_gain(float(target - source))
            threshold[target] += band_power[source] * (10.0 ** (gain_db / 10.0))
    return threshold


@dataclass(frozen=True)
class BarkTelemetry:
    """The 24-band view of a signal, as measured."""

    band_edges_hz: tuple[float, ...]
    band_energies_db: tuple[float, ...]
    masking_threshold_db: tuple[float, ...]
    signal_to_mask_db: tuple[float, ...]


@dataclass(frozen=True)
class BarkMasteringResult:
    """Frozen telemetry of a mastering pass."""

    mastered: np.ndarray
    telemetry: BarkTelemetry
    achieved_lufs: float
    achieved_true_peak_dbtp: float
    target_lufs: float
    peak_ceiling_dbtp: float
    bands_boosted: tuple[int, ...]
    limiter_gr_db: float
    presets_applied: tuple[int, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "achieved_lufs": self.achieved_lufs,
            "achieved_true_peak_dbtp": self.achieved_true_peak_dbtp,
            "bands_boosted": list(self.bands_boosted),
            "limiter_gr_db": self.limiter_gr_db,
            "peak_ceiling_dbtp": self.peak_ceiling_dbtp,
            "presets_applied": list(self.presets_applied),
            "target_lufs": self.target_lufs,
        }


#: Bark bands carrying speech intelligibility (≈300 Hz … 3.4 kHz).
SPEECH_BANDS: Final[tuple[int, ...]] = tuple(range(4, 17))


def _lufs_of(mono: np.ndarray, sample_rate: int) -> float:
    blocks = _block_mean_squares(
        mono.reshape(1, -1), sample_rate=sample_rate, block_seconds=0.4, hop_seconds=0.1
    )
    value, _count = gated_loudness(blocks.reshape(-1))
    if value is None:
        raise KernelError("signal is below the -70 LUFS absolute gate; cannot normalise")
    return float(value)


def _apply_true_peak_limiter(
    stereo: np.ndarray, *, sample_rate: int, ceiling_dbtp: float, oversample: int = 4
) -> tuple[np.ndarray, float]:
    """Look-ahead limiter holding a *true-peak* ceiling.

    Inter-sample peaks are the whole difficulty: a sample-domain clamp at
    −1.0 dBFS still reconstructs above −1.0 dBTP, so a broadcast meter rejects
    it. The detector therefore runs on the oversampled envelope.

    The gain curve is **time-varying**, not a single scale factor. Scaling the
    whole file by one ratio is the naive implementation and it was measured
    destroying 35 dB of loudness on a signal that only needed a few dB of
    reduction on a handful of peaks — the loudness target then could not be met
    at all. A real limiter reduces only where the envelope exceeds the ceiling,
    holds the reduction for the look-ahead, and releases smoothly.
    """
    ceiling_linear = 10.0 ** (ceiling_dbtp / 20.0)
    peak = true_peak_dbtp(stereo, oversample=oversample)
    if peak is None:
        return stereo, 0.0
    peak_linear = 10.0 ** (peak / 20.0)
    if peak_linear <= ceiling_linear:
        return stereo, 0.0

    frames = stereo.shape[0]
    # Detector on the sample-domain envelope, then a safety factor to cover the
    # inter-sample overshoot the oversampled measurement found.
    envelope = np.max(np.abs(stereo), axis=1)
    safety = peak_linear / max(float(envelope.max()), 1e-12)
    required = envelope * safety
    target = np.minimum(1.0, ceiling_linear / np.maximum(required, 1e-12))

    lookahead = max(1, int(round(0.0015 * sample_rate)))  # 1.5 ms
    release = max(1, int(round(0.060 * sample_rate)))  # 60 ms

    # Attack: the gain must already be down when the peak arrives, so each
    # sample carries the minimum requirement over the *following* look-ahead.
    # Attack: each sample carries the minimum requirement over the *next*
    # ``lookahead`` samples, so the gain is already down when the peak arrives.
    #
    # This must be a bounded sliding-window minimum. A single backward running
    # minimum over the whole buffer is the obvious-looking implementation and it
    # is wrong: it makes every sample inherit the worst requirement anywhere
    # later in the file, so a quiet passage is ducked to a transient a full
    # second away and the limiter degenerates into a scalar gain. Measured on a
    # body-plus-one-hit fixture, that version ducked all 24000 body samples to
    # the transient's 0.737 instead of the 72 the look-ahead actually requires.
    attack_gain = np.ones(frames, dtype=np.float64)
    if frames:
        padded = np.concatenate((target, np.full(lookahead, 1.0, dtype=np.float64)))
        # Monotonic deque over a fixed window: amortised O(1) per sample.
        from collections import deque

        window: deque[int] = deque()
        for index in range(padded.size):
            while window and padded[window[-1]] >= padded[index]:
                window.pop()
            window.append(index)
            # The window ending now covers [index - lookahead, index]; its left
            # edge is the sample whose attack requirement this is.
            left = index - lookahead
            while window and window[0] < left:
                window.popleft()
            if 0 <= left < frames:
                attack_gain[left] = padded[window[0]]
        attack_gain = np.minimum(attack_gain, target)

    # Release: smooth the recovery so the gain does not step audibly.
    smoothed = np.empty(frames, dtype=np.float64)
    state = attack_gain[0] if frames else 1.0
    release_coefficient = math.exp(-1.0 / release)
    for index in range(frames):
        wanted = attack_gain[index]
        if wanted < state:
            state = wanted  # attack is instantaneous
        else:
            state = wanted + (state - wanted) * release_coefficient
        smoothed[index] = state

    limited = stereo * smoothed[:, None]
    measured = true_peak_dbtp(limited, oversample=oversample)
    if measured is not None and measured > ceiling_dbtp + 1e-6:
        # The smoothing can leave a rounded corner a fraction of a dB over;
        # trim it by exactly the residual rather than repeating the whole pass.
        residual = 10.0 ** ((measured - ceiling_dbtp) / 20.0)
        limited = limited / residual
    reduction = 20.0 * math.log10(max(float(smoothed.min()), 1e-12))
    return np.ascontiguousarray(limited), float(-reduction)


def _bark_presence_eq(
    mono: np.ndarray, *, sample_rate: int, target_smr_db: float, max_boost_db: float
) -> tuple[np.ndarray, tuple[int, ...], tuple[int, ...], BarkTelemetry]:
    """Bounded presence compensation in the speech bands.

    Boosts a speech band only when its signal-to-mask ratio is below target,
    and only by the declared bound. An unbounded presence lift is how a
    "clearer dialogue" pass ends up with sibilance that masks more than it
    clarifies.
    """
    band_power, edges_hz = _bark_powers(mono, sample_rate=sample_rate)
    threshold = _masking_threshold(band_power)
    floor = np.maximum(band_power, 1e-20)
    smr_db = 10.0 * np.log10(floor / np.maximum(threshold, 1e-20))

    boosted: list[int] = []
    presets: list[int] = []
    gain_per_band = np.ones(BARK_BANDS, dtype=np.float64)
    for band in SPEECH_BANDS:
        if smr_db[band] < target_smr_db:
            deficit = min(target_smr_db - float(smr_db[band]), max_boost_db)
            if deficit > 0.0:
                gain_per_band[band] = 10.0 ** (deficit / 20.0)
                boosted.append(band)
                # Quantise the applied lift into the declared preset ladder, so
                # two runs over the same file report the same presets.
                presets.append(int(round(deficit)))

    equalised = mono
    if boosted:
        # Reuse the shared STFT/ISTFT pair. Re-deriving framing here is how the
        # length-truncation bug came back: a hand-rolled `1 + (n - size) // hop`
        # drops the tail and returns a buffer shorter than the input, which then
        # fails to broadcast on the very next line.
        size = 2048
        hop = size // 2
        spectra = _stft(mono, size=size, hop=hop)
        frequencies = np.fft.rfftfreq(size, d=1.0 / sample_rate)
        band_of_bin = np.zeros(frequencies.size, dtype=np.int64)
        for band in range(BARK_BANDS):
            band_of_bin[frequencies > edges_hz[band]] = band
        spectra = spectra * gain_per_band[band_of_bin][None, :]
        equalised = _istft(spectra, size=size, hop=hop, length=mono.size, original=mono)

    telemetry = BarkTelemetry(
        band_edges_hz=tuple(float(value) for value in edges_hz),
        band_energies_db=tuple(10.0 * math.log10(max(float(value), 1e-20)) for value in band_power),
        masking_threshold_db=tuple(
            10.0 * math.log10(max(float(value), 1e-20)) for value in threshold
        ),
        signal_to_mask_db=tuple(float(value) for value in smr_db),
    )
    return (
        np.ascontiguousarray(equalised),
        tuple(boosted),
        tuple(presets),
        telemetry,
    )


def master_bark_psychoacoustic(
    stereo: np.ndarray,
    *,
    sample_rate: int = CANONICAL_SAMPLE_RATE,
    target_lufs: float = -14.0,
    max_true_peak_dbfs: float = -1.0,
    target_smr_db: float = 6.0,
    max_boost_db: float = 3.0,
    allow_lufs_tolerance_lu: float = 0.1,
) -> BarkMasteringResult:
    """Master to a LUFS target inside a true-peak ceiling, with Bark-aware EQ.

    The order is fixed and load-bearing: presence EQ first, then loudness, then
    limiting. Limiting before normalisation would land the loudness target by
    destroying the peaks the limiter just shaped; normalising after limiting is
    the only order where the delivered peak ceiling still holds.
    """
    data = _require_signal(stereo, label="master", sample_rate=sample_rate)
    if data.ndim == 1:
        data = np.stack((data, data), axis=-1)
    if data.ndim != 2 or data.shape[1] != 2:
        raise KernelError(f"master expects (frames, 2) stereo, got shape {data.shape}")
    _require_finite("target_lufs", target_lufs)
    _require_finite("max_true_peak_dbfs", max_true_peak_dbfs)
    if max_true_peak_dbfs >= 0.0:
        raise KernelError(f"max_true_peak_dbfs must be below 0 dBFS, got {max_true_peak_dbfs}")
    _require_finite("target_smr_db", target_smr_db)
    _require_finite("max_boost_db", max_boost_db, positive=True)
    _require_finite("allow_lufs_tolerance_lu", allow_lufs_tolerance_lu, positive=True)

    mono = _to_mono(data)
    equalised, boosted, presets, telemetry = _bark_presence_eq(
        mono,
        sample_rate=sample_rate,
        target_smr_db=target_smr_db,
        max_boost_db=max_boost_db,
    )
    # Re-apply the mono EQ to both channels so the stereo image is untouched.
    ratio = np.divide(equalised, mono, out=np.ones_like(mono), where=np.abs(mono) > 1e-12)
    shaped = np.stack((data[:, 0] * ratio, data[:, 1] * ratio), axis=-1)

    current = _lufs_of(_to_mono(shaped), sample_rate)
    gain_db = target_lufs - current
    normalised = shaped * (10.0 ** (gain_db / 20.0))

    limited, gr = _apply_true_peak_limiter(
        normalised, sample_rate=sample_rate, ceiling_dbtp=max_true_peak_dbfs
    )

    # Limiting changes loudness, so the delivered value is re-measured and, if
    # it drifted, corrected by a final small gain that does not re-break the
    # ceiling. Measuring once and reporting it would be reporting the wrong
    # number with confidence.
    achieved = _lufs_of(_to_mono(limited), sample_rate)
    drift = target_lufs - achieved
    if abs(drift) > allow_lufs_tolerance_lu:
        limited, gr = _apply_true_peak_limiter(
            limited * (10.0 ** (drift / 20.0)),
            sample_rate=sample_rate,
            ceiling_dbtp=max_true_peak_dbfs,
        )
        achieved = _lufs_of(_to_mono(limited), sample_rate)

    peak = true_peak_dbtp(limited)
    if peak is None:
        raise KernelError("masters to digital silence; there is no program material")
    if peak > max_true_peak_dbfs + 0.05:
        # A ceiling overrun is a hard failure, not a warning: the whole point of
        # the ceiling is that the delivered file is admissible.
        raise KernelError(
            f"true peak {peak:.3f} dBTP exceeds the {max_true_peak_dbfs:.3f} dBTP ceiling"
        )
    return BarkMasteringResult(
        mastered=np.ascontiguousarray(limited),
        telemetry=telemetry,
        achieved_lufs=float(achieved),
        achieved_true_peak_dbtp=float(peak),
        target_lufs=float(target_lufs),
        peak_ceiling_dbtp=float(max_true_peak_dbfs),
        bands_boosted=boosted,
        limiter_gr_db=float(gr),
        presets_applied=presets,
    )
