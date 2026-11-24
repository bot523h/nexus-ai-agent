"""Wave 5: biophysical portrait inversion and Bures-Wasserstein transport."""

from __future__ import annotations

import importlib
import math

import pytest

from nexus_ai_agent.creative.packs.vision import biophysical_portrait as portrait
from nexus_ai_agent.creative.packs.vision import scene_optimal_transport as transport
from nexus_ai_agent.creative.packs.vision.biophysical_portrait import (
    HueProtectionResult,
    Oklch,
    SkinChromophoreEstimate,
    SkinModelError,
    enforce_skin_hue_protection,
    estimate_chromophores,
    linear_srgb_to_oklab,
    linear_srgb_to_oklch,
    oklab_to_linear_srgb,
    oklch_to_linear_srgb,
    reflectance_from_chromophores,
    skin_luminance,
)
from nexus_ai_agent.creative.packs.vision.scene_optimal_transport import (
    GaussianColorModel,
    OptimalTransportError,
    OptimalTransportResult,
    apply_transport_map,
    bures_wasserstein_distance_sq,
    geodesic_covariance,
    hurter_driffield_density,
    jacobi_eigh,
    matrix_inverse_spd,
    matrix_inverse_sqrt_spd,
    matrix_sqrt_psd,
    optimal_transport_map,
)

IDENTITY = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def _close(first: tuple[tuple[float, ...], ...], second: tuple[tuple[float, ...], ...]) -> bool:
    return all(
        abs(first[row][column] - second[row][column]) < 1e-5
        for row in range(3)
        for column in range(3)
    )


# ---------------------------------------------------------------- portrait


def test_chromophore_roundtrip() -> None:
    reflectance = reflectance_from_chromophores(0.42, 0.31)
    estimate = estimate_chromophores(reflectance)
    assert estimate.melanin == pytest.approx(0.42, abs=1e-6)
    assert estimate.hemoglobin == pytest.approx(0.31, abs=1e-6)
    assert estimate.residual_norm < 1e-6
    assert estimate.clamped is False
    assert len(estimate.reconstructed_optical_density) == 6


def test_chromophore_zero_and_clamping() -> None:
    baseline_only = reflectance_from_chromophores(0.0, 0.0)
    estimate = estimate_chromophores(baseline_only)
    assert estimate.melanin == pytest.approx(0.0, abs=1e-8)
    assert estimate.hemoglobin == pytest.approx(0.0, abs=1e-8)
    # Optical density below baseline forces a negative projection, clamped to 0.
    lower = tuple(
        math.exp(-(base - 0.2 * mel))
        for base, mel in zip(
            portrait.BASELINE_OPTICAL_DENSITY, portrait.MELANIN_EXTINCTION, strict=True
        )
    )
    clamped = estimate_chromophores(lower)
    assert clamped.clamped is True
    assert clamped.melanin == 0.0
    assert clamped.residual_norm > 0.0


def test_chromophore_validation() -> None:
    with pytest.raises(SkinModelError):
        estimate_chromophores([])
    with pytest.raises(SkinModelError):
        estimate_chromophores([0.5] * 5)
    with pytest.raises(SkinModelError):
        estimate_chromophores([-0.1] + [0.5] * 5)
    with pytest.raises(SkinModelError):
        estimate_chromophores([1.1] + [0.5] * 5)
    with pytest.raises(SkinModelError):
        estimate_chromophores([math.nan] + [0.5] * 5)
    with pytest.raises(SkinModelError):
        estimate_chromophores([True] + [0.5] * 5)  # type: ignore[list-item]
    with pytest.raises(SkinModelError):
        estimate_chromophores([0.5] * 6, ridge=-1.0)
    with pytest.raises(SkinModelError):
        estimate_chromophores([0.0] * 6, ridge=math.nan)
    with pytest.raises(SkinModelError):
        estimate_chromophores([0.5] * 6, wavelengths_nm=[0.0] * 6)
    with pytest.raises(SkinModelError):
        estimate_chromophores([0.5] * 6, wavelengths_nm=[math.inf] * 6)
    with pytest.raises(SkinModelError):
        reflectance_from_chromophores(-0.1, 0.2)
    with pytest.raises(SkinModelError):
        reflectance_from_chromophores(0.1, -0.2)
    with pytest.raises(SkinModelError):
        reflectance_from_chromophores(0.1, 0.2, baseline=(0.1,))
    explicit = estimate_chromophores(
        reflectance_from_chromophores(0.2, 0.1), wavelengths_nm=(450, 500, 550, 600, 650, 700)
    )
    assert explicit.melanin == pytest.approx(0.2, abs=1e-6)

    with pytest.raises(SkinModelError):
        SkinChromophoreEstimate(-1.0, 0.0, 0.0, (1.0,), False)
    with pytest.raises(SkinModelError):
        SkinChromophoreEstimate(0.0, math.nan, 0.0, (1.0,), False)
    with pytest.raises(SkinModelError):
        SkinChromophoreEstimate(0.0, 0.0, 0.0, (), False)
    with pytest.raises(SkinModelError):
        SkinChromophoreEstimate(0.0, 0.0, 0.0, (math.nan,), False)
    with pytest.raises(TypeError):
        SkinChromophoreEstimate(0.0, 0.0, 0.0, (1.0,), 1)  # type: ignore[arg-type]


