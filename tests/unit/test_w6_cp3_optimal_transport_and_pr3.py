"""W6 CP3: Bures–Wasserstein-2 optimal transport + PR #333 supersession.

Proves the SPD-geometry formulas (non-negativity, symmetry, triangle
inequality, Monge map algebra, McCann geodesic) and verifies the RFC 8785
JCS canonicalization contract of old PR #333 on the current base.
"""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.science.optimal_transport_spd import (
    _as_covariance,
    bures_wasserstein_2,
    matrix_inverse,
    matrix_inverse_square_root,
    matrix_square_root,
    mccann_geodesic,
    monge_map_tensor,
    monge_transport_map,
    optimal_transport_distance,
)
from nexus_ai_agent.provenance.jcs import (
    JcsError,
    canonical_digest,
    canonical_text,
    jcs_digest,
)


def spd_random(rng, dim, floor=0.5):
    arr = rng.normal(size=(dim, dim))
    return arr @ arr.T + np.eye(dim) * floor


# ---------------------------------------------------------------------------
# SPD algebra helpers
# ---------------------------------------------------------------------------


class TestSPDAlgebra:
    def test_square_root_of_identity_is_identity(self):
        assert np.allclose(matrix_square_root(np.eye(3)), np.eye(3), atol=1e-12)

    def test_inverse_of_diagonal(self):
        cov = np.diag([2.0, 4.0, 8.0])
        assert np.allclose(matrix_inverse(cov), np.diag([0.5, 0.25, 0.125]), atol=1e-12)

    def test_square_root_is_symmetric(self):
        rng = np.random.default_rng(42)
        root = matrix_square_root(spd_random(rng, 3))
        assert np.allclose(root, root.T, atol=1e-10)

    def test_inverse_of_spd_is_spd(self):
        rng = np.random.default_rng(3)
        inv = matrix_inverse(spd_random(rng, 4))
        eigs = np.linalg.eigvalsh(inv)
        assert eigs.min() > 0.0


# ---------------------------------------------------------------------------
# Bures–Wasserstein distance
# ---------------------------------------------------------------------------


class TestBuresWasserstein:
    def test_distance_is_zero_for_identical_gaussians(self):
        rng = np.random.default_rng(11)
        mean = np.array([1.0, 2.0])
        cov = spd_random(rng, 2)
        assert bures_wasserstein_2(mean, cov, mean, cov) == pytest.approx(0.0, abs=1e-12)

    def test_distance_is_non_negative(self):
        rng = np.random.default_rng(12)
        w = bures_wasserstein_2(
            np.array([1.0, 2.0]),
            spd_random(rng, 2),
            np.array([3.0, -1.0]),
            spd_random(rng, 2),
        )
        assert w >= 0.0

    def test_distance_is_symmetric(self):
        rng = np.random.default_rng(13)
        m1, c1 = np.array([1.0, 2.0]), spd_random(rng, 2)
        m2, c2 = np.array([3.0, -1.0]), spd_random(rng, 2)
        w12 = bures_wasserstein_2(m1, c1, m2, c2)
        w21 = bures_wasserstein_2(m2, c2, m1, c1)
        assert w12 == pytest.approx(w21, rel=1e-12)

    def test_triangle_inequality_holds_numerically(self):
        rng = np.random.default_rng(14)
        m1, c1 = np.array([0.5, 1.0]), spd_random(rng, 2)
        m2, c2 = np.array([2.0, -1.0]), spd_random(rng, 2)
        m3, c3 = np.array([-1.5, 3.0]), spd_random(rng, 2)
        d12 = optimal_transport_distance(m1, c1, m2, c2)
        d23 = optimal_transport_distance(m2, c2, m3, c3)
        d13 = optimal_transport_distance(m1, c1, m3, c3)
        assert d13 <= d12 + d23 + 1e-3

    def test_distance_equals_mean_shift_for_equal_covariances(self):
        rng = np.random.default_rng(15)
        cov = spd_random(rng, 2)
        w = bures_wasserstein_2(np.array([0.0, 0.0]), cov, np.array([3.0, 4.0]), cov)
        assert w == pytest.approx(25.0, rel=1e-10)

    def test_distance_positive_for_scale_step(self):
        cov_small = spd_random(np.random.default_rng(16), 2, floor=0.3)
        cov_large = np.eye(2) * 4.0
        w = bures_wasserstein_2(np.array([0.0, 0.0]), cov_small, np.array([0.0, 0.0]), cov_large)
        assert w > 0.0


# ---------------------------------------------------------------------------
# Monge map
# ---------------------------------------------------------------------------


