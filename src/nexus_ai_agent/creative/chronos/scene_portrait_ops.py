"""Deterministic scene-linear inpainting, frequency retouch, sky and tone curves."""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from .contracts import KernelError

FloatImage = NDArray[np.float64]


def inpaint_masked_region_pde(
    image: FloatImage, mask: NDArray[np.bool_], *, iterations: int = 500, tolerance: float = 1e-6
) -> tuple[FloatImage, float, float]:
    """Solve a discrete harmonic extension by Jacobi iteration with fixed exterior."""
    src, omega = np.asarray(image, float), np.asarray(mask, bool)
    if (
        src.ndim != 3
        or omega.shape != src.shape[:2]
        or not np.any(omega)
        or iterations < 1
        or tolerance <= 0
    ):
        raise KernelError("invalid PDE image, mask, or solver controls")
    result = src.copy()
    ys, xs = np.nonzero(omega)
    result[omega] = np.mean(src[~omega], axis=0) if np.any(~omega) else 0

    def residual(a: FloatImage) -> float:
        lap = 4 * a[1:-1, 1:-1] - a[:-2, 1:-1] - a[2:, 1:-1] - a[1:-1, :-2] - a[1:-1, 2:]
        active = omega[1:-1, 1:-1]
        return float(np.mean(lap[active] ** 2)) if np.any(active) else 0.0

    before = residual(result)
    for _ in range(iterations):
        old = result.copy()
        avg = (old[:-2, 1:-1] + old[2:, 1:-1] + old[1:-1, :-2] + old[1:-1, 2:]) / 4
        inner = omega[1:-1, 1:-1]
        result[1:-1, 1:-1][inner] = avg[inner]
        if float(np.max(np.abs(result - old))) < tolerance:
            break
    return result, before, residual(result)


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


def tonemap_scene_linear_filmic(hdr: FloatImage) -> FloatImage:
    """Monotonic Hable-style rational curve, clamped to display RGB [0,1]."""
    x = np.asarray(hdr, float)
    if np.any(~np.isfinite(x)) or np.any(x < 0):
        raise KernelError("HDR radiance must be finite and nonnegative")
    a, b, c, d, e, f = 0.15, 0.50, 0.10, 0.20, 0.02, 0.30

    def curve(v: NDArray[np.float64]) -> NDArray[np.float64]:
        return ((v * (a * v + c * b) + d * e) / (v * (a * v + b) + d * f)) - e / f

    white = float(curve(np.array(11.2)))
    return np.asarray(np.clip(curve(x) / white, 0, 1), dtype=np.float64)