def test_degenerate_spectral_basis(monkeypatch: pytest.MonkeyPatch) -> None:
    implementation = importlib.import_module(
        "nexus_ai_agent.creative.temporal.biophysical_portrait"
    )
    monkeypatch.setattr(implementation, "MELANIN_EXTINCTION", (1.0,) * 6)
    monkeypatch.setattr(implementation, "HEMOGLOBIN_EXTINCTION", (1.0,) * 6)
    with pytest.raises(SkinModelError):
        estimate_chromophores([0.5] * 6, ridge=0.0)


def test_oklab_known_colours() -> None:
    white = linear_srgb_to_oklab((1.0, 1.0, 1.0))
    assert white[0] == pytest.approx(1.0, abs=1e-4)
    assert white[1] == pytest.approx(0.0, abs=1e-4)
    assert white[2] == pytest.approx(0.0, abs=1e-4)
    black = linear_srgb_to_oklab((0.0, 0.0, 0.0))
    assert black == pytest.approx((0.0, 0.0, 0.0), abs=1e-9)
    red = linear_srgb_to_oklab((1.0, 0.0, 0.0))
    assert red[0] == pytest.approx(0.6279, abs=1e-3)
    assert red[1] == pytest.approx(0.2249, abs=1e-3)
    assert red[2] == pytest.approx(0.1258, abs=1e-3)


def test_oklab_roundtrip_and_validation() -> None:
    for colour in ((0.2, 0.4, 0.6), (0.9, 0.1, 0.3), (0.05, 0.95, 0.5)):
        lab = linear_srgb_to_oklab(colour)
        back = oklab_to_linear_srgb(lab)
        assert back == pytest.approx(colour, abs=1e-6)
    with pytest.raises(SkinModelError):
        linear_srgb_to_oklab((1.0, 1.0))
    with pytest.raises(SkinModelError):
        linear_srgb_to_oklab((1.2, 0.0, 0.0))
    with pytest.raises(SkinModelError):
        linear_srgb_to_oklab((math.nan, 0.0, 0.0))
    with pytest.raises(SkinModelError):
        oklab_to_linear_srgb((1.0, 0.0))
    with pytest.raises(SkinModelError):
        oklab_to_linear_srgb((math.nan, 0.0, 0.0))


def test_oklch_conversion() -> None:
    lch = linear_srgb_to_oklch((1.0, 0.0, 0.0))
    assert 0.0 <= lch.hue_deg < 360.0
    back = oklch_to_linear_srgb(lch)
    assert back == pytest.approx((1.0, 0.0, 0.0), abs=1e-6)
    with pytest.raises(SkinModelError):
        Oklch(math.nan, 0.0, 0.0)
    with pytest.raises(SkinModelError):
        Oklch(0.5, -0.1, 0.0)
    with pytest.raises(SkinModelError):
        Oklch(0.5, 0.1, math.nan)


def test_skin_luminance() -> None:
    assert skin_luminance((0.0, 0.0, 0.0)) == 0.0
    assert skin_luminance((1.0, 1.0, 1.0)) == pytest.approx(1.0)
    with pytest.raises(SkinModelError):
        skin_luminance((1.0, 1.0))
    with pytest.raises(SkinModelError):
        skin_luminance((2.0, 0.0, 0.0))


def test_gamma_linear_validation() -> None:
    with pytest.raises(SkinModelError):
        portrait._gamma_to_linear((0.0, 0.0))
    with pytest.raises(SkinModelError):
        portrait._gamma_to_linear((1.5, 0.0, 0.0))


def test_hue_protection_allows_small_shift() -> None:
    colour = (0.80, 0.55, 0.45)
    result = enforce_skin_hue_protection(colour, colour)
    assert result.allowed is True
    assert result.delta_deg == pytest.approx(0.0, abs=1e-9)
    assert result.adjusted_rgb == pytest.approx(colour)
    grey = enforce_skin_hue_protection(colour, (0.6, 0.6, 0.6))
    assert grey.allowed is True


