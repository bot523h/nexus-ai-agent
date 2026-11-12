"""Tests for the linear-radiance Porter-Duff compositor."""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.composition import (
    ALL_OPERATORS,
    CompositeError,
    PorterDuff,
    check_premultiplied,
    composite,
    linear_to_srgb,
    srgb_to_linear,
)


def _src() -> np.ndarray:
    # premultiplied red, alpha 0.5
    return np.array([[0.5, 0.0, 0.0, 0.5]], dtype=np.float32)


def _dst() -> np.ndarray:
    # premultiplied green, alpha 1.0
    return np.array([[0.0, 1.0, 0.0, 1.0]], dtype=np.float32)


def test_all_fifteen_operators_present() -> None:
    assert len(ALL_OPERATORS) == 15
    for op in ALL_OPERATORS:
        out = composite(op, _src(), _dst())
        assert out.shape == (1, 4)
        assert np.all(np.isfinite(out))


def test_clear_operator_zeroes() -> None:
    out = composite(PorterDuff.CLEAR, _src(), _dst())
    assert np.allclose(out, 0.0)


def test_src_operator_returns_source() -> None:
    out = composite(PorterDuff.SRC, _src(), _dst())
    assert np.allclose(out, _src())


def test_dst_operator_returns_dest() -> None:
    out = composite(PorterDuff.DST, _src(), _dst())
    assert np.allclose(out, _dst())


def test_src_over_analytic() -> None:
    # out = S + (1-a_s) D
    s, d = _src(), _dst()
    out = composite(PorterDuff.SRC_OVER, s, d)
    expected = s + (1 - s[..., 3:]) * d
    assert np.allclose(out, expected, atol=1e-6)


def test_dst_over_analytic() -> None:
    s, d = _src(), _dst()
    out = composite(PorterDuff.DST_OVER, s, d)
    expected = d + (1 - d[..., 3:]) * s
    assert np.allclose(out, expected, atol=1e-6)


def test_src_in_and_dst_in() -> None:
    s, d = _src(), _dst()
    assert np.allclose(composite(PorterDuff.SRC_IN, s, d), s * d[..., 3:], atol=1e-6)
    assert np.allclose(composite(PorterDuff.DST_IN, s, d), d * s[..., 3:], atol=1e-6)


def test_src_out_and_dst_out() -> None:
    s, d = _src(), _dst()
    assert np.allclose(composite(PorterDuff.SRC_OUT, s, d), s * (1 - d[..., 3:]), atol=1e-6)
    assert np.allclose(composite(PorterDuff.DST_OUT, s, d), d * (1 - s[..., 3:]), atol=1e-6)


def test_atop_operators() -> None:
    s, d = _src(), _dst()
    src_atop = composite(PorterDuff.SRC_ATOP, s, d)
    dst_atop = composite(PorterDuff.DST_ATOP, s, d)
    assert np.all(np.isfinite(src_atop)) and np.all(np.isfinite(dst_atop))


def test_xor_analytic() -> None:
    s, d = _src(), _dst()
    out = composite(PorterDuff.XOR, s, d)
    expected = s * (1 - d[..., 3:]) + d * (1 - s[..., 3:])
    assert np.allclose(out, expected, atol=1e-6)


def test_plus_additive() -> None:
    s, d = _src(), _dst()
    out = composite(PorterDuff.PLUS, s, d)
    assert out[..., 0][0] == pytest.approx(0.5)  # red from src
    assert out[..., 1][0] == pytest.approx(1.0)  # green from dst


def test_multiply_and_screen() -> None:
    s, d = _src(), _dst()
    mul = composite(PorterDuff.MULTIPLY, s, d)
    scr = composite(PorterDuff.SCREEN, s, d)
    assert mul[..., 3][0] == pytest.approx(0.5)
    assert scr[..., 3][0] == pytest.approx(1.0)


def test_shape_mismatch_refused() -> None:
    a = np.zeros((1, 4), dtype=np.float32)
    b = np.zeros((2, 4), dtype=np.float32)
    with pytest.raises(CompositeError):
        composite(PorterDuff.SRC_OVER, a, b)


def test_bad_shape_refused() -> None:
    with pytest.raises(CompositeError):
        check_premultiplied(np.zeros((3,), dtype=np.float32))
    with pytest.raises(CompositeError):
        check_premultiplied(np.zeros((2, 3), dtype=np.float32))


def test_nan_refused() -> None:
    arr = np.array([[np.nan, 0, 0, 1]], dtype=np.float32)
    with pytest.raises(CompositeError):
        check_premultiplied(arr)


def test_alpha_out_of_range_refused() -> None:
    with pytest.raises(CompositeError):
        check_premultiplied(np.array([[0.0, 0, 0, 1.5]], dtype=np.float32))


def test_colour_exceeds_alpha_refused() -> None:
    # C > alpha is invalid for premultiplied alpha
    with pytest.raises(CompositeError):
        check_premultiplied(np.array([[0.9, 0, 0, 0.5]], dtype=np.float32))


def test_srgb_round_trip() -> None:
    x = np.array([0.0, 0.04045, 0.5, 1.0], dtype=np.float32)
    back = linear_to_srgb(srgb_to_linear(x))
    assert np.allclose(back, x, atol=1e-5)


def test_srgb_to_linear_monotonic() -> None:
    lin = srgb_to_linear(np.array([0.0, 0.5, 1.0], dtype=np.float32))
    assert lin[0] == 0.0
    assert lin[1] < lin[2]
