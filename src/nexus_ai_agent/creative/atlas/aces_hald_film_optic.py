"""WAVE 26 — ACES colour pipeline, HALD CLUTs, halation and film grain.

A film-emulation module built on *published* ACES matrices and a closed-form
tone-map, so the transforms are checkable rather than guessed:

* **ACES colour science** — the ACES2065-1 (AP0) -> XYZ (D60) matrix, the
  XYZ -> AP1 matrix, and their composition, verified against the known
  white-point row sums and against round-trip inversion.
* **Tone mapping** — the Narkowicz ACES RRT/ODT analytic fit, checked for
  monotonicity, endpoint behaviour and the ``f(0)=0`` invariant.
* **Output transform** — ACEScg (AP1, D60) back to sRGB (D65), the exact inverse
  of the forward chain, so a display path never encodes AP1 coordinates as sRGB.
* **HALD CLUT** — identity colour lookup tables in the HALD layout with
  trilinear interpolation; applying the identity CLUT to an image must
  reproduce it to interpolation error (a self-check on the layout).
* **Halation** — a red-weighted convolution spread (the physical cause is light
  scattering back off the film base).
* **Film grain** — seeded, band-limited multiplicative grain with a declared
  amplitude.

All operations are offline, bounded and deterministic; none of them claim to
be a colourimetric certificate.
"""

from __future__ import annotations

from typing import Final

import numpy as np

__all__ = [
    "FilmOpticError",
    "AP0_TO_XYZ",
    "XYZ_TO_AP1",
    "AP0_TO_AP1",
    "AP1_TO_AP0",
    "SRGB_D65_TO_AP0_D60",
    "AP1_TO_SRGB_D65",
    "scene_linear_srgb_to_display_linear",
    "srgb_eotf_to_linear",
    "linear_to_srgb_oetf",
    "linear_srgb_d65_to_aces_ap0_d60",
    "aces_ap0_to_acescg",
    "acescg_to_ap0",
    "aces_tone_map",
    "hald_identity_clut",
    "apply_hald_clut",
    "halation",
    "film_grain",
    "MAX_LEVEL",
    "MAX_IMAGE_PIXELS",
]

#: ACES2065-1 (AP0) to CIE XYZ under the D60 white point.
AP0_TO_XYZ: Final[np.ndarray] = np.array(
    [
        [0.9525523959, 0.0000000000, 0.0000936786],
        [0.3439664498, 0.7281660966, -0.0721325464],
        [0.0000000000, 0.0000000000, 1.0088251844],
    ]
)
#: CIE XYZ to ACEScg (AP1) under D60.
XYZ_TO_AP1: Final[np.ndarray] = np.array(
    [
        [1.6410233797, -0.3248032942, -0.2364246952],
        [-0.6636628587, 1.6153315917, 0.0167563477],
        [0.0117218943, -0.0082844420, 0.9883948585],
    ]
)
#: AP0 -> AP1 (compose XYZtoAP1 @ AP0toXYZ).
AP0_TO_AP1: Final[np.ndarray] = XYZ_TO_AP1 @ AP0_TO_XYZ
#: AP1 -> AP0 (exact inverse).
AP1_TO_AP0: Final[np.ndarray] = np.linalg.inv(AP0_TO_AP1)

_SRGB_EOTF_THRESHOLD: Final[float] = 0.04045
_SRGB_OETF_THRESHOLD: Final[float] = _SRGB_EOTF_THRESHOLD / 12.92


def _build_srgb_d65_to_ap0_d60() -> np.ndarray:
    """Exact Bradford-adapted linear sRGB/Rec.709 (D65) -> ACES2065-1 AP0 (D60) matrix."""
    rx, ry = 0.64, 0.33
    gx, gy = 0.30, 0.60
    bx, by = 0.15, 0.06
    wx, wy = 0.3127, 0.3290
    d65 = np.array([wx / wy, 1.0, (1.0 - wx - wy) / wy], dtype=np.float64)
    primaries = np.array(
        [
            [rx / ry, gx / gy, bx / by],
            [1.0, 1.0, 1.0],
            [(1.0 - rx - ry) / ry, (1.0 - gx - gy) / gy, (1.0 - bx - by) / by],
        ],
        dtype=np.float64,
    )
    scales = np.linalg.solve(primaries, d65)
    srgb_to_xyz_d65 = primaries * scales[None, :]
    d60 = AP0_TO_XYZ @ np.ones(3, dtype=np.float64)
    bradford = np.array(
        [
            [0.8951000, 0.2664000, -0.1614000],
            [-0.7502000, 1.7135000, 0.0367000],
            [0.0389000, -0.0685000, 1.0296000],
        ],
        dtype=np.float64,
    )
    cone_d65 = bradford @ d65
    cone_d60 = bradford @ d60
    cat_d65_to_d60 = np.linalg.solve(bradford, np.diag(cone_d60 / cone_d65) @ bradford)
    return np.linalg.solve(AP0_TO_XYZ, cat_d65_to_d60 @ srgb_to_xyz_d65)


