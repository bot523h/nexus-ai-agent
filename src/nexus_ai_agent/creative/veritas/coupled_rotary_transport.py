"""Explicit achromatic, column-conservative boundary model for rotary diagnostics.

This is a lossy constrained model re-render, not recovery of unknown scene colors
or a uniquely inferred fluid velocity. Every RGB8 column retains the reference
G-channel radiance exactly; 2-D texture within that constraint remains free to
move. The manifest must declare this model whenever it is used.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]


def balance_rotary_radiance(frames: FloatArray, reference: FloatArray) -> FloatArray:
    """Least-change bounded G-column projection plus balanced integer rounding.

    A scalar water-filling offset is solved in each column. Largest-remainder
    rounding preserves its integer target exactly. Achromatic reference-green
    output is deliberate and must not be presented as chart-calibrated color.
    No temporal frame copying, spatial uniformization or contrast floor is used.
    """
    target = np.rint(np.clip(reference[..., 1], 0, 1) * 255).astype(np.int64).sum(axis=0)
    output = np.empty_like(frames)
    for index, frame in enumerate(frames):
        green = np.clip(frame[..., 1] * 255, 0, 255)
        lower, upper = np.full(green.shape[1], -255.0), np.full(green.shape[1], 255.0)
        for _ in range(48):
            midpoint = (lower + upper) / 2
            under = np.clip(green + midpoint[None], 0, 255).sum(axis=0) <= target
            lower = np.where(under, midpoint, lower)
            upper = np.where(under, upper, midpoint)
        adjusted = np.clip(green + lower[None], 0, 255)
        integer = np.floor(adjusted).astype(np.int64)
        for column, total in enumerate(target):
            missing = int(total - integer[:, column].sum())
            order = np.argsort(-(adjusted[:, column] - integer[:, column]), kind="stable")
            integer[order[:missing], column] += 1
        output[index] = integer[..., None] / 255
    return output


def verify_balanced_radiance(frames: FloatArray, reference: FloatArray) -> None:
    """Independent integer conservation, achromaticity and nonflat-texture checks."""
    pixels = np.rint(frames * 255).astype(np.int64)
    targets = np.rint(reference[..., 1] * 255).astype(np.int64).sum(axis=0)
    if (
        not np.array_equal(pixels[..., 0], pixels[..., 1])
        or not np.array_equal(pixels[..., 1], pixels[..., 2])
        or not np.all(pixels[..., 1].sum(axis=1) == targets[None])
    ):
        raise ValueError("coupled radiance/achromatic boundary invariant violated")
    # A textured source may not be silently replaced by a flat output. This is
    # a refusal, not an operator which invents contrast to satisfy an assertion.
    if np.std(reference[..., 1]) >= 0.08 and np.any(np.std(frames[..., 1], axis=(1, 2)) < 0.08):
        raise ValueError("coupled rotary projection lost required texture contrast")
