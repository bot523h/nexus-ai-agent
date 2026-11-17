"""Pure-NumPy portrait-imaging kernels (Wave 4, adapter layer).

Deterministic kernels for the ten missing ``portrait.*`` catalogue operations.
They live in the composition package (not in ``packs/``) because the pack purity
gate forbids heavy imports inside ``creative/packs``; the portrait pack stays
declarative and imports these kernels. No cloud API is called.
"""

from __future__ import annotations

import numpy as np

from nexus_ai_agent.creative.packs.portrait.errors import (
    DegenerateWarpError,
    InvalidImageError,
    InvalidRegionError,
)

__all__ = [
    "validate_image",
    "validate_region",
    "gaussian_blur",
    "srgb_to_lab",
    "lab_to_srgb",
    "gaze_redirection",
    "eyeblink_synthesis",
    "facial_relighting_gradient",
    "skintone_harmonization",
    "micro_expression_morph",
    "teeth_whitening",
    "facial_hair_continuity",
    "head_repose_3d",
    "depth_of_field_bokeh",
    "aging_deaging_geometry",
    "SKIN_L_RANGE",
    "SKIN_A_RANGE",
    "SKIN_B_RANGE",
]

SKIN_L_RANGE = (30.0, 85.0)
SKIN_A_RANGE = (8.0, 25.0)
SKIN_B_RANGE = (10.0, 30.0)


def validate_image(img: np.ndarray) -> np.ndarray:
    """Coerce to float32 and refuse a malformed / non-finite image."""
    arr = np.asarray(img, dtype=np.float32)
    if arr.ndim != 3 or arr.shape[2] < 3:
        raise InvalidImageError(f"image must be (H, W, C>=3), got {arr.shape}")
    if not np.all(np.isfinite(arr)):
        raise InvalidImageError("image contains NaN/Inf")
    return arr