#: Linear sRGB/Rec.709 (D65) -> ACES2065-1 AP0 (D60) with Bradford chromatic adaptation.
SRGB_D65_TO_AP0_D60: Final[np.ndarray] = _build_srgb_d65_to_ap0_d60()

#: ACEScg (AP1, D60) -> linear sRGB/Rec.709 (D65): the output transform that the
#: display path needs. It is the exact inverse of the forward chain
#: ``sRGB(D65) -> AP0(D60) -> AP1``, so the two can never drift apart.
AP1_TO_SRGB_D65: Final[np.ndarray] = np.linalg.inv(SRGB_D65_TO_AP0_D60) @ AP1_TO_AP0

MAX_LEVEL: Final[int] = 16
MAX_IMAGE_PIXELS: Final[int] = 64_000_000


class FilmOpticError(ValueError):
    """Raised for malformed colours, images, or out-of-range parameters."""


def _check_colour(rgb: np.ndarray, name: str = "rgb") -> np.ndarray:
    _require_finite_array(rgb, name="rgb")
    arr = np.asarray(rgb, dtype=np.float64)
    if arr.shape[-1] != 3:
        raise FilmOpticError(f"{name} must have a trailing dimension of 3")
    if not np.all(np.isfinite(arr)):
        raise FilmOpticError(f"{name} contains NaN/Inf")
    return arr


def srgb_eotf_to_linear(srgb: np.ndarray) -> np.ndarray:
    """IEC 61966-2-1 sRGB EOTF: display-encoded ``srgb`` in ``[0, 1]`` -> linear light."""
    _require_finite_array(srgb, name="srgb")
    arr = np.asarray(srgb, dtype=np.float64)
    if not np.all(np.isfinite(arr)):
        raise FilmOpticError("srgb input contains NaN/Inf")
    if arr.size > 0 and (arr.min() < 0.0 or arr.max() > 1.0):
        raise FilmOpticError("srgb input must lie in [0, 1]")
    out = np.where(
        arr <= _SRGB_EOTF_THRESHOLD,
        arr / 12.92,
        np.power((np.maximum(arr, _SRGB_EOTF_THRESHOLD) + 0.055) / 1.055, 2.4),
    )
    return np.asarray(out, dtype=np.float64)


def linear_to_srgb_oetf(linear: np.ndarray) -> np.ndarray:
    """IEC 61966-2-1 sRGB OETF (inverse EOTF): linear values -> display-encoded ``[0, 1]``."""
    _require_finite_array(linear, name="linear")
    arr = np.asarray(linear, dtype=np.float64)
    if not np.all(np.isfinite(arr)):
        raise FilmOpticError("linear input contains NaN/Inf")
    if arr.size > 0 and arr.min() < 0.0:
        raise FilmOpticError("linear input must be non-negative")
    clamped = np.clip(arr, 0.0, 1.0)
    out = np.where(
        clamped <= _SRGB_OETF_THRESHOLD,
        clamped * 12.92,
        1.055 * np.power(np.maximum(clamped, _SRGB_OETF_THRESHOLD), 1.0 / 2.4) - 0.055,
    )
    return np.asarray(np.clip(out, 0.0, 1.0), dtype=np.float64)


def linear_srgb_d65_to_aces_ap0_d60(rgb_linear: np.ndarray) -> np.ndarray:
    """Convert linear sRGB/Rec.709 (D65) values to ACES2065-1 AP0 (D60) via Bradford CAT."""
    _require_finite_array(rgb_linear, name="rgb_linear")
    arr = _check_colour(rgb_linear, "rgb_linear")
    out: np.ndarray = np.asarray(arr @ SRGB_D65_TO_AP0_D60.T, dtype=np.float64)
    return out


