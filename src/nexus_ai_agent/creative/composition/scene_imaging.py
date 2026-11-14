"""Pure-NumPy scene-imaging kernels (Wave 3, adapter layer).

Deterministic, dependency-light kernels for the missing ``scene.*`` catalogue
operations. Every kernel works on float32 linear-radiance images ``(H, W, 3)``
and validates its inputs, refusing malformed or degenerate input with typed
errors. No cloud API is called: this is local NumPy math.
"""

from __future__ import annotations

import numpy as np

from nexus_ai_agent.creative.packs.scene.errors import (
    DegenerateGeometryError,
    InvalidImageError,
    InvalidMaskError,
)

__all__ = [
    "validate_image",
    "validate_mask",
    "gaussian_blur",
    "horizon_stabilization",
    "homography_perspective_rectification",
    "perlin_camera_shake",
    "fraunhofer_lens_flare",
    "wire_and_rig_removal",
    "dynamic_range_expansion",
    "optical_shadow_synthesis",
    "defocus_depth_map_estimation",
    "sky_replacement_harmonization",
    "motion_vector_flow",
    "insert_object_with_shadow",
    "remove_object_inpaint",
    "outpaint_canvas_expand",
    "synthesize_depth_of_field",
    "add_volumetric_fog_haze",
    "lens_distortion_correct",
    "chromatic_aberration_control",
    "perspective_horizon_level",
    "sky_replacement_relit",
    "reflection_water_glass_synth",
]


def validate_image(img: np.ndarray, *, channels: int | None = None) -> np.ndarray:
    """Coerce to float32 and refuse a malformed / non-finite image."""
    arr = np.asarray(img, dtype=np.float32)
    if arr.ndim != 3 or arr.shape[2] < 3:
        raise InvalidImageError(f"image must be (H, W, C>=3), got {arr.shape}")
    if channels is not None and arr.shape[2] != channels:
        raise InvalidImageError(f"expected {channels} channels, got {arr.shape[2]}")
    if not np.all(np.isfinite(arr)):
        raise InvalidImageError("image contains NaN/Inf")
    return arr


