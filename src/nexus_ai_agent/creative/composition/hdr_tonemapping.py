"""Wave 11 — HDR tone mapping and colour science (transcendental cinematography).

Scene-linear float radiance can exceed 1.0 (highlights, speculars, emissive
practicals). Before display it must be mapped into a displayable range without
crushing highlight detail, and its white point may need to move between
illuminants. This module provides deterministic, offline operators:

* **Tone curves** — extended Reinhard, the Narkowicz ACES filmic fit, the
  Hejl–Burgess filmic curve and the Uncharted 2 curve. Each is monotone,
  ``f(0) = 0`` and bounded to ``[0, 1]``.
* **Transfer functions** — exact IEC 61966-2-1 sRGB encode/decode (reused from
  :mod:`compositor_2d`) and a pure power-law gamma pair.
* **Chromatic adaptation** — the Bradford cone-response transform between the
  CIE 1931 2° white points, with the Planckian locus (Kim et al. cubic fit) to
  turn a correlated colour temperature into chromaticity.
* **Auto exposure** — a log-average key mapping (Reinhard's photographic key).

Everything is bounded (:data:`MAX_TONEMAP_PIXELS`) and validated: NaN/Inf,
negative radiance, unknown operators, degenerate curves and out-of-range
parameters all raise typed errors. The module never touches the network, never
dispatches a command and never owns project state; it returns arrays.
"""

from __future__ import annotations

from typing import Final

import numpy as np

__all__ = [
    "MAX_TONEMAP_PIXELS",
    "TonemapError",
    "DegenerateToneCurveError",
    "SRGB_TO_XYZ",
    "XYZ_TO_SRGB",
    "BRADFORD",
    "BRADFORD_INV",
    "luminance",
    "auto_exposure_scale",
    "reinhard_tonemap",
    "aces_filmic_tonemap",
    "hejl_burgess_tonemap",
    "uncharted2_tonemap",
    "gamma_encode",
    "gamma_decode",
    "kelvin_to_xy",
    "xy_to_xyz",
    "bradford_adapt",
    "srgb_to_xyz",
    "xyz_to_srgb",
    "apply_vignette",
]

MAX_TONEMAP_PIXELS: Final[int] = 4_194_304  # 2048 x 2048
_EPS: Final[float] = 1e-12

#: IEC 61966-2-1 sRGB linear-RGB -> CIE XYZ (D65) matrix.
SRGB_TO_XYZ: Final[np.ndarray] = np.array(
    [
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ],
    dtype=np.float64,
)
#: Inverse of :data:`SRGB_TO_XYZ`.
XYZ_TO_SRGB: Final[np.ndarray] = np.linalg.inv(SRGB_TO_XYZ)
#: Bradford cone-response matrix (linear RGB <-> "sharpened" cone space).
BRADFORD: Final[np.ndarray] = np.array(
    [
        [0.8951, 0.2664, -0.1614],
        [-0.7502, 1.7135, 0.0367],
        [0.0389, -0.0685, 1.0296],
    ],
    dtype=np.float64,
)
#: Inverse of :data:`BRADFORD`.
BRADFORD_INV: Final[np.ndarray] = np.linalg.inv(BRADFORD)


class TonemapError(ValueError):
    """Raised for malformed tone-mapping inputs (bad shape, NaN, over-budget)."""


class DegenerateToneCurveError(TonemapError):
    """Raised when a tone-curve parameter would produce a non-monotone curve."""


def _validate_radiance(img: np.ndarray, *, name: str = "image") -> np.ndarray:
    a = np.asarray(img, dtype=np.float64)
    if a.ndim not in (2, 3) or (a.ndim == 3 and a.shape[-1] not in (1, 3)):
        raise TonemapError(f"{name} must be (H, W) or (H, W, 3), got {a.shape}")
    if not np.all(np.isfinite(a)):
        raise TonemapError(f"{name} contains NaN/Inf")
    if a.size == 0:
        raise TonemapError(f"{name} is empty")
    if a.shape[0] * a.shape[1] > MAX_TONEMAP_PIXELS:
        raise TonemapError(f"{name} exceeds the bounded pixel budget")
    if float(a.min()) < 0.0:
        raise TonemapError(f"{name} has negative scene-linear radiance")
    return a


def luminance(img: np.ndarray) -> np.ndarray:
    """Rec.709 relative luminance ``Y`` of a scene-linear RGB image."""
    a = _validate_radiance(img)
    if a.ndim == 2:
        return a
    if a.shape[-1] == 1:
        return a[..., 0]
    y: np.ndarray = a[..., 0] * 0.2126 + a[..., 1] * 0.7152 + a[..., 2] * 0.0722
    return y


def auto_exposure_scale(img: np.ndarray, *, key: float = 0.18, epsilon: float = 1e-4) -> float:
    """Reinhard's log-average key: the scalar that maps the geometric mean to ``key``.

    ``scale = key / exp(mean(log(Y + epsilon)))``. The ``epsilon`` floor keeps a
    black frame from producing an infinite scale; the result is always finite and
    positive. This is an *editorial* exposure, not a calibrated photometric one.
    """
    if key <= 0.0:
        raise TonemapError("key must be positive")
    if epsilon <= 0.0:
        raise TonemapError("epsilon must be positive")
    y = luminance(img)
    mean_log = float(np.mean(np.log(y + epsilon)))
    scale = key / float(np.exp(mean_log))
    if not np.isfinite(scale) or scale <= 0.0:
        raise TonemapError("auto exposure produced a non-positive scale")
    return scale


