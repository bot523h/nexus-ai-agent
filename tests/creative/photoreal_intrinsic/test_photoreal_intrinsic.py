from dataclasses import replace
from typing import cast

import numpy as np
import pytest

from nexus_ai_agent.creative.photoreal_intrinsic import (
    IntrinsicBiophysicalVault,
    LogChromophoreTomographyBaker,
    OcularCausticsAndFilmChemistry,
    RiemannianStrainWrinkleTensor,
    ScreenedPoissonSkinTransport,
    dctn,
    idctn,
)


def _orthogonal_matrix(n: int) -> np.ndarray:
    i = np.arange(n, dtype=np.float64)[:, None]
    k = np.arange(n, dtype=np.float64)[None, :]
    matrix = np.cos(np.pi * (i + 0.5) * k / n) * np.sqrt(2 / n)
    matrix[:, 0] = 1 / np.sqrt(n)
    return cast(np.ndarray, matrix)


def test_dct_and_idct_match_independent_explicit_matrix() -> None:
    rng = np.random.default_rng(8)
    image = rng.normal(size=(5, 7))
    left, right = _orthogonal_matrix(5), _orthogonal_matrix(7)
    reference = left.T @ image @ right
    assert np.allclose(dctn(image), reference, atol=1e-12)
    assert np.allclose(idctn(reference), image, atol=1e-12)


def test_green_lagrange_linear_strain_is_analytic() -> None:
    eps = 0.1
    _, x = np.mgrid[:8, :9]
    flow = np.zeros((8, 9, 2), dtype=np.float64)
    flow[..., 0] = eps * x
    result = RiemannianStrainWrinkleTensor().analyze(flow)
    assert np.allclose(result["E_xx"], eps + 0.5 * eps**2, atol=1e-12)


def test_eigenvectors_diagonalize_full_strain_tensor() -> None:
    rng = np.random.default_rng(91)
    flow = rng.normal(scale=0.15, size=(9, 11, 2))
    result = RiemannianStrainWrinkleTensor().analyze(flow)
    e = np.empty((*flow.shape[:2], 2, 2))
    e[..., 0, 0], e[..., 1, 1] = result["E_xx"], result["E_yy"]
    e[..., 0, 1] = e[..., 1, 0] = result["E_xy"]
    for index, epsilon in ((1, "epsilon_1"), (2, "epsilon_2")):
        vector = result[f"eigenvector_{index}"]
        assert np.allclose(
            np.einsum("...ij,...j->...i", e, vector),
            result[epsilon][..., None] * vector,
            atol=1e-12,
        )


def test_surface_metric_and_wrinkle_normals() -> None:
    y, x = np.mgrid[:12, :13]
    flow = np.zeros((12, 13, 2), dtype=np.float64)
    flow[..., 0] = 0.1 * x
    depth = 0.1 * np.sin(x / 2) * np.cos(y / 3)
    mechanics = RiemannianStrainWrinkleTensor().analyze(flow, surface_depth=depth)
    flat = RiemannianStrainWrinkleTensor().analyze(flow)
    assert np.max(np.abs(mechanics["epsilon_1"] - flat["epsilon_1"])) > 0
    wrinkle = np.zeros((12, 13))
    wrinkle[:, 6:] = 1
    normals = np.zeros((12, 13, 3))
    normals[..., 2] = 1
    tilted = RiemannianStrainWrinkleTensor.perturb_normals(normals, wrinkle)
    assert np.max(np.abs(tilted - normals)) > 0


def test_compile_responds_to_reversed_normals() -> None:
    baker = LogChromophoreTomographyBaker()
    key = b"for-test-only"
    vault = baker.bake(baker.canonical_archetype(12, 12), hmac_key=key)
    candidate = IntrinsicBiophysicalVault(
        vault.melanin,
        vault.hemoglobin,
        vault.shading,
        vault.albedo,
        -vault.normals,
        vault.depth,
        replace(vault.seal, digest=""),
        vault.metadata,
    )
    signed = replace(candidate, seal=baker._create_seal(candidate, key))
    a = OcularCausticsAndFilmChemistry.compile(vault, hmac_key=key)["float32"]
    b = OcularCausticsAndFilmChemistry.compile(signed, hmac_key=key)["float32"]
    assert isinstance(a, np.ndarray) and isinstance(b, np.ndarray)
    assert np.mean(np.abs(a - b)) > 0.05


@pytest.mark.parametrize("channels", [None, 3])
@pytest.mark.parametrize("dx", [0.0, 0.5])
def test_warp_gray_and_rgb(channels: int | None, dx: float) -> None:
    base = np.arange(20, dtype=np.float32).reshape(4, 5)
    image = base if channels is None else np.stack((base, 2 * base, 3 * base), axis=-1)
    flow = np.zeros((4, 5, 2), dtype=np.float32)
    flow[..., 0] = dx
    warped = RiemannianStrainWrinkleTensor.warp(image, flow)
    assert warped.shape == image.shape
    expected = image if dx == 0 else (image[:, :-1] + image[:, 1:]) / 2
    assert np.allclose(warped if dx == 0 else warped[:, :-1], expected)