def aces_ap0_to_acescg(rgb: np.ndarray) -> np.ndarray:
    """Convert ACES2065-1 (AP0) linear values to ACEScg (AP1)."""
    _require_finite_array(rgb, name="rgb")
    arr = _check_colour(rgb, "ap0")
    out: np.ndarray = np.asarray(arr @ AP0_TO_AP1.T, dtype=np.float64)
    return out


def acescg_to_ap0(rgb: np.ndarray) -> np.ndarray:
    """Convert ACEScg (AP1) linear values back to ACES2065-1 (AP0)."""
    _require_finite_array(rgb, name="rgb")
    arr = _check_colour(rgb, "ap1")
    out: np.ndarray = np.asarray(arr @ AP1_TO_AP0.T, dtype=np.float64)
    return out


def aces_tone_map(
    linear: np.ndarray,
    *,
    a: float = 2.51,
    b: float = 0.03,
    c: float = 2.43,
    d: float = 0.59,
    e: float = 0.14,
) -> np.ndarray:
    """Narkowicz ACES RRT/ODT analytic fit ``(x(ax+b)) / (x(cx+d)+e)``.

    Input is linear scene radiance; output is display-referred in ``[0, 1]``.
    The fit is monotonic and ``f(0) = 0``.
    """
    _require_finite_array(linear, name="linear")
    _require_finite_scalar(a, name="a")
    _require_finite_scalar(b, name="b")
    _require_finite_scalar(c, name="c")
    _require_finite_scalar(d, name="d")
    _require_finite_scalar(e, name="e")
    arr = np.asarray(linear, dtype=np.float64)
    if not np.all(np.isfinite(arr)):
        raise FilmOpticError("linear input contains NaN/Inf")
    if arr.min() < 0.0:
        raise FilmOpticError("linear input must be non-negative")
    if a <= 0.0 or c <= 0.0 or e <= 0.0:
        raise FilmOpticError("a, c and e must be positive")
    num = arr * (a * arr + b)
    den = arr * (c * arr + d) + e
    out: np.ndarray = np.clip(num / den, 0.0, 1.0)
    return out


def scene_linear_srgb_to_display_linear(rgb_linear: np.ndarray) -> np.ndarray:
    """Scene-referred linear sRGB/D65 -> display-referred linear sRGB/D65 in ``[0, 1]``.

    This is the single colour core shared by every display path: sRGB(D65) -> AP0
    (D60, Bradford) -> AP1 -> one ACES tone-map in AP1 -> AP1 -> sRGB(D65) output
    transform -> clip to gamut. The caller applies the sRGB OETF exactly once.
    """
    _require_finite_array(rgb_linear, name="rgb_linear")
    ap0 = linear_srgb_d65_to_aces_ap0_d60(rgb_linear)
    ap1 = np.maximum(aces_ap0_to_acescg(ap0), 0.0)
    mapped = aces_tone_map(ap1)
    out: np.ndarray = np.asarray(np.clip(mapped @ AP1_TO_SRGB_D65.T, 0.0, 1.0), dtype=np.float64)
    return out


def hald_identity_clut(level: int = 8) -> np.ndarray:
    """An identity HALD CLUT of the given ``level`` (size ``level**3`` samples).

    Returns an ``(level**3, 3)`` array. The sample order follows the HALD
    convention: the first axis is the blue index, then green, then red (so the
    RGB value at index ``(r, g, b)`` is ``(r, g, b)/(level-1)``).
    """
    _require_finite_scalar(level, name="level")
    if level < 2 or level > MAX_LEVEL:
        raise FilmOpticError(f"level must be in [2, {MAX_LEVEL}]")
    n = level
    # HALD layout: index = (b * n + g) * n + r
    b = np.arange(n).repeat(n * n)
    g = np.tile(np.arange(n).repeat(n), n)
    r = np.tile(np.arange(n), n * n)
    denom = float(n - 1)
    clut = np.stack([r, g, b], axis=-1).astype(np.float64) / denom
    return clut


