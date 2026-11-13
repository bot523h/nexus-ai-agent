"""Wave 10 — optical-flow computational cinematography (variable frame rate).

Pure-NumPy dense optical flow and flow-based temporal interpolation for
variable-frame-rate retiming and motion-compensated synthesis. The math is the
classical variational/least-squares literature, implemented deterministically:

* **Lucas–Kanade** local least squares, solved per pixel from the windowed
  structure tensor, in a coarse-to-fine Gaussian/box pyramid.
* **Horn–Schunck** global variational flow with a smoothness prior.
* **Forward–backward consistency** to expose occlusions honestly instead of
  inventing a confident motion where the two directions disagree.
* **Occlusion-aware frame interpolation** at a fractional time ``t``: each
  source is warped along its own flow to ``t`` and blended, with the visible
  source preferred where only one direction is consistent.

Everything operates on scene-linear float radiance in ``[0, 1]`` and is bounded
(:data:`MAX_FLOW_PIXELS`). The module never touches the network and never owns
project state; it returns arrays to the caller.

Sign convention: ``flow[p]`` is the displacement that carries the point at ``p``
in ``img_a`` to its position in ``img_b``, so ``a(p) ~ b(p + flow(p))``. Because
:func:`warp_by_flow` samples ``img(x + flow_x, y + flow_y)``, aligning ``b`` onto
``a`` is ``warp_by_flow(b, flow_ab)`` and aligning ``a`` onto ``b`` is
``warp_by_flow(a, -flow_ab)``.
"""

from __future__ import annotations

import math

import numpy as np

__all__ = [
    "MAX_FLOW_PIXELS",
    "FlowError",
    "DegenerateFlowError",
    "to_gray",
    "warp_by_flow",
    "forward_backward_consistency",
    "lucas_kanade_flow",
    "horn_schunck_flow",
    "interpolate_frame",
    "flow_endpoint_error",
]

MAX_FLOW_PIXELS = 4_194_304  # 2048 x 2048
_EPS = 1e-12
# Robustness constants for the Lucas-Kanade solver (see its docstring).
_RIDGE_RELATIVE = 0.05
_MAX_STEP = 1.0


class FlowError(ValueError):
    """Raised for malformed flow inputs (bad shape, NaN, over-budget)."""


class DegenerateFlowError(FlowError):
    """Raised when a required motion field cannot be estimated honestly."""


def _validate_image(img: np.ndarray, *, name: str) -> np.ndarray:
    a = np.asarray(img, dtype=np.float64)
    if a.ndim not in (2, 3) or (a.ndim == 3 and a.shape[-1] not in (1, 3)):
        raise FlowError(f"{name} must be (H, W) or (H, W, 3), got {a.shape}")
    if not np.all(np.isfinite(a)):
        raise FlowError(f"{name} contains NaN/Inf")
    if a.shape[0] * a.shape[1] > MAX_FLOW_PIXELS:
        raise FlowError(f"{name} exceeds the bounded pixel budget")
    return a


def _validate_flow(flow: np.ndarray, shape: tuple[int, int], *, name: str) -> np.ndarray:
    f = np.asarray(flow, dtype=np.float64)
    if f.shape != (shape[0], shape[1], 2):
        raise FlowError(f"{name} must be (H, W, 2) matching the image, got {f.shape}")
    if not np.all(np.isfinite(f)):
        raise FlowError(f"{name} contains NaN/Inf")
    return f


def to_gray(img: np.ndarray) -> np.ndarray:
    """Rec.709 luma of an RGB image, or the image itself if already 2-D."""
    a = _validate_image(img, name="image")
    if a.ndim == 2:
        return a
    if a.shape[-1] == 1:
        return a[..., 0]
    return a[..., 0] * 0.2126 + a[..., 1] * 0.7152 + a[..., 2] * 0.0722


