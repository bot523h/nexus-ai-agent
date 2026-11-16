import numpy as np
import pytest

from nexus_ai_agent.creative.photoreal_intrinsic import (
    LogChromophoreTomographyBaker,
    OcularCausticsAndFilmChemistry,
    RiemannianStrainWrinkleTensor,
    ScreenedPoissonSkinTransport,
)


@pytest.fixture
def baker() -> LogChromophoreTomographyBaker:
    return LogChromophoreTomographyBaker()


@pytest.mark.parametrize("i", range(15))
def test_bake_reproducible_and_sealed(baker: LogChromophoreTomographyBaker, i: int) -> None:
    rgb = baker.canonical_archetype(8 + i, 9)
    a = baker.bake(rgb)
    b = baker.bake(rgb)
    assert a.seal == b.seal and a.verify()
    assert not hasattr(a, "hmac_key")
    assert a.albedo.shape == rgb.shape
    assert np.isfinite(a.normals).all()


@pytest.mark.parametrize("shape", [(4, 5), (8, 8), (12, 7), (16, 10), (3, 9)])
def test_bake_shapes(baker: LogChromophoreTomographyBaker, shape: tuple[int, int]) -> None:
    v = baker.bake(baker.canonical_archetype(*shape))
    assert v.melanin.shape == shape


@pytest.mark.parametrize("flush", [-1, -0.5, 0, 0.5, 1])
def test_flush_warms_red(flush: float) -> None:
    tr = ScreenedPoissonSkinTransport()
    x = np.full((12, 12, 3), 0.5, np.float32)
    y = tr.shade(x, sympathetic_flush=flush)
    baseline = tr.shade(x)
    ratio = y[..., 0].mean() / y[..., 1].mean()
    baseline_ratio = baseline[..., 0].mean() / baseline[..., 1].mean()
    assert (ratio - baseline_ratio) * flush >= -1e-8


@pytest.mark.parametrize(
    "radius", [(3.4, 1.35, 0.65), (4, 2, 1), (2, 1, 0.5), (1, 1, 1), (5, 3, 2)]
)
def test_transport_finite(radius: tuple[float, float, float]) -> None:
    x = np.zeros((20, 20, 3), np.float32)
    x[:, :10] = 1
    y = ScreenedPoissonSkinTransport().transport(x, radius)
    assert y.shape == x.shape and np.isfinite(y).all()


@pytest.mark.parametrize("axis", [0, 1])
def test_red_bleeds_more_than_blue(axis: int) -> None:
    x = np.zeros((48, 48, 3), np.float32)
    if axis == 0:
        x[:24, :, :] = 1
        shadow_pixel = (25, 24)
    else:
        x[:, :24, :] = 1
        shadow_pixel = (24, 25)
    out = ScreenedPoissonSkinTransport().transport(x)
    assert out[shadow_pixel][0] > out[shadow_pixel][2]


@pytest.mark.parametrize("strain", [0.02, 0.05, 0.1, 0.2, 0.3])
def test_tensile_roughness_decreases(strain: float) -> None:
    u = np.zeros((30, 30, 2), np.float32)
    u[..., 0] = np.arange(30)[None, :] * strain
    r = RiemannianStrainWrinkleTensor().analyze(u)
    assert r["roughness"].mean() < 0.5


@pytest.mark.parametrize("strain", [-0.05, -0.1, -0.2, -0.3, -0.4])
def test_compression_wrinkles(strain: float) -> None:
    u = np.zeros((30, 30, 2), np.float32)
    u[..., 0] = np.arange(30)[None, :] * strain
    r = RiemannianStrainWrinkleTensor().analyze(u)
    assert r["wrinkle"].sum() > 0


@pytest.mark.parametrize("illum", [0, 1, 10, 100, 1000])
def test_pupil_response(illum: float) -> None:
    assert (
        OcularCausticsAndFilmChemistry.pupil_diameter(illum)
        < OcularCausticsAndFilmChemistry.pupil_diameter(0) + 1e-8
    )


@pytest.mark.parametrize("seed", range(5))
def test_film_bounded_and_deterministic(seed: int) -> None:
    x = np.full((12, 12, 3), 0.5, np.float32)
    a = OcularCausticsAndFilmChemistry.film(x, seed=seed)
    assert (
        a.dtype == np.float32
        and np.isfinite(a).all()
        and np.array_equal(a, OcularCausticsAndFilmChemistry.film(x, seed=seed))
    )


@pytest.mark.parametrize("shape", [(8, 8), (10, 12), (3, 4), (20, 15), (6, 5)])
def test_compile_frame(baker: LogChromophoreTomographyBaker, shape: tuple[int, int]) -> None:
    result = OcularCausticsAndFilmChemistry.compile(baker.bake(baker.canonical_archetype(*shape)))
    frame = result["rgb"]
    assert isinstance(frame, np.ndarray)
    assert frame.shape == (*shape, 3) and frame.dtype == np.uint8


@pytest.mark.parametrize("bad", [-2, 2])
def test_flush_bounds(bad: float) -> None:
    with pytest.raises(ValueError):
        ScreenedPoissonSkinTransport().shade(np.ones((2, 2, 3)), sympathetic_flush=bad)


@pytest.mark.parametrize("bad", [0.1, 1.2])
def test_oxygen_bounds(bad: float) -> None:
    with pytest.raises(ValueError):
        ScreenedPoissonSkinTransport().shade(np.ones((2, 2, 3)), blood_oxygenation=bad)


@pytest.mark.parametrize("shape", [(5, 5), (9, 12), (13, 13), (7, 15), (20, 20)])
def test_warp_identity(shape: tuple[int, int]) -> None:
    im = np.arange(shape[0] * shape[1] * 3, dtype=np.float32).reshape(*shape, 3)
    flow = np.zeros((*shape, 2), np.float32)
    assert np.array_equal(RiemannianStrainWrinkleTensor.warp(im, flow), im)


def test_bake_rejects_invalid_shape() -> None:
    with pytest.raises(ValueError):
        LogChromophoreTomographyBaker().bake(np.zeros((4, 4), dtype=np.float32))


def test_bake_scales_byte_rgb() -> None:
    result = LogChromophoreTomographyBaker().bake(np.full((4, 4, 3), 128, dtype=np.uint8))
    assert result.albedo.shape == (4, 4, 3)


def test_seal_rejects_tampering() -> None:
    from dataclasses import replace

    vault = LogChromophoreTomographyBaker().bake(
        LogChromophoreTomographyBaker.canonical_archetype(4, 4)
    )
    forged_seal = replace(vault.seal, signature="0" * 64)
    changed = replace(vault, seal=forged_seal)
    assert not changed.verify()
    vault.albedo.setflags(write=True)
    vault.albedo[0, 0, 0] += 0.1
    assert not vault.verify()


def test_caustic_has_expected_shape() -> None:
    result = OcularCausticsAndFilmChemistry.caustic((12, 14), light_x=-1)
    assert result.shape == (12, 14) and np.isfinite(result).all()


def test_transport_rejects_non_rgb() -> None:
    with pytest.raises(ValueError):
        ScreenedPoissonSkinTransport().transport(np.zeros((3, 4), dtype=np.float32))


def test_strain_rejects_invalid_flow() -> None:
    with pytest.raises(ValueError):
        RiemannianStrainWrinkleTensor().analyze(np.zeros((4, 4), dtype=np.float32))
