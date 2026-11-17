"""WAVE 19 — tests for entropic 2-Wasserstein transport and the Schrödinger bridge.

Every claimed principle is *measured*: marginal conservation, the identity
``S_eps(mu, mu) = 0``, symmetry, non-negativity and the triangle inequality on
``sqrt(S_eps)``, plus exact mass conservation along the bridge. The negative
cases prove the solver fails closed on malformed input.
"""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.atlas import wasserstein_schrodinger_bridge as wsb


def _grid(n: int) -> np.ndarray:
    return np.linspace(0.0, 1.0, n)[:, None]


def _cost(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.asarray((a - b.T) ** 2, dtype=np.float64)


def _dist(n: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    d = rng.random(n)
    return d / d.sum()


def _point_cloud(n: int) -> np.ndarray:
    base = _grid(n)
    return np.concatenate([base, base], axis=1)


# --- distribution / cost / eps validation ----------------------------------------


def test_rejects_zero_probability_mass() -> None:
    with pytest.raises(wsb.TransportError):
        wsb.solve_entropic_wasserstein_ot(
            np.array([0.5, 0.5, -1e-9]), np.array([0.5, 0.5]), np.ones((3, 2))
        )


def test_rejects_negative_mass() -> None:
    with pytest.raises(wsb.TransportError):
        wsb.solve_entropic_wasserstein_ot(
            np.array([0.9, 0.2]), np.array([0.5, 0.5]), np.ones((2, 2))
        )


def test_rejects_non_normalised_simplex() -> None:
    with pytest.raises(wsb.TransportError):
        wsb.solve_entropic_wasserstein_ot(
            np.array([0.2, 0.2]), np.array([0.5, 0.5]), np.ones((2, 2))
        )


def test_rejects_non_finite_distribution() -> None:
    with pytest.raises(wsb.TransportError):
        wsb.solve_entropic_wasserstein_ot(
            np.array([np.nan, 0.5]), np.array([0.5, 0.5]), np.ones((2, 2))
        )


def test_rejects_mismatched_cost_shape() -> None:
    with pytest.raises(wsb.TransportError):
        wsb.solve_entropic_wasserstein_ot(
            np.array([0.5, 0.5]), np.array([0.5, 0.5]), np.ones((3, 2))
        )


def test_rejects_negative_cost() -> None:
    with pytest.raises(wsb.TransportError):
        wsb.solve_entropic_wasserstein_ot(
            np.array([0.5, 0.5]), np.array([0.5, 0.5]), np.array([[0.0, -1.0], [0.0, 0.0]])
        )


def test_rejects_non_finite_cost() -> None:
    with pytest.raises(wsb.TransportError):
        wsb.solve_entropic_wasserstein_ot(
            np.array([0.5, 0.5]), np.array([0.5, 0.5]), np.array([[0.0, np.inf], [0.0, 0.0]])
        )


@pytest.mark.parametrize("eps", [1e-4, 0.0, -1.0, 100.0, float("nan")])
def test_rejects_out_of_range_eps(eps: float) -> None:
    with pytest.raises(wsb.TransportError):
        wsb.solve_entropic_wasserstein_ot(
            np.array([0.5, 0.5]), np.array([0.5, 0.5]), np.ones((2, 2)), eps=eps
        )


def test_rejects_bad_max_iterations() -> None:
    with pytest.raises(wsb.TransportError):
        wsb.solve_entropic_wasserstein_ot(
            np.array([0.5, 0.5]), np.array([0.5, 0.5]), np.ones((2, 2)), max_iterations=0
        )


def test_rejects_bad_tolerance() -> None:
    with pytest.raises(wsb.TransportError):
        wsb.solve_entropic_wasserstein_ot(
            np.array([0.5, 0.5]), np.array([0.5, 0.5]), np.ones((2, 2)), tolerance=0.0
        )


# --- the entropic OT solve --------------------------------------------------------


def test_plan_conserves_both_marginals() -> None:
    mu = _dist(5, 1)
    nu = _dist(7, 2)
    result = wsb.solve_entropic_wasserstein_ot(mu, nu, _cost(_grid(5), _grid(7)), eps=0.05)
    assert result.marginal_residual_source < wsb.MARGINAL_TOLERANCE
    assert result.marginal_residual_target < wsb.MARGINAL_TOLERANCE
    assert result.converged is True


@pytest.mark.parametrize("eps", [1e-3, 0.01, 0.5, 1.0, 10.0])
def test_log_domain_is_stable_across_eps(eps: float) -> None:
    mu = _dist(6, 3)
    nu = _dist(6, 4)
    result = wsb.solve_entropic_wasserstein_ot(mu, nu, _cost(_grid(6), _grid(6)), eps=eps)
    assert not np.isnan(result.plan).any()
    assert not np.isinf(result.plan).any()
    assert result.marginal_residual_source < 1e-7


def test_plan_mass_equals_one() -> None:
    mu = _dist(4, 5)
    nu = _dist(5, 6)
    result = wsb.solve_entropic_wasserstein_ot(mu, nu, _cost(_grid(4), _grid(5)), eps=0.05)
    assert result.mass == pytest.approx(1.0, abs=1e-9)


def test_transport_cost_is_positive() -> None:
    mu = _dist(4, 7)
    nu = _dist(4, 8)
    result = wsb.solve_entropic_wasserstein_ot(mu, nu, _cost(_grid(4), _grid(4)), eps=0.05)
    assert result.transport_cost > 0.0


def test_identity_divergence_vanishes_for_identical_input() -> None:
    mu = _dist(6, 9)
    self_cost = _cost(_grid(6), _grid(6))
    result = wsb.solve_entropic_wasserstein_ot(mu, mu, self_cost, eps=0.05)
    assert abs(result.sinkhorn_divergence) < wsb.IDENTITY_TOLERANCE


def test_identity_divergence_vanishes_with_explicit_self_costs() -> None:
    mu = _dist(5, 10)
    result = wsb.solve_entropic_wasserstein_ot(
        mu,
        mu,
        _cost(_grid(5), _grid(5)),
        eps=0.05,
        source_cost=_cost(_grid(5), _grid(5)),
        target_cost=_cost(_grid(5), _grid(5)),
    )
    assert abs(result.sinkhorn_divergence) < wsb.IDENTITY_TOLERANCE


# --- the dynamic Schrödinger bridge ----------------------------------------------


def test_bridge_conserves_mass_at_every_time() -> None:
    mu = _dist(5, 11)
    nu = _dist(7, 12)
    src = _point_cloud(5)
    tgt = _point_cloud(7)
    for t in np.linspace(0.0, 1.0, 11):
        result = wsb.interpolate_schrodinger_bridge_scene(src, tgt, mu, nu, t=float(t), eps=0.05)
        assert result.mass_conserved is True
        assert result.positions.shape == (5, 2)
        assert np.all(np.isfinite(result.positions))


def test_bridge_brownian_variance_is_eps_t_one_minus_t() -> None:
    mu = _dist(4, 13)
    nu = _dist(4, 14)
    result = wsb.interpolate_schrodinger_bridge_scene(
        _point_cloud(4), _point_cloud(4), mu, nu, t=0.5, eps=0.2
    )
    assert result.brownian_variance == pytest.approx(0.2 * 0.5 * 0.5)


def test_bridge_endpoint_at_t_zero_is_source() -> None:
    mu = _dist(4, 15)
    nu = _dist(4, 16)
    src = _point_cloud(4)
    result = wsb.interpolate_schrodinger_bridge_scene(src, _point_cloud(4), mu, nu, t=0.0)
    assert np.allclose(result.positions, src)


def test_bridge_rejects_time_out_of_range() -> None:
    mu = _dist(3, 17)
    nu = _dist(3, 18)
    with pytest.raises(wsb.TransportError):
        wsb.interpolate_schrodinger_bridge_scene(_point_cloud(3), _point_cloud(3), mu, nu, t=1.5)


def test_bridge_rejects_non_finite_time() -> None:
    mu = _dist(3, 19)
    nu = _dist(3, 20)
    with pytest.raises(wsb.TransportError):
        wsb.interpolate_schrodinger_bridge_scene(
            _point_cloud(3), _point_cloud(3), mu, nu, t=float("inf")
        )


def test_bridge_rejects_point_count_mismatch() -> None:
    mu = _dist(3, 21)
    nu = _dist(3, 22)
    with pytest.raises(wsb.TransportError):
        wsb.interpolate_schrodinger_bridge_scene(_point_cloud(3), _point_cloud(4), mu, nu, t=0.5)


def test_bridge_rejects_non_finite_points() -> None:
    mu = _dist(3, 23)
    nu = _dist(3, 24)
    bad = _point_cloud(3)
    bad[0, 0] = np.nan
    with pytest.raises(wsb.TransportError):
        wsb.interpolate_schrodinger_bridge_scene(bad, _point_cloud(3), mu, nu, t=0.5)


# --- the Wasserstein barycentre ---------------------------------------------------


def test_barycenter_is_normalised() -> None:
    refs = tuple(_dist(5, 30 + i) for i in range(3))
    bary = wsb.compute_wasserstein_barycenter(
        refs, np.array([0.5, 0.3, 0.2]), _cost(_grid(5), _grid(5)), eps=0.05
    )
    assert bary.support.sum() == pytest.approx(1.0, abs=1e-9)
    assert bary.converged is True
    assert bary.marginal_residual < 1e-9


def test_barycenter_of_identical_references_is_that_reference() -> None:
    # The entropic barycentre converges to the true Wasserstein barycentre as
    # eps -> 0, so a small eps recovers the common reference exactly.
    mu = _dist(5, 40)
    bary = wsb.compute_wasserstein_barycenter(
        (mu, mu, mu), np.array([0.3, 0.3, 0.4]), _cost(_grid(5), _grid(5)), eps=1e-3
    )
    assert np.allclose(bary.support, mu, atol=1e-3)


def test_barycenter_rejects_non_normalised_weights() -> None:
    refs = tuple(_dist(4, 50 + i) for i in range(2))
    with pytest.raises(wsb.TransportError):
        wsb.compute_wasserstein_barycenter(
            refs, np.array([0.4, 0.4]), _cost(_grid(4), _grid(4)), eps=0.05
        )


def test_barycenter_rejects_mismatched_support_sizes() -> None:
    refs = (_dist(4, 60), _dist(5, 61))
    with pytest.raises(wsb.TransportError):
        wsb.compute_wasserstein_barycenter(
            refs, np.array([0.5, 0.5]), _cost(_grid(4), _grid(4)), eps=0.05
        )


def test_barycenter_rejects_negative_weights() -> None:
    refs = tuple(_dist(4, 70 + i) for i in range(2))
    with pytest.raises(wsb.TransportError):
        wsb.compute_wasserstein_barycenter(
            refs, np.array([1.2, -0.2]), _cost(_grid(4), _grid(4)), eps=0.05
        )


def test_barycenter_rejects_wrong_weight_count() -> None:
    refs = tuple(_dist(4, 80 + i) for i in range(3))
    with pytest.raises(wsb.TransportError):
        wsb.compute_wasserstein_barycenter(
            refs, np.array([0.5, 0.5]), _cost(_grid(4), _grid(4)), eps=0.05
        )


def test_barycenter_rejects_empty_tuple() -> None:
    with pytest.raises(wsb.TransportError):
        wsb.compute_wasserstein_barycenter((), np.array([]), _cost(_grid(4), _grid(4)), eps=0.05)


# --- the metric audit -------------------------------------------------------------


def test_metric_audit_measures_all_principles() -> None:
    mu = _dist(5, 90)
    nu = _dist(7, 91)
    audit = wsb.audit_wasserstein_metric(
        mu,
        nu,
        _cost(_grid(5), _grid(7)),
        eps=0.05,
        source_cost=_cost(_grid(5), _grid(5)),
        target_cost=_cost(_grid(7), _grid(7)),
    )
    assert audit.marginal_conserved is True
    assert audit.identity_holds is True
    assert audit.symmetry_residual < 1e-10
    assert audit.non_negative is True


def test_metric_audit_triangle_inequality_holds() -> None:
    mu = _dist(5, 92)
    nu = _dist(7, 93)
    gamma = _dist(4, 94)
    audit = wsb.audit_wasserstein_metric(
        mu,
        nu,
        _cost(_grid(5), _grid(7)),
        eps=0.05,
        source_cost=_cost(_grid(5), _grid(5)),
        target_cost=_cost(_grid(7), _grid(7)),
        third=gamma,
        third_cost=(_cost(_grid(5), _grid(4)), _cost(_grid(7), _grid(4))),
    )
    assert audit.triangle_residual <= 1e-9


def test_metric_audit_serialises() -> None:
    mu = _dist(4, 95)
    nu = _dist(4, 96)
    audit = wsb.audit_wasserstein_metric(mu, nu, _cost(_grid(4), _grid(4)), eps=0.05)
    payload = audit.to_dict()
    assert payload["kind"] == "atlas.wasserstein_metric_audit"
    assert payload["identity_residual"] == audit.identity_residual
