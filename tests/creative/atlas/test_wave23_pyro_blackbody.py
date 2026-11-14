"""WAVE 23 — tests for buoyant pyro CFD and blackbody radiation."""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.atlas import buoyant_pyro_blackbody as p

# --- blackbody physics (closed form, checked against analytic limits) -------------


def test_planck_peak_matches_wien() -> None:
    # The argmax of the Planck curve must equal Wien's b/T.
    t = 5778.0
    lam = np.linspace(50e-9, 4000e-9, 400_000)
    b = p.planck_spectral_radiance(lam, t)
    peak_nm = lam[int(np.argmax(b))] * 1e9
    assert peak_nm == pytest.approx(p.wien_peak_wavelength_nm(t), rel=1e-3)


def test_planck_integrates_to_stefan_boltzmann() -> None:
    # Integral of B_lambda over all wavelengths = sigma T^4 / pi.
    t = 6000.0
    lam = np.linspace(1e-9, 20000e-9, 2_000_000)
    integral = np.trapezoid(p.planck_spectral_radiance(lam, t), lam)
    assert integral == pytest.approx(p.STEFAN_SIGMA * t**4 / np.pi, rel=2e-3)


def test_stefan_boltzmann_scaling() -> None:
    assert p.stefan_boltzmann_flux(2.0) == pytest.approx(16.0 * p.stefan_boltzmann_flux(1.0))


def test_stefan_boltzmann_zero_temperature() -> None:
    assert p.stefan_boltzmann_flux(0.0) == 0.0


def test_wien_sun_temperature() -> None:
    # The Sun's ~5778 K peak is ~500 nm (green).
    assert p.wien_peak_wavelength_nm(5778.0) == pytest.approx(501.5, abs=1.0)


def test_blackbody_rgb_hot_is_blue_cool_is_red() -> None:
    cool = p.blackbody_rgb(2000.0)
    hot = p.blackbody_rgb(20000.0)
    assert cool[0] > cool[2]  # red-dominant
    assert hot[2] > hot[0]  # blue-dominant
    assert np.all(cool <= 1.0) and np.all(hot <= 1.0)


# --- advection --------------------------------------------------------------------


def test_semi_lagrangian_advects_one_cell() -> None:
    q = np.zeros((8, 8))
    q[4, 2] = 1.0
    v = np.zeros((2, 8, 8))
    v[0, :, :] = 1.0  # unit velocity in +x
    out = p.advect_semi_lagrangian(q, v, dt=1.0)
    assert out[4, 3] == pytest.approx(1.0)


def test_semi_lagrangian_stable_at_huge_dt() -> None:
    q = np.zeros((16, 16))
    q[8, 8] = 1.0
    v = np.zeros((2, 16, 16))
    v[0, :, :] = 5.0
    out = p.advect_semi_lagrangian(q, v, dt=1000.0)  # would explode an explicit scheme
    assert np.all(np.isfinite(out))
    assert out.max() <= 1.0 + 1e-9


def test_semi_lagrangian_zero_dt_is_identity() -> None:
    q = np.arange(64.0).reshape(8, 8)
    v = np.zeros((2, 8, 8))
    out = p.advect_semi_lagrangian(q, v, dt=0.0)
    np.testing.assert_allclose(out, q)


# --- projection -------------------------------------------------------------------


def test_projection_reduces_divergence() -> None:
    rng = np.random.default_rng(0)
    v = rng.normal(0.0, 0.5, (2, 32, 32))
    d0 = float(np.abs(p._divergence(v)).max())
    out, residual, used = p.project_divergence_free(v, iters=100)
    assert residual < d0  # strictly reduces the divergence
    assert used >= 1


def test_projection_converges_with_more_iters() -> None:
    rng = np.random.default_rng(1)
    v = rng.normal(0.0, 0.5, (2, 24, 24))
    _, r20, _ = p.project_divergence_free(v, iters=20)
    _, r200, _ = p.project_divergence_free(v, iters=200)
    assert r200 < r20


def test_projection_early_stop() -> None:
    rng = np.random.default_rng(2)
    v = rng.normal(0.0, 0.5, (2, 16, 16))
    _, _, used = p.project_divergence_free(v, iters=200, tolerance=1e9)
    assert used == 1  # converges immediately with a huge tolerance


# --- buoyancy / vorticity ---------------------------------------------------------