class TestMongeMap:
    def test_transport_matrix_is_symmetric_positive_definite(self):
        rng = np.random.default_rng(17)
        a = monge_transport_map(
            np.array([1.0, -2.0]),
            spd_random(rng, 2),
            np.array([3.0, 0.5]),
            spd_random(rng, 2),
        )
        assert np.allclose(a, a.T, atol=1e-8)
        assert min(np.linalg.eigvalsh(a)) > 0.0

    def test_transport_matrix_satisfies_covariance_equation(self):
        rng = np.random.default_rng(18)
        c1 = spd_random(rng, 2)
        c2 = spd_random(rng, 2)
        a = monge_transport_map(np.array([1.0, -2.0]), c1, np.array([3.0, 0.5]), c2)
        assert np.allclose(a @ c1 @ a.T, c2, atol=1e-8)

    def test_affine_map_produces_target_mean(self):
        rng = np.random.default_rng(19)
        m1 = np.array([1.0, -2.0])
        m2 = np.array([3.0, 0.5])
        c1 = spd_random(rng, 2)
        c2 = spd_random(rng, 2)
        a = monge_transport_map(m1, c1, m2, c2)
        affine = monge_map_tensor(m2, a, m1)
        # affine(m1) == m2 is the exact identity (the Monge map sends the mean to the mean)
        assert np.allclose(affine(m1), m2, atol=1e-10)
        # T(x) is an affine map, so it pushes the Gaussian mean through exactly:
        # for x = m1 + epsilon (any epsilon), T(x) = m2 + A @ epsilon.
        rng_samples = np.random.default_rng(20)
        epsilon = rng_samples.normal(size=2)
        x = m1 + epsilon
        assert np.allclose(affine(x), m2 + a @ epsilon, atol=1e-10)

    def test_transport_matrix_is_target_first_for_isotropic_covariance(self):
        # If covariances are scalar multiples of identity, A is a scalar multiple of I.
        c1 = np.eye(2)
        c2 = np.eye(2) * 4.0
        a = monge_transport_map(np.array([0.0, 0.0]), c1, np.array([0.0, 0.0]), c2)
        assert np.allclose(a, np.eye(2) * 2.0, atol=1e-12)


# ---------------------------------------------------------------------------
# McCann geodesic
# ---------------------------------------------------------------------------


class TestMcCannGeodesic:
    def test_t_zero_is_source(self):
        rng = np.random.default_rng(21)
        m1, c1 = np.array([1.0, -2.0]), spd_random(rng, 2)
        m2, c2 = np.array([3.0, 0.5]), spd_random(rng, 2)
        mean_t, cov_t = mccann_geodesic(0.0, m1, c1, m2, c2)
        assert np.allclose(mean_t, m1, atol=1e-12)
        assert np.allclose(cov_t, c1, atol=1e-10)

    def test_t_one_is_target(self):
        rng = np.random.default_rng(22)
        m1, c1 = np.array([1.0, -2.0]), spd_random(rng, 2)
        m2, c2 = np.array([3.0, 0.5]), spd_random(rng, 2)
        mean_t, cov_t = mccann_geodesic(1.0, m1, c1, m2, c2)
        assert np.allclose(mean_t, m2, atol=1e-10)
        assert np.allclose(cov_t, c2, atol=1e-8)

    def test_geodesic_distance_scales_quadratically(self):
        rng = np.random.default_rng(23)
        m1, c1 = np.array([1.0, -2.0]), spd_random(rng, 2)
        m2, c2 = np.array([3.0, 0.5]), spd_random(rng, 2)
        w_full = bures_wasserstein_2(m1, c1, m2, c2)
        for t in [0.25, 0.5, 0.75]:
            mean_t, cov_t = mccann_geodesic(t, m1, c1, m2, c2)
            w_t = bures_wasserstein_2(m1, c1, mean_t, cov_t)
            assert w_t == pytest.approx(t**2 * w_full, rel=1e-8)

    def test_geodesic_mean_is_linear_interpolation(self):
        rng = np.random.default_rng(24)
        m1, c1 = np.array([1.0, -2.0]), spd_random(rng, 2)
        m2, c2 = np.array([3.0, 0.5]), spd_random(rng, 2)
        for t in [0.2, 0.6]:
            mean_t, _ = mccann_geodesic(t, m1, c1, m2, c2)
            assert np.allclose(mean_t, (1.0 - t) * m1 + t * m2, atol=1e-12)

    def test_t_outside_range_refused(self):
        with pytest.raises(ValueError):
            mccann_geodesic(1.5, np.zeros(2), np.eye(2), np.zeros(2), np.eye(2))


# ---------------------------------------------------------------------------
# PR #333 supersession: RFC 8785 JCS canonicalization
# ---------------------------------------------------------------------------


