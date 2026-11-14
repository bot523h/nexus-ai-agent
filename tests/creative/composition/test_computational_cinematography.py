"""Tests for Wave 10 — optical-flow computational cinematography.

Real computation only: analytic ground-truth flows, closed-form interpolation,
and adversarial/refusal inputs. No mocks.
"""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.composition import computational_cinematography as cc


def _smooth(n: int = 64) -> np.ndarray:
    ys, xs = np.mgrid[0:n, 0:n].astype(np.float64)
    return (
        0.5 + 0.3 * np.sin(0.3 * xs) + 0.2 * np.cos(0.4 * ys) + 0.1 * np.sin(0.2 * xs + 0.25 * ys)
    )


def _rgb(n: int = 64) -> np.ndarray:
    g = _smooth(n)
    return np.stack([g, 0.5 * g, 0.25 * g], axis=-1)


def _shift(img: np.ndarray, dx: float, dy: float) -> np.ndarray:
    h, w = img.shape[:2]
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float64)
    return cc._bilinear_sample(img, xs - dx, ys - dy)


def _constant_flow(n: int, dx: float, dy: float) -> np.ndarray:
    return np.dstack([np.full((n, n), dx), np.full((n, n), dy)])


# --- to_gray ---------------------------------------------------------------------


def test_to_gray_rec709_weights() -> None:
    img = np.zeros((2, 2, 3))
    img[..., 0] = 1.0
    assert cc.to_gray(img) == pytest.approx(np.ones((2, 2)) * 0.2126)


def test_to_gray_passthrough_2d() -> None:
    a = np.arange(9.0).reshape(3, 3)
    assert np.array_equal(cc.to_gray(a), a)


def test_to_gray_single_channel() -> None:
    a = np.arange(4.0).reshape(2, 2, 1)
    assert np.array_equal(cc.to_gray(a), a[..., 0])


def test_to_gray_rejects_nan() -> None:
    img = np.zeros((2, 2, 3))
    img[0, 0, 0] = np.nan
    with pytest.raises(cc.FlowError):
        cc.to_gray(img)


def test_to_gray_rejects_bad_channels() -> None:
    with pytest.raises(cc.FlowError):
        cc.to_gray(np.zeros((2, 2, 4)))


def test_to_gray_rejects_over_budget() -> None:
    with pytest.raises(cc.FlowError):
        cc.to_gray(np.zeros((2049, 2049)))


# --- warp_by_flow ----------------------------------------------------------------


def test_warp_identity_is_exact() -> None:
    img = _rgb()
    out = cc.warp_by_flow(img, np.zeros((64, 64, 2)))
    assert np.allclose(out, img, atol=1e-12)


def test_warp_by_integer_shift_matches_reference() -> None:
    img = _rgb()
    flow = _constant_flow(64, 2.0, 0.0)
    out = cc.warp_by_flow(img, flow)
    # out(p) = img(p + (2,0)) -> interior equals img shifted left by 2
    assert np.allclose(out[:, :60], img[:, 2:62], atol=1e-9)


def test_warp_aligns_shifted_frame_onto_reference() -> None:
    # a(p) ~ b(p + flow) with flow = (2, 0): warping b by the flow recovers a.
    a = _rgb(64)
    b = _shift(a, 2.0, 0.0)
    flow = cc.lucas_kanade_flow(a, b, levels=3, iters=3)
    aligned = cc.warp_by_flow(b, flow)
    mask = np.zeros((64, 64), dtype=bool)
    mask[8:56, 8:56] = True
    assert float(np.mean(np.abs(aligned - a)[mask])) < 0.02


def test_warp_rejects_shape_mismatch() -> None:
    with pytest.raises(cc.FlowError):
        cc.warp_by_flow(_rgb(64), np.zeros((32, 32, 2)))


def test_warp_rejects_nan_flow() -> None:
    flow = np.zeros((64, 64, 2))
    flow[0, 0, 0] = np.nan
    with pytest.raises(cc.FlowError):
        cc.warp_by_flow(_rgb(64), flow)


# --- lucas_kanade_flow -----------------------------------------------------------