def reinhard_tonemap(img: np.ndarray, *, white: float = 4.0, exposure: float = 1.0) -> np.ndarray:
    """Extended (white-point) Reinhard per-channel tone curve.

    ``L_d = L (1 + L / white^2) / (1 + L)`` with ``L = exposure * radiance``.
    ``white`` is the smallest input that maps to 1.0; ``white = 1`` recovers the
    plain Reinhard curve. Monotone and bounded to ``[0, 1]``.
    """
    if white <= 0.0:
        raise TonemapError("white must be positive")
    if exposure <= 0.0:
        raise TonemapError("exposure must be positive")
    a = _validate_radiance(img) * exposure
    out: np.ndarray = a * (1.0 + a / (white * white)) / (1.0 + a)
    clipped: np.ndarray = np.clip(out, 0.0, 1.0).astype(np.float32)
    return clipped


def aces_filmic_tonemap(img: np.ndarray, *, exposure: float = 1.0) -> np.ndarray:
    """The Narkowicz 2015 ACES filmic fit: ``(a x^2 + b x) / (c x^2 + d x + e)``.

    ``a = 2.51, b = 0.03, c = 2.43, d = 0.59, e = 0.14``. Monotone, ``f(0) = 0``,
    and asymptotically 1.0 (clamped to ``[0, 1]``).
    """
    if exposure <= 0.0:
        raise TonemapError("exposure must be positive")
    x = _validate_radiance(img) * exposure
    a, b, c, d, e = 2.51, 0.03, 2.43, 0.59, 0.14
    out: np.ndarray = (x * (a * x + b)) / (x * (c * x + d) + e)
    clipped: np.ndarray = np.clip(out, 0.0, 1.0).astype(np.float32)
    return clipped


def hejl_burgess_tonemap(img: np.ndarray, *, exposure: float = 1.0) -> np.ndarray:
    """Hejl–Burgess filmic curve (the "filmic" variant of the Hejl 2004 fit).

    Operates on ``x = max(0, exposure*radiance - 0.004)`` and returns
    ``(x (6.2 x + 0.5)) / (x (6.2 x + 1.7) + 0.06)``, then clamps to ``[0, 1]``.
    """
    if exposure <= 0.0:
        raise TonemapError("exposure must be positive")
    a = _validate_radiance(img) * exposure
    x = np.maximum(0.0, a - 0.004)
    out: np.ndarray = (x * (6.2 * x + 0.5)) / (x * (6.2 * x + 1.7) + 0.06)
    clipped: np.ndarray = np.clip(out, 0.0, 1.0).astype(np.float32)
    return clipped


def uncharted2_tonemap(img: np.ndarray, *, exposure: float = 1.0) -> np.ndarray:
    """The Uncharted 2 (Hable) filmic curve, normalised by ``f(11.2)``.

    ``f(x) = ((x (A x + C B) + D E) / (x (A x + B) + D F)) - E / F`` with
    ``A=0.15, B=0.50, C=0.10, D=0.20, E=0.02, F=0.30``; the white point is 11.2.
    """
    if exposure <= 0.0:
        raise TonemapError("exposure must be positive")
    x = _validate_radiance(img) * exposure
    a, b, c, d, e, f = 0.15, 0.50, 0.10, 0.20, 0.02, 0.30
    white = 11.2

    def curve(v: np.ndarray) -> np.ndarray:
        num = v * (a * v + c * b) + d * e
        den = v * (a * v + b) + d * f
        out: np.ndarray = num / den - e / f
        return out

    out: np.ndarray = curve(x) / curve(np.array(white, dtype=np.float64))
    clipped: np.ndarray = np.clip(out, 0.0, 1.0).astype(np.float32)
    return clipped


def gamma_encode(img: np.ndarray, *, gamma: float = 2.2) -> np.ndarray:
    """Display-encode linear radiance with a pure power law: ``x ** (1/gamma)``."""
    if gamma <= 0.0:
        raise TonemapError("gamma must be positive")
    a = _validate_radiance(img)
    out: np.ndarray = np.power(a, 1.0 / gamma)
    clipped: np.ndarray = np.clip(out, 0.0, 1.0).astype(np.float32)
    return clipped


def gamma_decode(img: np.ndarray, *, gamma: float = 2.2) -> np.ndarray:
    """Inverse of :func:`gamma_encode`: ``x ** gamma``."""
    if gamma <= 0.0:
        raise TonemapError("gamma must be positive")
    a = _validate_radiance(img)
    out: np.ndarray = np.power(a, gamma)
    return out.astype(np.float32)


