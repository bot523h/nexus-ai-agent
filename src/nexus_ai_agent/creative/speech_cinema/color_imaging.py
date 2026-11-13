"""Pure NumPy/Pillow color kernels for Hyperion Phase 3.

These kernels live behind the speech_cinema adapter boundary because they touch
pixel buffers and PNG codecs.  The capability-pack substrate in
``creative/packs/color`` only carries the data contract and must stay NumPy-free.

All transforms operate on floating-point RGB arrays and are deterministic.  They
never silently clip floating-point results; PNG export is the explicit boundary
where values are quantized to ``uint8``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import numpy as np
from numpy.typing import NDArray
from PIL import Image

Array = NDArray[np.float64]

# Bradford cone-response matrix (ICC/CIE convention).
_BRADFORD: Array = np.array(
    [[0.8951, 0.2664, -0.1614], [-0.7502, 1.7135, 0.0367], [0.0389, -0.0685, 1.0296]],
    dtype=np.float64,
)
_BRADFORD_INV: Array = np.asarray(np.linalg.inv(_BRADFORD), dtype=np.float64)
_RGB_TO_XYZ: Array = np.array(
    [
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ],
    dtype=np.float64,
)
_XYZ_TO_RGB: Array = np.asarray(np.linalg.inv(_RGB_TO_XYZ), dtype=np.float64)
# sRGB defines its white as D65, so D65 is taken as the XYZ of (1,1,1) under this matrix,
# normalised to Y=1.  The 7-digit matrix then makes the sRGB white neutral to ~1e-16.
_D65_RAW: Array = np.asarray(_RGB_TO_XYZ @ np.ones(3), dtype=np.float64)
_D65: Array = np.asarray(_D65_RAW / _D65_RAW[1], dtype=np.float64)
_LUMA_WEIGHTS: Array = np.array([0.2126, 0.7152, 0.0722], dtype=np.float64)


def _rgb(image: object, *, name: str = "image") -> Array:
    arr: Array = np.asarray(image, dtype=np.float64)
    if arr.ndim < 2 or arr.shape[-1] != 3:
        raise ValueError(f"{name} must have a final RGB dimension of length 3")
    if not bool(np.all(np.isfinite(arr))):
        raise ValueError(f"{name} must contain only finite values")
    return arr


KELVIN_MIN: float = 1000.0
KELVIN_MAX: float = 40000.0
#: CIE standard illuminant D65 chromaticity implied by ``_D65`` (x=0.312727, y=0.329023).
D65_XY: tuple[float, float] = (0.312727, 0.329023)


def _temperature_white_point(kelvin: float) -> Array:
    """Approximate a black-body chromaticity as an XYZ white point.

    The Kim-type cubic fit is published for roughly 1667--25000 K.  The accepted
    range 1000--40000 K is an extrapolation outside that band: the chromaticity
    stays finite and is used for both source and destination, so identity and
    round-trip guarantees hold, but absolute colour accuracy is not claimed there.
    """
    if not np.isfinite(kelvin) or not KELVIN_MIN <= kelvin <= KELVIN_MAX:
        raise ValueError("kelvin must be finite and in the inclusive range 1000..40000")
    t = kelvin
    if t <= 4000:
        x = -0.2661239e9 / t**3 - 0.2343580e6 / t**2 + 0.8776956e3 / t + 0.179910
    else:
        x = -3.0258469e9 / t**3 + 2.1070379e6 / t**2 + 0.2226347e3 / t + 0.240390
    if t <= 2222:
        y = -1.1063814 * x**3 - 1.34811020 * x**2 + 2.18555832 * x - 0.20219683
    elif t <= 4000:
        y = -0.9549476 * x**3 - 1.37418593 * x**2 + 2.09137015 * x - 0.16748867
    else:
        y = 3.0817580 * x**3 - 5.87338670 * x**2 + 3.75112997 * x - 0.37001483
    return np.array([x / y, 1.0, (1.0 - x - y) / y], dtype=np.float64)


def _rgb_to_xyz_white(rgb: Array) -> Array:
    # sRGB/D65 matrix, used only to obtain a chromaticity estimate.
    return np.asarray(_RGB_TO_XYZ @ rgb, dtype=np.float64)


def white_balance_chromatic_adaptation(
    image: Array,
    kelvin: float | None = None,
    *,
    method: Literal["gray_world", "kelvin"] = "gray_world",
    target_kelvin: float | None = None,
) -> Array:
    """White-balance RGB data using Bradford LMS chromatic adaptation.

    ``gray_world`` estimates the scene illuminant from channel means and maps it
    to neutral D65.  With ``kelvin`` (or ``method='kelvin'``), the black-body white
    point at ``kelvin`` is mapped to exact CIE D65 by default.  When ``target_kelvin``
    is given, the black-body white at ``target_kelvin`` is used instead, with the same
    locus function at both ends, so equal temperatures give the identity.  Results are
    floating point and intentionally not clipped.
    """
    arr = _rgb(image)
    if method not in {"gray_world", "kelvin"}:
        raise ValueError("method must be 'gray_world' or 'kelvin'")
    if method == "kelvin" and kelvin is None:
        raise ValueError("kelvin is required when method='kelvin'")
    if kelvin is not None and method == "gray_world":
        method = "kelvin"
    if method == "kelvin":
        if kelvin is None:  # pragma: no cover - guarded above; narrows the type
            raise ValueError("kelvin is required when method='kelvin'")
        source_xyz = _temperature_white_point(float(kelvin))
        target_xyz = (
            _D65 if target_kelvin is None else _temperature_white_point(float(target_kelvin))
        )
    else:
        means: Array = np.mean(arr.reshape(-1, 3), axis=0)
        if bool(np.any(means <= 0)):
            raise ValueError("gray-world channel means must be strictly positive")
        source_xyz = _rgb_to_xyz_white(means / float(np.mean(means)))
        target_xyz = _D65
    adaptation_rgb = _adaptation_rgb(source_xyz, target_xyz)
    return np.asarray(arr @ adaptation_rgb.T, dtype=np.float64)


def _adaptation_rgb(source_xyz: Array, target_xyz: Array) -> Array:
    """Return linear-RGB matrix M = M_A^-1 diag(rho_t/rho_s, gamma_t/gamma_s, beta_t/beta_s) M_A."""
    source_lms: Array = np.asarray(_BRADFORD @ source_xyz, dtype=np.float64)
    target_lms: Array = np.asarray(_BRADFORD @ target_xyz, dtype=np.float64)
    if bool(np.any(np.abs(source_lms) < np.finfo(float).eps)):
        raise ValueError("source white point is unsuitable for Bradford adaptation")
    adaptation_xyz = _BRADFORD_INV @ np.diag(target_lms / source_lms) @ _BRADFORD
    return np.asarray(_XYZ_TO_RGB @ adaptation_xyz @ _RGB_TO_XYZ, dtype=np.float64)


def bradford_adapt(
    image: object, src_kelvin: float, dst_kelvin: float | None = None
) -> Array:
    """Bradford-adapt linear RGB from the ``src_kelvin`` black-body white.

    The destination is exact CIE D65 unless ``dst_kelvin`` is given (then its black-body white).
    """
    arr = _rgb(image)
    target = _D65 if dst_kelvin is None else _temperature_white_point(float(dst_kelvin))
    matrix = _adaptation_rgb(_temperature_white_point(float(src_kelvin)), target)
    return np.asarray(arr @ matrix.T, dtype=np.float64)


def hdr_to_sdr_filmic_tonemap(hdr: Array, *, exposure: float = 0.0, white: float = 11.2) -> Array:
    """Map finite, nonnegative HDR RGB values to SDR with a filmic curve.

    Luminance is tonemapped and chroma is restored by scaling each pixel by the
    luminance ratio, which avoids hue shifts and retains highlight detail.
    """
    arr = _rgb(hdr, name="hdr")
    if bool(np.any(arr < 0)):
        raise ValueError("hdr must be nonnegative")
    if not np.isfinite(exposure) or not np.isfinite(white) or white <= 0:
        raise ValueError("exposure must be finite and white must be positive")
    lum: Array = np.asarray(np.sum(arr * _LUMA_WEIGHTS, axis=-1), dtype=np.float64)
    x: Array = lum * (2.0**exposure)
    # Hable/Uncharted-style rational filmic mapping; strictly increasing on x>=0.
    a, b, c, d, e = 0.22, 0.30, 0.10, 0.20, 0.01

    def curve(value: Array) -> Array:
        return np.asarray(
            (value * (a * value + b)) / (value * (c * value + d) + e), dtype=np.float64
        )

    # Normalize by the asymptote so SDR output stays bounded at extreme HDR.
    mapped: Array = curve(x) / (a / c)
    scale: Array = np.divide(mapped, lum, out=np.zeros_like(mapped), where=lum > 0)
    out: Array = np.asarray(arr * scale[..., None], dtype=np.float64)
    # Saturated highlights can push one channel above 1 even though luminance
    # is bounded.  Scale such pixels down uniformly, which preserves hue and
    # keeps every output channel inside the SDR range [0, 1].
    peak: Array = np.max(out, axis=-1, keepdims=True)
    headroom: Array = np.minimum(1.0, 1.0 / np.maximum(peak, np.finfo(np.float64).tiny))
    return np.asarray(out * headroom, dtype=np.float64)


def validate_spd_covariance(covariance: object, *, tolerance: float = 1e-10) -> Array:
    """Validate and return a symmetric positive-definite 3x3 covariance."""
    cov: Array = np.asarray(covariance, dtype=np.float64)
    if cov.shape != (3, 3) or not bool(np.all(np.isfinite(cov))):
        raise ValueError("covariance must be a finite 3x3 matrix")
    cov = (cov + cov.T) / 2.0
    eigenvalues: Array = np.linalg.eigvalsh(cov)
    if float(np.min(eigenvalues)) <= tolerance:
        raise ValueError("covariance must be symmetric positive definite")
    return cov


def matrix_sqrt_spd(matrix: object, *, inverse: bool = False) -> Array:
    """Compute a symmetric (inverse) square root of an SPD 3x3 matrix."""
    mat = validate_spd_covariance(matrix)
    values, vectors = np.linalg.eigh(mat)
    powers: Array = np.asarray(values ** (-0.5 if inverse else 0.5), dtype=np.float64)
    return np.asarray((vectors * powers) @ vectors.T, dtype=np.float64)


def shot_to_shot_color_match_ot(source: Array, reference: Array, *, alpha: float = 1.0) -> Array:
    """Match source mean/covariance to reference using a Gaussian OT map.

    Inputs may be an image or an arbitrary array ending in RGB.  ``alpha`` blends
    the full affine match with the identity transform (0 is a no-op, 1 is full).
    """
    src = _rgb(source, name="source")
    ref = _rgb(reference, name="reference")
    if not 0.0 <= alpha <= 1.0 or not np.isfinite(alpha):
        raise ValueError("alpha must be finite and in the range 0..1")
    src_flat: Array = src.reshape(-1, 3)
    ref_flat: Array = ref.reshape(-1, 3)
    if len(src_flat) < 2 or len(ref_flat) < 2:
        raise ValueError("source and reference require at least two RGB samples")
    src_mean: Array = src_flat.mean(axis=0)
    ref_mean: Array = ref_flat.mean(axis=0)
    src_cov = validate_spd_covariance(np.cov(src_flat, rowvar=False, bias=True))
    ref_cov = validate_spd_covariance(np.cov(ref_flat, rowvar=False, bias=True))
    transform: Array = np.asarray(
        matrix_sqrt_spd(ref_cov) @ matrix_sqrt_spd(src_cov, inverse=True), dtype=np.float64
    )
    centered: Array = src_flat - src_mean
    matched: Array = np.asarray(centered @ transform.T + ref_mean, dtype=np.float64)
    result: Array = (1.0 - alpha) * src_flat + alpha * matched
    return np.asarray(result.reshape(src.shape), dtype=np.float64)


def save_color_png(image: Array, path: str | Path) -> None:
    """Persist RGB data as an 8-bit PNG (the only operation that clips)."""
    arr = _rgb(image)
    if bool(np.any((arr < 0) | (arr > 1))):
        raise ValueError("PNG helper expects normalized RGB values in 0..1")
    Image.fromarray(np.rint(arr * 255.0).astype(np.uint8), mode="RGB").save(path, format="PNG")


def load_color_png(path: str | Path) -> Array:
    """Load a PNG as normalized float64 RGB data."""
    with Image.open(path) as image:
        return np.asarray(image.convert("RGB"), dtype=np.float64) / 255.0


def verify_color_png(path: str | Path) -> bool:
    """Verify that a file is a readable RGB/RGBA PNG."""
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            return image.format == "PNG" and image.mode in {"RGB", "RGBA", "L", "LA"}
    except (OSError, ValueError):
        return False


# Compatibility aliases used by early Hyperion callers.
white_balance = white_balance_chromatic_adaptation
tonemap_hdr_to_sdr = hdr_to_sdr_filmic_tonemap
color_match_ot = shot_to_shot_color_match_ot
save_png = save_color_png
load_png = load_color_png
verify_png = verify_color_png
