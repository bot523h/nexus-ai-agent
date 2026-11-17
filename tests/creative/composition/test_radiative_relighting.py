"""Wave 9 — radiative-transfer relighting: SH irradiance, Frankot–Chellappa, PBR.

Real-computation tests over NumPy arrays. Every analytic identity is checked
against an independent computation (Monte-Carlo orthonormality, a closed-form
ambient irradiance, the matrix-vs-sum equivalence, the discrete curl).
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from nexus_ai_agent.creative.composition import radiative_relighting as r


def _sphere_dirs(n: int, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal((n, 3))
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def _hemisphere_normals(h: int, w: int) -> np.ndarray:
    y, x = np.mgrid[0:h, 0:w]
    nx = (x - w / 2) / (w / 2)
    ny = (y - h / 2) / (h / 2)
    nz = np.sqrt(np.clip(1.0 - nx * nx - ny * ny, 0.05, None))
    n = np.stack([nx, ny, nz], axis=-1)
    return n / np.linalg.norm(n, axis=-1, keepdims=True)


def test_sh_constants_match_reference_values() -> None:
    assert r.SH_L0 == pytest.approx(0.2820947917738781, rel=1e-12)
    assert r.SH_L1 == pytest.approx(0.4886025119029199, rel=1e-12)
    assert r.SH_L2A == pytest.approx(1.0925484305920792, rel=1e-12)
    assert r.SH_L2B == pytest.approx(0.3153915652525200, rel=1e-12)
    assert r.SH_L2C == pytest.approx(0.5462742152960396, rel=1e-12)
    assert r.IRRADIANCE_TRANSFER == (math.pi, 2 * math.pi / 3, math.pi / 4)


def test_sh_basis_analytic_values() -> None:
    n = np.array([[0.0, 0.0, 1.0]])
    basis = r.sh_basis(n)[0]
    assert basis[0] == pytest.approx(r.SH_L0)
    assert basis[1] == pytest.approx(0.0)  # Y1,-1 ~ ny
    assert basis[2] == pytest.approx(r.SH_L1)  # Y10 ~ nz
    assert basis[3] == pytest.approx(0.0)
    assert basis[6] == pytest.approx(r.SH_L2B * 2.0)  # (3 nz^2 - 1) at nz=1
    assert basis[8] == pytest.approx(0.0)  # nx^2 - ny^2


def test_sh_basis_is_orthonormal_on_the_sphere() -> None:
    dirs = _sphere_dirs(200_000)
    basis = r.sh_basis(dirs)
    gram = (basis.T @ basis) / dirs.shape[0] * (4.0 * math.pi)
    assert np.allclose(gram, np.eye(r.SH_BASIS_SIZE), atol=0.06)


def test_irradiance_matrix_equals_sh_sum() -> None:
    rng = np.random.default_rng(3)
    coeffs = rng.standard_normal((r.SH_BASIS_SIZE, 3))
    normals = _sphere_dirs(5_000, seed=7)
    sum_irr = r.sh_irradiance(coeffs, normals)
    matrix_irr = r.irradiance_from_matrix(r.irradiance_matrix(coeffs), normals)
    assert np.max(np.abs(sum_irr - matrix_irr)) < 1e-9


def test_irradiance_matrix_is_symmetric() -> None:
    rng = np.random.default_rng(11)
    matrix = r.irradiance_matrix(rng.standard_normal((9, 3)))
    for ch in range(3):
        assert np.allclose(matrix[ch], matrix[ch].T, atol=1e-12)


def test_estimate_sh_environment_recovers_constant_ambient() -> None:
    frame = np.full((64, 128, 3), 0.7, dtype=np.float64)
    probe = r.estimate_sh_from_environment_frame(frame)
    normals = _sphere_dirs(500, seed=2)
    irradiance = probe.irradiance(normals)
    # a uniform environment of radiance c gives Lambertian irradiance pi * c
    assert np.allclose(irradiance, math.pi * 0.7, atol=2e-3)


def test_estimate_sh_from_subject_normals_roundtrip() -> None:
    rng = np.random.default_rng(5)
    coeffs = rng.standard_normal((9, 3)) * 0.2 + 0.5  # positive ambient, no clipping
    true_probe = r.SphericalHarmonicsLightProbe(coeffs)
    normals = _hemisphere_normals(64, 64)
    subject = true_probe.irradiance(normals)
    mask = np.ones((64, 64), dtype=np.float64)
    fit = r.estimate_sh_from_subject_normals(subject, normals, mask, ridge_lambda=1e-8)
    err = np.max(np.abs(fit.irradiance(normals) - true_probe.irradiance(normals)))
    assert err < 1e-3


def test_frankot_chellappa_recovers_integrable_surface() -> None:
    x = np.linspace(0, 2 * np.pi, 96)
    xx, yy = np.meshgrid(x, x)
    surface_true = np.sin(xx) * np.cos(yy)
    gy, gx = np.gradient(surface_true)
    surface, normals = r.reconstruct_integrable_normal_field(gx, gy)
    dz_dy, dz_dx = np.gradient(surface)
    curl = np.gradient(dz_dx, axis=0) - np.gradient(dz_dy, axis=1)
    assert np.max(np.abs(curl)) < 1e-10
    assert np.allclose(np.linalg.norm(normals, axis=-1), 1.0, atol=1e-12)


def test_frankot_chellappa_projects_non_integrable_field() -> None:
    y, x = np.mgrid[0:64, 0:64].astype(np.float64)
    p = y  # dz/dx = y
    q = -x  # dz/dy = -x  -> curl = dq/dx - dp/dy = -1 - 1 = -2 (non-integrable)
    surface, normals = r.reconstruct_integrable_normal_field(p, q)
    assert np.all(np.isfinite(surface))
    assert np.allclose(np.linalg.norm(normals, axis=-1), 1.0, atol=1e-12)


def test_cook_torrance_specular_nonnegative_and_zero_below_horizon() -> None:
    normals = _hemisphere_normals(24, 24)
    light = (0.0, 0.0, -1.0)  # pointing away -> ndotl <= 0
    spec = r.cook_torrance_specular(normals, (0.0, 0.0, 1.0), light)
    assert np.all(spec >= 0.0)
    assert np.allclose(spec, 0.0)


def test_cook_torrance_specular_peaks_at_mirror_reflection() -> None:
    view = (0.0, 0.0, 1.0)
    light = (0.0, 0.0, 1.0)
    mirror_normal = np.array([[[0.0, 0.0, 1.0]]])
    off_normal = np.array([[[0.6, 0.0, 0.8]]])
    peak = r.cook_torrance_specular(mirror_normal, view, light)[0, 0, 0]
    off = r.cook_torrance_specular(off_normal, view, light)[0, 0, 0]
    assert peak > off


def test_relight_with_identical_probe_is_identity_without_specular() -> None:
    probe = r.SphericalHarmonicsLightProbe(np.full((9, 3), 0.2))
    normals = _hemisphere_normals(32, 32)
    subject = np.clip(probe.irradiance(normals), 0.0, 1.0)
    mask = np.ones((32, 32), dtype=np.float64)
    out = r.relight_subject_to_environment(
        subject, normals, mask, source_probe=probe, target_probe=probe, specular_strength=0.0
    )
    assert np.max(np.abs(out - subject)) < 1e-5


def test_relight_ratio_scales_interior_irradiance() -> None:
    ambient = np.zeros((9, 3))
    ambient[0] = 0.1  # pure ambient -> constant irradiance everywhere
    source = r.SphericalHarmonicsLightProbe(ambient)
    target = source.scaled(2.0)
    normals = _hemisphere_normals(32, 32)
    subject = np.clip(source.irradiance(normals), 1e-6, None)
    mask = np.ones((32, 32), dtype=np.float64)
    out = r.relight_subject_to_environment(
        subject, normals, mask, source_probe=source, target_probe=target, specular_strength=0.0
    )
    ratio = out / subject
    assert np.allclose(ratio, 2.0, atol=1e-4)


def test_unmasked_region_is_preserved() -> None:
    source = r.SphericalHarmonicsLightProbe(np.full((9, 3), 0.2))
    target = source.scaled(3.0)
    normals = _hemisphere_normals(16, 16)
    subject = np.full((16, 16, 3), 0.4)
    mask = np.zeros((16, 16), dtype=np.float64)
    mask[4:12, 4:12] = 1.0
    out = r.relight_subject_to_environment(
        subject, normals, mask, source_probe=source, target_probe=target
    )
    assert np.allclose(out[0, 0], subject[0, 0])


def test_dominant_direction_is_unit_and_ambient_default() -> None:
    ambient = r.SphericalHarmonicsLightProbe(np.zeros((9, 3)))
    assert ambient.dominant_direction == (0.0, 0.0, 1.0)
    coeffs = np.zeros((9, 3))
    coeffs[3] = 1.0  # L_1,1 -> x
    probe = r.SphericalHarmonicsLightProbe(coeffs)
    d = probe.dominant_direction
    assert d == pytest.approx((1.0, 0.0, 0.0))
    assert float(np.linalg.norm(d)) == pytest.approx(1.0)


# --- adversarial / refusal cases -------------------------------------------------


def test_non_unit_normals_are_refused() -> None:
    with pytest.raises(r.DegenerateNormalsError):
        r.sh_basis(np.array([[[1.0, 1.0, 0.0]]]))


def test_nan_normals_are_refused() -> None:
    with pytest.raises(r.DegenerateNormalsError):
        r.sh_basis(np.array([[[np.nan, 0.0, 1.0]]]))


def test_bad_coefficient_shape_is_refused() -> None:
    with pytest.raises(r.RelightingError):
        r.irradiance_matrix(np.zeros((4, 3)))


def test_nan_coefficients_are_refused() -> None:
    bad = np.zeros((9, 3))
    bad[0, 0] = np.inf
    with pytest.raises(r.RelightingError):
        r.SphericalHarmonicsLightProbe(bad)


def test_non_integrable_shape_mismatch_is_refused() -> None:
    with pytest.raises(r.RelightingError):
        r.reconstruct_integrable_normal_field(np.zeros((4, 4)), np.zeros((5, 5)))


def test_nan_gradient_field_is_refused() -> None:
    bad = np.zeros((8, 8))
    bad[0, 0] = np.inf
    with pytest.raises(r.RelightingError):
        r.reconstruct_integrable_normal_field(bad, np.zeros((8, 8)))


def test_nonpositive_epsilon_is_refused() -> None:
    with pytest.raises(r.RelightingError):
        r.reconstruct_integrable_normal_field(np.zeros((4, 4)), np.zeros((4, 4)), epsilon=0.0)


def test_out_of_range_roughness_is_refused() -> None:
    normals = _hemisphere_normals(8, 8)
    with pytest.raises(r.RelightingError):
        r.cook_torrance_specular(normals, (0.0, 0.0, 1.0), (0.0, 0.0, 1.0), roughness=0.0)


def test_non_unit_view_dir_is_refused() -> None:
    normals = _hemisphere_normals(8, 8)
    with pytest.raises(r.RelightingError):
        r.cook_torrance_specular(normals, (0.0, 0.0, 2.0), (0.0, 0.0, 1.0))


def test_zero_mask_is_refused() -> None:
    probe = r.SphericalHarmonicsLightProbe(np.full((9, 3), 0.2))
    normals = _hemisphere_normals(8, 8)
    subject = np.full((8, 8, 3), 0.4)
    with pytest.raises(r.RelightingError):
        r.relight_subject_to_environment(
            subject,
            normals,
            np.zeros((8, 8)),
            source_probe=probe,
            target_probe=probe,
        )


def test_zero_luminance_subject_is_refused() -> None:
    probe = r.SphericalHarmonicsLightProbe(np.full((9, 3), 0.2))
    normals = _hemisphere_normals(8, 8)
    with pytest.raises(r.RelightingError):
        r.relight_subject_to_environment(
            np.zeros((8, 8, 3)),
            normals,
            np.ones((8, 8)),
            source_probe=probe,
            target_probe=probe,
        )


def test_too_few_masked_pixels_is_refused() -> None:
    normals = _hemisphere_normals(8, 8)
    subject = np.full((8, 8, 3), 0.4)
    mask = np.zeros((8, 8))
    mask[0, :4] = 1.0  # 4 < 9 coefficients
    with pytest.raises(r.DegenerateNormalsError):
        r.estimate_sh_from_subject_normals(subject, normals, mask)


def test_negative_ridge_is_refused() -> None:
    normals = _hemisphere_normals(8, 8)
    subject = np.full((8, 8, 3), 0.4)
    with pytest.raises(r.RelightingError):
        r.estimate_sh_from_subject_normals(subject, normals, np.ones((8, 8)), ridge_lambda=-1.0)


def test_nan_subject_is_refused() -> None:
    probe = r.SphericalHarmonicsLightProbe(np.full((9, 3), 0.2))
    normals = _hemisphere_normals(8, 8)
    subject = np.full((8, 8, 3), 0.4)
    subject[0, 0, 0] = np.nan
    with pytest.raises(r.RelightingError):
        r.relight_subject_to_environment(
            subject, normals, np.ones((8, 8)), source_probe=probe, target_probe=probe
        )


# --- additional real-computation and branch coverage -----------------------------


def test_irradiance_via_matrix_matches_direct() -> None:
    probe = r.SphericalHarmonicsLightProbe(np.full((9, 3), 0.25))
    normals = _hemisphere_normals(12, 12)
    assert np.allclose(probe.irradiance(normals), probe.irradiance_via_matrix(normals))


def test_dominant_direction_non_unit_dipole_is_normalised() -> None:
    coeffs = np.zeros((9, 3))
    coeffs[1] = 3.0  # L_1,-1 -> y, magnitude 3
    probe = r.SphericalHarmonicsLightProbe(coeffs)
    d = probe.dominant_direction
    assert d == pytest.approx((0.0, 1.0, 0.0))
    assert float(np.linalg.norm(d)) == pytest.approx(1.0)


def test_irradiance_from_matrix_rejects_bad_shape() -> None:
    with pytest.raises(r.RelightingError):
        r.irradiance_from_matrix(np.zeros((3, 3, 3)), np.array([[0.0, 0.0, 1.0]]))


def test_estimate_sh_from_environment_rejects_non_finite_frame() -> None:
    frame = np.zeros((8, 16, 3))
    frame[0, 0, 0] = np.inf
    with pytest.raises(r.RelightingError):
        r.estimate_sh_from_environment_frame(frame)


def test_ambient_probe_negative_radiance_refused() -> None:
    with pytest.raises(r.RelightingError):
        r.ambient_probe(-0.5)


def test_ambient_probe_is_constant_irradiance() -> None:
    probe = r.ambient_probe(0.4)
    normals = _sphere_dirs(200, seed=9)
    assert np.allclose(probe.irradiance(normals), math.pi * 0.4, atol=1e-9)


def test_estimate_sh_shape_mismatch_refused() -> None:
    with pytest.raises(r.DegenerateNormalsError):
        r.estimate_sh_from_subject_normals(
            np.full((8, 8, 3), 0.4), _hemisphere_normals(9, 9), np.ones((8, 8))
        )


def test_estimate_sh_mask_shape_mismatch_refused() -> None:
    with pytest.raises(r.DegenerateNormalsError):
        r.estimate_sh_from_subject_normals(
            np.full((8, 8, 3), 0.4), _hemisphere_normals(8, 8), np.ones((7, 8))
        )


def test_relight_shape_mismatch_refused() -> None:
    probe = r.ambient_probe(0.2)
    with pytest.raises(r.DegenerateNormalsError):
        r.relight_subject_to_environment(
            np.full((8, 8, 3), 0.4),
            _hemisphere_normals(9, 9),
            np.ones((8, 8)),
            source_probe=probe,
            target_probe=probe,
        )


def test_relight_invalid_specular_strength_refused() -> None:
    probe = r.ambient_probe(0.2)
    with pytest.raises(r.RelightingError):
        r.relight_subject_to_environment(
            np.full((8, 8, 3), 0.4),
            _hemisphere_normals(8, 8),
            np.ones((8, 8)),
            source_probe=probe,
            target_probe=probe,
            specular_strength=1.5,
        )


def test_relight_specular_adds_highlight() -> None:
    source = r.ambient_probe(0.2)
    target = r.ambient_probe(0.3)
    normals = _hemisphere_normals(24, 24)
    subject = np.full((24, 24, 3), 0.4)
    mask = np.ones((24, 24), dtype=np.float64)
    plain = r.relight_subject_to_environment(
        subject, normals, mask, source_probe=source, target_probe=target, specular_strength=0.0
    )
    shiny = r.relight_subject_to_environment(
        subject, normals, mask, source_probe=source, target_probe=target, specular_strength=0.5
    )
    assert float(shiny.max()) >= float(plain.max())


def test_environment_frame_pixel_budget_is_enforced() -> None:
    big = np.zeros((2049, 2049, 3))
    with pytest.raises(r.RelightingError):
        r.estimate_sh_from_environment_frame(big)


def test_estimate_sh_nan_normals_refused() -> None:
    bad = _hemisphere_normals(8, 8)
    bad[0, 0] = np.nan
    with pytest.raises(r.DegenerateNormalsError):
        r.estimate_sh_from_subject_normals(np.full((8, 8, 3), 0.4), bad, np.ones((8, 8)))


def test_relight_mask_shape_mismatch_refused() -> None:
    probe = r.ambient_probe(0.2)
    with pytest.raises(r.DegenerateNormalsError):
        r.relight_subject_to_environment(
            np.full((8, 8, 3), 0.4),
            _hemisphere_normals(8, 8),
            np.ones((7, 8)),
            source_probe=probe,
            target_probe=probe,
        )


def test_cook_torrance_rejects_non_unit_light_dir() -> None:
    normals = _hemisphere_normals(8, 8)
    with pytest.raises(r.RelightingError):
        r.cook_torrance_specular(normals, (0.0, 0.0, 1.0), (0.0, 0.0, 2.0))


def test_relight_rejects_bad_rgb_shape() -> None:
    probe = r.ambient_probe(0.2)
    with pytest.raises(r.RelightingError):
        r.relight_subject_to_environment(
            np.zeros((8, 8, 4)),
            _hemisphere_normals(8, 8),
            np.ones((8, 8)),
            source_probe=probe,
            target_probe=probe,
        )


def test_estimate_sh_singular_system_raises() -> None:
    # A normal map constant along the masked rows makes the Gram matrix
    # rank-deficient; with zero ridge the solve must be reported as singular.
    normals = np.tile(np.array([0.0, 0.0, 1.0]), (8, 8, 1))
    subject = np.full((8, 8, 3), 0.4)
    with pytest.raises(r.SingularSystemError):
        r.estimate_sh_from_subject_normals(subject, normals, np.ones((8, 8)), ridge_lambda=0.0)


def test_non_trailing_axis_normals_refused() -> None:
    with pytest.raises(r.DegenerateNormalsError):
        r.sh_basis(np.zeros((4, 4)))