class TestJcsSupersession:
    def test_legacy_scheme_matches_json_dumps_compact(self):
        import hashlib
        import json

        value = {"b": 2, "a": [1, 2, 3], "c": {"d": "e"}}
        legacy = canonical_digest(value, scheme="legacy-json-v1")
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        expected = "sha256:" + hashlib.sha256(encoded).hexdigest()
        assert legacy == expected

    def test_jcs_scheme_is_stable_and_self_consistent(self):
        value = {"b": 2, "a": [1, 2, 3], "c": {"d": "e"}}
        assert canonical_digest(value, scheme="rfc8785-jcs-v1") == jcs_digest(value)

    def test_legacy_and_jcs_spellings_differ(self):
        # The whole point of the versioned contract: two spellings for one value.
        value = {"b": 2.0, "a": [1, 2, 3], "c": {"d": "e"}}
        assert canonical_digest(value, scheme="legacy-json-v1") != canonical_digest(
            value, scheme="rfc8785-jcs-v1"
        )

    def test_canonical_text_matches_jcs_digest_input(self):
        value = {"hello": "world"}
        assert canonical_text(value).encode("utf-8") != b""

    def test_unknown_scheme_is_refused(self):
        with pytest.raises(ValueError):
            canonical_digest({"a": 1}, scheme="rfc8785-jcs-v1-draft")  # type: ignore[arg-type]

    def test_hostile_input_is_refused_by_jcs(self):
        with pytest.raises(JcsError):
            jcs_digest({"a": float("nan")})
        with pytest.raises(JcsError):
            jcs_digest(Object())  # unsupported concrete type

    def test_cyclic_structure_is_refused(self):
        cyclic: dict = {}
        cyclic["self"] = cyclic
        with pytest.raises(JcsError):
            canonical_text(cyclic)


class Object:
    pass


# ---------------------------------------------------------------------------
# W6-CP5: P1 regression tests — complex detection, symmetry-before-symmetrize,
# eigen-decomposition roots, monotonic structural checks
# ---------------------------------------------------------------------------


class TestComplexDetection:
    def test_complex_input_rejected_before_casting(self):
        cov = np.array([[1.0 + 1j, 0.0], [0.0, 1.0]])
        with pytest.raises(ValueError, match="real"):
            _as_covariance(cov)

    def test_complex_input_rejected_early_in_bures(self):
        cov = np.array([[1.0, 0.5], [0.5, 1.0]], dtype=complex)
        cov[0, 1] = 0.5 + 1e-3j
        with pytest.raises(ValueError, match="real"):
            bures_wasserstein_2([0.0, 0.0], cov, [0.0, 0.0], np.eye(2))


class TestSymmetryBeforeSymmetrization:
    def test_asymmetric_rejected_before_averaging(self):
        # Naive eigvalsh on the lower triangle would pass this matrix.
        cov = np.array([[1.0, 100.0], [0.0, 1.0]])
        with pytest.raises(ValueError, match="symmetric"):
            _as_covariance(cov)

    def test_repeated_eigenvalues_still_valid(self):
        # Scalar covariances have degenerate eigenspaces: valid input.
        cov = np.eye(2) * 1.5
        root = matrix_square_root(cov)
        assert np.allclose(root @ root, cov, atol=1e-12)


class TestPositiveDefinitenessGate:
    def test_indefinite_covariance_rejected(self):
        with pytest.raises(ValueError, match="positive definite"):
            _as_covariance(np.diag([1.0, -1.0]))

    def test_near_singular_covariance_rejected(self):
        with pytest.raises(ValueError, match="ill-conditioned"):
            _as_covariance(np.diag([1.0, 1e-16]))

    def test_ill_conditioned_covariance_rejected(self):
        with pytest.raises(ValueError, match="ill-conditioned"):
            _as_covariance(np.diag([1.0, 1e-12]))

    def test_finite_check(self):
        with pytest.raises(ValueError, match="finite"):
            _as_covariance(np.array([[np.nan, 0.0], [0.0, 1.0]]))

    def test_empty_and_nonsquare_rejected(self):
        with pytest.raises(ValueError, match="square"):
            _as_covariance(np.ones((2, 3)))
        with pytest.raises(ValueError, match="non-empty"):
            _as_covariance(np.zeros((0, 0)))


class TestEigendecompositionRoots:
    def test_root_square_identity(self):
        # R R^T == Σ via eigh construction
        cov = np.diag([2.0, 8.0])
        root = matrix_square_root(cov)
        assert np.allclose(root @ root, cov, atol=1e-12)
        assert np.allclose(root, root.T, atol=1e-12)

    def test_inverse_square_root_inverts_root(self):
        cov = np.diag([2.0, 8.0])
        root = matrix_square_root(cov)
        inv_root = matrix_inverse_square_root(cov)
        assert np.allclose(inv_root @ root, np.eye(2), atol=1e-12)

    def test_matrix_inverse_is_eigen_decomposition_based(self):
        cov = np.diag([2.0, 8.0])
        inv = matrix_inverse(cov)
        assert np.allclose(inv @ cov, np.eye(2), atol=1e-12)

    def test_eigenvector_degeneracy_still_symmetric_root(self):
        cov = np.eye(3) * 4.0
        root = matrix_square_root(cov)
        assert np.allclose(root, 2.0 * np.eye(3), atol=1e-12)


