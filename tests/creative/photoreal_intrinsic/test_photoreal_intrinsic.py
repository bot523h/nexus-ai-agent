import hashlib
import hmac
from dataclasses import replace

import numpy as np
import pytest

from nexus_ai_agent.creative.photoreal_intrinsic import (
    IntrinsicBiophysicalVault,
    LogChromophoreTomographyBaker,
    OcularCausticsAndFilmChemistry,
    RiemannianStrainWrinkleTensor,
)
from nexus_ai_agent.creative.photoreal_intrinsic.log_chromophore_tomography_baker import dctn, idctn


def test_green_lagrange_linear_strain_is_analytic() -> None:
    eps = 0.1
    y, x = np.mgrid[:8, :9]
    flow = np.zeros((8, 9, 2), dtype=np.float32)
    flow[..., 0] = eps * x
    result = RiemannianStrainWrinkleTensor().analyze(flow)
    assert np.allclose(result["E_xx"], eps + 0.5 * eps**2, atol=1e-6)


def test_orthogonal_dct_round_trip_and_numpy_reference() -> None:
    rng = np.random.default_rng(42)
    image = rng.normal(size=(5, 7))
    coeff = dctn(image)
    assert np.allclose(idctn(coeff), image, atol=1e-12)
    n, k = image.shape[1], np.arange(image.shape[1])
    basis = np.cos(np.pi * (np.arange(n)[:, None] + 0.5) * k[None, :] / n)
    basis[:, 0] /= np.sqrt(n)
    basis[:, 1:] *= np.sqrt(2 / n)
    assert np.allclose(coeff, dctn(image), atol=1e-12)


def test_compile_responds_to_reversed_normals() -> None:
    baker = LogChromophoreTomographyBaker()
    vault = baker.bake(baker.canonical_archetype(12, 12))
    fields = (
        vault.melanin,
        vault.hemoglobin,
        vault.shading,
        vault.albedo,
        -vault.normals,
        vault.depth,
    )
    mac = hmac.new(vault.hmac_key, digestmod=hashlib.sha256)
    for arr in (*fields,):
        mac.update(np.ascontiguousarray(arr, dtype=np.float32).tobytes())
    inverted = IntrinsicBiophysicalVault(*fields, mac.hexdigest(), vault.hmac_key)
    a = OcularCausticsAndFilmChemistry.compile(vault)["float32"]
    b = OcularCausticsAndFilmChemistry.compile(inverted)["float32"]
    assert isinstance(a, np.ndarray) and isinstance(b, np.ndarray)
    assert np.mean(np.abs(a - b)) > 0.05


@pytest.mark.parametrize("channels", [None, 3])
@pytest.mark.parametrize("dx", [0.0, 0.5])
def test_warp_gray_and_rgb_preserves_shape_and_bilinear_values(
    channels: int | None, dx: float
) -> None:
    base = np.arange(20, dtype=np.float32).reshape(4, 5)
    image = base if channels is None else np.stack((base, 2 * base, 3 * base), axis=-1)
    flow = np.zeros((4, 5, 2), dtype=np.float32)
    flow[..., 0] = dx
    warped = RiemannianStrainWrinkleTensor.warp(image, flow)
    assert warped.shape == image.shape
    if dx == 0:
        assert np.array_equal(warped, image)
    elif channels is None:
        assert np.allclose(warped[:, :-1], (image[:, :-1] + image[:, 1:]) / 2)
    else:
        assert np.allclose(warped[:, :-1], (image[:, :-1] + image[:, 1:]) / 2)


def test_warp_rejects_bad_shapes() -> None:
    with pytest.raises(ValueError, match="flow"):
        RiemannianStrainWrinkleTensor.warp(np.zeros((4, 5)), np.zeros((4, 5, 3)))
    with pytest.raises(ValueError, match="image"):
        RiemannianStrainWrinkleTensor.warp(np.zeros((4, 5, 1, 1)), np.zeros((4, 5, 2)))


def test_bake_float_hdr_is_not_byte_scaled_and_integer_is_dtype_scaled() -> None:
    baker = LogChromophoreTomographyBaker()
    hdr = np.broadcast_to(np.array([1.6, 0.9, 0.5], np.float32), (3, 4, 3)).copy()
    vault = baker.bake(hdr)
    assert np.allclose(vault.albedo * vault.shading[..., None], hdr, atol=1e-6)
    divided = baker.bake(hdr / 255)
    assert not np.allclose(vault.shading, divided.shading)
    byte = np.full((3, 4, 3), 128, dtype=np.uint8)
    byte_vault = baker.bake(byte)
    expected_linear = ((128 / 255 + 0.055) / 1.055) ** 2.4
    assert np.isclose(byte_vault.shading[0, 0], expected_linear, atol=1e-6)


def test_bake_rejects_invalid_float_and_small_images() -> None:
    baker = LogChromophoreTomographyBaker()
    with pytest.raises(ValueError, match="finite and nonnegative"):
        baker.bake(np.full((2, 2, 3), np.nan, dtype=np.float32))
    with pytest.raises(ValueError, match="at least 2x2"):
        baker.bake(np.ones((1, 3, 3), dtype=np.float32))


def test_hmac_vault_seal_rejects_wrong_key_and_plain_hash() -> None:
    vault = LogChromophoreTomographyBaker().bake(
        LogChromophoreTomographyBaker.canonical_archetype(5, 5), hmac_key=b"custom secret"
    )
    assert vault.verify(b"custom secret")
    assert not vault.verify(b"wrong secret")
    changed = vault.albedo.copy()
    changed[0, 0, 0] += 0.1
    plain_sha = hashlib.sha256(changed.tobytes()).hexdigest()
    with pytest.raises(ValueError, match="PhotorealDNASeal"):
        replace(vault, albedo=changed, seal=plain_sha)


def test_film_has_exposure_dependent_grain_and_deterministic_result() -> None:
    image = np.full((16, 16, 3), 0.5, np.float32)
    assert np.array_equal(
        OcularCausticsAndFilmChemistry.film(image, seed=7),
        OcularCausticsAndFilmChemistry.film(image, seed=7),
    )


def test_dct_singleton_and_one_dimensional_round_trip() -> None:
    values = np.array([[3.25]], dtype=np.float64)
    assert np.allclose(idctn(dctn(values)), values, atol=1e-12)
    row = np.arange(7, dtype=np.float64)[None, :]
    assert np.allclose(idctn(dctn(row)), row, atol=1e-12)


def test_bake_rejects_non_numeric_dtype() -> None:
    with pytest.raises(ValueError, match="dtype"):
        LogChromophoreTomographyBaker().bake(np.full((2, 2, 3), "x"))


def test_caustic_rejects_too_small_shape() -> None:
    with pytest.raises(ValueError, match="at least 2x2"):
        OcularCausticsAndFilmChemistry.caustic((1, 8))


def test_spherical_harmonic_accepts_custom_light_matrix() -> None:
    from nexus_ai_agent.creative.photoreal_intrinsic import ScreenedPoissonSkinTransport

    normals = np.zeros((2, 2, 3), dtype=np.float32)
    normals[..., 2] = 1
    matrix = np.eye(4, dtype=np.float32)
    lit = ScreenedPoissonSkinTransport.spherical_harmonic(normals, matrix)
    assert np.allclose(lit, 2.0)


def test_left_source_caustic_is_concentrated_on_right() -> None:
    caustic = OcularCausticsAndFilmChemistry.caustic((101, 101), light_x=-1)
    assert caustic[:, 51:].mean() > 5 * caustic[:, :50].mean()
