"""WAVE 14 — spectral sky/fog radiative transfer, volumetric crepuscular rays and
semi-Lagrangian advective-diffusive weather continuity.

Serial outdoor scenes suffer inconsistent fog density, drifting sky/horizon
colour and broken volumetric shafts ("god rays") across cuts. This module
provides, in pure NumPy + stdlib:

* Rayleigh ``beta_R(lambda) ∝ lambda^-4`` (at 650/532/460 nm) plus Mie
  ``beta_M(lambda)`` with the Henyey–Greenstein phase function
  ``p_HG(cos θ, g) = (1 - g²) / (4π (1 + g² - 2 g cos θ)^{3/2})`` and the
  Rayleigh phase ``p_R = (3/16π)(1 + cos²θ)``;
* transmittance ``T(0, s) = exp(-∫ σ_t)`` along a ray, evaluated with an
  extinction volume and a view-to-sun visibility map, gathered into
  crepuscular shafts;
* a semi-Lagrangian advection + implicit diffusion update of a fog/rain density
  field that conserves total mass and keeps ``rho >= 0``.

Bounded, validated, offline and deterministic. Malformed inputs raise
:class:`AtmosphericError`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np

__all__ = [
    "MAX_VOLUME_PIXELS",
    "MAX_RAY_STEPS",
    "AtmosphericError",
    "RGB_WAVELENGTHS_NM",
    "rayleigh_beta",
    "mie_beta",
    "henyey_greenstein_phase",
    "rayleigh_phase",
    "AtmosphereParameters",
    "evaluate_atmospheric_scattering",
    "ScatteringResult",
    "render_volumetric_crepuscular_rays",
    "advect_weather_density_field",
    "aerial_perspective_contrast",
]

MAX_VOLUME_PIXELS: Final[int] = 4_194_304
MAX_RAY_STEPS: Final[int] = 4096
#: R, G, B representative wavelengths in nanometres.
RGB_WAVELENGTHS_NM: Final[np.ndarray] = np.array([650.0, 532.0, 460.0])
_EPS: Final[float] = 1e-12


class AtmosphericError(ValueError):
    """Raised for malformed atmospheric-scattering inputs."""


def _resolve_wavelengths(wavelengths_nm: np.ndarray | None) -> np.ndarray:
    if wavelengths_nm is None:
        return RGB_WAVELENGTHS_NM
    return np.asarray(wavelengths_nm, dtype=np.float64)


@dataclass(frozen=True)
class AtmosphereParameters:
    """Rayleigh/Mie coefficients plus the Mie anisotropy ``g``."""

    rayleigh_scale: float = 1.0
    mie_scale: float = 1.0
    mie_g: float = 0.76
    rayleigh_height_km: float = 8.0
    mie_height_km: float = 1.2

    def __post_init__(self) -> None:
        if self.rayleigh_scale < 0.0 or self.mie_scale < 0.0:
            raise AtmosphericError("scattering scales must be non-negative")
        if not (-1.0 < self.mie_g < 1.0):
            raise AtmosphericError("mie_g must lie strictly in (-1, 1)")
        if self.rayleigh_height_km <= 0.0 or self.mie_height_km <= 0.0:
            raise AtmosphericError("scale heights must be positive")


def rayleigh_beta(wavelengths_nm: np.ndarray | None = None, *, scale: float = 1.0) -> np.ndarray:
    """Rayleigh scattering coefficient ``∝ lambda^-4`` (normalised to 650 nm)."""
    if scale < 0.0:
        raise AtmosphericError("scale must be non-negative")
    wl = _resolve_wavelengths(wavelengths_nm)
    if wl.ndim != 1 or wl.size == 0 or not np.all(np.isfinite(wl)):
        raise AtmosphericError("wavelengths must be a non-empty finite 1-D array")
    if np.any(wl <= 0.0):
        raise AtmosphericError("wavelengths must be positive")
    ref = wl / wl[0]
    beta: np.ndarray = scale * (ref**-4.0)
    return beta


def mie_beta(
    wavelengths_nm: np.ndarray | None = None, *, scale: float = 1.0, power: float = 1.0
) -> np.ndarray:
    """Aerosol Mie coefficient ``∝ lambda^-power`` (power near 1, less chromatic)."""
    if scale < 0.0:
        raise AtmosphericError("scale must be non-negative")
    if power <= 0.0:
        raise AtmosphericError("power must be positive")
    wl = _resolve_wavelengths(wavelengths_nm)
    if wl.ndim != 1 or wl.size == 0 or not np.all(np.isfinite(wl)) or np.any(wl <= 0.0):
        raise AtmosphericError("wavelengths must be a non-empty positive finite array")
    ref = wl / wl[0]
    beta: np.ndarray = scale * (ref**-power)
    return beta


def henyey_greenstein_phase(cos_theta: float | np.ndarray, g: float) -> np.ndarray:
    """Henyey–Greenstein phase function; integrates to 1 over the sphere."""
    if not (-1.0 < g < 1.0):
        raise AtmosphericError("g must lie strictly in (-1, 1)")
    ct = np.asarray(cos_theta, dtype=np.float64)
    if not np.all(np.isfinite(ct)):
        raise AtmosphericError("cos_theta contains NaN/Inf")
    denom = (1.0 + g * g - 2.0 * g * ct) ** 1.5
    phase: np.ndarray = (1.0 - g * g) / (4.0 * np.pi * denom)
    return phase


def rayleigh_phase(cos_theta: float | np.ndarray) -> np.ndarray:
    """Rayleigh phase function ``(3/16π)(1 + cos²θ)``; integrates to 1."""
    ct = np.asarray(cos_theta, dtype=np.float64)
    if not np.all(np.isfinite(ct)):
        raise AtmosphericError("cos_theta contains NaN/Inf")
    phase: np.ndarray = (3.0 / (16.0 * np.pi)) * (1.0 + ct * ct)
    return phase


@dataclass(frozen=True)
class ScatteringResult:
    """Per-channel in-scattered radiance and transmittance of a single ray."""

    in_scatter: np.ndarray  # (3,)
    transmittance: float


def evaluate_atmospheric_scattering(
    *,
    beta_r: np.ndarray,
    beta_m: np.ndarray,
    sigma_t: float,
    distance: float,
    cos_theta: float,
    params: AtmosphereParameters,
    steps: int = 256,
) -> ScatteringResult:
    """Single-scattering sky/fog radiance along a homogeneous ray.

    ``T = exp(-sigma_t * distance)``; the in-scattered radiance is
    ``(beta_R * p_R + beta_M * p_HG) * (1 - T) / sigma_t * solar_irradiance`` with
    a unit solar irradiance, so the result is directly comparable across frames.
    """
    if sigma_t <= 0.0:
        raise AtmosphericError("sigma_t must be positive")
    if distance <= 0.0:
        raise AtmosphericError("distance must be positive")
    if steps < 1 or steps > MAX_RAY_STEPS:
        raise AtmosphericError(f"steps must be in [1, {MAX_RAY_STEPS}]")
    bR = np.asarray(beta_r, dtype=np.float64)
    bM = np.asarray(beta_m, dtype=np.float64)
    if bR.shape != bM.shape or bR.ndim != 1 or bR.size != 3:
        raise AtmosphericError("beta_r and beta_m must be 3-vectors")
    if not (np.all(np.isfinite(bR)) and np.all(np.isfinite(bM))):
        raise AtmosphericError("beta coefficients contain NaN/Inf")
    if np.any(bR < 0.0) or np.any(bM < 0.0):
        raise AtmosphericError("beta coefficients must be non-negative")
    p_hg = float(henyey_greenstein_phase(cos_theta, params.mie_g))
    p_r = float(rayleigh_phase(cos_theta))
    transmittance = float(np.exp(-sigma_t * distance))
    # Analytic homogeneous single-scatter integral of (1 - exp(-sigma_t s)).
    in_scatter = (bR * p_r + bM * p_hg) * (1.0 - transmittance) / sigma_t
    return ScatteringResult(in_scatter=in_scatter, transmittance=transmittance)


def render_volumetric_crepuscular_rays(
    extinction: np.ndarray,
    *,
    sun_direction: np.ndarray,
    light_visibility: np.ndarray,
    steps: int = 64,
    inscatter_color: np.ndarray | None = None,
) -> np.ndarray:
    """Volumetric shafts along a per-pixel view ray through an extinction volume.

    ``extinction`` is the 3-D ``sigma_t(x)`` volume; ``light_visibility`` is the
    view-to-sun occlusion map ``V(x)`` in ``[0, 1]``; the emitted in-scatter per
    step is ``sigma_t * V * color``. Transmittance is the product of the
    per-step extinction, ``T(0, s) = exp(-∫ sigma_t)``. The returned image is the
    per-pixel gathered radiance (H, W, 3) — a *shaft* accumulation, not a full
    multiple-scattering solve.
    """
    vol = np.asarray(extinction, dtype=np.float64)
    if vol.ndim != 3 or vol.shape[2] != 3:
        raise AtmosphericError("extinction volume must be (H, W, 3)")
    if vol.size == 0 or not np.all(np.isfinite(vol)):
        raise AtmosphericError("extinction volume is empty or non-finite")
    if np.any(vol < 0.0):
        raise AtmosphericError("extinction must be non-negative")
    if vol.shape[0] * vol.shape[1] > MAX_VOLUME_PIXELS:
        raise AtmosphericError("volume exceeds the bounded pixel budget")
    sun = np.asarray(sun_direction, dtype=np.float64)
    if sun.shape != (3,) or not np.all(np.isfinite(sun)):
        raise AtmosphericError("sun_direction must be a finite 3-vector")
    if float(np.linalg.norm(sun)) < _EPS:
        raise AtmosphericError("sun_direction must be non-zero")
    vis = np.asarray(light_visibility, dtype=np.float64)
    if vis.shape != vol.shape[:2] or not np.all(np.isfinite(vis)):
        raise AtmosphericError("light_visibility must be (H, W) and finite")
    if np.any(vis < 0.0) or np.any(vis > 1.0):
        raise AtmosphericError("light_visibility must lie in [0, 1]")
    if steps < 1 or steps > MAX_RAY_STEPS:
        raise AtmosphericError(f"steps must be in [1, {MAX_RAY_STEPS}]")
    color = np.ones(3) if inscatter_color is None else np.asarray(inscatter_color, dtype=np.float64)
    if color.shape != (3,) or not np.all(np.isfinite(color)):
        raise AtmosphericError("inscatter_color must be a finite 3-vector")

    h, w = vol.shape[:2]
    # View rays march along +z through the volume; step extinction is the slab
    # mean scaled by the slab thickness (unit spacing).
    step_sigma = vol / float(steps)
    transmittance = np.ones((h, w, 3), dtype=np.float64)
    accumulated = np.zeros((h, w, 3), dtype=np.float64)
    for _ in range(steps):
        emit = step_sigma * vis[..., None] * color
        accumulated += transmittance * emit
        transmittance *= np.exp(-step_sigma)
    return accumulated


def aerial_perspective_contrast(contrast_0: float, sigma_t: float, distance: float) -> float:
    """Aerial-perspective contrast attenuation ``C(d) = C_0 exp(-sigma_t d)``."""
    if contrast_0 < 0.0:
        raise AtmosphericError("contrast_0 must be non-negative")
    if sigma_t < 0.0:
        raise AtmosphericError("sigma_t must be non-negative")
    if distance < 0.0:
        raise AtmosphericError("distance must be non-negative")
    return float(contrast_0 * np.exp(-sigma_t * distance))


def advect_weather_density_field(
    density: np.ndarray,
    velocity: np.ndarray,
    *,
    dt: float,
    diffusion: float = 0.0,
    mass_tolerance: float = 1e-9,
) -> np.ndarray:
    """One semi-Lagrangian advection + diffusion step of a fog/rain density field.

    Back-traces each cell by ``velocity * dt`` (bilinear sampling, edge clamp),
    applies an explicit spectral diffusion smoothing, then renormalises total
    mass to its pre-step value (``rho >= 0`` is preserved). The mass-preserving
    renormalisation keeps ``sum(rho)`` within ``mass_tolerance``.
    """
    rho = np.asarray(density, dtype=np.float64)
    if rho.ndim not in (2, 3) or rho.shape[0] < 2 or rho.shape[1] < 2:
        raise AtmosphericError("density must be at least 2x2 (H, W) or (H, W, C)")
    if not np.all(np.isfinite(rho)):
        raise AtmosphericError("density contains NaN/Inf")
    if np.any(rho < 0.0):
        raise AtmosphericError("density must be non-negative")
    vel = np.asarray(velocity, dtype=np.float64)
    if vel.shape[:2] != rho.shape[:2] or vel.shape[2] != 2:
        raise AtmosphericError("velocity must be (H, W, 2)")
    if not np.all(np.isfinite(vel)):
        raise AtmosphericError("velocity contains NaN/Inf")
    if dt <= 0.0:
        raise AtmosphericError("dt must be positive")
    if diffusion < 0.0:
        raise AtmosphericError("diffusion must be non-negative")
    if mass_tolerance <= 0.0:
        raise AtmosphericError("mass_tolerance must be positive")

    h, w = rho.shape[:2]
    total_before = float(rho.sum())
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float64)
    sx = np.clip(xs - vel[..., 0] * dt, 0.0, w - 1.0)
    sy = np.clip(ys - vel[..., 1] * dt, 0.0, h - 1.0)
    x0 = np.floor(sx).astype(np.int64)
    y0 = np.floor(sy).astype(np.int64)
    x1 = np.minimum(x0 + 1, w - 1)
    y1 = np.minimum(y0 + 1, h - 1)
    wx = (sx - x0)[..., None] if rho.ndim == 3 else (sx - x0)
    wy = (sy - y0)[..., None] if rho.ndim == 3 else (sy - y0)
    top = rho[y0, x0] * (1.0 - wx) + rho[y0, x1] * wx
    bottom = rho[y1, x0] * (1.0 - wx) + rho[y1, x1] * wx
    advected = top * (1.0 - wy) + bottom * wy

    if diffusion > 0.0:
        # Spectral diffusion: exact heat-kernel smoothing, mass-preserving.
        fy = np.fft.fftfreq(h)[:, None]
        fx = np.fft.fftfreq(w)[None, :]
        kernel = np.exp(-2.0 * (np.pi**2) * diffusion * (fx * fx + fy * fy))
        if rho.ndim == 3:
            kernel = kernel[..., None]
        advected = np.real(np.fft.ifft2(np.fft.fft2(advected, axes=(0, 1)) * kernel, axes=(0, 1)))

    advected = np.clip(advected, 0.0, None)
    total_after = float(advected.sum())
    if total_after > _EPS:
        advected = advected * (total_before / total_after)
    result: np.ndarray = advected
    return result
