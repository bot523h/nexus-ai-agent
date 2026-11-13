"""Deterministic scene-linear inpainting, frequency retouch, sky and tone curves."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from .contracts import KernelError

FloatImage = NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class PDEInpaintReceipt:
    image: FloatImage
    dirichlet_energy_before: float
    dirichlet_energy_after: float
    biharmonic_rms_residual: float


def inpaint_masked_region_pde(
    image: FloatImage,
    mask: NDArray[np.bool_],
    *,
    iterations: int = 500,
    tolerance: float = 1e-6,
    relaxation: float = 1.35,
) -> PDEInpaintReceipt:
    """Coupled harmonic/biharmonic extension using fixed-exterior SOR updates."""
    src, region = np.asarray(image, float), np.asarray(mask, bool)
    if (
        src.ndim != 3
        or src.shape[2] != 3
        or region.shape != src.shape[:2]
        or not np.any(region)
        or iterations < 1
        or tolerance <= 0
        or not np.isfinite(relaxation)
        or not 1.0 <= relaxation <= 1.9
        or not np.all(np.isfinite(src))
    ):
        raise KernelError("invalid PDE image, mask, or solver controls")
    result = src.copy()
    result[region] = np.mean(src[~region], axis=0) if np.any(~region) else 0

    def energy(a: FloatImage) -> float:
        dx, dy = np.diff(a, axis=1), np.diff(a, axis=0)
        mx, my = region[:, 1:] | region[:, :-1], region[1:, :] | region[:-1, :]
        return float(np.sum(dx[mx] ** 2) + np.sum(dy[my] ** 2))

    def laplacian(a: FloatImage) -> FloatImage:
        padded = np.pad(a, ((1, 1), (1, 1), (0, 0)), mode="edge")
        return padded[:-2, 1:-1] + padded[2:, 1:-1] + padded[1:-1, :-2] + padded[1:-1, 2:] - 4 * a

    before = energy(src)
    height, width = region.shape
    for _ in range(iterations):
        max_change = 0.0
        # Refresh edge extension each sweep; masked pixels themselves are
        # relaxed in-place, while exterior pixels remain Dirichlet boundary data.
        padded = np.pad(result, ((2, 2), (2, 2), (0, 0)), mode="edge")
        for y, x in zip(*np.nonzero(region), strict=True):
            if y == 0 or x == 0 or y == height - 1 or x == width - 1:
                continue
            py, px = y + 2, x + 2
            axial = (
                padded[py - 1, px] + padded[py + 1, px] + padded[py, px - 1] + padded[py, px + 1]
            )
            harmonic = axial / 4
            diagonal = (
                padded[py - 1, px - 1]
                + padded[py - 1, px + 1]
                + padded[py + 1, px - 1]
                + padded[py + 1, px + 1]
            )
            axial_two = (
                padded[py - 2, px] + padded[py + 2, px] + padded[py, px - 2] + padded[py, px + 2]
            )
            biharmonic = (8 * axial - 2 * diagonal - axial_two) / 20
            old = result[y, x].copy()
            target = 0.5 * (harmonic + biharmonic)
            result[y, x] += relaxation * (target - old)
            max_change = max(max_change, float(np.max(np.abs(result[y, x] - old))))
        if max_change < tolerance:
            break
    lap = laplacian(result)
    biharmonic_residual = laplacian(lap)[region]
    residual_rms = (
        float(np.sqrt(np.mean(biharmonic_residual**2))) if biharmonic_residual.size else 0.0
    )
    return PDEInpaintReceipt(result, before, energy(result), residual_rms)


def retouch_portrait_frequency_separation(
    image: FloatImage, skin_mask: NDArray[np.bool_], *, attenuation: float = 0.35
) -> FloatImage:
    """Attenuate mid-band luminance variation within mask, retaining high-band detail."""
    src, mask = np.asarray(image, float), np.asarray(skin_mask, bool)
    if (
        src.ndim != 3
        or src.shape[2] != 3
        or mask.shape != src.shape[:2]
        or not 0 <= attenuation <= 1
    ):
        raise KernelError("invalid retouch image, mask, or attenuation")
    lum = src @ np.array([0.2126, 0.7152, 0.0722])
    padded = np.pad(lum, 1, mode="edge")
    base = (
        sum(
            padded[dy : dy + lum.shape[0], dx : dx + lum.shape[1]]
            for dy, dx in ((0, 1), (2, 1), (1, 0), (1, 2))
        )
        / 4
    )
    mid = lum - base
    # Conductivity suppresses smoothing across strong luminance edges.
    grad = np.hypot(np.gradient(lum, axis=0), np.gradient(lum, axis=1))
    conduct = np.exp(-((grad / 0.08) ** 2))
    corrected = lum - mask * (1 - attenuation) * mid * conduct
    ratio = np.divide(corrected, lum, out=np.ones_like(lum), where=np.abs(lum) > 1e-8)
    return np.asarray(np.clip(src * ratio[..., None], 0, None), dtype=np.float64)


def synthesize_physical_sky_dome(
    width: int,
    height: int,
    solar_elevation_deg: float,
    *,
    camera_pitch_deg: float = 0.0,
    solar_azimuth_deg: float = 0.0,
) -> FloatImage:
    """Procedural Rayleigh/Mie-like sky radiance gradient, row-locked to horizon."""
    if (
        min(width, height) <= 0
        or not -90 <= solar_elevation_deg <= 90
        or not -90 <= camera_pitch_deg <= 90
    ):
        raise KernelError("invalid sky dimensions or angles")
    y = np.linspace(1, -1, height)[:, None]
    elev = np.radians(solar_elevation_deg - camera_pitch_deg)
    t = np.clip((y + 1) / 2, 0, 1)
    horizon = np.stack(
        (
            0.75 + 0.15 * np.cos(np.radians(solar_azimuth_deg)),
            0.48 + 0.1 * np.sin(elev),
            0.28 + 0.2 * np.sin(elev),
        ),
        axis=-1,
    )
    zenith = np.array([0.12, 0.28, 0.65])
    mix = (t**0.65)[..., None]
    return np.broadcast_to(mix * horizon + (1 - mix) * zenith, (height, width, 3)).copy()


def tonemap_scene_linear_filmic(
    hdr: FloatImage,
    *,
    d_min: float = 0.0,
    d_max: float = 1.0,
    gamma_contrast: float = 1.0,
    halation_strength: float = 0.0,
    halation_threshold: float = 1.0,
    halation_radius_px: int = 2,
    log_exposure_slope: float = 2.0,
) -> FloatImage:
    """Apply an explicit H&D-like density curve and bounded red halation."""
    x = np.asarray(hdr, float)
    if (
        x.ndim != 3
        or x.shape[2] != 3
        or np.any(~np.isfinite(x))
        or np.any(x < 0)
        or not np.isfinite(d_min)
        or not np.isfinite(d_max)
        or not 0 <= d_min < d_max <= 1
        or not np.isfinite(gamma_contrast)
        or gamma_contrast <= 0
        or halation_strength < 0
        or halation_threshold < 0
        or halation_radius_px < 0
        or log_exposure_slope <= 0
    ):
        raise KernelError("invalid HDR radiance or emulsion parameters")
    exposed = x.copy()
    if halation_strength and halation_radius_px:
        highlight = np.maximum(x.max(axis=2) - halation_threshold, 0.0)
        radius = halation_radius_px
        coords = np.arange(-radius, radius + 1, dtype=float)
        kernel = np.exp(-(coords**2) / max(1.0, radius**2 / 2))
        kernel /= kernel.sum()
        padded = np.pad(highlight, radius, mode="edge")
        blurred = np.zeros_like(highlight)
        for i, weight in enumerate(kernel):
            blurred += weight * padded[radius:-radius, i : i + highlight.shape[1]]
        padded = np.pad(blurred, ((radius, radius), (0, 0)), mode="edge")
        spread = np.zeros_like(highlight)
        for i, weight in enumerate(kernel):
            spread += weight * padded[i : i + highlight.shape[0], :]
        exposed[..., 0] += halation_strength * spread
    log_exposure = np.log1p(exposed)
    # H&D-like density grows with log exposure and approaches D_max
    # asymptotically, giving an explicit highlight shoulder without clipping.
    shoulder = log_exposure / (log_exposure + 1.0 / log_exposure_slope)
    normalized_density = np.power(shoulder, gamma_contrast)
    density = d_min + (d_max - d_min) * normalized_density
    return np.asarray(np.clip(density, d_min, d_max), dtype=np.float64)