def _bilinear_sample(arr: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """Bilinear sample ``arr`` at float coordinates ``(xs, ys)`` with edge clamp."""
    h, w = arr.shape[0], arr.shape[1]
    xs = np.clip(xs, 0.0, w - 1.0)
    ys = np.clip(ys, 0.0, h - 1.0)
    x0 = np.floor(xs).astype(np.int64)
    y0 = np.floor(ys).astype(np.int64)
    x1 = np.minimum(x0 + 1, w - 1)
    y1 = np.minimum(y0 + 1, h - 1)
    wx = xs - x0
    wy = ys - y0
    if arr.ndim == 2:
        top2 = arr[y0, x0] * (1 - wx) + arr[y0, x1] * wx
        bot2 = arr[y1, x0] * (1 - wx) + arr[y1, x1] * wx
        out2: np.ndarray = top2 * (1 - wy) + bot2 * wy
        return out2
    wx = wx[..., None]
    wy = wy[..., None]
    top = arr[y0, x0] * (1 - wx) + arr[y0, x1] * wx
    bot = arr[y1, x0] * (1 - wx) + arr[y1, x1] * wx
    out3: np.ndarray = top * (1 - wy) + bot * wy
    return out3


def warp_by_flow(img: np.ndarray, flow: np.ndarray) -> np.ndarray:
    """Resample ``img`` along ``flow``: ``out(p) = img(p + flow(p))``."""
    a = _validate_image(img, name="image")
    f = _validate_flow(flow, a.shape[:2], name="flow")
    h, w = a.shape[:2]
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float64)
    return _bilinear_sample(a, xs + f[..., 0], ys + f[..., 1])


def _window_sum(arr: np.ndarray, radius: int) -> np.ndarray:
    """Sliding sum over a ``(2r+1)^2`` window with edge padding."""
    a = arr.astype(np.float64)
    pads = [(radius, radius), (radius, radius)] + [(0, 0)] * (a.ndim - 2)
    padded = np.pad(a, pads, mode="edge")
    h, w = a.shape[:2]
    out = np.zeros_like(a, dtype=np.float64)
    for dy in range(2 * radius + 1):
        for dx in range(2 * radius + 1):
            out += padded[dy : dy + h, dx : dx + w]
    return out


def _gaussian_kernel(sigma: float) -> np.ndarray:
    radius = max(1, int(math.ceil(3.0 * sigma)))
    x = np.arange(-radius, radius + 1, dtype=np.float64)
    k = np.exp(-(x * x) / (2.0 * sigma * sigma))
    out: np.ndarray = k / k.sum()
    return out


def _blur(img: np.ndarray, sigma: float) -> np.ndarray:
    k = _gaussian_kernel(sigma)
    r = k.shape[0] // 2
    a = img.astype(np.float64)
    if a.ndim == 2:
        a = a[..., None]
    pads = [(r, r), (r, r), (0, 0)]
    padded = np.pad(a, pads, mode="edge")
    h, w, c = a.shape
    tmp = np.zeros_like(a)
    for i, kv in enumerate(k):
        tmp += kv * padded[i : i + h, r : r + w]
    padded = np.pad(tmp, pads, mode="edge")
    out = np.zeros_like(a)
    for j, kv in enumerate(k):
        out += kv * padded[r : r + h, j : j + w]
    return out[..., 0] if out.shape[-1] == 1 else out


def _downsample(img: np.ndarray) -> np.ndarray:
    a = _blur(img, 1.0)
    return a[::2, ::2]