class TestMongeMapStructuralChecks:
    def test_non_commuting_covariances_satisfy_map_equation(self):
        # Σ₁ and Σ₂ do not commute: the map must still satisfy A Σ₁ Aᵀ = Σ₂.
        s1 = np.array([[2.0, 1.0], [1.0, 2.0]])
        s2 = np.array([[4.0, -0.5], [-0.5, 1.5]])
        # The module's own map must satisfy the structural identity.
        a_module = monge_transport_map([0.0, 0.0], s1, [1.0, 2.0], s2)
        assert np.allclose(a_module @ s1 @ a_module.T, s2, atol=1e-8)
        assert np.allclose(a_module, a_module.T, atol=1e-10)
        # Monge map must be SPD.
        eigs = np.linalg.eigvalsh(a_module)
        assert eigs.min() > 0.0

    def test_transport_matrix_is_positive_definite_and_symmetric(self):
        s1 = np.diag([1.0, 4.0])
        s2 = np.diag([2.0, 8.0])
        a = monge_transport_map([0.0, 0.0], s1, [1.0, 1.0], s2)
        eigs = np.linalg.eigvalsh(a)
        assert eigs.min() > 0.0


class TestMcCannGeodesicSpotChecks:
    def test_geodesic_at_t_half_matches_endpoints(self):
        m1 = np.array([0.0, 0.0])
        m2 = np.array([1.0, 1.0])
        c1 = np.diag([1.0, 4.0])
        c2 = np.diag([4.0, 1.0])
        for t in (0.0, 0.5, 1.0):
            mean_t, cov_t = mccann_geodesic(t, m1, c1, m2, c2)
            expected_mean = (1.0 - t) * m1 + t * m2
            assert np.allclose(mean_t, expected_mean, atol=1e-12)
        mean_half, cov_half = mccann_geodesic(0.5, m1, c1, m2, c2)
        assert np.allclose(mean_half, 0.5 * (m1 + m2), atol=1e-12)
        eigs = np.linalg.eigvalsh(cov_half)
        assert eigs.min() > 0.0

    def test_geodesic_endpoints_pass_through_source_and_target(self):
        m1 = np.array([0.0, 0.0])
        m2 = np.array([1.0, 1.0])
        c1 = np.diag([1.0, 4.0])
        c2 = np.diag([4.0, 1.0])
        m0, c0 = mccann_geodesic(0.0, m1, c1, m2, c2)
        m1_, c1_ = mccann_geodesic(1.0, m1, c1, m2, c2)
        assert np.allclose(m0, m1, atol=1e-12)
        assert np.allclose(c0, c1, atol=1e-10)
        assert np.allclose(m1_, m2, atol=1e-12)
        assert np.allclose(c1_, c2, atol=1e-10)


class TestBuresNegativeClamp:
    def test_identical_gaussians_give_machine_noise_zero_clamped_to_zero(self):
        # Identical covariances: W_2^2 is exactly 0; the clamp must absorb noise.
        c = np.diag([1.0, 4.0])
        w2 = bures_wasserstein_2([0.0, 0.0], c, [0.0, 0.0], c)
        assert w2 == 0.0

    def test_identical_with_mean_shift_positive(self):
        c = np.diag([1.0, 4.0])
        w2 = bures_wasserstein_2([0.0, 0.0], c, [1.0, 2.0], c)
        assert w2 == pytest.approx(5.0, rel=1e-12)

    def test_mean_dimension_mismatch_refused(self):
        with pytest.raises(ValueError, match="dimension"):
            bures_wasserstein_2([0.0, 0.0, 0.0], np.eye(2), [0.0, 0.0], np.eye(2))

    def test_mean_dimension_mismatch_inside_monge_map(self):
        with pytest.raises(ValueError, match="dimension"):
            monge_transport_map([0.0], np.eye(2), [0.0, 0.0], np.eye(2))


class TestComplexMeanAndNonNumericCovariance:
    def test_complex_mean_rejected(self):
        with pytest.raises(ValueError, match="real"):
            bures_wasserstein_2(
                np.array([0.0 + 1j, 0.0]),
                np.eye(2),
                [0.0, 0.0],
                np.eye(2),
            )

    def test_non_numeric_covariance_rejected(self):
        with pytest.raises(ValueError, match="numeric"):
            _as_covariance(["not", "a", "number"])  # type: ignore[arg-type]

    def test_complex_covariance_rejected_early_in_matrix_sqrt(self):
        cov = np.eye(2) + np.eye(2) * 1j
        with pytest.raises(ValueError, match="real"):
            matrix_square_root(cov)