def _trilinear_sample(clut: np.ndarray, level: int, coords: np.ndarray) -> np.ndarray:
    _require_finite_array(clut, name="clut")
    _require_finite_scalar(level, name="level")
    _require_finite_array(coords, name="coords")
    n = level
    # coords in [0, 1]^3 -> continuous lattice position
    pos = np.clip(coords, 0.0, 1.0) * (n - 1)
    lo = np.floor(pos).astype(np.int64)
    hi = np.minimum(lo + 1, n - 1)
    frac = pos - lo
    r0, g0, b0 = lo[..., 0], lo[..., 1], lo[..., 2]
    r1, g1, b1 = hi[..., 0], hi[..., 1], hi[..., 2]
    fr = frac[..., 0]
    fg = frac[..., 1]
    fb = frac[..., 2]

    def idx(r: np.ndarray, g: np.ndarray, b: np.ndarray) -> np.ndarray:
        _require_finite_array(r, name="r")
        _require_finite_array(g, name="g")
        _require_finite_array(b, name="b")
        out: np.ndarray = (b * n + g) * n + r
        return out

    c000 = clut[idx(r0, g0, b0)]
    c100 = clut[idx(r1, g0, b0)]
    c010 = clut[idx(r0, g1, b0)]
    c110 = clut[idx(r1, g1, b0)]
    c001 = clut[idx(r0, g0, b1)]
    c101 = clut[idx(r1, g0, b1)]
    c011 = clut[idx(r0, g1, b1)]
    c111 = clut[idx(r1, g1, b1)]
    fr = fr[..., None]
    fg = fg[..., None]
    fb = fb[..., None]
    c00 = c000 * (1 - fr) + c100 * fr
    c01 = c001 * (1 - fr) + c101 * fr
    c10 = c010 * (1 - fr) + c110 * fr
    c11 = c011 * (1 - fr) + c111 * fr
    c0 = c00 * (1 - fg) + c10 * fg
    c1 = c01 * (1 - fg) + c11 * fg
    out: np.ndarray = c0 * (1 - fb) + c1 * fb
    return out


def apply_hald_clut(image: np.ndarray, clut: np.ndarray, level: int) -> np.ndarray:
    """Apply a HALD CLUT to an ``(..., 3)`` image in ``[0, 1]``."""
    _require_finite_array(image, name="image")
    _require_finite_array(clut, name="clut")
    _require_finite_scalar(level, name="level")
    arr = _check_colour(image, "image")
    c = np.asarray(clut, dtype=np.float64)
    if c.shape != (level**3, 3):
        raise FilmOpticError(f"clut must have shape ({level**3}, 3) for level {level}")
    if level < 2 or level > MAX_LEVEL:
        raise FilmOpticError(f"level must be in [2, {MAX_LEVEL}]")
    if arr.size // 3 > MAX_IMAGE_PIXELS:
        raise FilmOpticError("image is too large")
    if arr.min() < 0.0 or arr.max() > 1.0:
        raise FilmOpticError("image must lie in [0, 1]")
    out: np.ndarray = np.clip(_trilinear_sample(c, level, arr), 0.0, 1.0)
    return out


def halation(
    image: np.ndarray,
    *,
    radius: float = 4.0,
    strength: float = 0.25,
    red_bias: tuple[float, float, float] = (1.0, 0.35, 0.15),
) -> np.ndarray:
    """Red-biased light spread (halation) via a Gaussian convolution.

    The kernel is a normalised 2-D Gaussian; each channel is blurred and mixed
    back with a channel-dependent weight (red scatters most).
    """
    _require_finite_array(image, name="image")
    _require_finite_scalar(radius, name="radius")
    _require_finite_scalar(strength, name="strength")
    _require_finite_array(red_bias, name="red_bias")
    arr = _check_colour(image, "image")
    if arr.ndim != 3:
        raise FilmOpticError("image must be (h, w, 3)")
    if radius <= 0.0:
        raise FilmOpticError("radius must be positive")
    if not (0.0 <= strength <= 1.0):
        raise FilmOpticError("strength must lie in [0, 1]")
    rb = np.asarray(red_bias, dtype=np.float64)
    if rb.shape != (3,) or np.any(rb < 0.0):
        raise FilmOpticError("red_bias must be a non-negative 3-vector")
    if arr.shape[0] > 4096 or arr.shape[1] > 4096:
        raise FilmOpticError("image is too large for halation")

    h, w = arr.shape[:2]
    k = int(np.ceil(3.0 * radius))
    ys = np.arange(-k, k + 1)
    xs = np.arange(-k, k + 1)
    yy, xx = np.meshgrid(ys, xs, indexing="ij")
    kernel = np.exp(-(xx * xx + yy * yy) / (2.0 * radius * radius))
    kernel /= kernel.sum()

    out = arr.copy()
    for ch in range(3):
        padded = np.pad(arr[..., ch], k, mode="edge")
        blurred = np.zeros((h, w), dtype=np.float64)
        for dy in range(-k, k + 1):
            for dx in range(-k, k + 1):
                wgt = kernel[dy + k, dx + k]
                if wgt == 0.0:
                    continue
                blurred += wgt * padded[k + dy : k + dy + h, k + dx : k + dx + w]
        out[..., ch] = np.clip(arr[..., ch] + strength * rb[ch] * blurred, 0.0, None)
    return out