def test_buoyancy_pushes_up() -> None:
    v = np.zeros((2, 8, 8))
    t = np.ones((8, 8))
    out = p.add_buoyancy(v, t, dt=1.0, coefficient=2.0)
    assert np.all(out[1] == pytest.approx(-2.0))  # +y is down -> negative
    assert np.all(out[0] == 0.0)


def test_vorticity_confinement_is_finite_and_nonzero() -> None:
    v = np.zeros((2, 16, 16))
    v[0, 8, 8] = 1.0
    v[1, 9, 8] = -1.0
    out = p.vorticity_confinement(v, dt=0.1, epsilon=0.5)
    assert np.all(np.isfinite(out))
    assert not np.allclose(out, v)


# --- full step --------------------------------------------------------------------


def _hot_spot(n: int = 32) -> p.PyroField:
    v = np.zeros((2, n, n))
    t = np.zeros((n, n))
    d = np.zeros((n, n))
    t[n // 2, n // 2] = 5.0
    d[n // 2, n // 2] = 1.0
    return p.PyroField(velocity=v, temperature=t, density=d)


def test_pyro_step_is_deterministic() -> None:
    a, ra, _ = p.pyro_step(_hot_spot(), dt=0.1, projection_iters=40)
    b, rb, _ = p.pyro_step(_hot_spot(), dt=0.1, projection_iters=40)
    np.testing.assert_array_equal(a.temperature, b.temperature)
    assert ra == rb


def test_pyro_step_conserves_total_temperature_under_zero_velocity() -> None:
    # With no velocity and zero dissipation, advection+projection must not
    # create or destroy temperature (mass of the scalar is conserved).
    field = _hot_spot()
    out, _, _ = p.pyro_step(field, dt=0.1, buoyancy=0.0, confinement=0.0)
    assert out.temperature.sum() == pytest.approx(field.temperature.sum(), rel=1e-6)


def test_pyro_step_dissipation_reduces_temperature() -> None:
    field = _hot_spot()
    out, _, _ = p.pyro_step(field, dt=0.1, buoyancy=0.0, confinement=0.0, dissipation=0.5)
    assert out.temperature.sum() < field.temperature.sum()


def test_pyro_step_produces_upward_velocity() -> None:
    field = _hot_spot()
    out, _, _ = p.pyro_step(field, dt=0.2, buoyancy=2.0, confinement=0.0)
    # Net vertical velocity is upward (negative in screen coords).
    assert out.velocity[1].sum() < 0.0


# --- adversarial / refusal --------------------------------------------------------


def test_refuse_bad_planck_temperature() -> None:
    with pytest.raises(p.PyroError):
        p.planck_spectral_radiance(500e-9, 0.0)


def test_refuse_nonpositive_wavelength() -> None:
    with pytest.raises(p.PyroError):
        p.planck_spectral_radiance(np.array([-1e-9, 500e-9]), 5000.0)


def test_refuse_bad_wien_temperature() -> None:
    with pytest.raises(p.PyroError):
        p.wien_peak_wavelength_nm(-1.0)


def test_refuse_negative_stefan_temperature() -> None:
    with pytest.raises(p.PyroError):
        p.stefan_boltzmann_flux(-1.0)


def test_refuse_bad_gamma() -> None:
    with pytest.raises(p.PyroError):
        p.blackbody_rgb(5000.0, gamma=0.0)


def test_refuse_bad_blackbody_temperature() -> None:
    with pytest.raises(p.PyroError):
        p.blackbody_rgb(0.0)


def test_pyro_field_shape_property() -> None:
    f = p.PyroField(
        velocity=np.zeros((2, 4, 6)), temperature=np.zeros((4, 6)), density=np.zeros((4, 6))
    )
    assert f.shape == (4, 6)


def test_refuse_velocity_wrong_shape() -> None:
    with pytest.raises(p.PyroError):
        p.PyroField(
            velocity=np.zeros((3, 8, 8)), temperature=np.zeros((8, 8)), density=np.zeros((8, 8))
        )


def test_refuse_mismatched_shapes() -> None:
    with pytest.raises(p.PyroError):
        p.PyroField(
            velocity=np.zeros((2, 8, 8)), temperature=np.zeros((8, 9)), density=np.zeros((8, 8))
        )


def test_refuse_nan_temperature() -> None:
    t = np.zeros((8, 8))
    t[0, 0] = np.nan
    with pytest.raises(p.PyroError):
        p.PyroField(velocity=np.zeros((2, 8, 8)), temperature=t, density=np.zeros((8, 8)))


def test_refuse_negative_temperature() -> None:
    t = np.zeros((8, 8))
    t[0, 0] = -1.0
    with pytest.raises(p.PyroError):
        p.PyroField(velocity=np.zeros((2, 8, 8)), temperature=t, density=np.zeros((8, 8)))


def test_refuse_negative_density() -> None:
    d = np.zeros((8, 8))
    d[0, 0] = -1.0
    with pytest.raises(p.PyroError):
        p.PyroField(velocity=np.zeros((2, 8, 8)), temperature=np.zeros((8, 8)), density=d)


def test_refuse_empty_field() -> None:
    with pytest.raises(p.PyroError):
        p.PyroField(
            velocity=np.zeros((2, 0, 0)), temperature=np.zeros((0, 0)), density=np.zeros((0, 0))
        )


def test_refuse_oversized_grid() -> None:
    n = p.MAX_GRID + 1
    with pytest.raises(p.PyroError):
        p.PyroField(
            velocity=np.zeros((2, 4, n)), temperature=np.zeros((4, n)), density=np.zeros((4, n))
        )


def test_refuse_advect_bad_quantity_ndim() -> None:
    with pytest.raises(p.PyroError):
        p.advect_semi_lagrangian(np.zeros(8), np.zeros((2, 8, 8)), 0.1)


def test_refuse_advect_velocity_mismatch() -> None:
    with pytest.raises(p.PyroError):
        p.advect_semi_lagrangian(np.zeros((8, 8)), np.zeros((2, 8, 9)), 0.1)


def test_refuse_negative_dt_advect() -> None:
    with pytest.raises(p.PyroError):
        p.advect_semi_lagrangian(np.zeros((8, 8)), np.zeros((2, 8, 8)), -0.1)


def test_refuse_bad_projection_iters() -> None:
    v = np.zeros((2, 8, 8))
    with pytest.raises(p.PyroError):
        p.project_divergence_free(v, iters=0)
    with pytest.raises(p.PyroError):
        p.project_divergence_free(v, iters=p.MAX_JACOBI_ITERS + 1)


def test_refuse_bad_projection_tolerance() -> None:
    with pytest.raises(p.PyroError):
        p.project_divergence_free(np.zeros((2, 8, 8)), tolerance=0.0)


def test_refuse_projection_bad_velocity_shape() -> None:
    with pytest.raises(p.PyroError):
        p.project_divergence_free(np.zeros((3, 8, 8)))


def test_refuse_buoyancy_shape_mismatch() -> None:
    with pytest.raises(p.PyroError):
        p.add_buoyancy(np.zeros((2, 8, 8)), np.zeros((8, 9)), dt=0.1)


def test_refuse_negative_buoyancy_coefficient() -> None:
    with pytest.raises(p.PyroError):
        p.add_buoyancy(np.zeros((2, 8, 8)), np.zeros((8, 8)), dt=0.1, coefficient=-1.0)


def test_refuse_buoyancy_negative_dt() -> None:
    with pytest.raises(p.PyroError):
        p.add_buoyancy(np.zeros((2, 8, 8)), np.zeros((8, 8)), dt=-0.1)


def test_refuse_vorticity_bad_shape() -> None:
    with pytest.raises(p.PyroError):
        p.vorticity_confinement(np.zeros((3, 8, 8)), dt=0.1)


def test_refuse_vorticity_negative_epsilon() -> None:
    with pytest.raises(p.PyroError):
        p.vorticity_confinement(np.zeros((2, 8, 8)), dt=0.1, epsilon=-1.0)


def test_refuse_vorticity_negative_dt() -> None:
    with pytest.raises(p.PyroError):
        p.vorticity_confinement(np.zeros((2, 8, 8)), dt=-0.1)


def test_refuse_pyro_step_bad_dt() -> None:
    with pytest.raises(p.PyroError):
        p.pyro_step(_hot_spot(), dt=0.0)


def test_refuse_pyro_step_bad_dissipation() -> None:
    with pytest.raises(p.PyroError):
        p.pyro_step(_hot_spot(), dt=0.1, dissipation=1.5)
