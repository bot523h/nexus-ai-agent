"""Depth-sorted 2.5D linear compositor with deterministic tiled persistence."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from .color_science import read_rgb8_png, srgb_encode, write_rgb8_png
from .contracts import KernelError
from .geometry import circle_of_confusion_px

FloatImage = NDArray[np.float64]


def _composite(
    layers: Sequence[tuple[FloatImage, FloatImage, float]],
    haze: FloatImage,
    beta: float,
    focus: tuple[float, float, float, float, int],
) -> FloatImage:
    if not layers or beta < 0:
        raise KernelError("layers required and extinction must be nonnegative")
    shape = np.asarray(layers[0][0]).shape
    if len(shape) != 3 or shape[2] != 3 or np.asarray(haze).shape != shape:
        raise KernelError("RGB layer and haze shapes must match")
    out = np.broadcast_to(haze, shape).copy()
    for rgb, alpha, depth in sorted(layers, key=lambda item: item[2], reverse=True):
        image, mask = np.asarray(rgb, float), np.asarray(alpha, float)
        if (
            image.shape != shape
            or mask.shape != shape[:2]
            or depth <= 0
            or np.any((mask < 0) | (mask > 1))
        ):
            raise KernelError("invalid 2.5D layer or alpha")
        transmission = np.exp(-beta * depth)
        scene = image * transmission + haze * (1 - transmission)
        radius = circle_of_confusion_px(depth, *focus)
        # Small, deterministic separable box approximation to a defocus disk.
        rad = min(8, int(np.ceil(radius / 2)))
        if rad:
            padded = np.pad(scene, ((rad, rad), (rad, rad), (0, 0)), mode="edge")
            blurred = np.zeros_like(scene)
            for dy in range(2 * rad + 1):
                for dx in range(2 * rad + 1):
                    blurred += padded[dy : dy + shape[0], dx : dx + shape[1], :]
            scene = blurred / ((2 * rad + 1) ** 2)
        out = scene * mask[..., None] + out * (1 - mask[..., None])
    return np.asarray(out, dtype=np.float64)


def render_25d_multiplane_composite(
    layers: Sequence[tuple[FloatImage, FloatImage, float]],
    haze: FloatImage,
    output_path: str | Path,
    *,
    beta: float | tuple[float, float, float],
    focus: tuple[float, float, float, float, int],
    tile_size_px: int = 32,
) -> str:
    """Evaluate each padded tile independently; only output RGB8 is frame-sized."""
    import hashlib
    import math

    if tile_size_px <= 0 or not layers:
        raise KernelError("tile size must be positive and layers non-empty")
    haze_array = np.asarray(haze, dtype=np.float64)
    height, width, channels = haze_array.shape
    if channels != 3:
        raise KernelError("haze must be RGB")
    beta_rgb = np.asarray((beta,) * 3 if isinstance(beta, (int, float)) else beta, dtype=float)
    if beta_rgb.shape != (3,) or np.any(beta_rgb < 0):
        raise KernelError("extinction must be a nonnegative scalar or RGB triple")
    order = sorted(layers, key=lambda item: item[2], reverse=True)
    halo = max(math.ceil(circle_of_confusion_px(depth, *focus) / 2) for _, _, depth in order)
    radii = [min(8, math.ceil(circle_of_confusion_px(depth, *focus) / 2)) for _, _, depth in order]
    output = np.empty((height, width, 3), dtype=np.uint8)
    for y0 in range(0, height, tile_size_px):
        y1 = min(height, y0 + tile_size_px)
        for x0 in range(0, width, tile_size_px):
            x1 = min(width, x0 + tile_size_px)
            ya, yb = max(0, y0 - halo), min(height, y1 + halo)
            xa, xb = max(0, x0 - halo), min(width, x1 + halo)
            pad = (
                (max(0, halo - y0), max(0, y1 + halo - height)),
                (max(0, halo - x0), max(0, x1 + halo - width)),
            )
            htile = np.pad(haze_array[ya:yb, xa:xb], pad + ((0, 0),), mode="edge")
            acc = htile.copy()
            for (rgb, alpha, depth), radius in zip(order, radii, strict=True):
                image = np.pad(np.asarray(rgb, float)[ya:yb, xa:xb], pad + ((0, 0),), mode="edge")
                mask = np.pad(np.asarray(alpha, float)[ya:yb, xa:xb], pad, mode="edge")
                trans = np.exp(-beta_rgb * depth)
                plane = image * trans + htile * (1 - trans)
                if radius:
                    pp = np.pad(plane, ((radius, radius), (radius, radius), (0, 0)), mode="edge")
                    blurred = np.zeros_like(plane)
                    for dy in range(2 * radius + 1):
                        for dx in range(2 * radius + 1):
                            blurred += pp[dy : dy + plane.shape[0], dx : dx + plane.shape[1]]
                    plane = blurred / ((2 * radius + 1) ** 2)
                acc = plane * mask[..., None] + acc * (1 - mask[..., None])
            oy, ox = y0 - ya + pad[0][0], x0 - xa + pad[1][0]
            interior = acc[oy : oy + (y1 - y0), ox : ox + (x1 - x0)]
            output[y0:y1, x0:x1] = np.rint(srgb_encode(interior) * 255).astype(np.uint8)
    write_rgb8_png(output_path, output)
    return hashlib.sha256(Path(output_path).read_bytes()).hexdigest()


def verify_tiled_compositor_equivalence(
    layers: Sequence[tuple[FloatImage, FloatImage, float]],
    haze: FloatImage,
    tiled_path: str | Path,
    full_path: str | Path,
    *,
    beta: float,
    focus: tuple[float, float, float, float, int],
    tile_size_px: int = 32,
) -> bool:
    """Persist tiled and monolithic outputs and require exact RGB8 equality."""
    render_25d_multiplane_composite(
        layers, haze, tiled_path, beta=beta, focus=focus, tile_size_px=tile_size_px
    )
    full = _composite(layers, haze, beta, focus)
    write_rgb8_png(full_path, np.rint(srgb_encode(full) * 255).astype(np.uint8))
    return np.array_equal(read_rgb8_png(tiled_path), read_rgb8_png(full_path))