def _upsample(small: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    h, w = shape
    ys = np.linspace(0, small.shape[0] - 1, h)
    xs = np.linspace(0, small.shape[1] - 1, w)
    gy, gx = np.meshgrid(ys, xs, indexing="ij")
    return _bilinear_sample(small, gx, gy)


def _median_filter3(flow: np.ndarray) -> np.ndarray:
    """3x3 median filter of a (H, W, 2) field (edge-clamped), outlier-robust."""
    out = flow.copy()
    h, w = flow.shape[:2]
    for c in range(2):
        p = np.pad(flow[..., c], 1, mode="edge")
        stack = np.stack(
            [p[dy : dy + h, dx : dx + w] for dy in range(3) for dx in range(3)], axis=-1
        )
        out[..., c] = np.median(stack, axis=-1)
    return out


def lucas_kanade_flow(
    img_a: np.ndarray,
    img_b: np.ndarray,
    *,
    window: int = 5,
    iters: int = 3,
    levels: int = 3,
) -> np.ndarray:
    """Coarse-to-fine Lucas–Kanade dense flow from ``img_a`` to ``img_b``.

    At each pyramid level the windowed structure tensor ``G`` and residual
    ``[Σ Ix It, Σ Iy It]`` give the per-pixel increment
    ``df = -(G + ridge I)^{-1} [Σ Ix It, Σ Iy It]``. Two robustness devices keep
    the estimate stable on real footage: a relative Levenberg–Marquardt ridge
    (``ridge = 0.05 * trace(G) / 2``) so a near-flat window is damped rather than
    amplified, and a per-increment magnitude cap of one pixel so a bad
    linearisation cannot overshoot. A final 3x3 median filter removes isolated
    outliers. This is a deterministic local estimator with low endpoint error on
    synthetic translations (see the tests); it is not a global-optimality claim.
    """
    if window < 3 or window % 2 == 0:
        raise FlowError("window must be an odd integer >= 3")
    if iters < 1:
        raise FlowError("iters must be >= 1")
    if levels < 1:
        raise FlowError("levels must be >= 1")
    ga = to_gray(img_a)
    gb = to_gray(img_b)
    if ga.shape != gb.shape:
        raise FlowError("flow inputs must have the same shape")
    h, w = ga.shape
    max_levels = 1
    while max_levels < levels and min(h, w) // (2**max_levels) >= 16:
        max_levels += 1

    pyramid_a = [ga]
    pyramid_b = [gb]
    for _ in range(1, max_levels):
        pyramid_a.append(_downsample(pyramid_a[-1]))
        pyramid_b.append(_downsample(pyramid_b[-1]))

    radius = window // 2
    flow: np.ndarray | None = None
    for level in range(max_levels - 1, -1, -1):
        la = pyramid_a[level]
        lb = pyramid_b[level]
        lh, lw = la.shape
        if flow is None:
            flow = np.zeros((lh, lw, 2), dtype=np.float64)
        else:
            fx = _upsample(flow[..., 0], (lh, lw)) * 2.0
            fy = _upsample(flow[..., 1], (lh, lw)) * 2.0
            flow = np.stack([fx, fy], axis=-1)
        ys_grid = np.mgrid[0:lh][:, None]
        xs_grid = np.mgrid[0:lw][None, :]
        for _ in range(iters):
            warped = _bilinear_sample(lb, xs_grid + flow[..., 0], ys_grid + flow[..., 1])
            grad_y, grad_x = np.gradient(warped)
            residual = warped - la
            ixx = _window_sum(grad_x * grad_x, radius)
            iyy = _window_sum(grad_y * grad_y, radius)
            ixy = _window_sum(grad_x * grad_y, radius)
            ixt = _window_sum(grad_x * residual, radius)
            iyt = _window_sum(grad_y * residual, radius)
            ridge = _RIDGE_RELATIVE * 0.5 * (ixx + iyy)
            a11 = ixx + ridge
            a22 = iyy + ridge
            det = a11 * a22 - ixy * ixy
            safe = np.where(det < _EPS, _EPS, det)
            du = -(a22 * ixt - ixy * iyt) / safe
            dv = -(a11 * iyt - ixy * ixt) / safe
            mag = np.sqrt(du * du + dv * dv)
            scale = np.where(mag > _MAX_STEP, _MAX_STEP / np.maximum(mag, _EPS), 1.0)
            flow[..., 0] += du * scale
            flow[..., 1] += dv * scale
        flow = _median_filter3(flow)
    assert flow is not None  # max_levels >= 1 guarantees at least one pass
    result: np.ndarray = flow.astype(np.float32)
    return result


def horn_schunck_flow(
    img_a: np.ndarray,
    img_b: np.ndarray,
    *,
    alpha: float = 1.0,
    iters: int = 200,
) -> np.ndarray:
    """Horn–Schunck global variational flow (``alpha`` weights smoothness)."""
    if alpha <= 0.0:
        raise FlowError("alpha must be positive")
    if iters < 1:
        raise FlowError("iters must be >= 1")
    ga = to_gray(img_a)
    gb = to_gray(img_b)
    if ga.shape != gb.shape:
        raise FlowError("flow inputs must have the same shape")
    gy, gx = np.gradient(ga)
    it = gb - ga
    u = np.zeros_like(ga)
    v = np.zeros_like(ga)
    a2 = alpha * alpha
    for _ in range(iters):
        u_avg = _window_sum(u, 1) / 9.0
        v_avg = _window_sum(v, 1) / 9.0
        denom = a2 + gx * gx + gy * gy
        common = (gx * u_avg + gy * v_avg + it) / denom
        u = u_avg - gx * common
        v = v_avg - gy * common
    return np.stack([u, v], axis=-1).astype(np.float32)


def forward_backward_consistency(
    flow_ab: np.ndarray,
    flow_ba: np.ndarray,
    *,
    threshold: float = 0.5,
) -> np.ndarray:
    """Boolean occlusion mask from forward–backward flow agreement.

    A pixel ``p`` is *consistent* (``True``) when warping it forward by
    ``flow_ab`` and back by ``flow_ba`` returns near ``p``:
    ``|| flow_ab(p) + flow_ba(p + flow_ab(p)) || <= threshold``. Inconsistent
    pixels are occlusions/disocclusions where the motion is not trusted.
    """
    fa = np.asarray(flow_ab, dtype=np.float64)
    fb = np.asarray(flow_ba, dtype=np.float64)
    if fa.shape != fb.shape or fa.ndim != 3 or fa.shape[-1] != 2:
        raise FlowError("forward/backward flows must share shape (H, W, 2)")
    if threshold <= 0.0:
        raise FlowError("threshold must be positive")
    h, w = fa.shape[:2]
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float64)
    back_x = _bilinear_sample(fb[..., 0], xs + fa[..., 0], ys + fa[..., 1])
    back_y = _bilinear_sample(fb[..., 1], xs + fa[..., 0], ys + fa[..., 1])
    error = np.sqrt((fa[..., 0] + back_x) ** 2 + (fa[..., 1] + back_y) ** 2)
    consistent: np.ndarray = error <= threshold
    return consistent


