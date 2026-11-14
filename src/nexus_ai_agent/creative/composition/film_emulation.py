"""Wave 12 — photochemical film emulation and objective quality metrics.

Digital images are too clean: no grain, no emulsion halation, no projector gate
weave. This module adds those *editorial* artefacts deterministically and, in
the same breath, provides the objective metrics (PSNR / SSIM) needed to prove
that the other Waves' operators (interpolation, relighting, tone mapping) do not
destroy the signal. Pure NumPy + stdlib, offline, bounded and seeded.

* **Halation** — light that passes the emulsion, scatters off the anti-halation
  backing and re-exposes the layer; because red light penetrates furthest, the
  glow is a large-radius, red-weighted bleed of the bright regions.
* **Film grain** — luminance-dependent, seeded Gaussian grain. A flat patch of
  area ``A`` gets grain whose RMS is scaled by ``1/sqrt(A)`` so downscaling keeps
  the perceived grain size roughly constant; a fixed seed makes it reproducible.
* **Gate weave** — deterministic sub-pixel per-frame jitter emulating the
  mechanical instability of a projector gate (a smooth low-frequency drift plus
  a small seeded high-frequency term).
* **Metrics** — global PSNR and the Wang et al. (2004) SSIM (Gaussian-window,
  constants ``C1 = (0.01·L)²``, ``C2 = (0.03·L)²``) for honest verification.

Everything is bounded (:data:`MAX_FILM_PIXELS`); NaN/Inf, negative radiance,
out-of-range parameters, mismatched shapes and bad channel counts raise typed
errors. The module never touches the network and never owns project state.
"""

from __future__ import annotations

from typing import Final

import numpy as np

__all__ = [
    "MAX_FILM_PIXELS",
    "FilmEmulationError",
    "halation",
    "film_grain",
    "gate_weave",
    "apply_film_emulation",
    "psnr",
    "ssim",
    "gaussian_kernel_1d",
]

MAX_FILM_PIXELS: Final[int] = 4_194_304  # 2048 x 2048
_EPS: Final[float] = 1e-12


class FilmEmulationError(ValueError):
    """Raised for malformed film-emulation inputs (bad shape, NaN, over-budget)."""


def _validate_image(img: np.ndarray, *, name: str = "image") -> np.ndarray:
    a = np.asarray(img, dtype=np.float64)
    if a.ndim not in (2, 3) or (a.ndim == 3 and a.shape[-1] not in (1, 3)):
        raise FilmEmulationError(f"{name} must be (H, W) or (H, W, 3), got {a.shape}")
    if not np.all(np.isfinite(a)):
        raise FilmEmulationError(f"{name} contains NaN/Inf")
    if a.size == 0:
        raise FilmEmulationError(f"{name} is empty")
    if a.shape[0] * a.shape[1] > MAX_FILM_PIXELS:
        raise FilmEmulationError(f"{name} exceeds the bounded pixel budget")
    if float(a.min()) < 0.0:
        raise FilmEmulationError(f"{name} has negative scene-linear radiance")
    return a


def gaussian_kernel_1d(sigma: float, *, truncate: float = 3.0) -> np.ndarray:
    """Normalised 1-D Gaussian kernel of the given ``sigma``."""
    if sigma <= 0.0:
        raise FilmEmulationError("sigma must be positive")
    if truncate <= 0.0:
        raise FilmEmulationError("truncate must be positive")
    radius = max(1, int(truncate * sigma + 0.5))
    x = np.arange(-radius, radius + 1, dtype=np.float64)
    k = np.exp(-0.5 * (x / sigma) ** 2)
    kernel: np.ndarray = k / k.sum()
    return kernel


def _blur(img: np.ndarray, sigma: float) -> np.ndarray:
    """Separable Gaussian blur of a float image (2-D or HxWxC) with edge clamp."""
    k = gaussian_kernel_1d(sigma)
    r = k.shape[0] // 2
    a = img.astype(np.float64)
    if a.ndim == 2:
        a = a[..., None]
    pads = [(r, r), (r, r), (0, 0)]
    padded = np.pad(a, pads, mode="edge")
    h, w = a.shape[:2]
    kh = padded.shape[0]
    # Horizontal pass then vertical pass via strided windows.
    out = np.zeros_like(a)
    acc = np.zeros((kh, w, a.shape[2]), dtype=np.float64)
    for i, weight in enumerate(k):
        acc += weight * padded[:, i : i + w, :]
    for j, weight in enumerate(k):
        out += weight * acc[j : j + h, :, :]
    if out.shape[2] == 1 and img.ndim == 2:
        return out[..., 0]
    return out


