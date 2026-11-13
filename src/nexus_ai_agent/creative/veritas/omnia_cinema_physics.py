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

from dataclasses import dataclass

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
class AnamorphicOpticsResult:
    """Receipt for Law 12 anamorphic dioptric rectification."""

    intrinsic_matrix: FloatArray
    bokeh_aspect: float
    fringe_before: float
    fringe_after: float
    vignetting_map: FloatArray
    healed_image: FloatArray
    violations: tuple[str, ...]

    @property
    def healed(self) -> bool:
        return self.fringe_after < self.fringe_before


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
    cauchy_b_coefficient: float = 1.5e-3,
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
    # Cauchy dispersion n(lambda) = A + B/lambda^2 sets the sign of the radial
    # lateral displacement between the red and blue channels.
    epsilon = float(np.clip(fringe_before * 0.5 + cauchy_b_coefficient, 0.0, 0.05))

    def _candidate(sign: float) -> FloatArray:
        healed = np.array(array, copy=True)
        healed[:, :, 0] = _radial_resample(array[:, :, 0], 1.0 + sign * epsilon)
        healed[:, :, 2] = _radial_resample(array[:, :, 2], 1.0 - sign * epsilon)
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
    healed_image = np.clip(corrected / np.clip(vignetting, 1e-6, None)[:, :, None], 0.0, 1.0)

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
    kappa1: float = 0.8,
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

    required = np.clip(kappa1 * (formant1 - 300.0) / 700.0, 0.0, 1.0)
    silent = energy < silence_rms

    healed_jaw = np.array(jaw, copy=True)
    healed_lips = np.array(lips, copy=True)
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

    residual = np.abs(healed_jaw - required)
    residual[silent] = 0.0
    violations_after: list[str] = []
    if bool(np.any((~silent) & (formant1 > 600.0) & (healed_jaw < required - 1e-9))):
        violations_after.append("jaw_underarticulation")
    if bool(np.any(silent & (healed_jaw > 1e-6))):
        violations_after.append("ventriloquist_zombie_mouth")

    return VisemeFormantResult(
        au26_healed=np.asarray(np.clip(healed_jaw, 0.0, 1.0), dtype=np.float64),
        au20_healed=np.asarray(np.clip(healed_lips, 0.0, 1.0), dtype=np.float64),
        required_opening=np.asarray(required, dtype=np.float64),
        violations_before=tuple(violations),
        violations_after=tuple(violations_after),
        max_desync=float(np.max(residual)) if residual.size else 0.0,
    )


def apply_acoustic_doppler_shift(
    pcm_signal: FloatArray,
    sample_rate_hz: float,
    radial_velocity_m_s: float,
    *,
    sound_speed_m_s: float = 343.0,
) -> DopplerShiftResult:
    """Apply the exact relativistic acoustic Doppler factor, length-preserving."""

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
    indices = np.arange(signal.size, dtype=np.float64)
    source = np.clip(indices * factor, 0.0, float(signal.size - 1))
    shifted = np.interp(source, indices, signal)
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


def _spectral_divergence(velocity: FloatArray) -> FloatArray:
    height, width = int(velocity.shape[1]), int(velocity.shape[2])
    kx = 2.0 * np.pi * np.fft.rfftfreq(width, d=1.0 / width) / width
    ky = 2.0 * np.pi * np.fft.fftfreq(height, d=1.0 / height) / height
    kx[-1] = 0.0
    if height % 2 == 0:
        ky[height // 2] = 0.0
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
    kx = 2.0 * np.pi * np.fft.rfftfreq(n_cols, d=1.0 / n_cols) / n_cols
    ky = 2.0 * np.pi * np.fft.fftfreq(n_rows, d=1.0 / n_rows) / n_rows
    kx[-1] = 0.0
    if n_rows % 2 == 0:
        ky[n_rows // 2] = 0.0
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
    aliased = bool(abs(delta_phi) > np.pi or delta_phi * delta_alias < 0.0)

    violations: list[str] = []
    if abs(delta_phi) > np.pi:
        violations.append("nyquist_rotary_aliasing")
    if delta_phi * delta_alias < 0.0:
        violations.append("wagon_wheel_sign_reversal")

    angles = np.linspace(0.0, blur_arc, samples, endpoint=False, dtype=np.float64)
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


__all__ = [
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