def test_hue_protection_clamps_large_shift() -> None:
    original = (0.85, 0.55, 0.45)
    graded = (0.20, 0.85, 0.35)
    result = enforce_skin_hue_protection(original, graded, max_delta_deg=10.0)
    assert result.allowed is False
    assert result.delta_deg > 10.0
    applied = abs(portrait._shortest_angle(result.applied_hue_deg, result.original_hue_deg))
    assert applied == pytest.approx(10.0, abs=1e-6)
    assert all(0.0 <= value <= 1.0 for value in result.adjusted_rgb)
    with pytest.raises(SkinModelError):
        enforce_skin_hue_protection(original, graded, max_delta_deg=0.0)
    with pytest.raises(SkinModelError):
        enforce_skin_hue_protection(original, graded, max_delta_deg=181.0)


def test_hue_protection_result_validation() -> None:
    HueProtectionResult(10.0, 20.0, 20.0, 10.0, True, (0.5, 0.5, 0.5))
    with pytest.raises(SkinModelError):
        HueProtectionResult(math.nan, 20.0, 20.0, 10.0, True, (0.5, 0.5, 0.5))
    with pytest.raises(SkinModelError):
        HueProtectionResult(10.0, 20.0, 20.0, -1.0, True, (0.5, 0.5, 0.5))
    with pytest.raises(SkinModelError):
        HueProtectionResult(10.0, 20.0, 20.0, 181.0, True, (0.5, 0.5, 0.5))
    with pytest.raises(TypeError):
        HueProtectionResult(10.0, 20.0, 20.0, 10.0, 1, (0.5, 0.5, 0.5))  # type: ignore[arg-type]
    with pytest.raises(SkinModelError):
        HueProtectionResult(10.0, 20.0, 20.0, 10.0, True, (1.5, 0.5, 0.5))


# ---------------------------------------------------------------- optimal transport


def test_jacobi_and_matrix_functions() -> None:
    diagonal = ((4.0, 0.0, 0.0), (0.0, 9.0, 0.0), (0.0, 0.0, 16.0))
    assert _close(matrix_sqrt_psd(diagonal), ((2.0, 0.0, 0.0), (0.0, 3.0, 0.0), (0.0, 0.0, 4.0)))
    assert _close(
        matrix_inverse_spd(diagonal),
        ((0.25, 0.0, 0.0), (0.0, 1.0 / 9.0, 0.0), (0.0, 0.0, 1.0 / 16.0)),
    )
    assert _close(
        matrix_inverse_sqrt_spd(diagonal),
        ((0.5, 0.0, 0.0), (0.0, 1.0 / 3.0, 0.0), (0.0, 0.0, 0.25)),
    )
    unsymmetric = ((1.0, 0.5, 0.0), (0.5, 1.0, 0.0), (0.0, 0.0, 1.0))
    values, vectors = jacobi_eigh(unsymmetric)
    assert sorted(values) == pytest.approx([0.5, 1.0, 1.5])
    rebuilt = transport._matmul(
        vectors,
        transport._matmul(
            ((values[0], 0.0, 0.0), (0.0, values[1], 0.0), (0.0, 0.0, values[2])),
            transport._matmul(
                (
                    (vectors[0][0], vectors[1][0], vectors[2][0]),
                    (vectors[0][1], vectors[1][1], vectors[2][1]),
                    (vectors[0][2], vectors[1][2], vectors[2][2]),
                ),
                IDENTITY,
            ),
        ),
    )
    assert _close(rebuilt, unsymmetric)
    root = matrix_sqrt_psd(unsymmetric)
    assert _close(transport._matmul(root, root), unsymmetric)


def test_matrix_error_paths() -> None:
    with pytest.raises(OptimalTransportError):
        matrix_sqrt_psd(((1.0, 0.0), (0.0, 1.0)))
    with pytest.raises(OptimalTransportError):
        matrix_sqrt_psd(((1.0, 1.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)))
    with pytest.raises(OptimalTransportError):
        matrix_sqrt_psd(((True, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)))
    with pytest.raises(OptimalTransportError):
        matrix_sqrt_psd(((math.nan, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)))
    with pytest.raises(OptimalTransportError):
        matrix_sqrt_psd(((-1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)))
    with pytest.raises(OptimalTransportError):
        matrix_inverse_spd(((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)))
    with pytest.raises(OptimalTransportError):
        matrix_inverse_sqrt_spd(((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)))