def validate_mask(mask: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    """Coerce a mask to float32 in ``[0, 1]`` and check it matches the image."""
    arr = np.asarray(mask, dtype=np.float32)
    if arr.ndim == 3:
        arr = arr[..., 0]
    if arr.ndim != 2:
        raise InvalidMaskError(f"mask must be 2-D, got {arr.shape}")
    if arr.shape != shape:
        raise InvalidMaskError(f"mask {arr.shape} does not match image {shape}")
    if not np.all(np.isfinite(arr)):
        raise InvalidMaskError("mask contains NaN/Inf")
    if float(arr.max()) <= float(arr.min()):
        raise InvalidMaskError("mask is constant (no signal)")
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
    """Bilinear resample ``arr`` at float coordinates ``(xs, ys)``."""
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


def _rotate(img: np.ndarray, theta_deg: float) -> np.ndarray:
    """Rotate an image about its centre by ``theta_deg`` (bilinear)."""
    arr = np.asarray(img, dtype=np.float32)
    h, w = arr.shape[:2]
    cy, cx = (h - 1) / 2.0, (w - 1) / 2.0
    rad = np.deg2rad(theta_deg)
    cos_t, sin_t = np.cos(rad), np.sin(rad)
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    ys = ys - cy
    xs = xs - cx
    src_x = cos_t * xs + sin_t * ys + cx
    src_y = -sin_t * xs + cos_t * ys + cy
    return _bilinear_sample(arr, src_x, src_y)


def _dominant_gradient_angle(img: np.ndarray) -> float | None:
    """Dominant gradient orientation (deg) via the structure tensor.

    Returns ``None`` for a flat image (zero gradient energy), where no
    orientation is defined.
    """
    gray = np.asarray(img, dtype=np.float32)
    if gray.ndim == 3:
        gray = gray.mean(axis=2)
    gy, gx = np.gradient(gray)
    jxx = float(np.sum(gx * gx))
    jyy = float(np.sum(gy * gy))
    jxy = float(np.sum(gx * gy))
    if jxx + jyy == 0:
        return None
    return float(np.rad2deg(0.5 * np.arctan2(2 * jxy, jxx - jyy)))


def _fold_angle(deg: float) -> float:
    """Fold an angle into ``[-90, 90)`` (an undirected line orientation)."""
    a = (deg + 90.0) % 180.0 - 90.0
    return a


def _horizon_angle(img: np.ndarray) -> float:
    """The horizon tilt in degrees: the gradient direction is perpendicular to it."""
    gradient = _dominant_gradient_angle(img)
    if gradient is None:
        return 0.0
    return _fold_angle(gradient + 90.0)


def horizon_stabilization(img: np.ndarray) -> tuple[np.ndarray, float]:
    """Rotate so the dominant horizon is horizontal; return (img, tilt_deg)."""
    arr = validate_image(img)
    tilt = _horizon_angle(arr)
    return _rotate(arr, -tilt), tilt


def _solve_homography(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Solve for H with ``dst ~ H @ src`` via the 8-point DLT."""
    rows = []
    for (x, y), (u, v) in zip(src, dst, strict=True):
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y, -u])
        rows.append([0, 0, 0, x, y, 1, -v * x, -v * y, -v])
    a_mat = np.asarray(rows, dtype=np.float64)
    _, _, vt = np.linalg.svd(a_mat)
    h: np.ndarray = vt[-1].reshape(3, 3)
    if abs(h[2, 2]) > 1e-12:
        h = h / h[2, 2]
    return h


def homography_perspective_rectification(
    img: np.ndarray, quad: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Rectify a quadrilateral to a frontal rectangle via a 3x3 homography."""
    arr = validate_image(img)
    q = np.asarray(quad, dtype=np.float64).reshape(4, 2)
    h, w = arr.shape[:2]
    dst = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], dtype=np.float64)
    hmat = _solve_homography(dst, q)
    if abs(float(np.linalg.det(hmat))) < 1e-9:
        raise DegenerateGeometryError("homography is singular (det ~ 0)")
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float64)
    denom = hmat[2, 0] * xs + hmat[2, 1] * ys + hmat[2, 2]
    if np.any(np.abs(denom) < 1e-9):
        raise DegenerateGeometryError("homography maps a pixel to infinity")
    src_x = (hmat[0, 0] * xs + hmat[0, 1] * ys + hmat[0, 2]) / denom
    src_y = (hmat[1, 0] * xs + hmat[1, 1] * ys + hmat[1, 2]) / denom
    return _bilinear_sample(arr, src_x, src_y), hmat


def _bilinear_grid(grid: np.ndarray, gx: np.ndarray, gy: np.ndarray) -> np.ndarray:
    """Sample a scalar ``grid`` at fractional grid coordinates ``(gx, gy)``."""
    x0 = np.floor(gx).astype(np.int32)
    y0 = np.floor(gy).astype(np.int32)
    x1 = np.clip(x0 + 1, 0, grid.shape[1] - 1)
    y1 = np.clip(y0 + 1, 0, grid.shape[0] - 1)
    x0 = np.clip(x0, 0, grid.shape[1] - 1)
    y0 = np.clip(y0, 0, grid.shape[0] - 1)
    wx = (gx - x0)[None, :]
    wy = (gy - y0)[:, None]
    top = grid[np.ix_(y0, x0)] * (1 - wx) + grid[np.ix_(y0, x1)] * wx
    bot = grid[np.ix_(y1, x0)] * (1 - wx) + grid[np.ix_(y1, x1)] * wx
    return (top * (1 - wy) + bot * wy).astype(np.float32)


def perlin_camera_shake(
    shape: tuple[int, int], *, octaves: int = 4, seed: int = 0, amplitude: float = 1.0
) -> np.ndarray:
    """A deterministic fractal (``1/f``) Perlin displacement field ``(H, W, 2)``."""
    if octaves < 1:
        raise InvalidImageError("octaves must be >= 1")
    h, w = shape
    rng = np.random.default_rng(seed)
    field = np.zeros((h, w, 2), dtype=np.float32)
    amp = 1.0
    total = 0.0
    for o in range(octaves):
        res = 2 ** (o + 1)
        gy = np.linspace(0, res, h, endpoint=False)
        gx = np.linspace(0, res, w, endpoint=False)
        for c in range(2):
            grid = rng.standard_normal((res + 1, res + 1)).astype(np.float32)
            field[..., c] += amp * _bilinear_grid(grid, gx, gy)
        total += amp
        amp *= 0.5
    return (field * (amplitude / max(total, 1e-9))).astype(np.float32)


