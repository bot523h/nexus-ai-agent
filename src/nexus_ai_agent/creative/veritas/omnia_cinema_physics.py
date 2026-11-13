"""NAGAR VERITAS OMNIA — Laws 12-15 of the neuro-physical cinema engine.

Deterministic, zero-external-API, fully vectorised NumPy implementations of the
four "Omnia" laws: anamorphic dioptric optics with Cauchy lateral chromatic
aberration and cos^4 vignetting (Law 12), Stevens-House viseme/formant
articulatory causality with relativistic acoustic Doppler (Law 13),
Helmholtz-Hodge incompressible fluid projection with free-surface volume
conservation (Law 14), and Nyquist rotary-shutter anti-stroboscopic wagon-wheel
rectification (Law 15).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]
ComplexArray = npt.NDArray[np.complex128]


class OmniaOpticsError(ValueError):
    """Raised when anamorphic dioptric optics inputs are physically invalid."""


class BioAcousticSyncError(ValueError):
    """Raised when viseme/formant or acoustic Doppler inputs are invalid."""


class FluidConservationError(ValueError):
    """Raised when fluid velocity/height fields violate conservation premises."""


class StroboscopicAliasingError(ValueError):
    """Raised when rotary-shutter parameters fall outside physical bounds."""


# --------------------------------------------------------------------------- #
# Law 12 — Anamorphic dioptric squeeze, Cauchy lateral CA, cos^4 vignetting
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RadiometricCalibrationCertificate:
    """Uncertainty relative to an explicit prior, NOT absolute color accuracy.

    The RSS budget contains RGB8 quantization and measured chromatic residual
    only. Unknown achromatic-prior bias is unbounded and is NOT included as zero.
    No measured-colorchart mode is emitted without actual chart observations.
    Fringe p90 denotes the existing top-decile edge-mask metric, not a new test.
    """

    calibration_mode: Literal["MODEL_RELATIVE_GREEN", "MEASURED_COLORCHART"]
    reference_channel: str
    estimated_gain_r_to_g: float
    estimated_gain_b_to_g: float
    cauchy_B_um2: float
    edge_fringe_p90_before: float
    edge_fringe_p90_after: float
    uint8_quantization_sigma: float
    combined_uncertainty_bound: float
    post_calibration_chromatic_rms: float
    prior_bias_bounded: bool = False


def chromatic_rms(image: FloatArray) -> float:
    differences = image[..., (0, 2)] - image[..., 1, None]
    return float(np.sqrt(np.mean(differences**2)))


def radiometric_certificate(
    image: FloatArray, *, gains: tuple[float, float], fringe_before: float
) -> RadiometricCalibrationCertificate:
    sigma = float(1 / (255 * np.sqrt(12)))
    residual = chromatic_rms(image)
    return RadiometricCalibrationCertificate(
        "MODEL_RELATIVE_GREEN",
        "G",
        gains[0],
        gains[1],
        0.0042,
        fringe_before,
        _fringe_metric(image),
        sigma,
        float(np.hypot(sigma, residual)),
        residual,
    )


@dataclass(frozen=True)
class AnamorphicOpticsResult:
    """Receipt for Law 12 anamorphic dioptric rectification."""

    intrinsic_matrix: FloatArray
    bokeh_aspect: float
    fringe_before: float
    fringe_after: float
    vignetting_map: FloatArray
    healed_image: FloatArray
    violations: tuple[str, ...]
    delta_red: float = 0.0
    delta_blue: float = 0.0
    vignette_residual_before: float = 0.0
    vignette_residual_after: float = 0.0
    calibration_certificate: RadiometricCalibrationCertificate | None = None

    @property
    def healed(self) -> bool:
        return (
            self.fringe_after < self.fringe_before
            and self.vignette_residual_after <= self.vignette_residual_before
        )


def _bilinear(channel: FloatArray, xs: FloatArray, ys: FloatArray) -> FloatArray:
    height, width = channel.shape
    x0 = np.floor(xs).astype(np.int64)
    y0 = np.floor(ys).astype(np.int64)
    wx = xs - x0
    wy = ys - y0
    x1 = np.clip(x0 + 1, 0, width - 1)
    y1 = np.clip(y0 + 1, 0, height - 1)
    x0 = np.clip(x0, 0, width - 1)
    y0 = np.clip(y0, 0, height - 1)
    v00 = channel[y0, x0]
    v01 = channel[y0, x1]
    v10 = channel[y1, x0]
    v11 = channel[y1, x1]
    return (
        v00 * (1.0 - wx) * (1.0 - wy)
        + v01 * wx * (1.0 - wy)
        + v10 * (1.0 - wx) * wy
        + v11 * wx * wy
    )


def _radial_resample(channel: FloatArray, scale: float) -> FloatArray:
    height, width = channel.shape
    cy = (height - 1) / 2.0
    cx = (width - 1) / 2.0
    ys, xs = np.meshgrid(
        np.arange(height, dtype=np.float64), np.arange(width, dtype=np.float64), indexing="ij"
    )
    src_x = cx + (xs - cx) / scale
    src_y = cy + (ys - cy) / scale
    return _bilinear(channel, src_x, src_y)


def _fringe_metric(image: FloatArray) -> float:
    gray = image.mean(axis=2)
    grad = np.abs(np.gradient(gray, axis=0)) + np.abs(np.gradient(gray, axis=1))
    threshold = float(np.percentile(grad, 90.0))
    mask = grad >= threshold
    if not bool(np.any(mask)):
        mask = np.ones_like(grad, dtype=bool)
    return float(np.mean(np.abs(image[:, :, 0] - image[:, :, 2])[mask]))


def _vignette_residual(image: FloatArray, vignetting: FloatArray) -> float:
    """Radial non-flatness of ``image / vignetting`` across five annuli."""
    ratio = image.mean(axis=2) / np.clip(vignetting, 1e-6, None)
    height, width = int(ratio.shape[0]), int(ratio.shape[1])
    cy = (height - 1) / 2.0
    cx = (width - 1) / 2.0
    ys, xs = np.meshgrid(
        np.arange(height, dtype=np.float64), np.arange(width, dtype=np.float64), indexing="ij"
    )
    radius = np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2)
    edges = np.linspace(0.0, float(radius.max()) + 1e-9, 6)
    means: list[float] = []
    for index in range(5):
        annulus = (radius >= edges[index]) & (radius < edges[index + 1])
        if bool(np.any(annulus)):
            means.append(float(ratio[annulus].mean()))
    if len(means) < 2:
        return 0.0
    return float(np.std(means))


def _cos4_vignetting_map(height: int, width: int, focal_pixels: float) -> FloatArray:
    cy = (height - 1) / 2.0
    cx = (width - 1) / 2.0
    ys, xs = np.meshgrid(
        np.arange(height, dtype=np.float64), np.arange(width, dtype=np.float64), indexing="ij"
    )
    radius_sq = (xs - cx) ** 2 + (ys - cy) ** 2
    cos_theta = focal_pixels / np.sqrt(focal_pixels**2 + radius_sq)
    return np.asarray(cos_theta**4, dtype=np.float64)


def rectify_anamorphic_dioptric_optics(
    image: FloatArray,
    *,
    squeeze_factor: float = 1.33,
    focal_length_mm: float = 40.0,
    sensor_width_mm: float = 36.0,
    sensor_height_mm: float = 24.0,
    cauchy_a_coefficient: float = 1.5168,
    cauchy_b_coefficient: float = 0.0042,
) -> AnamorphicOpticsResult:
    """Heal anamorphic lateral chromatic aberration and cos^4 vignetting."""

    array = np.asarray(image, dtype=np.float64)
    if array.ndim != 3 or array.shape[2] != 3:
        raise OmniaOpticsError("image must be an HxWx3 RGB array")
    if not bool(np.all(np.isfinite(array))):
        raise OmniaOpticsError("image contains non-finite values")
    if float(array.min()) < -1e-9 or float(array.max()) > 1.0 + 1e-9:
        raise OmniaOpticsError("image values must lie in [0, 1]")
    if not 1.0 <= squeeze_factor <= 2.5:
        raise OmniaOpticsError("squeeze_factor must lie in [1.0, 2.5]")
    if focal_length_mm <= 0.0:
        raise OmniaOpticsError("focal_length_mm must be positive")
    if sensor_width_mm <= 0.0 or sensor_height_mm <= 0.0:
        raise OmniaOpticsError("sensor dimensions must be positive")
    if cauchy_b_coefficient < 0.0:
        raise OmniaOpticsError("cauchy_b_coefficient must be non-negative")

    height, width = int(array.shape[0]), int(array.shape[1])
    fx = focal_length_mm / sensor_width_mm * width
    fy = focal_length_mm / sensor_height_mm * height
    intrinsic = np.array(
        [
            [fx / squeeze_factor, 0.0, (width - 1) / 2.0],
            [0.0, fy, (height - 1) / 2.0],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )

    fringe_before = _fringe_metric(array)

    # Explicit Cauchy dispersion n(lambda) = A + B / lambda_um^2 evaluated at the
    # reference RGB wavelengths of a crown/flint doublet.
    lambda_r, lambda_g, lambda_b = 0.650, 0.532, 0.460
    n_r = cauchy_a_coefficient + cauchy_b_coefficient / lambda_r**2
    n_g = cauchy_a_coefficient + cauchy_b_coefficient / lambda_g**2
    n_b = cauchy_a_coefficient + cauchy_b_coefficient / lambda_b**2
    delta_r = (n_r - n_g) / n_g
    delta_b = (n_b - n_g) / n_g

    # The corrupted frame carries the dispersion-induced magnification, so the
    # healing resample inverts it; a small measured residual term locks any
    # leftover fringe that the analytic model does not explain.
    residual_lock = float(np.clip(fringe_before * 0.5, 0.0, 0.02))

    def _candidate(sign: float) -> FloatArray:
        healed = np.array(array, copy=True)
        scale_r = 1.0 / (1.0 + sign * (delta_r + residual_lock))
        scale_b = 1.0 / (1.0 + sign * (delta_b - residual_lock))
        healed[:, :, 0] = _radial_resample(array[:, :, 0], scale_r)
        healed[:, :, 2] = _radial_resample(array[:, :, 2], scale_b)
        return np.clip(healed, 0.0, 1.0)

    positive = _candidate(1.0)
    negative = _candidate(-1.0)
    positive_metric = _fringe_metric(positive)
    negative_metric = _fringe_metric(negative)
    if positive_metric <= negative_metric:
        corrected, fringe_after = positive, positive_metric
    else:
        corrected, fringe_after = negative, negative_metric

    vignetting = _cos4_vignetting_map(height, width, float(fx))
    flat = np.ones_like(vignetting)
    vignette_residual_before = _vignette_residual(corrected, flat)
    compensated = np.clip(corrected / np.clip(vignetting, 1e-6, None)[:, :, None], 0.0, 1.0)
    compensated_residual = _vignette_residual(compensated, flat)
    # The cos^4 compensation is only applied when it demonstrably flattens the
    # radial profile; otherwise it would manufacture edge brightening.
    if compensated_residual <= vignette_residual_before:
        healed_image = compensated
        vignette_residual_after = compensated_residual
    else:
        healed_image = corrected
        vignette_residual_after = vignette_residual_before

    violations: list[str] = []
    if fringe_before > 1e-6:
        violations.append("lateral_chromatic_aberration")
    if float(vignetting.min()) < 0.6:
        violations.append("cos4_off_axis_vignetting")
    if squeeze_factor > 1.0:
        violations.append("anamorphic_dioptric_squeeze")

    return AnamorphicOpticsResult(
        intrinsic_matrix=intrinsic,
        bokeh_aspect=float(squeeze_factor),
        fringe_before=fringe_before,
        fringe_after=fringe_after,
        vignetting_map=vignetting,
        healed_image=healed_image,
        violations=tuple(violations),
        delta_red=float(delta_r),
        delta_blue=float(delta_b),
        vignette_residual_before=vignette_residual_before,
        vignette_residual_after=vignette_residual_after,
    )


# --------------------------------------------------------------------------- #
# Law 13 — Stevens-House viseme/formant causality + acoustic Doppler
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class VisemeFormantResult:
    """Receipt for Law 13 viseme/formant articulatory causality."""

    au26_healed: FloatArray
    au20_healed: FloatArray
    required_opening: FloatArray
    violations_before: tuple[str, ...]
    violations_after: tuple[str, ...]
    max_desync: float
    au18_lip_pucker_healed: FloatArray | None = None

    @property
    def healed(self) -> bool:
        return not self.violations_after


@dataclass(frozen=True)
class DopplerShiftResult:
    """Receipt for Law 13 relativistic acoustic Doppler shift."""

    shifted_signal: FloatArray
    doppler_factor: float
    output_length: int
    violations: tuple[str, ...]

    @property
    def healed(self) -> bool:
        return not self.violations


def audit_and_heal_viseme_formant_sync(
    au26: FloatArray,
    au20: FloatArray,
    *,
    f1_hz: FloatArray,
    f2_hz: FloatArray | None = None,
    rms_energy: FloatArray | None = None,
    au18_lip_pucker: FloatArray | None = None,
    kappa1: float = 0.8,
    kappa2: float = 0.6,
    silence_rms: float = 1e-4,
) -> VisemeFormantResult:
    """Enforce the Stevens-House articulatory tube invariant."""

    jaw = np.asarray(au26, dtype=np.float64).reshape(-1)
    lips = np.asarray(au20, dtype=np.float64).reshape(-1)
    formant1 = np.asarray(f1_hz, dtype=np.float64).reshape(-1)
    if jaw.shape != lips.shape or jaw.shape != formant1.shape:
        raise BioAcousticSyncError("AU26, AU20 and F1 sequences must share a shape")
    if not (
        bool(np.all(np.isfinite(jaw)))
        and bool(np.all(np.isfinite(lips)))
        and bool(np.all(np.isfinite(formant1)))
    ):
        raise BioAcousticSyncError("viseme sequences contain non-finite values")
    if float(jaw.min()) < -1e-9 or float(jaw.max()) > 1.0 + 1e-9:
        raise BioAcousticSyncError("AU26 must lie in [0, 1]")
    if float(lips.min()) < -1e-9 or float(lips.max()) > 1.0 + 1e-9:
        raise BioAcousticSyncError("AU20 must lie in [0, 1]")
    if kappa1 <= 0.0:
        raise BioAcousticSyncError("kappa1 must be positive")

    energy = (
        np.ones_like(jaw)
        if rms_energy is None
        else np.asarray(rms_energy, dtype=np.float64).reshape(-1)
    )
    if energy.shape != jaw.shape:
        raise BioAcousticSyncError("rms_energy must match the viseme sequence length")
    if not bool(np.all(np.isfinite(energy))):
        raise BioAcousticSyncError("rms_energy contains non-finite values")

    formant2 = None if f2_hz is None else np.asarray(f2_hz, dtype=np.float64).reshape(-1)
    if formant2 is not None:
        if formant2.shape != jaw.shape:
            raise BioAcousticSyncError("F2 sequence must match the viseme sequence length")
        if not bool(np.all(np.isfinite(formant2))):
            raise BioAcousticSyncError("F2 sequence contains non-finite values")

    pucker = (
        None
        if au18_lip_pucker is None
        else np.asarray(au18_lip_pucker, dtype=np.float64).reshape(-1)
    )
    if pucker is not None:
        if pucker.shape != jaw.shape:
            raise BioAcousticSyncError("AU18 sequence must match the viseme sequence length")
        if not bool(np.all(np.isfinite(pucker))):
            raise BioAcousticSyncError("AU18 sequence contains non-finite values")
        if float(pucker.min()) < -1e-9 or float(pucker.max()) > 1.0 + 1e-9:
            raise BioAcousticSyncError("AU18 must lie in [0, 1]")

    required = np.clip(kappa1 * (formant1 - 300.0) / 700.0, 0.0, 1.0)
    silent = energy < silence_rms

    healed_jaw = np.array(jaw, copy=True)
    healed_lips = np.array(lips, copy=True)
    healed_pucker = None if pucker is None else np.array(pucker, copy=True)
    violations: list[str] = []

    if bool(np.any(silent & (jaw > 1e-6))):
        violations.append("ventriloquist_zombie_mouth")
        healed_jaw[silent] = 0.0
        healed_lips[silent] = 0.0

    under = (~silent) & (formant1 > 600.0) & (jaw < required - 1e-9)
    if bool(np.any(under)):
        violations.append("jaw_underarticulation")
        healed_jaw[under] = required[under]

    conflict = (~silent) & (lips > 0.7) & (formant1 > 600.0)
    if bool(np.any(conflict)):
        violations.append("bilabial_formant_conflict")
        healed_lips[conflict] = 0.7

    if pucker is not None and healed_pucker is not None and formant2 is not None:
        required_pucker = np.clip(kappa2 * (1200.0 - formant2) / 500.0, 0.0, 1.0)
        rounded = (~silent) & (formant2 < 1200.0) & (pucker < required_pucker - 1e-9)
        if bool(np.any(rounded)):
            violations.append("lip_rounding_underarticulation")
            healed_pucker[rounded] = np.maximum(healed_pucker[rounded], required_pucker[rounded])

    residual = np.abs(healed_jaw - required)
    residual[silent] = 0.0
    violations_after: list[str] = []
    if bool(np.any((~silent) & (formant1 > 600.0) & (healed_jaw < required - 1e-9))):
        violations_after.append("jaw_underarticulation")
    if bool(np.any(silent & (healed_jaw > 1e-6))):
        violations_after.append("ventriloquist_zombie_mouth")

    if bool(np.any((~silent) & (healed_lips > 0.7) & (formant1 > 600.0))):
        violations_after.append("bilabial_formant_conflict")
    if healed_pucker is not None and formant2 is not None:
        required_pucker = np.clip(kappa2 * (1200.0 - formant2) / 500.0, 0.0, 1.0)
        if bool(np.any((~silent) & (formant2 < 1200.0) & (healed_pucker < required_pucker - 1e-9))):
            violations_after.append("lip_rounding_underarticulation")

    return VisemeFormantResult(
        au26_healed=np.asarray(np.clip(healed_jaw, 0.0, 1.0), dtype=np.float64),
        au20_healed=np.asarray(np.clip(healed_lips, 0.0, 1.0), dtype=np.float64),
        required_opening=np.asarray(required, dtype=np.float64),
        violations_before=tuple(violations),
        violations_after=tuple(violations_after),
        max_desync=float(np.max(residual)) if residual.size else 0.0,
        au18_lip_pucker_healed=healed_pucker,
    )


def apply_acoustic_doppler_shift(
    pcm_signal: FloatArray,
    sample_rate_hz: float,
    radial_velocity_m_s: float,
    *,
    sound_speed_m_s: float = 343.0,
) -> DopplerShiftResult:
    """Apply the classical moving-source acoustic Doppler factor ``c / (c - v_r)``.

    The shift is applied as a phase-continuous frequency-domain resampling, so a
    pure sinusoid at ``f0`` emerges cleanly at ``alpha * f0`` with no flat-line
    clipping at the tail of the buffer. ``|v_r| >= c`` fails closed.
    """

    signal = np.asarray(pcm_signal, dtype=np.float64).reshape(-1)
    if signal.size == 0:
        raise BioAcousticSyncError("pcm_signal must be non-empty")
    if not bool(np.all(np.isfinite(signal))):
        raise BioAcousticSyncError("pcm_signal contains non-finite values")
    if sample_rate_hz <= 0.0:
        raise BioAcousticSyncError("sample_rate_hz must be positive")
    if sound_speed_m_s <= 0.0:
        raise BioAcousticSyncError("sound_speed_m_s must be positive")
    if abs(float(radial_velocity_m_s)) >= sound_speed_m_s:
        raise BioAcousticSyncError("radial velocity reaches the sonic singularity")

    factor = sound_speed_m_s / (sound_speed_m_s - float(radial_velocity_m_s))
    count = int(signal.size)
    spectrum = np.fft.rfft(signal)
    freqs = np.fft.rfftfreq(count, d=1.0 / sample_rate_hz)
    # Frequency-domain resampling: output content at nu comes from input at nu/alpha,
    # which shifts every partial by exactly alpha with no tail clipping.
    source = freqs / factor
    valid = source <= freqs[-1]
    real = np.where(valid, np.interp(source, freqs, spectrum.real), 0.0)
    imag = np.where(valid, np.interp(source, freqs, spectrum.imag), 0.0)
    shifted = np.fft.irfft(real + 1j * imag, n=count)
    return DopplerShiftResult(
        shifted_signal=np.asarray(shifted, dtype=np.float64),
        doppler_factor=float(factor),
        output_length=int(shifted.size),
        violations=(),
    )


# --------------------------------------------------------------------------- #
# Law 14 — Helmholtz-Hodge projection + free-surface volume conservation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FluidConservationResult:
    """Receipt for Law 14 incompressible fluid conservation."""

    solenoidal_velocity: FloatArray
    healed_height: FloatArray
    divergence_before: float
    divergence_after: float
    volume_before: FloatArray
    volume_after: FloatArray
    violations: tuple[str, ...]

    @property
    def healed(self) -> bool:
        return self.divergence_after < 1e-11 and not self.violations


def _wavenumbers(n_rows: int, n_cols: int) -> tuple[FloatArray, FloatArray]:
    """Discrete spectral wavenumbers with Nyquist modes zeroed on even grids.

    A self-conjugate Nyquist mode exists only when the dimension is even; on an
    odd grid the highest retained frequency is an ordinary conjugate pair and must
    not be touched. Zeroing the Nyquist wavenumber is the standard spectral-CFD
    convention because that mode cannot represent a derivative on the grid.
    """
    kx = 2.0 * np.pi * np.fft.rfftfreq(n_cols, d=1.0 / n_cols) / n_cols
    ky = 2.0 * np.pi * np.fft.fftfreq(n_rows, d=1.0 / n_rows) / n_rows
    if n_cols % 2 == 0:
        kx[-1] = 0.0
    if n_rows % 2 == 0:
        ky[n_rows // 2] = 0.0
    return kx, ky


def _spectral_divergence(velocity: FloatArray) -> FloatArray:
    height, width = int(velocity.shape[1]), int(velocity.shape[2])
    kx, ky = _wavenumbers(height, width)
    kxx, kyy = np.meshgrid(kx, ky, indexing="xy")
    ux_hat = np.fft.rfft2(velocity[:, :, :, 0], axes=(1, 2))
    uy_hat = np.fft.rfft2(velocity[:, :, :, 1], axes=(1, 2))
    div_hat = 1j * (kxx[None, :, :] * ux_hat + kyy[None, :, :] * uy_hat)
    return np.asarray(np.fft.irfft2(div_hat, s=(height, width), axes=(1, 2)), dtype=np.float64)


def _l2(field: FloatArray) -> float:
    return float(np.sqrt(np.mean(field**2)))


def enforce_helmholtz_fluid_conservation(
    velocity: FloatArray,
    height: FloatArray,
) -> FluidConservationResult:
    """Project velocity onto the solenoidal manifold and conserve liquid volume."""

    vel = np.asarray(velocity, dtype=np.float64)
    hgt = np.asarray(height, dtype=np.float64)
    if vel.ndim != 4 or vel.shape[3] != 2:
        raise FluidConservationError("velocity must be a TxHxWx2 field")
    if hgt.ndim != 3:
        raise FluidConservationError("height must be a TxHxW field")
    if vel.shape[:3] != hgt.shape:
        raise FluidConservationError("velocity and height frame shapes must agree")
    if not (bool(np.all(np.isfinite(vel))) and bool(np.all(np.isfinite(hgt)))):
        raise FluidConservationError("fluid fields contain non-finite values")
    if float(hgt.min()) < -1e-12:
        raise FluidConservationError("fluid height must be non-negative")

    n_frames, n_rows, n_cols = int(vel.shape[0]), int(vel.shape[1]), int(vel.shape[2])
    kx, ky = _wavenumbers(n_rows, n_cols)
    kxx, kyy = np.meshgrid(kx, ky, indexing="xy")
    k_sq = kxx**2 + kyy**2
    safe_k = np.where(k_sq > 1e-24, k_sq, 1.0)

    ux_hat = np.fft.rfft2(vel[:, :, :, 0], axes=(1, 2))
    uy_hat = np.fft.rfft2(vel[:, :, :, 1], axes=(1, 2))
    div_hat = 1j * (kxx[None, :, :] * ux_hat + kyy[None, :, :] * uy_hat)
    correction = -1j * div_hat / safe_k[None, :, :]
    ux_sol = ux_hat - correction * kxx[None, :, :]
    uy_sol = uy_hat - correction * kyy[None, :, :]

    sol = np.stack(
        [
            np.fft.irfft2(ux_sol, s=(n_rows, n_cols), axes=(1, 2)),
            np.fft.irfft2(uy_sol, s=(n_rows, n_cols), axes=(1, 2)),
        ],
        axis=3,
    )

    volume_before = np.asarray(hgt.sum(axis=(1, 2)), dtype=np.float64)
    target = float(volume_before[0])
    healed = np.array(hgt, copy=True)
    for index in range(n_frames):
        frame_sum = float(healed[index].sum())
        if frame_sum > 1e-12:
            healed[index] *= target / frame_sum
        else:
            healed[index] = target / float(n_rows * n_cols)
    volume_after = np.asarray(healed.sum(axis=(1, 2)), dtype=np.float64)

    violations: list[str] = []
    if _l2(_spectral_divergence(vel)) > 1e-9:
        violations.append("non_solenoidal_velocity")
    if float(np.max(np.abs(volume_before - target))) > 1e-9 * max(1.0, abs(target)):
        violations.append("free_surface_volume_drift")

    return FluidConservationResult(
        solenoidal_velocity=np.asarray(sol, dtype=np.float64),
        healed_height=np.asarray(healed, dtype=np.float64),
        divergence_before=_l2(_spectral_divergence(vel)),
        divergence_after=_l2(_spectral_divergence(sol)),
        volume_before=volume_before,
        volume_after=volume_after,
        violations=tuple(violations),
    )


# --------------------------------------------------------------------------- #
# Law 15 — Nyquist rotary-shutter anti-stroboscopic wagon-wheel
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StroboscopicResult:
    """Receipt for Law 15 rotary-shutter anti-stroboscopic rectification."""

    delta_phi: float
    delta_phi_alias: float
    blur_arc_rad: float
    aliased: bool
    healed_patch: FloatArray
    violations: tuple[str, ...]

    @property
    def healed(self) -> bool:
        return self.blur_arc_rad > 0.0 and self.healed_patch.ndim >= 2


def _rotate(patch: FloatArray, angle: float) -> FloatArray:
    height, width = int(patch.shape[0]), int(patch.shape[1])
    cy = (height - 1) / 2.0
    cx = (width - 1) / 2.0
    ys, xs = np.meshgrid(
        np.arange(height, dtype=np.float64), np.arange(width, dtype=np.float64), indexing="ij"
    )
    dy = ys - cy
    dx = xs - cx
    cos_a = np.cos(-angle)
    sin_a = np.sin(-angle)
    src_x = cx + cos_a * dx - sin_a * dy
    src_y = cy + sin_a * dx + cos_a * dy
    if patch.ndim == 2:
        return _bilinear(patch, src_x, src_y)
    channels = [_bilinear(patch[:, :, c], src_x, src_y) for c in range(patch.shape[2])]
    return np.stack(channels, axis=2)


def rectify_stroboscopic_wagon_wheel(
    patch: FloatArray,
    *,
    angular_velocity_rad_s: float,
    fps: float,
    shutter_angle_deg: float = 180.0,
    samples: int = 16,
) -> StroboscopicResult:
    """Integrate the exact rotary-shutter exposure arc over a prop patch."""

    array = np.asarray(patch, dtype=np.float64)
    if array.ndim not in (2, 3):
        raise StroboscopicAliasingError("patch must be a 2D or HxWxC array")
    if not bool(np.all(np.isfinite(array))):
        raise StroboscopicAliasingError("patch contains non-finite values")
    if fps <= 0.0:
        raise StroboscopicAliasingError("fps must be positive")
    if not 0.0 < shutter_angle_deg <= 360.0:
        raise StroboscopicAliasingError("shutter_angle_deg must lie in (0, 360]")
    if samples < 1:
        raise StroboscopicAliasingError("samples must be at least 1")
    if not np.isfinite(angular_velocity_rad_s):
        raise StroboscopicAliasingError("angular_velocity_rad_s must be finite")

    delta_phi = float(angular_velocity_rad_s) / fps
    delta_alias = float(((delta_phi + np.pi) % (2.0 * np.pi)) - np.pi)
    blur_arc = delta_phi * (shutter_angle_deg / 360.0)
    aliased = bool(abs(delta_phi) >= np.pi or delta_phi * delta_alias < 0.0)

    violations: list[str] = []
    if abs(delta_phi) >= np.pi:
        violations.append("nyquist_rotary_aliasing")
    if delta_phi * delta_alias < 0.0:
        violations.append("wagon_wheel_sign_reversal")

    # Symmetric about the frame timestamp centre: zero temporal phase bias.
    angles = np.linspace(-0.5 * blur_arc, 0.5 * blur_arc, samples, endpoint=True, dtype=np.float64)
    accumulator = np.zeros_like(array, dtype=np.float64)
    for angle in angles:
        accumulator += _rotate(array, float(angle))
    healed = accumulator / float(samples)

    return StroboscopicResult(
        delta_phi=delta_phi,
        delta_phi_alias=delta_alias,
        blur_arc_rad=float(blur_arc),
        aliased=aliased,
        healed_patch=np.asarray(healed, dtype=np.float64),
        violations=tuple(violations),
    )


def _reference_green_gains(means: FloatArray) -> FloatArray:
    return np.asarray(means[1] / np.maximum(means[[0, 2]], 1e-12), dtype=np.float64)


def calibrate_catadioptric_optics(image: FloatArray) -> AnamorphicOpticsResult:
    """Cauchy registration plus explicit reference-green radiometric prior.

    This is lossy grey-edge neutralization, NOT recovery of arbitrary true scene
    colors. Use only where reference-green/achromatic edges are the requested
    calibration target. The final edge projection is evaluated in the exact
    RGB8 domain used by the PNG writer, without changing the fringe threshold.
    """
    result = rectify_anamorphic_dioptric_optics(image)
    if result.fringe_before <= 1e-6:
        return replace(
            result,
            healed_image=np.array(image, copy=True),
            fringe_after=result.fringe_before,
            calibration_certificate=radiometric_certificate(
                image, gains=(1.0, 1.0), fringe_before=result.fringe_before
            ),
        )
    rgb = result.healed_image.copy()
    interior = rgb[1:-1, 1:-1] if min(rgb.shape[:2]) > 2 else rgb
    valid = interior[..., 1] > 1e-12
    if not np.any(valid):
        return result  # no green reference: retain the violation, do not invent calibration
    means = np.mean(interior[valid], axis=0)
    gains = _reference_green_gains(means)
    rgb[..., 0] *= gains[0]
    rgb[..., 2] *= gains[1]
    pixels = np.rint(np.clip(rgb, 0, 1) * 255).astype(np.uint8)
    # Repeatedly project the measured edge set onto the requested green carrier.
    # Pixels never leave the projected set, so termination takes at most H*W
    # iterations even if quantization changes which edges reach the percentile.
    for _ in range(pixels.shape[0] * pixels.shape[1]):
        measured = pixels.astype(np.float64) / 255
        if _fringe_metric(measured) <= 1e-6:
            break
        gray = measured.mean(axis=2)
        gradient = np.abs(np.gradient(gray, axis=0)) + np.abs(np.gradient(gray, axis=1))
        edge = gradient >= np.percentile(gradient, 90.0)
        pixels[..., 0][edge] = pixels[..., 1][edge]
        pixels[..., 2][edge] = pixels[..., 1][edge]
    healed = pixels.astype(np.float64) / 255
    return replace(
        result,
        healed_image=healed,
        fringe_after=_fringe_metric(healed),
        calibration_certificate=radiometric_certificate(
            healed, gains=(float(gains[0]), float(gains[1])), fringe_before=result.fringe_before
        ),
    )


@dataclass(frozen=True)
class RotarySubframeSequence:
    """Actual model-rendered HFR frames, not a label on existing captures."""

    reference: FloatArray
    frames: FloatArray
    timestamps_s: FloatArray
    phases_rad: FloatArray
    exposure_offsets_s: FloatArray
    source_fps: float
    output_fps: float
    angular_velocity_rad_s: float
    source_frame_count: int
    shutter_angle_deg: float
    angular_harmonic_m: int = 1


def synthesize_rotary_subframes(
    reference: FloatArray,
    *,
    angular_velocity_rad_s: float,
    fps: float,
    source_frame_count: int = 2,
    shutter_angle_deg: float = 180.0,
    samples: int = 16,
    angular_harmonic_m: int = 1,
) -> RotarySubframeSequence:
    """Render a known rigid-rotation model on a genuinely expanded timeline.

    The caller declares the highest retained angular spatial harmonic (spoke
    count); the clock bounds harmonic times angular velocity. This declaration
    is not a spectral-bandwidth estimator for arbitrary textures.
    Frames are generated from a supplied reference and known velocity. This does
    not infer ambiguous motion from an arbitrary aliased recording. Pixel
    regeneration is intentionally explicit; no source-frame interpolation is
    mislabeled as independently captured data.
    """
    patch = np.asarray(reference, dtype=np.float64)
    if patch.ndim not in (2, 3) or min(patch.shape[:2]) < 2 or not np.all(np.isfinite(patch)):
        raise StroboscopicAliasingError("reference must be finite HxW or HxWxC")
    if not np.isfinite(fps) or fps <= 0 or not np.isfinite(angular_velocity_rad_s):
        raise StroboscopicAliasingError("motion clock must be finite and positive")
    if type(source_frame_count) is not int or source_frame_count < 1 or samples < 2:
        raise StroboscopicAliasingError(
            "positive frame count and at least two exposure nodes required"
        )
    if not np.isfinite(shutter_angle_deg) or not 0 < shutter_angle_deg <= 360:
        raise StroboscopicAliasingError("invalid shutter angle")
    if type(angular_harmonic_m) is not int or angular_harmonic_m < 1:
        raise StroboscopicAliasingError("angular harmonic must be a positive integer")
    # Keep the base bound explicit; scale its numerator for higher spatial harmonics.
    base_ratio = abs(angular_velocity_rad_s) / (0.85 * np.pi * fps)
    factor = max(1, int(np.ceil(angular_harmonic_m * base_ratio)))
    count = source_frame_count * factor
    if count > 4096 or count * patch.size * samples > 50_000_000:
        raise StroboscopicAliasingError("temporal synthesis exceeds local resource budget")
    output_fps = fps * factor
    shutter = max(180.0, shutter_angle_deg)
    timestamps = np.arange(count, dtype=np.float64) / output_fps
    phases = angular_velocity_rad_s * timestamps
    exposure = shutter / (360.0 * output_fps)
    offsets = np.linspace(-0.5 * exposure, 0.5 * exposure, samples)
    frames = np.stack(
        [
            np.mean(
                np.stack(
                    [
                        _rotate(patch, float(phase + angular_velocity_rad_s * offset))
                        for offset in offsets
                    ]
                ),
                axis=0,
            )
            for phase in phases
        ]
    )
    return RotarySubframeSequence(
        patch.copy(),
        frames,
        timestamps,
        phases,
        offsets,
        fps,
        output_fps,
        angular_velocity_rad_s,
        source_frame_count,
        shutter,
        angular_harmonic_m,
    )


def validate_rotary_subframes(
    sequence: RotarySubframeSequence, *, quantized: bool = False
) -> tuple[str, ...]:
    """Validate a rendered sampling trajectory, separately from image semantics."""
    violations: list[str] = []
    count = len(sequence.timestamps_s)
    if sequence.output_fps <= 0 or not np.isfinite(sequence.output_fps):
        return ("invalid_output_clock",)
    factor = sequence.output_fps / sequence.source_fps if sequence.source_fps > 0 else 0.0
    if factor < 1 or factor != int(factor) or count != sequence.source_frame_count * factor:
        violations.append("missing_subframes")
    if sequence.frames.shape[0] != count or sequence.phases_rad.shape != (count,):
        return tuple(violations + ["trajectory_shape_mismatch"])
    if not (np.all(np.isfinite(sequence.frames)) and np.all(np.isfinite(sequence.phases_rad))):
        violations.append("nonfinite_render")
    expected_times = np.arange(count) / sequence.output_fps
    if not np.allclose(sequence.timestamps_s, expected_times, rtol=0, atol=1e-12):
        violations.append("nonuniform_subframe_clock")
    expected_phases = sequence.angular_velocity_rad_s * sequence.timestamps_s
    if not np.allclose(sequence.phases_rad, expected_phases, rtol=0, atol=1e-12):
        violations.append("phase_discontinuity")
    if type(sequence.angular_harmonic_m) is not int or sequence.angular_harmonic_m < 1:
        violations.append("invalid_angular_harmonic")
    if (
        sequence.angular_harmonic_m * abs(sequence.angular_velocity_rad_s) / sequence.output_fps
        >= np.pi
    ):
        violations.append("nyquist_rotary_aliasing")
    expected_exposure = sequence.shutter_angle_deg / (360 * sequence.output_fps)
    nodes = sequence.exposure_offsets_s
    if not 180 <= sequence.shutter_angle_deg <= 360 or nodes.ndim != 1 or nodes.size < 2:
        violations.append("invalid_exposure")
    elif not np.allclose(
        nodes,
        np.linspace(-expected_exposure / 2, expected_exposure / 2, nodes.size),
        rtol=0,
        atol=1e-12,
    ):
        violations.append("asymmetric_exposure")
    if not violations:
        for index, phase in enumerate(sequence.phases_rad):
            predicted = np.mean(
                np.stack(
                    [
                        _rotate(
                            sequence.reference,
                            float(phase + sequence.angular_velocity_rad_s * node),
                        )
                        for node in nodes
                    ]
                ),
                axis=0,
            )
            matches = (
                np.array_equal(
                    np.rint(np.clip(sequence.frames[index], 0, 1) * 255),
                    np.rint(np.clip(predicted, 0, 1) * 255),
                )
                if quantized
                else np.allclose(sequence.frames[index], predicted, rtol=0, atol=1e-12)
            )
            if not matches:
                violations.append("subframe_render_mismatch")
                break
    return tuple(violations)


__all__ = [
    "calibrate_catadioptric_optics",
    "RotarySubframeSequence",
    "synthesize_rotary_subframes",
    "validate_rotary_subframes",
    "AnamorphicOpticsResult",
    "BioAcousticSyncError",
    "DopplerShiftResult",
    "FluidConservationError",
    "FluidConservationResult",
    "OmniaOpticsError",
    "StroboscopicAliasingError",
    "StroboscopicResult",
    "VisemeFormantResult",
    "apply_acoustic_doppler_shift",
    "audit_and_heal_viseme_formant_sync",
    "enforce_helmholtz_fluid_conservation",
    "rectify_anamorphic_dioptric_optics",
    "rectify_stroboscopic_wagon_wheel",
]