def test_lucas_kanade_recovers_translation() -> None:
    a = _smooth(96)
    b = _shift(a, 1.0, 1.0)
    flow = cc.lucas_kanade_flow(a, b, levels=3, iters=8)
    interior = flow[16:80, 16:80]
    assert float(np.median(interior[..., 0])) == pytest.approx(1.0, abs=0.2)
    assert float(np.median(interior[..., 1])) == pytest.approx(1.0, abs=0.2)


def test_lucas_kanade_zero_motion() -> None:
    a = _smooth(48)
    flow = cc.lucas_kanade_flow(a, a)
    assert np.allclose(flow, 0.0, atol=1e-6)


def test_lucas_kanade_single_level_recovers_translation() -> None:
    # A 2-D texture avoids the aperture problem of a rank-1 (planar) gradient.
    ys, xs = np.mgrid[0:64, 0:64].astype(np.float64)
    tex = 0.5 + 0.25 * np.sin(0.5 * xs) + 0.25 * np.cos(0.6 * ys)
    b = cc._bilinear_sample(tex, xs - 1.0, ys - 1.0)
    flow = cc.lucas_kanade_flow(tex, b, levels=1, iters=3)
    interior = flow[8:56, 8:56]
    assert float(np.median(interior[..., 0])) == pytest.approx(1.0, abs=0.05)
    assert float(np.median(interior[..., 1])) == pytest.approx(1.0, abs=0.05)


def test_lucas_kanade_warp_reduces_residual() -> None:
    a = _rgb(96)
    b = _shift(a, 2.0, -1.0)
    flow = cc.lucas_kanade_flow(a, b, levels=3, iters=3)
    aligned = cc.warp_by_flow(b, flow)
    mask = np.zeros((96, 96), dtype=bool)
    mask[16:80, 16:80] = True
    raw = float(np.mean(np.abs(a - b)[mask]))
    warped = float(np.mean(np.abs(aligned - a)[mask]))
    assert warped < raw


def test_lucas_kanade_rejects_bad_window() -> None:
    a = _smooth(32)
    with pytest.raises(cc.FlowError):
        cc.lucas_kanade_flow(a, a, window=4)
    with pytest.raises(cc.FlowError):
        cc.lucas_kanade_flow(a, a, window=1)


def test_lucas_kanade_rejects_bad_iters() -> None:
    a = _smooth(32)
    with pytest.raises(cc.FlowError):
        cc.lucas_kanade_flow(a, a, iters=0)


def test_lucas_kanade_rejects_bad_levels() -> None:
    a = _smooth(32)
    with pytest.raises(cc.FlowError):
        cc.lucas_kanade_flow(a, a, levels=0)


def test_lucas_kanade_rejects_shape_mismatch() -> None:
    with pytest.raises(cc.FlowError):
        cc.lucas_kanade_flow(_smooth(32), _smooth(48))


def test_lucas_kanade_flat_region_is_zero_motion() -> None:
    flat = np.full((48, 48), 0.4)
    flow = cc.lucas_kanade_flow(flat, flat)
    assert np.allclose(flow, 0.0, atol=1e-9)


# --- horn_schunck_flow -----------------------------------------------------------


def test_horn_schunck_zero_motion() -> None:
    a = _smooth(48)
    flow = cc.horn_schunck_flow(a, a, iters=50)
    assert np.allclose(flow, 0.0, atol=1e-6)


def test_horn_schunck_recovers_translation_sign() -> None:
    a = _smooth(64)
    b = _shift(a, 1.0, 0.0)
    flow = cc.horn_schunck_flow(a, b, alpha=0.5, iters=1500)
    interior = flow[16:48, 16:48]
    assert float(np.median(interior[..., 0])) > 0.0


def test_horn_schunck_rejects_nonpositive_alpha() -> None:
    a = _smooth(32)
    with pytest.raises(cc.FlowError):
        cc.horn_schunck_flow(a, a, alpha=0.0)


def test_horn_schunck_rejects_bad_iters() -> None:
    a = _smooth(32)
    with pytest.raises(cc.FlowError):
        cc.horn_schunck_flow(a, a, iters=0)


# --- forward_backward_consistency ------------------------------------------------


