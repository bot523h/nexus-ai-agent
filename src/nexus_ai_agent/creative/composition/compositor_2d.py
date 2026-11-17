"""Linear-radiance, premultiplied-alpha Porter-Duff compositor (Wave 2).

Implements the twelve Thomas Porter & Tom Duff (1984) operators plus ``PLUS``
(linear-light additive), ``MULTIPLY`` and ``SCREEN`` on float32 arrays in
*scene-linear radiance*, with premultiplied alpha.

The Porter-Duff factors are exact. For source ``S`` and destination ``D`` with
coverage ``alpha_s``, ``alpha_d`` and colours ``C_s``, ``C_d``:

    out_alpha = Fa * alpha_s + Fb * alpha_d
    out_color = Fa * C_s + Fb * C_d          (both already premultiplied)

with the per-operator factor pairs below. ``sRGB`` transfer functions are
applied on input and output so the caller can pass/return display-referred
values while all mixing happens in linear light.
"""

from __future__ import annotations

from enum import Enum

import numpy as np

from .errors import CompositeError

__all__ = [
    "PorterDuff",
    "OPERATOR_FACTORS",
    "srgb_to_linear",
    "linear_to_srgb",
    "composite",
    "check_premultiplied",
    "ALL_OPERATORS",
]


class PorterDuff(str, Enum):
    CLEAR = "clear"
    SRC = "src"
    DST = "dst"
    SRC_OVER = "src_over"
    DST_OVER = "dst_over"
    SRC_IN = "src_in"
    DST_IN = "dst_in"
    SRC_OUT = "src_out"
    DST_OUT = "dst_out"
    SRC_ATOP = "src_atop"
    DST_ATOP = "dst_atop"
    XOR = "xor"
    PLUS = "plus"
    MULTIPLY = "multiply"
    SCREEN = "screen"


#: ``(Fa, Fb)`` for the 12 classic Porter-Duff operators, as a pure function of
#: ``(alpha_s, alpha_d)``. The three extra blend modes use their own kernels.
OPERATOR_FACTORS: dict[PorterDuff, str] = {
    PorterDuff.CLEAR: "0,0",
    PorterDuff.SRC: "1,0",
    PorterDuff.DST: "0,1",
    PorterDuff.SRC_OVER: "1,1-alpha_s",
    PorterDuff.DST_OVER: "1-alpha_d,1",
    PorterDuff.SRC_IN: "alpha_d,0",
    PorterDuff.DST_IN: "0,alpha_s",
    PorterDuff.SRC_OUT: "1-alpha_d,0",
    PorterDuff.DST_OUT: "0,1-alpha_s",
    PorterDuff.SRC_ATOP: "alpha_d,1-alpha_s",
    PorterDuff.DST_ATOP: "1-alpha_d,alpha_s",
    PorterDuff.XOR: "1-alpha_d,1-alpha_s",
    PorterDuff.PLUS: "1,1",
    PorterDuff.MULTIPLY: "1-alpha_d,1-alpha_s",
    PorterDuff.SCREEN: "1,1",
}


