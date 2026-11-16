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
    key = b"for-test-only-0123456789"
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
    assert vault.verify(key) and not vault.verify(b"wrong-key-is-long-enough")
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
    with pytest.raises(ValueError, match="at least 16 bytes"):
        LogChromophoreTomographyBaker().bake(
            LogChromophoreTomographyBaker.canonical_archetype(3, 3), hmac_key=b""
        )


def test_environment_hmac_key_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_PHOTOREAL_HMAC_KEY", "environment secret")
    vault = LogChromophoreTomographyBaker().bake(
        LogChromophoreTomographyBaker.canonical_archetype(3, 3)
    )
    assert vault.verify()
    monkeypatch.setenv("NEXUS_PHOTOREAL_HMAC_KEY", "changed secret key")
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
    with pytest.raises(ValueError, match="heart_rate_bpm"):
        solver.shade(np.ones((2, 2, 3)), heart_rate_bpm=0)
    gray = solver.shade(np.ones((3, 4, 1)), radii=(1.0,))
    assert gray.shape == (3, 4, 1)
    normals = np.zeros((2, 2, 3), np.float32)
    normals[..., 2] = 1
    with pytest.raises(ValueError, match="symmetric"):
        solver.spherical_harmonic(normals, np.triu(np.ones((4, 4), np.float32)))