def halation(
    img: np.ndarray,
    *,
    threshold: float = 0.75,
    sigma: float = 6.0,
    strength: float = 0.4,
    red_bias: float = 1.6,
) -> np.ndarray:
    """Emulsion halation: a red-weighted glow bled from the bright regions.

    Pixels above ``threshold`` are isolated, blurred with a large Gaussian and
    added back, with the red channel scaled by ``red_bias`` (red light penetrates
    the emulsion furthest). ``strength = 0`` is an exact identity.
    """
    if strength < 0.0:
        raise FilmEmulationError("halation strength must be non-negative")
    if red_bias <= 0.0:
        raise FilmEmulationError("red_bias must be positive")
    a = _validate_image(img)
    if a.ndim == 2:
        a3 = np.repeat(a[..., None], 3, axis=-1)
    elif a.shape[-1] == 1:
        a3 = np.repeat(a, 3, axis=-1)
    else:
        a3 = a
    y = a3[..., 0] * 0.2126 + a3[..., 1] * 0.7152 + a3[..., 2] * 0.0722
    bright = np.clip(y - threshold, 0.0, None)[..., None] * a3
    glow = _blur(bright, sigma)
    weights = np.array([red_bias, 1.0, 1.0], dtype=np.float64)
    out = a3 + strength * glow * weights
    if a.ndim == 2:
        out2: np.ndarray = out[..., 0]
        return out2.astype(np.float32)
    return out.astype(np.float32)


def film_grain(
    img: np.ndarray,
    *,
    seed: int = 0,
    sigma: float = 0.03,
    area: int = 1,
) -> np.ndarray:
    """Luminance-dependent, seeded Gaussian film grain.

    The grain is strongest in the mid-tones and fades at both ends (a triangular
    luminance weight), and its RMS scales as ``1/sqrt(area)`` so a larger
    ``area`` (e.g. the frame's pixel count divided by a reference) yields finer
    grain. The same ``seed`` and shape always produce identical grain.
    """
    if seed < 0:
        raise FilmEmulationError("seed must be non-negative")
    if sigma < 0.0:
        raise FilmEmulationError("grain sigma must be non-negative")
    if area < 1:
        raise FilmEmulationError("area must be a positive integer")
    a = _validate_image(img)
    if sigma == 0.0:
        return a.astype(np.float32)
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(a.shape)
    y = a[..., 0] * 0.2126 + a[..., 1] * 0.7152 + a[..., 2] * 0.0722 if a.ndim == 3 else a
    # Triangular mid-tone weight, zero at pure black and pure white.
    weight = 1.0 - np.abs(2.0 * np.clip(y, 0.0, 1.0) - 1.0)
    if a.ndim == 3:
        weight = weight[..., None]
    amplitude = sigma / np.sqrt(float(area))
    out = a + amplitude * noise * weight
    grained: np.ndarray = np.clip(out, 0.0, 1.0).astype(np.float32)
    return grained


def gate_weave(
    img: np.ndarray,
    *,
    frame_index: int,
    seed: int = 0,
    amplitude: float = 1.5,
) -> np.ndarray:
    """Sub-pixel projector-gate jitter for one frame, deterministic in ``(seed, frame)``.

    Combines a smooth low-frequency drift with a small seeded high-frequency term
    and resamples the frame bilinearly (edge clamp). ``amplitude`` is the maximum
    displacement in pixels; ``amplitude = 0`` is an exact identity.
    """
    if frame_index < 0:
        raise FilmEmulationError("frame_index must be non-negative")
    if seed < 0:
        raise FilmEmulationError("seed must be non-negative")
    if amplitude < 0.0:
        raise FilmEmulationError("amplitude must be non-negative")
    a = _validate_image(img)
    if amplitude == 0.0:
        return a.astype(np.float32)
    rng = np.random.default_rng([seed, frame_index])
    drift_x = amplitude * 0.7 * np.sin(2.0 * np.pi * frame_index / 23.0)
    drift_y = amplitude * 0.7 * np.sin(2.0 * np.pi * frame_index / 31.0 + 1.0)
    jitter = rng.uniform(-1.0, 1.0, size=2) * amplitude * 0.3
    dx = drift_x + jitter[0]
    dy = drift_y + jitter[1]
    h, w = a.shape[:2]
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float64)
    sx = np.clip(xs + dx, 0.0, w - 1.0)
    sy = np.clip(ys + dy, 0.0, h - 1.0)
    x0 = np.floor(sx).astype(np.int64)
    y0 = np.floor(sy).astype(np.int64)
    x1 = np.minimum(x0 + 1, w - 1)
    y1 = np.minimum(y0 + 1, h - 1)
    wx = sx - x0
    wy = sy - y0
    if a.ndim == 3:
        wx = wx[..., None]
        wy = wy[..., None]
    top = a[y0, x0] * (1.0 - wx) + a[y0, x1] * wx
    bottom = a[y1, x0] * (1.0 - wx) + a[y1, x1] * wx
    out = top * (1.0 - wy) + bottom * wy
    woven: np.ndarray = out.astype(np.float32)
    return woven