def test_optimal_transport_identity_and_scaling() -> None:
    mean = (0.5, 0.5, 0.5)
    source = GaussianColorModel(mean, IDENTITY)
    identity = optimal_transport_map(source, source)
    assert _close(identity.map_matrix, IDENTITY)
    assert identity.wasserstein_sq == pytest.approx(0.0, abs=1e-9)
    assert identity.apply((0.7, 0.2, 0.1)) == pytest.approx((0.7, 0.2, 0.1))

    scaled_target = GaussianColorModel(mean, ((4.0, 0.0, 0.0), (0.0, 4.0, 0.0), (0.0, 0.0, 4.0)))
    scaled = optimal_transport_map(source, scaled_target)
    assert _close(scaled.map_matrix, ((2.0, 0.0, 0.0), (0.0, 2.0, 0.0), (0.0, 0.0, 2.0)))
    assert scaled.wasserstein_sq == pytest.approx(3.0, abs=1e-4)
    assert scaled.apply((1.0, 1.0, 1.0)) == pytest.approx((1.5, 1.5, 1.5))

    shifted = GaussianColorModel((1.0, 2.0, 3.0), IDENTITY)
    moved = optimal_transport_map(source, shifted)
    assert moved.apply((0.5, 0.5, 0.5)) == pytest.approx((1.0, 2.0, 3.0))
    assert apply_transport_map(moved, (0.5, 0.5, 0.5)) == pytest.approx((1.0, 2.0, 3.0))


def test_bures_distance_symmetry_and_geodesic() -> None:
    first = ((1.0, 0.2, 0.0), (0.2, 2.0, 0.1), (0.0, 0.1, 3.0))
    second = ((2.0, 0.1, 0.0), (0.1, 1.0, 0.2), (0.0, 0.2, 4.0))
    assert bures_wasserstein_distance_sq(first, second) == pytest.approx(
        bures_wasserstein_distance_sq(second, first), abs=1e-8
    )
    start = geodesic_covariance(first, second, 0.0)
    end = geodesic_covariance(first, second, 1.0)
    assert _close(start, first)
    assert _close(end, second)
    middle = geodesic_covariance(first, second, 0.5)
    eigenvalues, _vectors = jacobi_eigh(middle)
    assert min(eigenvalues) > 0.0
    with pytest.raises(OptimalTransportError):
        geodesic_covariance(first, second, -0.1)
    with pytest.raises(OptimalTransportError):
        geodesic_covariance(first, second, math.nan)


def test_gaussian_and_result_validation() -> None:
    with pytest.raises(OptimalTransportError):
        GaussianColorModel((0.0, 0.0), IDENTITY)  # type: ignore[arg-type]
    with pytest.raises(OptimalTransportError):
        GaussianColorModel((0.0, 0.0, 0.0), ((1.0, 0.5, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)))
    model = GaussianColorModel((0.0, 0.0, 0.0), IDENTITY)
    regular = model.regularized(0.5)
    assert regular.covariance[0][0] == pytest.approx(1.5)
    with pytest.raises(OptimalTransportError):
        model.regularized(-0.1)
    with pytest.raises(OptimalTransportError):
        model.regularized(math.nan)

    result = optimal_transport_map(model, model)
    with pytest.raises(TypeError):
        OptimalTransportResult("x", model, IDENTITY, 0.0)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        OptimalTransportResult(model, "x", IDENTITY, 0.0)  # type: ignore[arg-type]
    with pytest.raises(OptimalTransportError):
        OptimalTransportResult(model, model, IDENTITY, math.nan)
    with pytest.raises(OptimalTransportError):
        OptimalTransportResult(model, model, IDENTITY, -1.0)
    with pytest.raises(TypeError):
        apply_transport_map("x", (0.0, 0.0, 0.0))  # type: ignore[arg-type]
    with pytest.raises(OptimalTransportError):
        apply_transport_map(result, (0.0, 0.0))
    with pytest.raises(OptimalTransportError):
        apply_transport_map(result, (True, 0.0, 0.0))
    with pytest.raises(OptimalTransportError):
        apply_transport_map(result, (math.nan, 0.0, 0.0))
    with pytest.raises(OptimalTransportError):
        optimal_transport_map(model, model, regularization=-1.0)


def test_hurter_driffield_response() -> None:
    assert hurter_driffield_density(1e12) == pytest.approx(2.0, abs=1e-6)
    assert hurter_driffield_density(1e-12) == pytest.approx(0.05, abs=1e-6)
    low = hurter_driffield_density(0.5)
    high = hurter_driffield_density(2.0)
    assert low < high
    with pytest.raises(OptimalTransportError):
        hurter_driffield_density(0.0)
    with pytest.raises(OptimalTransportError):
        hurter_driffield_density(math.nan)
    with pytest.raises(OptimalTransportError):
        hurter_driffield_density(1.0, gamma=0.0)
    with pytest.raises(OptimalTransportError):
        hurter_driffield_density(1.0, d_max=0.01)
    with pytest.raises(OptimalTransportError):
        hurter_driffield_density(1.0, h0=-1.0)