def _factor_pair(
    op: PorterDuff, alpha_s: np.ndarray, alpha_d: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Return the exact ``(Fa, Fb)`` factor arrays for an operator (no eval)."""
    one = np.float32(1.0)
    a_s = alpha_s.astype(np.float32)
    a_d = alpha_d.astype(np.float32)
    table: dict[PorterDuff, tuple[np.ndarray, np.ndarray]] = {
        PorterDuff.CLEAR: (np.zeros_like(a_s), np.zeros_like(a_s)),
        PorterDuff.SRC: (np.ones_like(a_s), np.zeros_like(a_s)),
        PorterDuff.DST: (np.zeros_like(a_s), np.ones_like(a_s)),
        PorterDuff.SRC_OVER: (np.ones_like(a_s), one - a_s),
        PorterDuff.DST_OVER: (one - a_d, np.ones_like(a_s)),
        PorterDuff.SRC_IN: (a_d, np.zeros_like(a_s)),
        PorterDuff.DST_IN: (np.zeros_like(a_s), a_s),
        PorterDuff.SRC_OUT: (one - a_d, np.zeros_like(a_s)),
        PorterDuff.DST_OUT: (np.zeros_like(a_s), one - a_s),
        PorterDuff.SRC_ATOP: (a_d, one - a_s),
        PorterDuff.DST_ATOP: (one - a_d, a_s),
        PorterDuff.XOR: (one - a_d, one - a_s),
        PorterDuff.PLUS: (np.ones_like(a_s), np.ones_like(a_s)),
        PorterDuff.MULTIPLY: (one - a_d, one - a_s),
        PorterDuff.SCREEN: (np.ones_like(a_s), np.ones_like(a_s)),
    }
    return table[op]


ALL_OPERATORS: tuple[PorterDuff, ...] = tuple(PorterDuff)


def srgb_to_linear(x: np.ndarray) -> np.ndarray:
    """The exact IEC 61966-2-1 sRGB EOTF (display-referred -> linear)."""
    a = np.asarray(x, dtype=np.float32)
    low = a / 12.92
    high = ((a + 0.055) / 1.055) ** 2.4
    return np.where(a <= 0.04045, low, high).astype(np.float32)


def linear_to_srgb(x: np.ndarray) -> np.ndarray:
    """The exact IEC 61966-2-1 inverse EOTF (linear -> display-referred)."""
    a = np.asarray(x, dtype=np.float32)
    low = a * 12.92
    high = 1.055 * np.maximum(a, 0.0) ** (1.0 / 2.4) - 0.055
    return np.where(a <= 0.0031308, low, high).astype(np.float32)


def check_premultiplied(rgba: np.ndarray) -> None:
    """Refuse an array that is not valid premultiplied alpha.

    Requires shape ``(..., 4)``, finite values, ``0 <= alpha <= 1`` and
    ``0 <= C <= alpha`` on every colour channel.
    """
    arr = np.asarray(rgba, dtype=np.float32)
    if arr.ndim < 1 or arr.shape[-1] != 4:
        raise CompositeError(f"expected trailing RGBA axis of size 4, got {arr.shape}")
    if not np.all(np.isfinite(arr)):
        raise CompositeError("RGBA buffer contains NaN/Inf")
    alpha = arr[..., 3]
    if np.any(alpha < 0.0) or np.any(alpha > 1.0):
        raise CompositeError("alpha must be within [0, 1]")
    colour = arr[..., :3]
    if np.any(colour < -1e-6) or np.any(colour > alpha[..., None] + 1e-6):
        raise CompositeError("colour must satisfy 0 <= C <= alpha for premultiplied alpha")


def composite(op: PorterDuff, src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Composite ``src`` over ``dst`` with a Porter-Duff operator.

    Both inputs are premultiplied-alpha linear-light ``(..., 4)`` float arrays.
    ``PLUS``/``MULTIPLY``/``SCREEN`` follow their standard linear-light blend
    definitions. The returned buffer is a new premultiplied array.
    """
    s = np.asarray(src, dtype=np.float32)
    d = np.asarray(dst, dtype=np.float32)
    if s.shape != d.shape:
        raise CompositeError(f"src {s.shape} and dst {d.shape} must match")
    check_premultiplied(s)
    check_premultiplied(d)
    alpha_s = s[..., 3]
    alpha_d = d[..., 3]
    fa, fb = _factor_pair(op, alpha_s, alpha_d)
    fa_c = fa[..., None]
    fb_c = fb[..., None]
    if op is PorterDuff.PLUS:
        plus: np.ndarray = s + d
        plus[..., 3] = np.clip(alpha_s + alpha_d, 0.0, 1.0)
        return plus.astype(np.float32)
    if op is PorterDuff.MULTIPLY:
        mul: np.ndarray = np.empty_like(s)
        mul[..., :3] = s[..., :3] * d[..., :3]
        mul[..., 3] = alpha_s * alpha_d
        return mul.astype(np.float32)
    if op is PorterDuff.SCREEN:
        scr: np.ndarray = np.empty_like(s)
        scr[..., :3] = s[..., :3] + d[..., :3] - s[..., :3] * d[..., :3]
        scr[..., 3] = alpha_s + alpha_d - alpha_s * alpha_d
        return scr.astype(np.float32)
    result: np.ndarray = fa_c * s + fb_c * d
    return result.astype(np.float32)