def film_grain(
    image: np.ndarray, *, amount: float = 0.05, seed: int = 0, low_pass: int = 1
) -> np.ndarray:
    """Multiplicative band-limited film grain (deterministic in ``seed``)."""
    _require_finite_array(image, name="image")
    _require_finite_scalar(amount, name="amount")
    _require_finite_scalar(seed, name="seed")
    _require_finite_scalar(low_pass, name="low_pass")
    arr = _check_colour(image, "image")
    if arr.ndim != 3:
        raise FilmOpticError("image must be (h, w, 3)")
    if not (0.0 <= amount <= 1.0):
        raise FilmOpticError("amount must lie in [0, 1]")
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
        raise FilmOpticError("seed must be a non-negative int")
    if low_pass < 1 or low_pass > 8:
        raise FilmOpticError("low_pass must be in [1, 8]")
    rng = np.random.default_rng(seed)
    h, w = arr.shape[:2]
    noise = rng.normal(0.0, 1.0, (h, w, 1))
    if low_pass > 1:
        # Simple box low-pass to band-limit the grain.
        k = low_pass
        padded = np.pad(noise[..., 0], k, mode="edge")
        acc = np.zeros((h, w), dtype=np.float64)
        for dy in range(-k, k + 1):
            for dx in range(-k, k + 1):
                acc += padded[k + dy : k + dy + h, k + dx : k + dx + w]
        noise = (acc / ((2 * k + 1) ** 2))[..., None]
    noise = noise / (noise.std() + 1e-12)
    out: np.ndarray = np.clip(arr * (1.0 + amount * noise), 0.0, None)
    return out


def _require_finite_scalar(value: object, *, name: str) -> None:
    """Numeric inputs never silently coerce booleans or non-finite values."""
    if isinstance(value, (bool, np.bool_)):
        raise TypeError(f"{name} must not be bool")
    if not isinstance(value, (int, float, np.integer, np.floating)):
        raise TypeError(f"{name} must be numeric")
    if not np.isfinite(value):
        raise FilmOpticError(f"{name} must be finite")


def _require_finite_array(value: object, *, name: str) -> None:
    """Validate before dtype conversion can erase boolean evidence."""
    raw = np.asarray(value, dtype=object)
    if any(isinstance(item, (bool, np.bool_)) for item in raw.flat):
        raise TypeError(f"{name} must not contain bool")
    if not np.all(np.isfinite(np.asarray(value))):
        raise FilmOpticError(f"{name} must be finite")


def verify_aces_colorimetric_invariants() -> dict[str, float | bool]:
    """Check AP0/XYZ/AP1 inverses and the sampled Narkowicz response."""
    errors = [
        float(np.linalg.norm(matrix @ np.linalg.inv(matrix) - np.eye(3)))
        for matrix in (AP0_TO_XYZ, XYZ_TO_AP1, AP0_TO_AP1)
    ]
    errors.append(float(np.linalg.norm(AP0_TO_AP1 @ AP1_TO_AP0 - np.eye(3))))
    x = np.linspace(0.0, 100.0, 10001)
    mapped = aces_tone_map(x)
    monotonic = bool(np.all(np.diff(mapped) >= -1e-12))
    return {
        "inverse_error": max(errors),
        "monotonic": monotonic,
        "verified": max(errors) < 1e-10 and monotonic,
    }
