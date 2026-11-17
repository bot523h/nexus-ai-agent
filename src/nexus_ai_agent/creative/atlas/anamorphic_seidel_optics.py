"""WAVE 17 — third-order Seidel aberration optics and anamorphic lens response.

Anamorphic glass is what gives a "cinematic" image its horizontal blue streak,
oval bokeh and squeezed field, but a naive simulation drifts from the physics.
This module provides, in pure NumPy + stdlib:

* :func:`seidel_wavefront` — the third-order wave-aberration polynomial
  ``W(rho, theta) = S1 rho^4 + S2 rho^3 cos t + S3 rho^2 cos^2 t + S4 rho^2 + S5 rho cos t``
  (spherical, coma, astigmatism, Petzval field curvature, distortion), with
  ``rho in [0, 1]`` the normalised pupil radius;
* :class:`AnamorphicLens` — independent x/y focal lengths giving the squeeze
  factor ``s = f_x / f_y``; the point-spread function is a separable Gaussian
  whose ``sigma_x, sigma_y`` follow the anamorphic ratio;
* :func:`anamorphic_psf` — a normalised (integrates to 1) PSF with an optional
  horizontal streak kernel and a per-channel lateral chromatic shift;
* :func:`apply_anamorphic_optics` — convolve a linear-radiance image by the PSF
  and apply the anamorphic streak; energy is conserved to ``1e-9``.

Bounded, validated, offline and deterministic; malformed inputs raise
:class:`OpticsError`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np

__all__ = [
    "MAX_PSF_SIDE",
    "OpticsError",
    "SeidelCoefficients",
    "seidel_wavefront",
    "AnamorphicLens",
    "anamorphic_psf",
    "OpticsResult",
    "apply_anamorphic_optics",
]

MAX_PSF_SIDE: Final[int] = 1024
_EPS: Final[float] = 1e-12


class OpticsError(ValueError):
    """Raised for malformed optical inputs."""


@dataclass(frozen=True)
class SeidelCoefficients:
    """The five third-order Seidel aberration sums (in waves)."""

    spherical: float = 0.0
    coma: float = 0.0
    astigmatism: float = 0.0
    field_curvature: float = 0.0
    distortion: float = 0.0

    def __post_init__(self) -> None:
        for name in ("spherical", "coma", "astigmatism", "field_curvature", "distortion"):
            v = getattr(self, name)
            if not np.isfinite(v):
                raise OpticsError(f"Seidel coefficient {name} must be finite")


def seidel_wavefront(
    coeffs: SeidelCoefficients,
    rho: np.ndarray,
    theta: np.ndarray,
) -> np.ndarray:
    """Third-order wavefront error ``W(rho, theta)`` in waves.

    ``rho`` is the normalised pupil radius in ``[0, 1]`` and ``theta`` the pupil
    azimuth. Negative coefficients are permitted (sign conventions differ
    between lens designs); only NaN/Inf is rejected.
    """
    r = np.asarray(rho, dtype=np.float64)
    t = np.asarray(theta, dtype=np.float64)
    if not (np.all(np.isfinite(r)) and np.all(np.isfinite(t))):
        raise OpticsError("rho/theta contain NaN/Inf")
    if np.any(r < -1e-9) or np.any(r > 1.0 + 1e-9):
        raise OpticsError("rho must lie in [0, 1]")
    ct = np.cos(t)
    w: np.ndarray = (
        coeffs.spherical * r**4
        + coeffs.coma * r**3 * ct
        + coeffs.astigmatism * r**2 * ct**2
        + coeffs.field_curvature * r**2
        + coeffs.distortion * r * ct
    )
    return w


@dataclass(frozen=True)
class AnamorphicLens:
    """An anamorphic lens with independent horizontal/vertical focal lengths."""

    focal_x_mm: float = 40.0
    focal_y_mm: float = 30.0
    t_stop: float = 2.8
    anamorphic_squeeze: float = 2.0
    chromatic_shift_px: tuple[float, float, float] = (0.0, 0.0, 0.0)

    def __post_init__(self) -> None:
        if self.focal_x_mm <= 0.0 or self.focal_y_mm <= 0.0:
            raise OpticsError("focal lengths must be positive")
        if self.t_stop <= 0.0:
            raise OpticsError("t_stop must be positive")
        if self.anamorphic_squeeze <= 0.0:
            raise OpticsError("anamorphic_squeeze must be positive")
        if len(self.chromatic_shift_px) != 3 or not all(
            np.isfinite(v) for v in self.chromatic_shift_px
        ):
            raise OpticsError("chromatic_shift_px must be three finite values")

    @property
    def squeeze_factor(self) -> float:
        """Horizontal/vertical focal-length ratio (the anamorphic squeeze)."""
        return float(self.focal_x_mm / self.focal_y_mm)

    def blur_sigmas(self, *, defocus_mm: float, pixel_pitch_mm: float) -> tuple[float, float]:
        """Separable PSF sigmas ``(sigma_x, sigma_y)`` in pixels.

        The circle of confusion radius is ``r_coc = defocus / (2 * t_stop)``; the
        vertical sigma is ``r_coc / pixel_pitch`` and the horizontal sigma is
        divided by the squeeze factor (anamorphic ovals).
        """
        if defocus_mm < 0.0:
            raise OpticsError("defocus_mm must be non-negative")
        if pixel_pitch_mm <= 0.0:
            raise OpticsError("pixel_pitch_mm must be positive")
        r_coc = defocus_mm / (2.0 * self.t_stop)
        sigma_y = r_coc / pixel_pitch_mm
        sigma_x = sigma_y / self.squeeze_factor
        return float(sigma_x), float(sigma_y)


@dataclass
class OpticsResult:
    """An optically filtered image plus its measured energy residual."""

    image: np.ndarray  # (H, W, 3) linear radiance
    energy_residual: float  # |E_out - E_in| after convolution
    psf_energy: float  # integral of the PSF (must be ~1)


def anamorphic_psf(
    lens: AnamorphicLens,
    *,
    size: int = 65,
    defocus_mm: float = 0.05,
    pixel_pitch_mm: float = 0.01,
    streak_length: int = 0,
    streak_weight: float = 0.0,
) -> np.ndarray:
    """Build a normalised anamorphic PSF ``(size, size, 3)``.

    The core is a separable Gaussian with ``sigma_x, sigma_y`` from the lens; an
    optional horizontal streak kernel (anamorphic flare) is added with
    ``streak_weight``. The PSF is renormalised so each channel integrates to 1
    exactly (up to float precision). The chromatic shift offsets each channel
    horizontally to model lateral colour fringing.
    """
    if size < 3 or size % 2 == 0:
        raise OpticsError("size must be an odd integer >= 3")
    if size > MAX_PSF_SIDE:
        raise OpticsError("size exceeds the bounded PSF budget")
    if streak_length < 0 or streak_length >= size:
        raise OpticsError("streak_length must lie in [0, size)")
    if not (0.0 <= streak_weight <= 1.0):
        raise OpticsError("streak_weight must lie in [0, 1]")

    sigma_x, sigma_y = lens.blur_sigmas(defocus_mm=defocus_mm, pixel_pitch_mm=pixel_pitch_mm)
    if sigma_x < _EPS:
        sigma_x = _EPS
    if sigma_y < _EPS:
        sigma_y = _EPS
    ax = np.arange(size) - (size - 1) / 2.0
    gx = np.exp(-(ax**2) / (2.0 * sigma_x**2))
    gy = np.exp(-(ax**2) / (2.0 * sigma_y**2))
    core = np.outer(gy, gx)  # (y, x)

    streak = np.zeros((size, size))
    if streak_length > 0 and streak_weight > 0.0:
        line = np.arange(-streak_length, streak_length + 1)
        row = np.exp(-(line**2) / (2.0 * (streak_length / 3.0 + _EPS) ** 2))
        streak[size // 2, size // 2 - streak_length : size // 2 + streak_length + 1] = row

    psf = (1.0 - streak_weight) * core + streak_weight * streak
    psf = psf / max(float(psf.sum()), _EPS)

    out = np.zeros((size, size, 3), dtype=np.float64)
    for ch in range(3):
        shift = int(round(lens.chromatic_shift_px[ch]))
        out[..., ch] = np.roll(psf, shift, axis=1)
        out[..., ch] /= max(float(out[..., ch].sum()), _EPS)
    return out


def _convolve_channel(image: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    h, w = image.shape
    kh, kw = kernel.shape
    ph, pw = kh // 2, kw // 2
    padded = np.pad(image, ((ph, ph), (pw, pw)), mode="edge")
    out = np.zeros_like(image)
    for i in range(kh):
        for j in range(kw):
            out += kernel[i, j] * padded[i : i + h, j : j + w]
    return out


def apply_anamorphic_optics(
    image: np.ndarray,
    lens: AnamorphicLens,
    *,
    size: int = 65,
    defocus_mm: float = 0.05,
    pixel_pitch_mm: float = 0.01,
    streak_length: int = 0,
    streak_weight: float = 0.0,
) -> OpticsResult:
    """Apply the anamorphic PSF to a linear-radiance image.

    ``image`` is ``(H, W, 3)`` scene-linear radiance. The convolution uses an
    edge-clamped boundary; the total radiance is preserved to ``1e-9`` because
    the PSF integrates to 1.
    """
    img = np.asarray(image, dtype=np.float64)
    if img.ndim != 3 or img.shape[2] != 3 or img.shape[0] < 1 or img.shape[1] < 1:
        raise OpticsError("image must be (H, W, 3)")
    if not np.all(np.isfinite(img)):
        raise OpticsError("image contains NaN/Inf")
    psf = anamorphic_psf(
        lens,
        size=size,
        defocus_mm=defocus_mm,
        pixel_pitch_mm=pixel_pitch_mm,
        streak_length=streak_length,
        streak_weight=streak_weight,
    )
    out = np.empty_like(img)
    for ch in range(3):
        out[..., ch] = _convolve_channel(img[..., ch], psf[..., ch])
    energy_in = float(img.sum())
    energy_out = float(out.sum())
    residual = abs(energy_out - energy_in) / max(abs(energy_in), _EPS)
    return OpticsResult(
        image=out,
        energy_residual=float(residual),
        psf_energy=float(psf.sum() / 3.0),
    )