def test_uint8_warp_uses_round_to_nearest() -> None:
    image = np.array([[0, 1], [0, 1]], dtype=np.uint8)
    flow = np.zeros((2, 2, 2), dtype=np.float64)
    flow[..., 0] = 0.5
    result = RiemannianStrainWrinkleTensor.warp(image, flow)
    assert result[0, 0] == 1


@pytest.mark.parametrize("dtype", [np.int8, np.int16])
def test_bake_rejects_signed_integers(dtype: type[np.signedinteger]) -> None:
    with pytest.raises(ValueError, match="Signed integer dtypes are not supported"):
        LogChromophoreTomographyBaker().bake(np.ones((3, 3, 3), dtype=dtype))


def test_float_hdr_and_integer_bake_scaling() -> None:
    baker = LogChromophoreTomographyBaker()
    hdr = np.broadcast_to(np.array([1.6, 0.9, 0.5], np.float32), (3, 4, 3)).copy()
    vault = baker.bake(hdr)
    assert np.allclose(vault.albedo * vault.shading[..., None], hdr, atol=1e-6)
    assert not np.allclose(vault.shading, baker.bake(hdr / 255).shading)
    integer = baker.bake(np.full((3, 4, 3), 128, dtype=np.uint8))
    assert np.isclose(integer.shading[0, 0], ((128 / 255 + 0.055) / 1.055) ** 2.4, atol=1e-6)


def test_hmac_metadata_and_key_are_not_exposed() -> None:
    key = b"private external test key"
    vault = LogChromophoreTomographyBaker().bake(
        LogChromophoreTomographyBaker.canonical_archetype(5, 5), hmac_key=key
    )
    assert vault.verify(key) and not vault.verify(b"wrong")
    assert not hasattr(vault, "hmac_key")
    assert vault.seal.algorithm == "HMAC-SHA256" and len(vault.seal.key_fingerprint) == 8
    vault.albedo.setflags(write=True)
    vault.albedo[0, 0, 0] += 0.1
    assert not vault.verify(key)


def test_compile_responds_to_flow_heart_rate_and_include_caustics() -> None:
    vault = LogChromophoreTomographyBaker().bake(
        LogChromophoreTomographyBaker.canonical_archetype(10, 10)
    )
    flow = np.zeros((10, 10, 2), np.float32)
    flow[..., 0] = 0.3
    base = OcularCausticsAndFilmChemistry.compile(vault, include_caustics=False)["float32"]
    shifted = OcularCausticsAndFilmChemistry.compile(
        vault, flow=flow, heart_rate_bpm=130, include_caustics=False
    )["float32"]
    assert isinstance(base, np.ndarray) and isinstance(shifted, np.ndarray)
    assert np.mean(np.abs(base - shifted)) > 0


def test_transport_radii_validation_and_no_periodic_wrap() -> None:
    solver = ScreenedPoissonSkinTransport()
    source = np.zeros((16, 16, 3), np.float32)
    source[:, 0, :] = 1
    for bad in ((), (1.0, 2.0), (0.0,), (-1.0,), (float("nan"),)):
        with pytest.raises(ValueError):
            solver.transport(source, bad)
    out = solver.transport(source)
    assert out[:, -1, :].max() < out[:, 1, :].max()


def test_neumann_screened_poisson_constant_is_fixed_point() -> None:
    source = np.full((5, 7, 3), 0.37, dtype=np.float32)
    out = ScreenedPoissonSkinTransport().transport(source)
    assert np.allclose(out, source, atol=1e-6)


def test_caustic_left_light_energy_is_on_opposite_half() -> None:
    caustic = OcularCausticsAndFilmChemistry.caustic((101, 101), light_x=-1)
    assert caustic[:, 51:].sum() > caustic[:, :50].sum()


def test_hmac_empty_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        LogChromophoreTomographyBaker().bake(
            LogChromophoreTomographyBaker.canonical_archetype(3, 3), hmac_key=b""
        )


def test_environment_hmac_key_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_PHOTOREAL_HMAC_KEY", "environment secret")
    vault = LogChromophoreTomographyBaker().bake(
        LogChromophoreTomographyBaker.canonical_archetype(3, 3)
    )
    assert vault.verify()
    monkeypatch.setenv("NEXUS_PHOTOREAL_HMAC_KEY", "changed secret")
    assert not vault.verify()


def test_bake_rejects_small_spatial_dimensions_and_invalid_float() -> None:
    baker = LogChromophoreTomographyBaker()
    with pytest.raises(ValueError, match="at least 2x2"):
        baker.bake(np.ones((1, 4, 3), dtype=np.float32))
    with pytest.raises(ValueError, match="finite and nonnegative"):
        baker.bake(np.full((2, 2, 3), -0.1, dtype=np.float32))
    with pytest.raises(ValueError, match="dtype"):
        baker.bake(np.full((2, 2, 3), "x"))


