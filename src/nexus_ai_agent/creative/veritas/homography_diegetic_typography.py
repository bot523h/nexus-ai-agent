"""Law 6 — projective SL(3) homography-locked diegetic typography.

AI video generators melt street signs, license plates and book pages into
unreadable hieroglyphs. This module solves the normalised Direct Linear
Transform homography that locks canonical sign coordinates to the screen,
renders deterministic procedural glyphs, and warps them through the inverse
homography with Jacobian-scaled elliptical filtering so spelling never mutates
or shimmers across frames.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]

_FONT_ROWS: dict[str, str] = {
    "A": "01110/10001/10001/11111/10001/10001/10001",
    "B": "11110/10001/10001/11110/10001/10001/11110",
    "C": "01111/10000/10000/10000/10000/10000/01111",
    "D": "11110/10001/10001/10001/10001/10001/11110",
    "E": "11111/10000/10000/11110/10000/10000/11111",
    "F": "11111/10000/10000/11110/10000/10000/10000",
    "G": "01111/10000/10000/10111/10001/10001/01111",
    "H": "10001/10001/10001/11111/10001/10001/10001",
    "I": "11111/00100/00100/00100/00100/00100/11111",
    "J": "00111/00010/00010/00010/00010/10010/01100",
    "K": "10001/10010/10100/11000/10100/10010/10001",
    "L": "10000/10000/10000/10000/10000/10000/11111",
    "M": "10001/11011/10101/10101/10001/10001/10001",
    "N": "10001/11001/10101/10011/10001/10001/10001",
    "O": "01110/10001/10001/10001/10001/10001/01110",
    "P": "11110/10001/10001/11110/10000/10000/10000",
    "Q": "01110/10001/10001/10001/10101/10010/01101",
    "R": "11110/10001/10001/11110/10100/10010/10001",
    "S": "01111/10000/10000/01110/00001/00001/11110",
    "T": "11111/00100/00100/00100/00100/00100/00100",
    "U": "10001/10001/10001/10001/10001/10001/01110",
    "V": "10001/10001/10001/10001/10001/01010/00100",
    "W": "10001/10001/10001/10101/10101/11011/10001",
    "X": "10001/10001/01010/00100/01010/10001/10001",
    "Y": "10001/10001/01010/00100/00100/00100/00100",
    "Z": "11111/00001/00010/00100/01000/10000/11111",
    "0": "01110/10001/10011/10101/11001/10001/01110",
    "1": "00100/01100/00100/00100/00100/00100/01110",
    "2": "01110/10001/00001/00010/00100/01000/11111",
    "3": "11111/00010/00100/00010/00001/10001/01110",
    "4": "00010/00110/01010/10010/11111/00010/00010",
    "5": "11111/10000/11110/00001/00001/10001/01110",
    "6": "00110/01000/10000/11110/10001/10001/01110",
    "7": "11111/00001/00010/00100/01000/01000/01000",
    "8": "01110/10001/10001/01110/10001/10001/01110",
    "9": "01110/10001/10001/01111/00001/00010/01100",
    " ": "00000/00000/00000/00000/00000/00000/00000",
    "-": "00000/00000/00000/11111/00000/00000/00000",
    ".": "00000/00000/00000/00000/00000/01100/01100",
    ":": "00000/01100/01100/00000/01100/01100/00000",
    "/": "00001/00010/00010/00100/01000/01000/10000",
}

FONT: dict[str, FloatArray] = {
    character: np.array(
        [[float(bit) for bit in row] for row in pattern.split("/")], dtype=np.float64
    )
    for character, pattern in _FONT_ROWS.items()
}
GLYPH_HEIGHT = 7
GLYPH_WIDTH = 5
GLYPH_SPACING = 1
TEXTURE_SHAPE = (32, 160)


class TypographyError(ValueError):
    """Raised for unsupported glyphs or invalid typography inputs."""


class DegenerateHomographyError(ValueError):
    """Raised when a planar quad cannot define a homography."""


@dataclass(frozen=True)
class DiegeticTextRender:
    """A homography-locked diegetic text render."""

    image: FloatArray
    mask: FloatArray
    rendered_text: str
    homography: FloatArray
    jacobian_frobenius: FloatArray
    mean_footprint: float


def procedural_glyph_bitmap(text: str) -> FloatArray:
    """Render text into a deterministic 5x7 procedural glyph bitmap."""
    if not isinstance(text, str):
        raise TypographyError("text must be a string")
    unsupported = sorted({character for character in text if character not in FONT})
    if unsupported:
        raise TypographyError(f"unsupported glyphs: {unsupported}")
    if not text:
        return np.zeros((GLYPH_HEIGHT, 1), dtype=np.float64)
    columns = len(text) * (GLYPH_WIDTH + GLYPH_SPACING) - GLYPH_SPACING
    bitmap = np.zeros((GLYPH_HEIGHT, columns), dtype=np.float64)
    for index, character in enumerate(text):
        start = index * (GLYPH_WIDTH + GLYPH_SPACING)
        bitmap[:, start : start + GLYPH_WIDTH] = FONT[character]
    return bitmap


def _normalise_points(points: FloatArray) -> tuple[FloatArray, FloatArray]:
    centroid = points.mean(axis=0)
    shifted = points - centroid
    scale = float(np.sqrt(np.mean(np.sum(shifted**2, axis=1))))
    if scale <= 1e-12:
        raise DegenerateHomographyError("quad is degenerate (coincident points)")
    transform = np.array(
        [
            [1.0 / scale, 0.0, -centroid[0] / scale],
            [0.0, 1.0 / scale, -centroid[1] / scale],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    return shifted / scale, transform


def _build_dlt(src: FloatArray, dst: FloatArray) -> FloatArray:
    rows: list[FloatArray] = []
    for (u, v), (x, y) in zip(src, dst, strict=True):
        rows.append(np.array([-u, -v, -1.0, 0.0, 0.0, 0.0, x * u, x * v, x], dtype=np.float64))
        rows.append(np.array([0.0, 0.0, 0.0, -u, -v, -1.0, y * u, y * v, y], dtype=np.float64))
    matrix = np.vstack(rows)
    _, _, vt = np.linalg.svd(matrix)
    return np.asarray(vt[-1].reshape(3, 3), dtype=np.float64)


def _to_sl3(homography: FloatArray) -> FloatArray:
    determinant = float(np.linalg.det(homography))
    if abs(determinant) <= 1e-12:
        raise DegenerateHomographyError("homography is singular")
    scale = np.sign(determinant) * abs(determinant) ** (-1.0 / 3.0)
    return np.asarray(homography * scale, dtype=np.float64)


def compute_planar_quad_homography(src_quad: FloatArray, dst_quad: FloatArray) -> FloatArray:
    """Solve the normalised DLT homography mapping one planar quad onto another."""
    src = np.asarray(src_quad, dtype=np.float64)
    dst = np.asarray(dst_quad, dtype=np.float64)
    if src.shape != (4, 2) or dst.shape != (4, 2):
        raise DegenerateHomographyError("quads must have shape (4, 2)")
    if not (np.all(np.isfinite(src)) and np.all(np.isfinite(dst))):
        raise DegenerateHomographyError("quad coordinates must be finite")
    src_norm, src_transform = _normalise_points(src)
    dst_norm, dst_transform = _normalise_points(dst)
    homography = _build_dlt(src_norm, dst_norm)
    denormalise = np.linalg.inv(dst_transform) @ homography @ src_transform
    return _to_sl3(denormalise)


def _texture_for_text(text: str, texture_shape: tuple[int, int]) -> FloatArray:
    rows, cols = texture_shape
    if rows < GLYPH_HEIGHT + 2 or cols < GLYPH_WIDTH + 2:
        raise TypographyError("texture must be larger than a single glyph")
    bitmap = procedural_glyph_bitmap(text)
    if bitmap.shape[1] > cols - 2:
        raise TypographyError("text does not fit the texture width")
    texture = np.zeros((rows, cols), dtype=np.float64)
    top = (rows - GLYPH_HEIGHT) // 2
    left = (cols - bitmap.shape[1]) // 2
    texture[top : top + GLYPH_HEIGHT, left : left + bitmap.shape[1]] = bitmap
    return texture


def _gaussian_blur(image: FloatArray, sigma_px: float) -> FloatArray:
    if sigma_px <= 1e-6:
        return image
    rows, cols = image.shape
    freq_r = np.fft.fftfreq(rows)[:, None]
    freq_c = np.fft.rfftfreq(cols)[None, :]
    transfer = np.exp(-2.0 * np.pi**2 * sigma_px**2 * (freq_r**2 + freq_c**2))
    return np.fft.irfft2(np.fft.rfft2(image) * transfer, s=image.shape)


def _sample_bilinear(image: FloatArray, u: FloatArray, v: FloatArray) -> FloatArray:
    rows, cols = image.shape
    x = np.clip(u * (cols - 1), 0.0, cols - 1)
    y = np.clip(v * (rows - 1), 0.0, rows - 1)
    x0 = np.floor(x).astype(np.intp)
    y0 = np.floor(y).astype(np.intp)
    x1 = np.minimum(x0 + 1, cols - 1)
    y1 = np.minimum(y0 + 1, rows - 1)
    wx = x - x0
    wy = y - y0
    top = image[y0, x0] * (1.0 - wx) + image[y0, x1] * wx
    bottom = image[y1, x0] * (1.0 - wx) + image[y1, x1] * wx
    return np.asarray(top * (1.0 - wy) + bottom * wy, dtype=np.float64)


def render_homography_locked_diegetic_text(
    text: str,
    dst_quad: FloatArray,
    canvas_shape: tuple[int, int],
    *,
    src_quad: FloatArray | None = None,
    illumination: float | FloatArray = 1.0,
    texture_shape: tuple[int, int] = TEXTURE_SHAPE,
) -> DiegeticTextRender:
    """Warp procedural glyphs through the inverse homography with EWA filtering."""
    if len(canvas_shape) != 2 or canvas_shape[0] <= 0 or canvas_shape[1] <= 0:
        raise TypographyError("canvas shape must be two positive dimensions")
    destination = np.asarray(dst_quad, dtype=np.float64)
    source = (
        np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]], dtype=np.float64)
        if src_quad is None
        else np.asarray(src_quad, dtype=np.float64)
    )
    homography = compute_planar_quad_homography(source, destination)
    inverse = np.linalg.inv(homography)
    texture = _texture_for_text(text, texture_shape)

    height, width = canvas_shape
    grid_y, grid_x = np.meshgrid(
        np.arange(height, dtype=np.float64),
        np.arange(width, dtype=np.float64),
        indexing="ij",
    )
    homogeneous = np.stack([grid_x, grid_y, np.ones_like(grid_x)], axis=-1)
    mapped = homogeneous @ inverse.T
    w = mapped[:, :, 2]
    if np.any(np.abs(w) <= 1e-12):
        raise TypographyError("inverse homography maps canvas to infinity")
    u = mapped[:, :, 0] / w
    v = mapped[:, :, 1] / w

    du_dy, du_dx = np.gradient(u)
    dv_dy, dv_dx = np.gradient(v)
    jacobian = np.sqrt(du_dx**2 + du_dy**2 + dv_dx**2 + dv_dy**2)
    mean_footprint = float(np.mean(jacobian))
    filtered = _gaussian_blur(texture, 0.5 * mean_footprint * max(texture.shape))

    inside = (u >= 0.0) & (u <= 1.0) & (v >= 0.0) & (v <= 1.0)
    mask = np.where(inside, _sample_bilinear(filtered, u, v), 0.0)
    gain = np.asarray(illumination, dtype=np.float64)
    if gain.ndim == 0:
        gain = np.full((height, width), float(gain), dtype=np.float64)
    if gain.shape != (height, width):
        raise TypographyError("illumination must be scalar or match the canvas")
    if not np.all(np.isfinite(gain)) or np.any(gain < 0.0):
        raise TypographyError("illumination must be finite and non-negative")
    return DiegeticTextRender(
        image=mask * gain,
        mask=mask,
        rendered_text=text,
        homography=homography,
        jacobian_frobenius=jacobian,
        mean_footprint=mean_footprint,
    )