def flow_endpoint_error(flow: np.ndarray, reference: np.ndarray) -> float:
    """Mean endpoint error between two flow fields (a real distance metric)."""
    f = np.asarray(flow, dtype=np.float64)
    ref = np.asarray(reference, dtype=np.float64)
    if f.shape != ref.shape or f.ndim != 3 or f.shape[-1] != 2:
        raise FlowError("flow and reference must share shape (H, W, 2)")
    diff = f - ref
    return float(np.mean(np.sqrt((diff * diff).sum(axis=-1))))


def interpolate_frame(img_a: np.ndarray, img_b: np.ndarray, t: float) -> np.ndarray:
    """Flow-based interpolation of the frame at fractional time ``t in [0, 1]``.

    Each source is warped along its own (forward/backward) flow to time ``t`` and
    blended with occlusion-aware weights: where only one direction is
    forward–backward consistent, that visible source is preferred; where both
    agree, a linear blend is used.
    """
    if not 0.0 <= t <= 1.0:
        raise FlowError("t must be in [0, 1]")
    a = _validate_image(img_a, name="img_a")
    b = _validate_image(img_b, name="img_b")
    if a.shape != b.shape:
        raise FlowError("interpolation inputs must have the same shape")
    if t == 0.0:
        return a.astype(np.float32)
    if t == 1.0:
        return b.astype(np.float32)

    flow_ab = lucas_kanade_flow(a, b)
    flow_ba = lucas_kanade_flow(b, a)
    consistent_ab = forward_backward_consistency(flow_ab, flow_ba)
    consistent_ba = forward_backward_consistency(flow_ba, flow_ab)

    # flow_ab(p) = d means b(p + d) ~ a(p), i.e. a(p) ~ b(p + d). Content at
    # time t sits at x - t*d relative to frame a, so warp a backwards along its
    # flow and b forwards along its (reversed) flow.
    warped_a = warp_by_flow(a, -flow_ab * t)
    warped_b = warp_by_flow(b, -flow_ba * (1.0 - t))

    wa = (1.0 - t) * consistent_ab.astype(np.float64)
    wb = t * consistent_ba.astype(np.float64)
    total = wa + wb
    fallback = total < _EPS
    wa = np.where(fallback, 1.0 - t, wa)
    wb = np.where(fallback, t, wb)
    total = wa + wb
    wa_norm = wa / total
    wb_norm = wb / total
    if a.ndim == 3:
        wa_norm = wa_norm[..., None]
        wb_norm = wb_norm[..., None]
    blended = warped_a * wa_norm + warped_b * wb_norm
    return np.clip(blended, 0.0, 1.0).astype(np.float32)
