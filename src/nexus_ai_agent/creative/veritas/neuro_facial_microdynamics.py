"""Law 10 — Ekman FACS synergy, asymmetric blinks and dermal subsurface scattering.

Uncanny faces come from smiles without orbicularis oculi activation, metronomic
blinks and plastic Lambertian skin. This module enforces the Duchenne smile
invariant and brow prosody coupling, synthesises asymmetric orbicularis /
levator eyelid kinematics, and applies a wavelength-dependent dual-Gaussian
dipole diffusion to skin irradiance while preserving specular pore detail.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]

AU_COUNT = 12
AU1 = 0  # inner brow raiser
AU2 = 1  # outer brow raiser
AU6 = 5  # orbicularis oculi
AU12 = 11  # zygomaticus major
AU25 = 24  # lips part (index 24 in a 26-AU vector)
AU26 = 25  # jaw drop

DUCHENNE_KAPPA = 0.5
DUCHENNE_TAU0 = 0.1
TAU_BLINK_DOWN_S = 0.075
TAU_BLINK_UP_S = 0.175


class FacialDynamicsError(ValueError):
    """Raised for invalid FACS, blink or skin-diffusion inputs."""


@dataclass(frozen=True)
class FacsResult:
    """Corrected FACS action-unit activation with violations."""

    action_units: FloatArray
    violations_before: tuple[str, ...]
    violations_after: tuple[str, ...]


@dataclass(frozen=True)
class BlinkCurve:
    """A sampled eyelid closure curve in [0, 1]."""

    time_s: FloatArray
    closure: FloatArray
    duration_s: float
    peak_closure: float


def _validate_facs(action_units: FloatArray) -> FloatArray:
    au = np.asarray(action_units, dtype=np.float64)
    if au.ndim != 2 or au.shape[1] < AU_COUNT:
        raise FacialDynamicsError("action units must have shape (frames, >=12)")
    if not np.all(np.isfinite(au)):
        raise FacialDynamicsError("action units must be finite")
    if np.any(au < 0.0):
        raise FacialDynamicsError("action-unit activation must be non-negative")
    return au


def _facs_violations(au: FloatArray) -> tuple[str, ...]:
    violations: list[str] = []
    required = DUCHENNE_KAPPA * np.maximum(0.0, au[:, AU12] - DUCHENNE_TAU0)
    if np.any(au[:, AU6] < required - 1e-9):
        violations.append("duchenne_smile")
    if au.shape[1] > AU26 and np.any(au[:, AU26] > au[:, AU25] + 1e-9):
        violations.append("jaw_lip_volume")
    return tuple(violations)


def enforce_facs_synergy_manifold(
    action_units: FloatArray,
    *,
    f0_envelope: FloatArray | None = None,
    duchenne_kappa: float = DUCHENNE_KAPPA,
    tau0: float = DUCHENNE_TAU0,
) -> FacsResult:
    """Project FACS activation onto the neurologically admissible manifold."""
    au = _validate_facs(action_units)
    if duchenne_kappa < 0.0:
        raise FacialDynamicsError("Duchenne coupling must be non-negative")
    violations_before = _facs_violations(au)
    corrected = np.array(au, dtype=np.float64, copy=True)

    required = duchenne_kappa * np.maximum(0.0, corrected[:, AU12] - tau0)
    corrected[:, AU6] = np.maximum(corrected[:, AU6], required)

    if f0_envelope is not None:
        envelope = np.asarray(f0_envelope, dtype=np.float64)
        if envelope.shape != (au.shape[0],):
            raise FacialDynamicsError("f0_envelope must have shape (frames,)")
        if np.any(envelope < 0.0) or not np.all(np.isfinite(envelope)):
            raise FacialDynamicsError("f0_envelope must be finite and non-negative")
        prosody = np.clip(envelope, 0.0, 1.0)
        corrected[:, AU1] = np.maximum(corrected[:, AU1], 0.5 * prosody)
        corrected[:, AU2] = np.maximum(corrected[:, AU2], 0.3 * prosody)

    if corrected.shape[1] > AU26:
        corrected[:, AU26] = np.minimum(corrected[:, AU26], corrected[:, AU25])

    violations_after = _facs_violations(corrected)
    return FacsResult(
        action_units=corrected,
        violations_before=violations_before,
        violations_after=violations_after,
    )


def synthesize_asymmetric_blink_curve(
    duration_s: float,
    *,
    onset_s: float = 0.0,
    tau_down_s: float = TAU_BLINK_DOWN_S,
    tau_up_s: float = TAU_BLINK_UP_S,
    sample_rate_hz: float = 200.0,
) -> BlinkCurve:
    """Synthesise a fast-close / slow-open physiological blink."""
    if not np.isfinite(duration_s) or duration_s <= 0.0:
        raise FacialDynamicsError("blink duration must be positive and finite")
    if not np.isfinite(onset_s) or onset_s < 0.0:
        raise FacialDynamicsError("blink onset must be non-negative and finite")
    if tau_down_s <= 0.0 or tau_up_s <= 0.0:
        raise FacialDynamicsError("eyelid time constants must be positive")
    if tau_up_s <= tau_down_s:
        raise FacialDynamicsError("levator reopening must be slower than closure")
    if sample_rate_hz <= 0.0:
        raise FacialDynamicsError("sample rate must be positive")

    samples = max(2, int(round(duration_s * sample_rate_hz)) + 1)
    time_s = np.linspace(0.0, duration_s, samples, dtype=np.float64)
    local = time_s - onset_s
    closure = np.zeros_like(time_s)
    closing = (local >= 0.0) & (local <= tau_down_s)
    opening = local > tau_down_s
    closure[closing] = local[closing] / tau_down_s
    peak = 1.0
    closure[opening] = peak * np.exp(-(local[opening] - tau_down_s) / tau_up_s)
    closure = np.clip(closure, 0.0, 1.0)
    return BlinkCurve(
        time_s=time_s,
        closure=closure,
        duration_s=duration_s,
        peak_closure=float(closure.max()),
    )


def _gaussian_transfer(shape: tuple[int, int], sigma_px: float) -> FloatArray:
    rows, cols = shape
    freq_r = np.fft.fftfreq(rows)[:, None]
    freq_c = np.fft.rfftfreq(cols)[None, :]
    radius_sq = freq_r**2 + freq_c**2
    return np.exp(-2.0 * (np.pi**2) * (sigma_px**2) * radius_sq)


def _channel_sigma(sigma_mm: tuple[float, float, float], pixel_scale_mm: float) -> list[float]:
    return [max(0.0, value) / pixel_scale_mm for value in sigma_mm]


def apply_dermal_subsurface_scattering(
    diffuse_rgb: FloatArray,
    specular_rgb: FloatArray,
    skin_mask: FloatArray,
    *,
    sigma_mm: tuple[float, float, float] = (2.0, 1.2, 0.6),
    pixel_scale_mm: float = 1.0,
) -> FloatArray:
    """Diffuse sub-dermal radiance with a dual-Gaussian dipole while keeping speculars."""
    diffuse = np.asarray(diffuse_rgb, dtype=np.float64)
    specular = np.asarray(specular_rgb, dtype=np.float64)
    mask = np.asarray(skin_mask, dtype=np.float64)
    if diffuse.ndim != 3 or diffuse.shape[2] != 3:
        raise FacialDynamicsError("diffuse_rgb must have shape (H, W, 3)")
    if specular.shape != diffuse.shape or mask.shape != diffuse.shape[:2]:
        raise FacialDynamicsError("specular and skin mask shapes must match diffuse")
    if not (np.all(np.isfinite(diffuse)) and np.all(np.isfinite(specular))):
        raise FacialDynamicsError("radiance buffers must be finite")
    if pixel_scale_mm <= 0.0:
        raise FacialDynamicsError("pixel scale must be positive")
    if any(value < 0.0 for value in sigma_mm):
        raise FacialDynamicsError("diffusion sigmas must be non-negative")

    shape = (diffuse.shape[0], diffuse.shape[1])
    sigmas = _channel_sigma(sigma_mm, pixel_scale_mm)
    blurred = np.empty_like(diffuse)
    for channel in range(3):
        sigma_px = sigmas[channel]
        if sigma_px <= 0.0:
            blurred[:, :, channel] = diffuse[:, :, channel]
            continue
        transfer = _gaussian_transfer(shape, sigma_px)
        spectrum = np.fft.rfft2(diffuse[:, :, channel])
        blurred[:, :, channel] = np.fft.irfft2(spectrum * transfer, s=shape)
    weight = np.clip(mask, 0.0, 1.0)[:, :, None]
    composite = weight * blurred + (1.0 - weight) * diffuse
    return composite + specular