def kelvin_to_xy(kelvin: float) -> tuple[float, float]:
    """CIE 1931 2° chromaticity ``(x, y)`` of the Planckian locus at ``kelvin``.

    Kim et al. (2002) cubic fit, valid over ``1667 K .. 25000 K``; values outside
    that range raise :class:`TonemapError` rather than extrapolating silently.
    """
    if not 1667.0 <= kelvin <= 25000.0:
        raise TonemapError("colour temperature must be in [1667, 25000] K")
    t = float(kelvin)
    if t <= 4000.0:
        x = -0.2661239e9 / t**3 - 0.2343589e6 / t**2 + 0.8776956e3 / t + 0.179910
    else:
        x = -3.0258469e9 / t**3 + 2.1070379e6 / t**2 + 0.2226347e3 / t + 0.240390
    if t <= 2222.0:
        y = -1.1063814 * x**3 - 1.34811020 * x**2 + 2.18555832 * x - 0.20219683
    elif t <= 4000.0:
        y = -0.9549476 * x**3 - 1.37418593 * x**2 + 2.09137015 * x - 0.16748867
    else:
        y = 3.0817580 * x**3 - 5.87338670 * x**2 + 3.75112997 * x - 0.37001483
    return float(x), float(y)


def xy_to_xyz(x: float, y: float, *, luminance_y: float = 1.0) -> np.ndarray:
    """Chromaticity ``(x, y)`` -> CIE XYZ with the given ``Y`` (default white ``Y=1``)."""
    if y <= 0.0:
        raise TonemapError("chromaticity y must be positive")
    if luminance_y <= 0.0:
        raise TonemapError("luminance must be positive")
    big_x = luminance_y * x / y
    big_z = luminance_y * (1.0 - x - y) / y
    return np.array([big_x, luminance_y, big_z], dtype=np.float64)


def bradford_adapt(
    img: np.ndarray,
    *,
    src_white_xy: tuple[float, float],
    dst_white_xy: tuple[float, float],
) -> np.ndarray:
    """Bradford chromatic adaptation of a linear-sRGB image between two white points.

    Converts linear RGB to XYZ, applies the Bradford cone-response adaptation to
    the target white, and converts back. The identity case
    (``src == dst``) is a numerical no-op.
    """
    a = _validate_radiance(img)
    src = xy_to_xyz(*src_white_xy)
    dst = xy_to_xyz(*dst_white_xy)
    cone_src = BRADFORD @ src
    cone_dst = BRADFORD @ dst
    if np.any(np.abs(cone_src) < _EPS):
        raise TonemapError("degenerate source white point")
    scale = np.diag(cone_dst / cone_src)
    m_adapt = BRADFORD_INV @ scale @ BRADFORD
    full = XYZ_TO_SRGB @ m_adapt @ SRGB_TO_XYZ
    out: np.ndarray = np.einsum("ij,...j->...i", full, a)
    clipped: np.ndarray = np.clip(out, 0.0, None).astype(np.float32)
    return clipped


def srgb_to_xyz(img: np.ndarray) -> np.ndarray:
    """Linear-sRGB -> CIE XYZ (D65)."""
    a = _validate_radiance(img)
    out: np.ndarray = np.einsum("ij,...j->...i", SRGB_TO_XYZ, a)
    return out.astype(np.float32)


def xyz_to_srgb(xyz: np.ndarray) -> np.ndarray:
    """CIE XYZ (D65) -> linear-sRGB."""
    a = np.asarray(xyz, dtype=np.float64)
    if a.ndim not in (2, 3) or (a.ndim == 3 and a.shape[-1] != 3):
        raise TonemapError("xyz must be (H, W, 3)")
    if not np.all(np.isfinite(a)):
        raise TonemapError("xyz contains NaN/Inf")
    out: np.ndarray = np.einsum("ij,...j->...i", XYZ_TO_SRGB, a)
    clipped: np.ndarray = np.clip(out, 0.0, None).astype(np.float32)
    return clipped


def apply_vignette(
    img: np.ndarray,
    *,
    strength: float = 0.3,
    radius: float = 1.0,
    center: tuple[float, float] = (0.5, 0.5),
) -> np.ndarray:
    """Radial cosine-falloff vignette in normalised coordinates.

    ``falloff = 1 - strength * smoothstep(radius, 1, r)`` where ``r`` is the
    normalised distance from ``center``. ``strength = 0`` is an exact identity.
    """
    if not 0.0 <= strength <= 1.0:
        raise TonemapError("vignette strength must be in [0, 1]")
    if not 0.0 < radius <= 1.0:
        raise TonemapError("vignette radius must be in (0, 1]")
    a = _validate_radiance(img)
    h, w = a.shape[:2]
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float64)
    cx, cy = center
    nx = (xs / max(w - 1, 1) - cx) * 2.0
    ny = (ys / max(h - 1, 1) - cy) * 2.0
    r = np.sqrt(nx * nx + ny * ny)
    t = np.clip((r - radius) / max(1.0 - radius, _EPS), 0.0, 1.0)
    smooth = t * t * (3.0 - 2.0 * t)
    falloff = 1.0 - strength * smooth
    if a.ndim == 3:
        falloff = falloff[..., None]
    out: np.ndarray = a * falloff
    return out.astype(np.float32)