def test_compile_rejects_forged_signature() -> None:
    baker = LogChromophoreTomographyBaker()
    vault = baker.bake(baker.canonical_archetype(4, 4))
    forged = replace(vault, seal=replace(vault.seal, signature="0" * 64))
    with pytest.raises(ValueError, match="Invalid vault seal"):
        OcularCausticsAndFilmChemistry.compile(forged)


def test_bad_surface_depth_and_warp_inputs_are_rejected() -> None:
    flow = np.zeros((4, 4, 2))
    with pytest.raises(ValueError, match="surface_depth"):
        RiemannianStrainWrinkleTensor().analyze(flow, surface_depth=np.zeros((3, 4)))
    with pytest.raises(ValueError, match="normals"):
        RiemannianStrainWrinkleTensor.perturb_normals(np.zeros((4, 4, 2)), np.zeros((4, 4)))
    with pytest.raises(ValueError, match="image"):
        RiemannianStrainWrinkleTensor.warp(np.zeros((4, 4, 1, 1)), np.zeros((4, 4, 2)))
    with pytest.raises(ValueError, match="flow"):
        RiemannianStrainWrinkleTensor.warp(np.zeros((4, 4)), np.full((4, 4, 2), np.nan))


def test_transport_and_lighting_validation_paths() -> None:
    solver = ScreenedPoissonSkinTransport()
    with pytest.raises(ValueError, match="finite"):
        solver.shade(np.ones((2, 2, 3)), heart_rate_bpm=float("nan"))
    with pytest.raises(ValueError, match="supported"):
        solver.shade(np.ones((2, 2, 3)), heart_rate_bpm=0)
    gray = solver.shade(np.ones((3, 4, 1)), radii=(1.0,))
    assert gray.shape == (3, 4, 1)
    normals = np.zeros((2, 2, 3), np.float32)
    normals[..., 2] = 1
    with pytest.raises(ValueError, match="symmetric"):
        solver.spherical_harmonic(normals, np.triu(np.ones((4, 4), np.float32)))


def test_compiler_chromophore_and_pulse_inputs_change_output() -> None:
    baker = LogChromophoreTomographyBaker()
    vault = baker.bake(baker.canonical_archetype(8, 8), hmac_key=b"compiler-key")
    base = OcularCausticsAndFilmChemistry.compile(
        vault, hmac_key=b"compiler-key", t=0.37, include_caustics=False
    )["float32"]
    altered_mel = np.array(vault.melanin, copy=True) + 2
    altered = replace(vault, melanin=altered_mel, seal=replace(vault.seal, digest="", signature=""))
    altered = replace(altered, seal=baker._create_seal(altered, b"compiler-key"))
    changed = OcularCausticsAndFilmChemistry.compile(
        altered, hmac_key=b"compiler-key", t=0.37, include_caustics=False
    )["float32"]
    pulse = OcularCausticsAndFilmChemistry.compile(
        vault, hmac_key=b"compiler-key", t=0.37, heart_rate_bpm=140, include_caustics=False
    )["float32"]
    hb_candidate = IntrinsicBiophysicalVault(
        vault.melanin,
        vault.hemoglobin + 2,
        vault.shading,
        vault.albedo,
        vault.normals,
        vault.depth,
        replace(vault.seal, digest="", signature=""),
        vault.metadata,
    )
    hb_candidate = replace(hb_candidate, seal=baker._create_seal(hb_candidate, b"compiler-key"))
    hb_changed = OcularCausticsAndFilmChemistry.compile(
        hb_candidate, hmac_key=b"compiler-key", t=0.37, include_caustics=False
    )["float32"]
    assert isinstance(base, np.ndarray) and isinstance(changed, np.ndarray)
    assert isinstance(pulse, np.ndarray) and isinstance(hb_changed, np.ndarray)
    assert np.mean(np.abs(base - changed)) > 0
    assert np.mean(np.abs(base - pulse)) > 0
    assert np.mean(np.abs(base - hb_changed)) > 0


def test_default_key_environment_fallbacks_to_module_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NEXUS_PHOTOREAL_HMAC_KEY", raising=False)
    vault = LogChromophoreTomographyBaker().bake(
        LogChromophoreTomographyBaker.canonical_archetype(3, 3)
    )
    assert vault.verify()


def test_singleton_inverse_dct_and_validation_branches() -> None:
    singleton = np.array([[2.5]], dtype=np.float64)
    assert np.allclose(idctn(dctn(singleton)), singleton, atol=1e-12)
    with pytest.raises(ValueError, match="at least 2x2"):
        OcularCausticsAndFilmChemistry.caustic((1, 8))
    with pytest.raises(ValueError, match="three components"):
        ScreenedPoissonSkinTransport.spherical_harmonic(np.zeros((2, 2, 2)))
    normals = np.zeros((2, 2, 3), dtype=np.float32)
    assert ScreenedPoissonSkinTransport.spherical_harmonic(
        normals, np.eye(4, dtype=np.float32)
    ).shape == (2, 2)


def test_transport_radii_conversion_error() -> None:
    with pytest.raises(ValueError, match="one-dimensional sequence"):
        ScreenedPoissonSkinTransport().transport(np.ones((3, 3, 3)), ("invalid",))  # type: ignore[arg-type]