def apply_film_emulation(
    img: np.ndarray,
    *,
    frame_index: int = 0,
    seed: int = 0,
    grain_sigma: float = 0.03,
    halation_strength: float = 0.4,
    weave_amplitude: float = 1.5,
) -> np.ndarray:
    """Full pipeline: gate weave -> halation -> grain, all seeded and bounded."""
    out = gate_weave(img, frame_index=frame_index, seed=seed, amplitude=weave_amplitude)
    out = halation(out, strength=halation_strength)
    return film_grain(out, seed=seed + 1, sigma=grain_sigma)


def psnr(reference: np.ndarray, test: np.ndarray, *, peak: float = 1.0) -> float:
    """Peak signal-to-noise ratio in dB; ``inf`` for identical inputs.

    Both images are validated; a shape mismatch raises :class:`FilmEmulationError`.
    """
    if peak <= 0.0:
        raise FilmEmulationError("peak must be positive")
    ref = _validate_image(reference, name="reference")
    tst = _validate_image(test, name="test")
    if ref.shape != tst.shape:
        raise FilmEmulationError("psnr inputs must have the same shape")
    mse = float(np.mean((ref - tst) ** 2))
    if mse <= _EPS:
        return float("inf")
    return float(10.0 * np.log10(peak * peak / mse))


def _gaussian_window(radius: int, sigma: float) -> np.ndarray:
    x = np.arange(-radius, radius + 1, dtype=np.float64)
    g = np.exp(-0.5 * (x / sigma) ** 2)
    win = np.outer(g, g)
    window: np.ndarray = win / win.sum()
    return window


def ssim(
    reference: np.ndarray,
    test: np.ndarray,
    *,
    peak: float = 1.0,
    sigma: float = 1.5,
    truncate: float = 3.0,
) -> float:
    """Global SSIM (Wang et al. 2004) on the luma, Gaussian-windowed.

    ``C1 = (0.01·peak)²``, ``C2 = (0.03·peak)²``. Returns the mean of the local
    SSIM map. Identical inputs give exactly ``1.0``; a shape mismatch refuses.
    """
    if peak <= 0.0:
        raise FilmEmulationError("peak must be positive")
    ref = _validate_image(reference, name="reference")
    tst = _validate_image(test, name="test")
    if ref.shape != tst.shape:
        raise FilmEmulationError("ssim inputs must have the same shape")

    def luma(a: np.ndarray) -> np.ndarray:
        if a.ndim == 2:
            return a
        if a.shape[-1] == 1:
            return a[..., 0]
        return a[..., 0] * 0.2126 + a[..., 1] * 0.7152 + a[..., 2] * 0.0722

    x = luma(ref)
    y = luma(tst)
    radius = max(1, int(truncate * sigma + 0.5))
    win = _gaussian_window(radius, sigma)
    c1 = (0.01 * peak) ** 2
    c2 = (0.03 * peak) ** 2
    mu_x = _local_mean(x, win)
    mu_y = _local_mean(y, win)
    mu_xx = _local_mean(x * x, win)
    mu_yy = _local_mean(y * y, win)
    mu_xy = _local_mean(x * y, win)
    sigma_x2 = mu_xx - mu_x * mu_x
    sigma_y2 = mu_yy - mu_y * mu_y
    sigma_xy = mu_xy - mu_x * mu_y
    numerator = (2.0 * mu_x * mu_y + c1) * (2.0 * sigma_xy + c2)
    denominator = (mu_x * mu_x + mu_y * mu_y + c1) * (sigma_x2 + sigma_y2 + c2)
    ssim_map = numerator / denominator
    return float(np.mean(ssim_map))


def _local_mean(img: np.ndarray, window: np.ndarray) -> np.ndarray:
    """Valid-region windowed mean via a flipped-kernel correlation (FFT-free)."""
    h, w = img.shape
    kh, kw = window.shape
    if h < kh or w < kw:
        # Degenerate small image: fall back to the global mean.
        return np.full_like(img, float(img.mean()))
    out = np.zeros((h - kh + 1, w - kw + 1), dtype=np.float64)
    for i in range(kh):
        for j in range(kw):
            out += window[i, j] * img[i : i + out.shape[0], j : j + out.shape[1]]
    return out