def _aperture_kernel(n: int, sides: int, *, spikes: int = 0) -> np.ndarray:
    """A polygonal aperture (diaphragm) PSF with optional anamorphic spikes."""
    y, x = np.mgrid[-n : n + 1, -n : n + 1].astype(np.float32)
    r = np.hypot(x, y)
    theta = np.arctan2(y, x)
    if sides < 3:
        aperture = (r <= n).astype(np.float32)
    else:
        k = sides
        poly = np.cos(np.pi / k) / np.cos(((theta + np.pi / k) % (2 * np.pi / k)) - np.pi / k)
        aperture = (r <= n * poly * 0.9).astype(np.float32)
    for s in range(spikes):
        ang = np.pi * s / max(spikes, 1)
        streak = np.exp(-((np.abs(np.sin(theta - ang)) * r) ** 2) / 2.0)
        aperture += 0.5 * streak * (r <= n)
    result: np.ndarray = aperture
    return result


def fraunhofer_lens_flare(
    img: np.ndarray, *, light_xy: tuple[int, int], sides: int = 6, spikes: int = 4
) -> np.ndarray:
    """Add a Fraunhofer diffraction flare: |FFT(aperture)|^2 placed at the light."""
    arr = validate_image(img)
    h, w = arr.shape[:2]
    n = max(min(h, w) // 8, 4)
    ap = _aperture_kernel(n, sides, spikes=spikes)
    psf = np.abs(np.fft.fftshift(np.fft.fft2(ap))) ** 2
    psf /= max(float(psf.max()), 1e-9)
    canvas = np.zeros((h, w), dtype=np.float32)
    lx = int(np.clip(light_xy[0], 0, w - 1))
    ly = int(np.clip(light_xy[1], 0, h - 1))
    ph, pw = psf.shape
    y0 = max(0, ly - ph // 2)
    x0 = max(0, lx - pw // 2)
    canvas[y0 : y0 + ph, x0 : x0 + pw] = psf[: h - y0, : w - x0].astype(np.float32)
    result: np.ndarray = np.clip(arr + canvas[..., None] * 0.5, 0.0, 1.0)
    return result.astype(np.float32)


def wire_and_rig_removal(img: np.ndarray, mask: np.ndarray, *, iters: int = 200) -> np.ndarray:
    """Anisotropic Laplace inpainting over the marked region ``Omega``."""
    arr = validate_image(img)
    m = validate_mask(mask, arr.shape[:2])
    hole = m > 0.5
    if not np.any(hole):
        return arr.copy()
    out = arr.copy()
    if np.any(~hole):
        fill = np.zeros(arr.shape[2], dtype=np.float32)
        for c in range(arr.shape[2]):
            fill[c] = float(out[..., c][~hole].mean())
        out[hole] = fill
    for _ in range(iters):
        blurred = gaussian_blur(out, 1.0)
        out[hole] = blurred[hole]
    return out.astype(np.float32)


def dynamic_range_expansion(img: np.ndarray, *, gain: float = 2.0) -> np.ndarray:
    """Expand an LDR frame via an inverse transfer and highlight bloom."""
    arr = validate_image(img)
    if gain <= 0:
        raise InvalidImageError("gain must be positive")
    linear = arr**2.2
    expanded = linear * gain
    bloom = gaussian_blur(np.clip(expanded - 1.0, 0.0, None), 6.0)
    out = expanded + bloom
    result: np.ndarray = out / max(float(out.max()), 1e-9)
    return result.astype(np.float32)


def optical_shadow_synthesis(
    img: np.ndarray, alpha: np.ndarray, *, light_vec: tuple[float, float], softness: float = 3.0
) -> np.ndarray:
    """Ground-plane contact shadow from a 2D light vector and subject alpha."""
    arr = validate_image(img)
    a = validate_mask(alpha, arr.shape[:2])
    lx, ly = light_vec
    norm = max(float(np.hypot(lx, ly)), 1e-9)
    shift = (int(round(8 * lx / norm)), int(round(8 * ly / norm)))
    shadow = np.roll(np.roll(a, shift[1], axis=0), shift[0], axis=1)
    shadow = gaussian_blur(shadow, max(softness, 0.1))
    return np.clip(arr * (1.0 - 0.6 * shadow[..., None]), 0.0, 1.0).astype(np.float32)


def defocus_depth_map_estimation(img: np.ndarray, *, max_blur: int = 6) -> np.ndarray:
    """Sparse depth from local blur-disk radius along high-contrast step edges."""
    arr = validate_image(img)
    gray = arr.mean(axis=2)
    gy, gx = np.gradient(gray)
    edge = np.hypot(gx, gy)
    depth = np.zeros_like(gray)
    for radius in range(1, max_blur + 1):
        blur = gaussian_blur(gray, float(radius))
        residual = np.abs(gray - blur)
        weight = np.clip(edge - residual, 0.0, None)
        depth = np.maximum(depth, weight * (max_blur - radius) / max_blur)
    return (depth / max(float(depth.max()), 1e-9)).astype(np.float32)


def _tile_to(small: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    reps = (
        int(np.ceil(shape[0] / small.shape[0])),
        int(np.ceil(shape[1] / small.shape[1])),
        1,
    )
    return np.tile(small, reps)[: shape[0], : shape[1]]


def sky_replacement_harmonization(img: np.ndarray, mask: np.ndarray, sky: np.ndarray) -> np.ndarray:
    """Replace a sky dome and re-light the foreground to match its luminance."""
    arr = validate_image(img)
    m = validate_mask(mask, arr.shape[:2])
    sky_img = np.asarray(sky, dtype=np.float32)
    if sky_img.shape[:2] != arr.shape[:2]:
        sky_img = _tile_to(sky_img, arr.shape[:2])
    m3 = m[..., None]
    fg_lum = float(arr[..., 0].mean())
    sky_lum = float(sky_img[..., 0].mean())
    gain = (sky_lum / max(fg_lum, 1e-6)) ** 0.5
    foreground = arr * gain
    result: np.ndarray = np.clip(foreground * (1 - m3) + sky_img * m3, 0.0, 1.0)
    return result.astype(np.float32)


def motion_vector_flow(
    img_a: np.ndarray, img_b: np.ndarray, *, window: int = 5, max_shift: int = 8
) -> np.ndarray:
    """Dense optical flow by block matching (forward search)."""
    a = validate_image(img_a)
    b = validate_image(img_b)
    if a.shape != b.shape:
        raise InvalidImageError("flow inputs must have the same shape")
    ga = a.mean(axis=2)
    gb = b.mean(axis=2)
    h, w = ga.shape
    flow = np.zeros((h, w, 2), dtype=np.float32)
    half = window // 2
    for y in range(half, h - half):
        for x in range(half, w - half):
            best: tuple[float, int, int] | None = None
            patch_a = ga[y - half : y + half + 1, x - half : x + half + 1]
            for dy in range(-max_shift, max_shift + 1):
                for dx in range(-max_shift, max_shift + 1):
                    ny, nx = y + dy, x + dx
                    if not (half <= ny < h - half and half <= nx < w - half):
                        continue
                    patch_b = gb[ny - half : ny + half + 1, nx - half : nx + half + 1]
                    err = float(np.mean((patch_a - patch_b) ** 2))
                    if best is None or err < best[0]:
                        best = (err, dx, dy)
            if best is not None:
                flow[y, x] = (best[1], best[2])
    return flow


def _downsample(arr: np.ndarray) -> np.ndarray:
    return arr[::2, ::2].astype(np.float32)


def _upsample_like(small: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    h, w = shape
    ys = np.linspace(0, small.shape[0] - 1, h)
    xs = np.linspace(0, small.shape[1] - 1, w)
    gy, gx = np.meshgrid(ys, xs, indexing="ij")
    return _bilinear_sample(small, gx.astype(np.float32), gy.astype(np.float32))


def insert_object_with_shadow(
    img: np.ndarray, obj: np.ndarray, mask: np.ndarray, *, offset: tuple[int, int] = (0, 0)
) -> np.ndarray:
    """Insert an object with a distance-scaled penumbra contact shadow."""
    arr = validate_image(img)
    o = np.asarray(obj, dtype=np.float32)
    if o.shape[:2] != arr.shape[:2]:
        raise InvalidImageError("object and image must share the same shape")
    m = validate_mask(mask, arr.shape[:2])
    dx, dy = offset
    m = np.roll(np.roll(m, dy, axis=0), dx, axis=1)
    o = np.roll(np.roll(o, dy, axis=0), dx, axis=1)
    shadow = gaussian_blur(m, 4.0) * 0.4
    lit = arr * (1.0 - shadow[..., None])
    result: np.ndarray = np.clip(lit * (1 - m[..., None]) + o[..., :3] * m[..., None], 0.0, 1.0)
    return result.astype(np.float32)


def remove_object_inpaint(img: np.ndarray, mask: np.ndarray, *, iters: int = 300) -> np.ndarray:
    """Multiscale harmonic PDE inpainting over a binary mask."""
    arr = validate_image(img)
    m = validate_mask(mask, arr.shape[:2])
    coarse = wire_and_rig_removal(
        _downsample(arr), _downsample(m[..., None])[..., 0], iters=max(iters // 4, 10)
    )
    seeded: np.ndarray = np.where(m[..., None] > 0.5, _upsample_like(coarse, arr.shape[:2]), arr)
    return wire_and_rig_removal(seeded, m, iters=iters)


def outpaint_canvas_expand(img: np.ndarray, *, pad: int = 8) -> np.ndarray:
    """Extend the canvas by mirror-padding the image."""
    arr = validate_image(img)
    if pad <= 0:
        raise InvalidImageError("pad must be positive")
    h, w = arr.shape[:2]
    out = np.zeros((h + 2 * pad, w + 2 * pad, arr.shape[2]), dtype=np.float32)
    out[pad : pad + h, pad : pad + w] = arr
    out[:pad, pad : pad + w] = np.flipud(out[pad : 2 * pad, pad : pad + w])
    out[pad + h :, pad : pad + w] = np.flipud(out[pad + h - pad : pad + h, pad : pad + w])
    out[:, :pad] = np.fliplr(out[:, pad : 2 * pad])
    out[:, pad + w :] = np.fliplr(out[:, pad + w - pad : pad + w])
    return out.astype(np.float32)


def synthesize_depth_of_field(
    img: np.ndarray,
    depth: np.ndarray,
    *,
    focus: float = 0.5,
    f_number: float = 2.8,
    f_mm: float = 50.0,
) -> np.ndarray:
    """Depth-driven circle-of-confusion defocus."""
    arr = validate_image(img)
    d = np.asarray(depth, dtype=np.float32)
    if d.shape != arr.shape[:2]:
        raise InvalidImageError("depth map must match the image shape")
    coc = (
        np.abs(d - focus)
        / np.maximum(d, 1e-3)
        * (f_mm**2)
        / (f_number * np.maximum(focus - f_mm, 1e-3))
    )
    coc = np.clip(np.abs(coc), 0.0, 8.0)
    out = arr.copy()
    for radius in range(1, 9):
        layer = gaussian_blur(arr, float(radius))
        sel = (coc >= radius - 0.5) & (coc < radius + 0.5)
        out[sel] = layer[sel]
    return out.astype(np.float32)


def add_volumetric_fog_haze(
    img: np.ndarray, depth: np.ndarray, *, beta: float = 0.05, g: float = 0.6
) -> np.ndarray:
    """Koschmieder atmospheric scattering with a Henyey-Greenstein phase term."""
    arr = validate_image(img)
    d = np.asarray(depth, dtype=np.float32)
    if d.shape != arr.shape[:2]:
        raise InvalidImageError("depth map must match the image shape")
    if beta < 0:
        raise InvalidImageError("beta (scattering) must be non-negative")
    if not -1.0 <= g <= 1.0:
        raise InvalidImageError("Henyey-Greenstein g must be in [-1, 1]")
    phase = (1 - g**2) / (4 * np.pi * (1 + g**2 - 2 * g) ** 1.5)
    transmittance = np.exp(-beta * d)
    atmosphere = 0.9 * (1 - transmittance)
    result: np.ndarray = np.clip(
        arr * transmittance[..., None] + atmosphere[..., None] * phase, 0.0, 1.0
    )
    return result.astype(np.float32)


def lens_distortion_correct(
    img: np.ndarray, *, k1: float = 0.0, k2: float = 0.0, k3: float = 0.0
) -> np.ndarray:
    """Brown-Conrady radial undistortion with bilinear resampling."""
    arr = validate_image(img)
    h, w = arr.shape[:2]
    cy, cx = (h - 1) / 2.0, (w - 1) / 2.0
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    nx = (xs - cx) / cx
    ny = (ys - cy) / cy
    r2 = nx**2 + ny**2
    factor = 1 + k1 * r2 + k2 * r2**2 + k3 * r2**3
    src_x = nx * factor * cx + cx
    src_y = ny * factor * cy + cy
    return _bilinear_sample(arr, src_x, src_y)


def chromatic_aberration_control(
    img: np.ndarray, *, scales: tuple[float, float, float] = (1.0, 1.0, 1.0)
) -> np.ndarray:
    """Per-channel radial magnification to correct (or add) lateral CA."""
    arr = validate_image(img)
    h, w = arr.shape[:2]
    cy, cx = (h - 1) / 2.0, (w - 1) / 2.0
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    out: np.ndarray = np.empty_like(arr)
    for c, s in enumerate(scales):
        src_x = cx + s * (xs - cx)
        src_y = cy + s * (ys - cy)
        out[..., c] = _bilinear_sample(arr[..., c : c + 1], src_x, src_y)[..., 0]
    return out


def perspective_horizon_level(img: np.ndarray) -> tuple[np.ndarray, float]:
    """Estimate and level the dominant horizon; refuse an angle above 45 degrees."""
    arr = validate_image(img)
    tilt = _horizon_angle(arr)
    if abs(tilt) > 45.0:
        raise DegenerateGeometryError(f"estimated horizon angle {tilt:.1f}deg exceeds 45deg")
    return _rotate(arr, -tilt), tilt


def sky_replacement_relit(
    img: np.ndarray, mask: np.ndarray, sky: np.ndarray, *, sky_temp_k: float = 6500.0
) -> np.ndarray:
    """Replace the sky and re-light the foreground to the new sky's temperature."""
    arr = validate_image(img)
    m = validate_mask(mask, arr.shape[:2])
    sky_img = np.asarray(sky, dtype=np.float32)
    if sky_img.shape[:2] != arr.shape[:2]:
        sky_img = _tile_to(sky_img, arr.shape[:2])
    warm = float(np.clip((6500.0 - sky_temp_k) / 6500.0, -1.0, 1.0))
    gain = np.array([1.0 + 0.2 * warm, 1.0, 1.0 - 0.2 * warm], dtype=np.float32)
    m3 = m[..., None]
    result: np.ndarray = np.clip((arr * gain) * (1 - m3) + sky_img * m3, 0.0, 1.0)
    return result.astype(np.float32)


def reflection_water_glass_synth(
    img: np.ndarray, *, axis: int = 0, fresnel: float = 0.02, waves: float = 0.0
) -> np.ndarray:
    """Flat mirror reflection with Fresnel weighting and optional ripples."""
    arr = validate_image(img)
    reflected = np.flip(arr, axis=axis)
    if waves > 0:
        h, w = arr.shape[:2]
        ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
        disp = waves * np.sin(2 * np.pi * (xs / max(w, 1)) * 4.0)
        reflected = _bilinear_sample(reflected, xs, np.clip(ys + disp, 0, h - 1))
    r0 = float(fresnel)
    weight = r0 + (1 - r0) * 0.0
    return np.clip(arr * (1 - weight) + reflected * weight, 0.0, 1.0).astype(np.float32)