def test_fbc_perfect_constant_flow_is_fully_consistent() -> None:
    n = 32
    fa = _constant_flow(n, 2.0, 0.0)
    fb = _constant_flow(n, -2.0, 0.0)
    assert cc.forward_backward_consistency(fa, fb, threshold=0.5).all()


def test_fbc_disagreeing_flow_is_inconsistent() -> None:
    n = 32
    fa = _constant_flow(n, 2.0, 0.0)
    assert not cc.forward_backward_consistency(fa, fa, threshold=0.5).any()


def test_fbc_marks_occlusion_hole() -> None:
    n = 32
    fa = _constant_flow(n, 2.0, 0.0)
    fb = _constant_flow(n, -2.0, 0.0)
    fb[10:20, 10:20] = 0.0
    mask = cc.forward_backward_consistency(fa, fb, threshold=0.5)
    assert not bool(mask[15, 15])
    assert bool(mask[0, 0])


def test_fbc_rejects_bad_threshold() -> None:
    fa = _constant_flow(16, 1.0, 0.0)
    with pytest.raises(cc.FlowError):
        cc.forward_backward_consistency(fa, fa, threshold=0.0)


def test_fbc_rejects_shape_mismatch() -> None:
    with pytest.raises(cc.FlowError):
        cc.forward_backward_consistency(_constant_flow(16, 1.0, 0.0), np.zeros((16, 16)))


# --- flow_endpoint_error ---------------------------------------------------------


def test_epe_identity_is_zero() -> None:
    flow = _constant_flow(16, 1.5, -0.5)
    assert cc.flow_endpoint_error(flow, flow) == 0.0


def test_epe_known_value() -> None:
    a = np.zeros((2, 2, 2))
    b = np.zeros((2, 2, 2))
    b[..., 0] = 3.0
    b[..., 1] = 4.0
    assert cc.flow_endpoint_error(a, b) == pytest.approx(5.0)


def test_epe_rejects_shape_mismatch() -> None:
    with pytest.raises(cc.FlowError):
        cc.flow_endpoint_error(_constant_flow(16, 1.0, 0.0), _constant_flow(8, 1.0, 0.0))


# --- interpolate_frame -----------------------------------------------------------


def test_interpolate_endpoints_are_exact() -> None:
    a = _rgb(48)
    b = _shift(a, 2.0, 0.0)
    assert np.array_equal(cc.interpolate_frame(a, b, 0.0), a.astype(np.float32))
    assert np.array_equal(cc.interpolate_frame(a, b, 1.0), b.astype(np.float32))


def test_interpolate_midpoint_matches_ground_truth() -> None:
    a = _rgb(64)
    b = _shift(a, 2.0, 0.0)
    for t in (0.25, 0.5, 0.75):
        interp = cc.interpolate_frame(a, b, t)
        ideal = _shift(a, 2.0 * t, 0.0)
        assert float(np.mean(np.abs(interp - ideal))) < 0.05


def test_interpolate_rejects_out_of_range_t() -> None:
    a = _rgb(32)
    with pytest.raises(cc.FlowError):
        cc.interpolate_frame(a, a, 1.5)
    with pytest.raises(cc.FlowError):
        cc.interpolate_frame(a, a, -0.1)


def test_interpolate_rejects_shape_mismatch() -> None:
    with pytest.raises(cc.FlowError):
        cc.interpolate_frame(_rgb(32), _rgb(48), 0.5)


def test_interpolate_output_is_bounded() -> None:
    a = _rgb(48)
    b = _shift(a, 1.0, 1.0)
    out = cc.interpolate_frame(a, b, 0.5)
    assert float(out.min()) >= 0.0
    assert float(out.max()) <= 1.0
    assert np.isfinite(out).all()


def test_blur_accepts_2d_and_3d() -> None:
    g2 = _smooth(32)
    g3 = np.stack([g2, g2, g2], axis=-1)
    b2 = cc._blur(g2, 1.2)
    b3 = cc._blur(g3, 1.2)
    assert b2.shape == g2.shape
    assert b3.shape == g3.shape
    assert np.allclose(b2, b3[..., 0], atol=1e-9)


def test_horn_schunck_rejects_shape_mismatch() -> None:
    with pytest.raises(cc.FlowError):
        cc.horn_schunck_flow(_smooth(32), _smooth(48))