def validate_region(mask: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    """Coerce a region mask to float32 in ``[0, 1]`` and check it matches."""
    arr = np.asarray(mask, dtype=np.float32)
    if arr.ndim == 3:
        arr = arr[..., 0]
    if arr.ndim != 2 or arr.shape != shape:
        raise InvalidRegionError(f"region {arr.shape} does not match image {shape}")
    if not np.all(np.isfinite(arr)):
        raise InvalidRegionError("region contains NaN/Inf")
    if float(arr.max()) <= float(arr.min()):
        raise InvalidRegionError("region is constant (no signal)")
    return np.clip(arr, 0.0, 1.0)


def gaussian_blur(img: np.ndarray, sigma: float) -> np.ndarray:
    """Separable Gaussian blur via a normalised 1-D kernel (no SciPy)."""
    arr = np.asarray(img, dtype=np.float32)
    if sigma <= 0:
        return arr.copy()
    radius = max(1, int(round(3 * sigma)))
    x = np.arange(-radius, radius + 1, dtype=np.float32)
    kernel = np.exp(-(x**2) / (2 * sigma**2))
    kernel /= kernel.sum()
    out: np.ndarray = arr
    for axis in (0, 1):
        length = out.shape[axis]
        pad = [(0, 0)] * out.ndim
        pad[axis] = (radius, radius)
        moved = np.moveaxis(np.pad(out, pad, mode="edge"), axis, 0)
        blurred: np.ndarray = np.zeros_like(np.moveaxis(out, axis, 0))
        for i, w in enumerate(kernel):
            blurred += w * moved[i : i + length]
        out = np.moveaxis(blurred, 0, axis)
    return out.astype(np.float32)


def _bilinear_sample(arr: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    h, w = arr.shape[:2]
    x0 = np.floor(xs).astype(np.int32)
    y0 = np.floor(ys).astype(np.int32)
    x1 = np.clip(x0 + 1, 0, w - 1)
    y1 = np.clip(y0 + 1, 0, h - 1)
    x0c = np.clip(x0, 0, w - 1)
    y0c = np.clip(y0, 0, h - 1)
    wx = (xs - x0)[..., None]
    wy = (ys - y0)[..., None]
    top = arr[y0c, x0c] * (1 - wx) + arr[y0c, x1] * wx
    bot = arr[y1, x0c] * (1 - wx) + arr[y1, x1] * wx
    return (top * (1 - wy) + bot * wy).astype(np.float32)


def _rgb_to_xyz(img: np.ndarray) -> np.ndarray:
    lin = np.where(img <= 0.04045, img / 12.92, ((img + 0.055) / 1.055) ** 2.4)
    m = np.array(
        [[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]],
        dtype=np.float32,
    )
    return lin @ m.T


def _f_lab(t: np.ndarray) -> np.ndarray:
    delta = 6.0 / 29.0
    return np.where(t > delta**3, np.cbrt(np.maximum(t, 0.0)), t / (3 * delta**2) + 4.0 / 29.0)


def srgb_to_lab(img: np.ndarray) -> np.ndarray:
    """Convert sRGB in ``[0, 1]`` to CIE L*a*b* (D65)."""
    arr = np.asarray(img, dtype=np.float32)[..., :3]
    xyz = _rgb_to_xyz(arr) / np.array([0.95047, 1.0, 1.08883], dtype=np.float32)
    f = _f_lab(xyz)
    lab = np.empty_like(f)
    lab[..., 0] = 116.0 * f[..., 1] - 16.0
    lab[..., 1] = 500.0 * (f[..., 0] - f[..., 1])
    lab[..., 2] = 200.0 * (f[..., 1] - f[..., 2])
    return lab


def lab_to_srgb(lab: np.ndarray) -> np.ndarray:
    """Convert CIE L*a*b* back to sRGB in ``[0, 1]`` (clipped)."""
    lab = np.asarray(lab, dtype=np.float32)
    fy = (lab[..., 0] + 16.0) / 116.0
    fx = fy + lab[..., 1] / 500.0
    fz = fy - lab[..., 2] / 200.0
    delta = 6.0 / 29.0
    xr = np.where(fx > delta, fx**3, 3 * delta**2 * (fx - 4.0 / 29.0))
    yr = np.where(fy > delta, fy**3, 3 * delta**2 * (fy - 4.0 / 29.0))
    zr = np.where(fz > delta, fz**3, 3 * delta**2 * (fz - 4.0 / 29.0))
    xyz = np.stack([xr, yr, zr], axis=-1) * np.array([0.95047, 1.0, 1.08883], dtype=np.float32)
    m = np.array(
        [[3.2406, -1.5372, -0.4986], [-0.9689, 1.8758, 0.0415], [0.0557, -0.2040, 1.0570]],
        dtype=np.float32,
    )
    lin = xyz @ m.T
    srgb = np.where(
        lin <= 0.0031308, lin * 12.92, 1.055 * np.maximum(lin, 0.0) ** (1 / 2.4) - 0.055
    )
    return np.clip(srgb, 0.0, 1.0).astype(np.float32)


def gaze_redirection(
    img: np.ndarray, eye_mask: np.ndarray, *, target: tuple[float, float], strength: float = 1.0
) -> np.ndarray:
    """Shift the iris/pupil disc inside the eye mask to redirect the gaze."""
    arr = validate_image(img)
    m = validate_region(eye_mask, arr.shape[:2])
    if not 0.0 <= strength <= 1.0:
        raise InvalidRegionError("strength must be in [0, 1]")
    h, w = arr.shape[:2]
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    tx = float(np.clip(target[0], -1.0, 1.0)) * 2.0 * strength
    ty = float(np.clip(target[1], -1.0, 1.0)) * 2.0 * strength
    shifted = _bilinear_sample(arr, np.clip(xs - tx, 0, w - 1), np.clip(ys - ty, 0, h - 1))
    m3 = m[..., None]
    result: np.ndarray = np.clip(shifted * m3 + arr * (1 - m3), 0.0, 1.0)
    return result.astype(np.float32)


def eyeblink_synthesis(img: np.ndarray, eye_mask: np.ndarray, *, phase: float = 0.5) -> np.ndarray:
    """Deterministic blink closure with a sin^2 ease (150 ms physiological window)."""
    arr = validate_image(img)
    m = validate_region(eye_mask, arr.shape[:2])
    if not 0.0 <= phase <= 1.0:
        raise InvalidRegionError("phase must be in [0, 1]")
    closure = float(np.sin(np.pi * phase) ** 2)
    if closure <= 0.0:
        return arr.copy()
    h, w = arr.shape[:2]
    squash = 1.0 - 0.6 * closure
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    cy = (h - 1) / 2.0
    src_y = cy + (ys - cy) / squash
    sampled = _bilinear_sample(arr, xs, np.clip(src_y, 0, h - 1))
    m3 = m[..., None]
    result: np.ndarray = np.clip(sampled * m3 + arr * (1 - m3), 0.0, 1.0)
    return result.astype(np.float32)


def facial_relighting_gradient(
    img: np.ndarray,
    *,
    light_azimuth_deg: float = 0.0,
    light_elevation_deg: float = 30.0,
    ambient: float = 0.3,
) -> np.ndarray:
    """Lambertian re-lighting from a surface normal estimated by the image gradient."""
    arr = validate_image(img)
    if not 0.0 <= ambient <= 1.0:
        raise InvalidImageError("ambient must be in [0, 1]")
    gray = arr.mean(axis=2)
    gy, gx = np.gradient(gray)
    scale = 4.0
    nx = -gx * scale
    ny = -gy * scale
    nz = np.ones_like(gray)
    norm = np.sqrt(nx**2 + ny**2 + nz**2)
    nx, ny, nz = nx / norm, ny / norm, nz / norm
    az = np.deg2rad(light_azimuth_deg)
    el = np.deg2rad(light_elevation_deg)
    lx = np.cos(el) * np.sin(az)
    ly = -np.cos(el) * np.cos(az)
    lz = np.sin(el)
    lambert = np.clip(nx * lx + ny * ly + nz * lz, 0.0, None)
    shade = ambient + (1.0 - ambient) * lambert
    result: np.ndarray = np.clip(arr * shade[..., None], 0.0, 1.0)
    return result.astype(np.float32)


def skintone_harmonization(
    img: np.ndarray, skin_mask: np.ndarray, *, strength: float = 0.5
) -> np.ndarray:
    """Constrain the skin region to the CIE L*a*b* skin locus, edge-preserving blend."""
    arr = validate_image(img)
    m = validate_region(skin_mask, arr.shape[:2])
    if not 0.0 <= strength <= 1.0:
        raise InvalidRegionError("strength must be in [0, 1]")
    lab = srgb_to_lab(arr)
    inside = m > 0.5
    l_c = float(np.clip(lab[..., 0][inside].mean(), *SKIN_L_RANGE)) if np.any(inside) else 60.0
    a_c = float(np.clip(lab[..., 1][inside].mean(), *SKIN_A_RANGE)) if np.any(inside) else 15.0
    b_c = float(np.clip(lab[..., 2][inside].mean(), *SKIN_B_RANGE)) if np.any(inside) else 20.0
    target = np.array([l_c, a_c, b_c], dtype=np.float32)
    harmonized = lab.copy()
    harmonized[..., 0] = np.clip(lab[..., 0], *SKIN_L_RANGE)
    harmonized[..., 1] = np.clip(lab[..., 1], *SKIN_A_RANGE)
    harmonized[..., 2] = np.clip(lab[..., 2], *SKIN_B_RANGE)
    for c in range(3):
        harmonized[..., c] = harmonized[..., c] * (1 - strength) + target[c] * strength
    converted = lab_to_srgb(harmonized)
    m3 = m[..., None]
    result: np.ndarray = np.clip(converted * m3 + arr * (1 - m3), 0.0, 1.0)
    return result.astype(np.float32)


def _tps_radial(r2: np.ndarray) -> np.ndarray:
    return np.where(r2 > 0, r2 * np.log(np.maximum(r2, 1e-12)) * 0.5, 0.0)


def _tps_kernel_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    d = a[:, None, :] - b[None, :, :]
    return _tps_radial((d**2).sum(axis=-1))


def _tps_kernel_points(src: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    dx = xs[..., None] - src[None, None, :, 0]
    dy = ys[..., None] - src[None, None, :, 1]
    return _tps_radial(dx**2 + dy**2)


def _tps_component(
    src: np.ndarray, dst_vals: np.ndarray, xs: np.ndarray, ys: np.ndarray
) -> np.ndarray:
    n = len(src)
    k_mat = _tps_kernel_matrix(src, src)
    p_mat = np.hstack([np.ones((n, 1)), src])
    lhs = np.zeros((n + 3, n + 3))
    lhs[:n, :n] = k_mat
    lhs[:n, n:] = p_mat
    lhs[n:, :n] = p_mat.T
    rhs = np.concatenate([dst_vals, np.zeros(3)])
    params = np.linalg.solve(lhs + np.eye(n + 3) * 1e-6, rhs)
    weights, a = params[:n], params[n:]
    k_grid = _tps_kernel_points(src, xs, ys)
    result: np.ndarray = k_grid @ weights + a[0] + a[1] * xs + a[2] * ys
    return result.astype(np.float32)


def micro_expression_morph(
    img: np.ndarray, control_points: tuple[tuple[float, float, float, float], ...]
) -> np.ndarray:
    """Thin-plate-spline warp from (src_x, src_y, dst_x, dst_y) control points."""
    arr = validate_image(img)
    if not control_points:
        raise InvalidRegionError("at least one control point is required")
    h, w = arr.shape[:2]
    src = np.array([(p[0], p[1]) for p in control_points], dtype=np.float64)
    dst = np.array([(p[2], p[3]) for p in control_points], dtype=np.float64)
    if len(control_points) < 3:
        dy = float(dst[:, 1].mean() - src[:, 1].mean())
        dx = float(dst[:, 0].mean() - src[:, 0].mean())
        ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
        return _bilinear_sample(arr, np.clip(xs - dx, 0, w - 1), np.clip(ys - dy, 0, h - 1))
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float64)
    # control points give the (src -> dst) displacement; sample the source at
    # ``dst - disp``. The displacement is clamped so the warp stays local and
    # never produces the runaway extrapolation a bare TPS can.
    bound = 8.0
    disp_x = np.clip(_tps_component(src, dst[:, 0] - src[:, 0], xs, ys), -bound, bound)
    disp_y = np.clip(_tps_component(src, dst[:, 1] - src[:, 1], xs, ys), -bound, bound)
    raw_x = xs - disp_x
    raw_y = ys - disp_y
    # fold-over guard on the *unclipped* map: clipping to the image border would
    # create a flat, zero-derivative margin and a spurious non-positive Jacobian
    d_sy_dy, d_sy_dx = np.gradient(raw_y)
    d_sx_dy, d_sx_dx = np.gradient(raw_x)
    det = d_sx_dx * d_sy_dy - d_sx_dy * d_sx_dx
    if np.any(det <= 0):
        raise DegenerateWarpError("micro-expression warp folds over (non-positive Jacobian)")
    return _bilinear_sample(
        arr,
        np.clip(raw_x, 0, w - 1).astype(np.float32),
        np.clip(raw_y, 0, h - 1).astype(np.float32),
    )


def teeth_whitening(
    img: np.ndarray, teeth_mask: np.ndarray, *, strength: float = 0.5
) -> np.ndarray:
    """Adaptive desaturation and brightness lift bounded to the teeth mask."""
    arr = validate_image(img)
    m = validate_region(teeth_mask, arr.shape[:2])
    if not 0.0 <= strength <= 1.0:
        raise InvalidRegionError("strength must be in [0, 1]")
    mean = arr.mean(axis=2, keepdims=True)
    desat = mean + (arr - mean) * (1.0 - 0.6 * strength)
    lifted = np.clip(desat * (1.0 + 0.15 * strength), 0.0, 1.0)
    m3 = m[..., None]
    result: np.ndarray = np.clip(lifted * m3 + arr * (1 - m3), 0.0, 1.0)
    return result.astype(np.float32)


def facial_hair_continuity(
    img: np.ndarray, beard_mask: np.ndarray, *, density: float = 0.5
) -> np.ndarray:
    """Gabor-filtered stubble texture whose density is held across shots."""
    arr = validate_image(img)
    m = validate_region(beard_mask, arr.shape[:2])
    if not 0.0 <= density <= 1.0:
        raise InvalidRegionError("density must be in [0, 1]")
    h, w = arr.shape[:2]
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    theta = np.deg2rad(75.0)
    gabor = np.sin(2 * np.pi * 0.5 * (xs * np.cos(theta) + ys * np.sin(theta)))
    texture = (gabor + 1.0) * 0.5
    darkening = 1.0 - 0.25 * density * texture
    m3 = m[..., None]
    shaded = arr * darkening[..., None]
    result: np.ndarray = np.clip(shaded * m3 + arr * (1 - m3), 0.0, 1.0)
    return result.astype(np.float32)


def head_repose_3d(img: np.ndarray, *, yaw_deg: float = 0.0, pitch_deg: float = 0.0) -> np.ndarray:
    """Weak-perspective cylindrical head re-pose within a +/-15 degree range."""
    arr = validate_image(img)
    if abs(yaw_deg) > 15.0 or abs(pitch_deg) > 15.0:
        raise InvalidRegionError("head repose is limited to +/-15 degrees")
    h, w = arr.shape[:2]
    cy, cx = (h - 1) / 2.0, (w - 1) / 2.0
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    nx = (xs - cx) / cx
    ny = (ys - cy) / cy
    yaw = np.deg2rad(yaw_deg)
    pitch = np.deg2rad(pitch_deg)
    src_x = cx + (nx * np.cos(yaw) + 0.15 * np.sin(yaw)) * cx
    src_y = cy + (ny * np.cos(pitch) + 0.15 * np.sin(pitch)) * cy
    return _bilinear_sample(arr, src_x, src_y)


def _bokeh_kernel(radius: int, blades: int) -> np.ndarray:
    y, x = np.mgrid[-radius : radius + 1, -radius : radius + 1].astype(np.float32)
    r = np.hypot(x, y)
    theta = np.arctan2(y, x)
    k = blades
    poly = np.cos(np.pi / k) / np.cos(((theta + np.pi / k) % (2 * np.pi / k)) - np.pi / k)
    result: np.ndarray = r <= radius * poly
    return result.astype(np.float32)


def _convolve2d(img: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    radius = kernel.shape[0] // 2
    pad = [(radius, radius), (radius, radius)] + [(0, 0)] * (img.ndim - 2)
    padded = np.pad(img, pad, mode="edge")
    out: np.ndarray = np.zeros_like(img)
    for i in range(kernel.shape[0]):
        for j in range(kernel.shape[1]):
            out += kernel[i, j] * padded[i : i + img.shape[0], j : j + img.shape[1]]
    return out


def depth_of_field_bokeh(
    img: np.ndarray, depth: np.ndarray, *, focus: float = 0.5, blades: int = 6
) -> np.ndarray:
    """Depth-driven polygonal-aperture bokeh that keeps the in-focus subject sharp."""
    arr = validate_image(img)
    d = np.asarray(depth, dtype=np.float32)
    if d.shape != arr.shape[:2]:
        raise InvalidImageError("depth map must match the image shape")
    if blades < 3:
        raise InvalidImageError("blades must be >= 3")
    kernel = _bokeh_kernel(6, blades)
    kernel /= max(float(kernel.sum()), 1e-9)
    blurred = _convolve2d(arr, kernel)
    coc = np.clip(np.abs(d - focus) * 12.0, 0.0, 1.0)
    weight = coc[..., None]
    result: np.ndarray = np.clip(arr * (1 - weight) + blurred * weight, 0.0, 1.0)
    return result.astype(np.float32)


def aging_deaging_geometry(
    img: np.ndarray, *, years: float = 0.0, strength: float = 0.5
) -> np.ndarray:
    """Metric landmark displacement (nasolabial fold, jaw line), high-frequency detail kept."""
    arr = validate_image(img)
    if not 0.0 <= strength <= 1.0:
        raise InvalidImageError("strength must be in [0, 1]")
    h, w = arr.shape[:2]
    cy, cx = (h - 1) / 2.0, (w - 1) / 2.0
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    nx = (xs - cx) / cx
    ny = (ys - cy) / cy
    amount = (years / 60.0) * strength
    fold = np.exp(-((np.abs(nx) - 0.4) ** 2 + (ny - 0.3) ** 2) / 0.05)
    jaw = np.exp(-((np.abs(nx) - 0.6) ** 2 + (ny - 0.7) ** 2) / 0.1)
    shading = 1.0 - 0.2 * amount * fold + 0.1 * amount * jaw
    result: np.ndarray = np.clip(arr * shading[..., None], 0.0, 1.0)
    return result.astype(np.float32)