def test_compiler_chromophore_and_pulse_inputs_change_output() -> None:
    baker = LogChromophoreTomographyBaker()
    vault = baker.bake(baker.canonical_archetype(8, 8), hmac_key=b"compiler-key-012345")
    base = OcularCausticsAndFilmChemistry.compile(
        vault, hmac_key=b"compiler-key-012345", t=0.37, include_caustics=False
    )["float32"]
    altered_mel = np.array(vault.melanin, copy=True) + 2
    altered = replace(vault, melanin=altered_mel, seal=replace(vault.seal, digest="", signature=""))
    altered = replace(altered, seal=baker._create_seal(altered, b"compiler-key-012345"))
    changed = OcularCausticsAndFilmChemistry.compile(
        altered, hmac_key=b"compiler-key-012345", t=0.37, include_caustics=False
    )["float32"]
    pulse = OcularCausticsAndFilmChemistry.compile(
        vault, hmac_key=b"compiler-key-012345", t=0.37, heart_rate_bpm=140, include_caustics=False
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
    hb_candidate = replace(
        hb_candidate, seal=baker._create_seal(hb_candidate, b"compiler-key-012345")
    )
    hb_changed = OcularCausticsAndFilmChemistry.compile(
        hb_candidate, hmac_key=b"compiler-key-012345", t=0.37, include_caustics=False
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


def test_fresnel_energy_and_caustic_resolution_invariance() -> None:
    reflectance, transmittance = OcularCausticsAndFilmChemistry.fresnel_coefficients(
        np.linspace(0, 1, 128)
    )
    assert np.allclose(reflectance + transmittance, 1, atol=1e-15)
    fields = [OcularCausticsAndFilmChemistry.caustic((n, n)) for n in (128, 256, 512)]
    means = np.asarray([field.mean() for field in fields])
    peaks = np.asarray([field.max() for field in fields])
    assert np.ptp(means) / means.mean() < 0.10
    assert np.ptp(peaks) / peaks.mean() < 0.10
    # The area integral is the transmitted in-frame plus reflected Fresnel budget.
    integrated = np.asarray([field.sum() * 4 / field.shape[0] ** 2 for field in fields])
    assert np.ptp(integrated) / integrated.mean() < 0.10


def test_persistent_key_policy_and_fail_closed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    baker = LogChromophoreTomographyBaker()
    image = baker.canonical_archetype(4, 4)
    monkeypatch.delenv("NEXUS_PHOTOREAL_HMAC_KEY", raising=False)
    with pytest.raises(ValueError, match="Persistent vault requires"):
        baker.bake(image, key_policy="persistent")
    with pytest.raises(ValueError, match="at least 16 bytes"):
        baker.bake(image, hmac_key=b"short")
    monkeypatch.setenv("NEXUS_PHOTOREAL_HMAC_KEY", "")
    with pytest.raises(ValueError, match="at least 16 bytes"):
        baker.bake(image)
    monkeypatch.setenv("NEXUS_PHOTOREAL_HMAC_KEY", "valid-environment-secret")
    persistent = baker.bake(image, key_policy="persistent")
    assert not persistent.seal.ephemeral and persistent.verify(key_policy="persistent")
    ephemeral = baker.bake(image, key_policy="ephemeral")
    assert ephemeral.seal.ephemeral


def test_persistent_vault_verifies_across_subprocesses(tmp_path: object) -> None:
    import os
    import subprocess
    import sys
    from pathlib import Path

    artifact = Path(str(tmp_path)) / "vault.pkl"
    key = "cross-process-test-key-123456"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path.cwd() / "src")
    environment["NEXUS_PHOTOREAL_HMAC_KEY"] = key
    create_code = (
        "import pickle; from nexus_ai_agent.creative.photoreal_intrinsic import "
        "LogChromophoreTomographyBaker as B; "
        "v=B().bake(B.canonical_archetype(4,4), key_policy='persistent'); "
        f"pickle.dump(v,open({str(artifact)!r},'wb'))"
    )
    subprocess.run(
        [sys.executable, "-c", create_code],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    verify_code = f"import pickle; print(pickle.load(open({str(artifact)!r},'rb')).verify())"
    good = subprocess.run(
        [sys.executable, "-c", verify_code],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert good.stdout.strip() == "True"
    wrong_environment = environment.copy()
    wrong_environment["NEXUS_PHOTOREAL_HMAC_KEY"] = "a-different-process-key-123456"
    bad = subprocess.run(
        [sys.executable, "-c", verify_code],
        env=wrong_environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert bad.stdout.strip() == "False"
    missing_environment = environment.copy()
    missing_environment.pop("NEXUS_PHOTOREAL_HMAC_KEY")
    bad_missing = subprocess.run(
        [sys.executable, "-c", verify_code],
        env=missing_environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert bad_missing.stdout.strip() == "False"
    persistent_code = (
        "from nexus_ai_agent.creative.photoreal_intrinsic import "
        "LogChromophoreTomographyBaker as B; "
        "B().bake(B.canonical_archetype(3,3),key_policy='persistent')"
    )
    failed = subprocess.run(
        [sys.executable, "-c", persistent_code],
        env=missing_environment,
        capture_output=True,
        text=True,
    )
    assert failed.returncode != 0 and "Persistent vault requires" in failed.stderr


def test_film_depth_offset_invariance_and_negative_time() -> None:
    image = np.full((9, 8, 3), 0.4, dtype=np.float32)
    y, x = np.mgrid[:9, :8]
    depth = 0.2 * np.sin(x / 2) + 0.15 * np.cos(y / 3)
    first = OcularCausticsAndFilmChemistry.film(image, depth=depth, focus=0.1, seed=3)
    shifted = OcularCausticsAndFilmChemistry.film(image, depth=depth + 10, focus=0.1, seed=3)
    assert np.array_equal(first, shifted)
    vault = LogChromophoreTomographyBaker().bake(
        LogChromophoreTomographyBaker.canonical_archetype(6, 6)
    )
    frame = OcularCausticsAndFilmChemistry.compile(vault, time_sec=1.25, include_caustics=False)
    assert isinstance(frame["rgb"], np.ndarray)
    with pytest.raises(ValueError, match="must be finite"):
        OcularCausticsAndFilmChemistry.compile(vault, time_sec=float("nan"))


def test_warp_zero_flow_returns_independent_array() -> None:
    source = np.ones((3, 4), dtype=np.uint8)
    result = RiemannianStrainWrinkleTensor.warp(source, np.zeros((3, 4, 2)))
    assert np.array_equal(result, source) and result is not source
    result[0, 0] = 0
    assert source[0, 0] == 1


def test_colorspace_contract_is_explicit() -> None:
    baker = LogChromophoreTomographyBaker()
    key = b"colorspace-contract-key"
    image = np.full((4, 5, 3), 128, dtype=np.uint8)
    srgb_integer = baker.bake(image, hmac_key=key, input_colorspace="srgb")
    srgb_float = baker.bake(
        np.full(image.shape, 128.0 / 255.0, dtype=np.float64),
        hmac_key=key,
        input_colorspace="srgb",
    )
    assert np.max(np.abs(srgb_integer.albedo - srgb_float.albedo)) < 1e-12
    assert np.max(np.abs(srgb_integer.melanin - srgb_float.melanin)) < 1e-12
    linear_integer = baker.bake(image, hmac_key=key, input_colorspace="linear")
    assert not np.allclose(linear_integer.shading, srgb_integer.shading)


def test_compile_warps_depth_and_relative_lighting(monkeypatch: pytest.MonkeyPatch) -> None:
    vault = LogChromophoreTomographyBaker().bake(
        LogChromophoreTomographyBaker.canonical_archetype(6, 7)
    )
    flow = np.zeros((6, 7, 2), dtype=np.float32)
    flow[..., 0] = 0.4
    expected = RiemannianStrainWrinkleTensor.warp(vault.depth, flow)
    seen: dict[str, np.ndarray] = {}

    def film(
        image: np.ndarray, depth: np.ndarray | None = None, focus: float = 1.0, seed: int = 0
    ) -> np.ndarray:
        assert depth is not None
        seen["depth"] = depth.copy()
        return image

    monkeypatch.setattr(OcularCausticsAndFilmChemistry, "film", staticmethod(film))
    result = OcularCausticsAndFilmChemistry.compile(vault, flow=flow, include_caustics=False)
    assert np.array_equal(seen["depth"], expected)
    assert isinstance(result["float32"], np.ndarray)


def test_benchmark_helper_reports_timing_and_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus_ai_agent.creative.photoreal_intrinsic.benchmark import benchmark_pipeline

    monkeypatch.delenv("PYTHONPATH", raising=False)
    result = benchmark_pipeline((4,), iterations=3)["4x4"]
    assert result["median_ms"] >= 0 and result["mad_ms"] >= 0
    assert len(result["raw_trials"]) == 3
    assert len({trial["pid"] for trial in result["raw_trials"]}) == 3
    trial = result["raw_trials"][0]
    assert trial["tracemalloc_peak_mib"] > 0 and trial["peak_rss_mib"] > 0
    assert trial["baseline_rss_mib"] > 0 and trial["incremental_rss_mib"] >= 0
    assert trial["peak_rss_mib"] >= trial["baseline_rss_mib"]
    from nexus_ai_agent.creative.photoreal_intrinsic.benchmark import _benchmark_trial

    direct = _benchmark_trial(3)
    assert direct["elapsed_ms"] >= 0 and direct["tracemalloc_peak_mib"] > 0
    with pytest.raises(ValueError, match="sizes"):
        benchmark_pipeline((1,), iterations=1)
    assert benchmark_pipeline((), iterations=1) == {}


def test_remaining_validation_branches_and_benchmark_warmup() -> None:
    baker = LogChromophoreTomographyBaker()
    image = baker.canonical_archetype(3, 3)
    with pytest.raises(ValueError, match="input_colorspace"):
        baker.bake(image, input_colorspace="xyz")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="normalized"):
        baker.bake(image * 2, input_colorspace="srgb")
    with pytest.raises(ValueError, match="key_policy"):
        baker.bake(image, key_policy="invalid")  # type: ignore[arg-type]
    solver = ScreenedPoissonSkinTransport()
    with pytest.raises(ValueError, match="2x2"):
        solver.transport(np.ones((1, 3, 3)))
    with pytest.raises(ValueError, match="finite"):
        solver.transport(np.full((2, 2, 3), np.inf))
    with pytest.raises(ValueError, match="2x2"):
        solver.shade(np.ones((1, 3, 3)))
    with pytest.raises(ValueError, match="finite"):
        solver.shade(np.full((2, 2, 3), np.nan))
    from nexus_ai_agent.creative.photoreal_intrinsic.benchmark import benchmark_pipeline

    assert len(benchmark_pipeline((3,), iterations=1)["3x3"]["raw_trials"]) == 1


def test_film_rejects_invalid_depth_and_focus() -> None:
    image = np.ones((3, 4, 3), dtype=np.float32)
    with pytest.raises(ValueError, match="focus"):
        OcularCausticsAndFilmChemistry.film(image, depth=np.ones((3, 4)), focus=float("inf"))
    with pytest.raises(ValueError, match="depth"):
        OcularCausticsAndFilmChemistry.film(image, depth=np.ones((2, 4)))


def test_compile_is_invariant_to_constant_depth_offset() -> None:
    baker = LogChromophoreTomographyBaker()
    key = b"compile-depth-offset-key-123456"
    vault = baker.bake(baker.canonical_archetype(8, 8), hmac_key=key)
    altered = replace(
        vault,
        depth=vault.depth.astype(np.float64) + 10.0,
        seal=replace(vault.seal, digest="", signature="", key_fingerprint=""),
    )
    shifted = replace(altered, seal=baker._create_seal(altered, key))
    original_frame = OcularCausticsAndFilmChemistry.compile(
        vault, hmac_key=key, include_caustics=False
    )["float32"]
    shifted_frame = OcularCausticsAndFilmChemistry.compile(
        shifted, hmac_key=key, include_caustics=False
    )["float32"]
    assert isinstance(original_frame, np.ndarray) and isinstance(shifted_frame, np.ndarray)
    assert np.max(np.abs(original_frame.astype(np.float64) - shifted_frame)) < 1e-12


def test_beer_lambert_absorption_is_strictly_monotone_by_chromophore() -> None:
    baker = LogChromophoreTomographyBaker()
    key = b"beer-lambert-monotonic-key"
    base_vault = baker.bake(baker.canonical_archetype(8, 9), hmac_key=key)

    def signed_variant(melanin: np.ndarray, hemoglobin: np.ndarray) -> IntrinsicBiophysicalVault:
        candidate = replace(
            base_vault,
            melanin=melanin,
            hemoglobin=hemoglobin,
            seal=replace(base_vault.seal, digest="", signature="", key_fingerprint=""),
        )
        return replace(candidate, seal=baker._create_seal(candidate, key))

    original = OcularCausticsAndFilmChemistry.compile(
        base_vault, hmac_key=key, include_caustics=False
    )["float32"]
    assert isinstance(original, np.ndarray)
    for pigment in ("melanin", "hemoglobin"):
        melanin = base_vault.melanin.copy()
        hemoglobin = base_vault.hemoglobin.copy()
        if pigment == "melanin":
            melanin += 0.75
        else:
            hemoglobin += 0.75
        increased = OcularCausticsAndFilmChemistry.compile(
            signed_variant(melanin, hemoglobin),
            hmac_key=key,
            include_caustics=False,
        )["float32"]
        assert isinstance(increased, np.ndarray)
        assert np.all(increased.mean(axis=(0, 1)) < original.mean(axis=(0, 1)))
        baseline_transmission = OcularCausticsAndFilmChemistry._beer_lambert_attenuation(
            base_vault.melanin, base_vault.hemoglobin, 1.0, 1.0, 0.97
        )
        increased_transmission = OcularCausticsAndFilmChemistry._beer_lambert_attenuation(
            melanin, hemoglobin, 1.0, 1.0, 0.97
        )
        assert np.all(
            base_vault.albedo * increased_transmission < base_vault.albedo * baseline_transmission
        )


def test_compile_rejects_nonphysical_physiology() -> None:
    vault = LogChromophoreTomographyBaker().bake(
        LogChromophoreTomographyBaker.canonical_archetype(4, 4)
    )
    for parameters in (
        {"heart_rate_bpm": float("nan")},
        {"sympathetic_flush": float("inf")},
        {"blood_oxygenation": -0.5},
        {"heart_rate_bpm": 241.0},
        {"sympathetic_flush": 1.1},
        {"blood_oxygenation": 1.0},
        {"focus": float("inf")},
    ):
        with pytest.raises(ValueError):
            OcularCausticsAndFilmChemistry.compile(vault, **parameters)  # type: ignore[arg-type]


def test_chromophore_basis_is_well_conditioned_and_noise_stable() -> None:
    basis = LogChromophoreTomographyBaker.basis
    assert np.linalg.matrix_rank(basis) == 3
    assert np.linalg.cond(basis) < 10.0
    inverse = np.linalg.inv(basis)
    assert np.allclose(basis @ inverse, np.eye(3), rtol=0, atol=1e-12)
    rng = np.random.default_rng(127)
    spectrum = rng.uniform(0.1, 2.0, size=(7, 9, 3))
    noise = rng.normal(0.0, 1e-9, size=spectrum.shape)
    recovered = spectrum @ inverse.T
    recovered_noisy = (spectrum + noise) @ inverse.T
    assert np.linalg.norm(recovered_noisy - recovered) / np.linalg.norm(noise) < np.linalg.cond(
        basis
    )


def _explicit_dct_matrix(length: int) -> np.ndarray:
    frequency = np.arange(length, dtype=np.float64)[:, None]
    position = np.arange(length, dtype=np.float64)[None, :]
    matrix = np.sqrt(2.0 / length) * np.cos(np.pi * frequency * (position + 0.5) / length)
    matrix[0] = np.sqrt(1.0 / length)
    return cast(np.ndarray, matrix)


def _reference_scale(
    source: np.ndarray,
    sigma: float,
    eigenvalues: np.ndarray,
    row_dct: np.ndarray,
    col_dct: np.ndarray,
) -> np.ndarray:
    coefficients = row_dct @ source @ col_dct.T
    return cast(
        np.ndarray,
        row_dct.T @ (coefficients / (1.0 + sigma * sigma * eigenvalues)) @ col_dct,
    )


def test_geometry_normals_and_real_three_scale_screened_poisson() -> None:
    baker = LogChromophoreTomographyBaker()
    shading = np.exp(np.random.default_rng(9).normal(0.0, 0.2, size=(9, 11))).astype(np.float32)
    normals, _ = baker._geometry(shading)
    assert np.max(np.abs(np.linalg.norm(normals, axis=-1) - 1.0)) < 1e-12


def test_actual_three_scale_components_match_neumann_reference_and_flux() -> None:
    solver = ScreenedPoissonSkinTransport()
    expected_weights = np.array((0.5218, 0.3341, 0.1441), dtype=np.float64)
    expected_scales = np.array((0.618, 1.618, 3.618), dtype=np.float64)
    assert np.array_equal(solver.weights, expected_weights)
    assert np.array_equal(solver.scales, expected_scales)
    assert solver.weights.sum() == 1.0
    for size in (32, 64, 128):
        source = np.random.default_rng(size).normal(size=(size, size, 3))
        components = solver.transport_with_scale_components(source)
        row_dct, col_dct = _explicit_dct_matrix(size), _explicit_dct_matrix(size)
        eigenvalues = (
            4 * np.sin(np.pi * np.arange(size) / (2 * size))[:, None] ** 2
            + 4 * np.sin(np.pi * np.arange(size) / (2 * size))[None, :] ** 2
        )
        reference_components = np.empty_like(components)
        for scale_index, scale in enumerate(expected_scales):
            for channel, radius in enumerate(solver.default_radii):
                sigma = radius / scale
                reference_components[scale_index, ..., channel] = _reference_scale(
                    source[..., channel], sigma, eigenvalues, row_dct, col_dct
                )
                field = components[scale_index, ..., channel]
                coefficients = row_dct @ field @ col_dct.T
                laplacian = row_dct.T @ (-eigenvalues * coefficients) @ col_dct
                residual = field - sigma * sigma * laplacian - source[..., channel]
                assert np.linalg.norm(residual) / np.linalg.norm(source[..., channel]) < 1e-12
                assert abs(float(field.mean() - source[..., channel].mean())) < 1e-12
        actual = solver.transport(source)
        reference = np.einsum("s,shwc->hwc", expected_weights, reference_components)
        error = actual - reference
        assert np.linalg.norm(error) / np.linalg.norm(reference) < 1e-12
        assert np.max(np.abs(error)) < 1e-12
        assert np.max(np.abs(actual.mean(axis=(0, 1)) - source.mean(axis=(0, 1)))) < 1e-12


def test_scientific_mutation_probes_detect_coefficients_weights_and_spectrum() -> None:
    melanin = np.full((3, 4), 0.7, dtype=np.float64)
    hemoglobin = np.full((3, 4), 0.4, dtype=np.float64)
    actual_attenuation = OcularCausticsAndFilmChemistry._beer_lambert_attenuation(
        melanin, hemoglobin, 1.0, 1.0, 0.97
    )
    absorption_hb = 0.97 * np.array((0.08, 0.24, 0.10)) + 0.03 * np.array((0.20, 0.72, 0.66))
    expected = np.exp(
        -0.035 * melanin[..., None] * np.array((0.22, 0.45, 0.82))
        - 0.08 * hemoglobin[..., None] * absorption_hb
    )
    assert np.allclose(actual_attenuation, expected, rtol=0, atol=1e-7)
    mutated_absorption = np.exp(
        -0.03501 * melanin[..., None] * np.array((0.22, 0.45, 0.82))
        - 0.08 * hemoglobin[..., None] * absorption_hb
    )
    assert np.max(np.abs(actual_attenuation - mutated_absorption)) > 1e-7

    solver = ScreenedPoissonSkinTransport()
    source = np.random.default_rng(32).normal(size=(8, 8, 3))
    components = solver.transport_with_scale_components(source)
    changed_weights = solver.weights.copy()
    changed_weights[0] += 0.001
    assert abs(float(changed_weights.sum()) - 1.0) > 1e-4
    expected_mix = np.einsum("s,shwc->hwc", solver.weights, components)
    mutated_mix = np.einsum("s,shwc->hwc", changed_weights, components)
    assert np.max(np.abs(expected_mix - mutated_mix)) > 1e-7
    row_dct = _explicit_dct_matrix(8)
    eigenvalues = (
        4 * np.sin(np.pi * np.arange(8) / 16)[:, None] ** 2
        + 4 * np.sin(np.pi * np.arange(8) / 16)[None, :] ** 2
    )
    sigma = solver.default_radii[0] / solver.scales[0]
    correct = _reference_scale(source[..., 0], sigma, eigenvalues, row_dct, row_dct)
    wrong = _reference_scale(source[..., 0], sigma, eigenvalues + 1e-4, row_dct, row_dct)
    assert np.max(np.abs(correct - wrong)) > 1e-7


def test_beer_lambert_rejects_negative_concentrations() -> None:
    with pytest.raises(ValueError, match="nonnegative"):
        OcularCausticsAndFilmChemistry._beer_lambert_attenuation(
            np.full((2, 2), -0.1), np.ones((2, 2)), 1.0, 1.0, 0.97
        )
    with pytest.raises(ValueError, match="nonnegative"):
        OcularCausticsAndFilmChemistry._beer_lambert_attenuation(
            np.ones((2, 2)), np.full((2, 2), -0.1), 1.0, 1.0, 0.97
        )


def test_benchmark_rss_is_portable_without_resource(monkeypatch: pytest.MonkeyPatch) -> None:
    import ctypes
    import sys
    from types import SimpleNamespace

    from nexus_ai_agent.creative.photoreal_intrinsic import benchmark

    monkeypatch.setattr(benchmark, "resource", None)
    monkeypatch.setattr(sys, "platform", "win32")

    def succeed(_process: object, pointer: object, _size: int) -> int:
        pointer._obj.PeakWorkingSetSize = 2 * 1024 * 1024  # type: ignore[attr-defined]
        return 1

    fake_windows = SimpleNamespace(
        kernel32=SimpleNamespace(GetCurrentProcess=lambda: 1),
        psapi=SimpleNamespace(GetProcessMemoryInfo=succeed),
    )
    monkeypatch.setattr(ctypes, "windll", fake_windows, raising=False)
    assert benchmark._rss_mib() == 2.0

    fake_windows.psapi.GetProcessMemoryInfo = lambda *_args: 0
    with pytest.raises(OSError, match="GetProcessMemoryInfo"):
        benchmark._rss_mib()

    monkeypatch.setattr(sys, "platform", "freebsd")
    with pytest.raises(RuntimeError, match="unsupported"):
        benchmark._rss_mib()


def test_benchmark_module_import_survives_missing_resource(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib
    from typing import Any, cast

    from nexus_ai_agent.creative.photoreal_intrinsic import benchmark

    original_import_module = importlib.import_module

    def import_without_resource(name: str, package: str | None = None) -> Any:
        if name == "resource":
            raise ImportError("simulated Windows environment")
        return original_import_module(name, package)

    monkeypatch.setattr(importlib, "import_module", import_without_resource)
    try:
        importlib.reload(benchmark)
        resource_was_optional = cast(Any, benchmark).resource is None
    finally:
        monkeypatch.setattr(importlib, "import_module", original_import_module)
        importlib.reload(benchmark)
    assert resource_was_optional


@pytest.mark.parametrize("bad_time", [-0.1, 1e308, 100000.0])
def test_compile_rejects_time_outside_physical_day(bad_time: float) -> None:
    vault = LogChromophoreTomographyBaker().bake(
        LogChromophoreTomographyBaker.canonical_archetype(3, 3)
    )
    with pytest.raises(ValueError):
        OcularCausticsAndFilmChemistry.compile(vault, time_sec=bad_time, include_caustics=False)


@pytest.mark.parametrize("bad_focus", [-0.01, 1.5])
def test_compile_rejects_focus_depth_outside_normalized_range(bad_focus: float) -> None:
    vault = LogChromophoreTomographyBaker().bake(
        LogChromophoreTomographyBaker.canonical_archetype(3, 3)
    )
    with pytest.raises(ValueError):
        OcularCausticsAndFilmChemistry.compile(vault, focus_depth=bad_focus, include_caustics=False)


def test_bake_validates_basis_and_regularization_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus_ai_agent.creative.photoreal_intrinsic import (
        log_chromophore_tomography_baker as baker_module,
    )

    baker = LogChromophoreTomographyBaker()
    image = baker.canonical_archetype(3, 4)
    invalid_bases = (
        np.ones((2, 3)),
        np.full((3, 3), np.nan),
        np.ones((3, 3)),
        np.diag((1.0, 1.0, 1e-5)),
    )
    for basis in invalid_bases:
        baker.basis = basis
        with pytest.raises(ValueError, match="chromophore basis"):
            baker.bake(image)
    baker.basis = LogChromophoreTomographyBaker.basis
    monkeypatch.setattr(baker_module, "_RADIANCE_EPSILON", 0.0)
    with pytest.raises(ValueError, match="regularization"):
        baker.bake(image)


def test_single_scale_solver_rejects_invalid_source_and_length() -> None:
    solver = ScreenedPoissonSkinTransport()
    with pytest.raises(ValueError, match="2x2"):
        solver.solve_single_scale(np.ones((1, 4)), 1.0)
    with pytest.raises(ValueError, match="finite"):
        solver.solve_single_scale(np.full((2, 2), np.nan), 1.0)
    for length in (0.0, float("inf"), 1e5):
        with pytest.raises(ValueError, match="diffusion_length"):
            solver.solve_single_scale(np.ones((2, 2)), length)
    with pytest.raises(ValueError, match="1e4"):
        solver.transport_with_scale_components(np.ones((2, 2, 3)), (1e5,))
