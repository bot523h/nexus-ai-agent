from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from nexus_ai_agent.creative.chronos.contracts import KernelError
from nexus_ai_agent.creative.chronos.weather_and_grading import (
    CubeLUT,
    apply_asc_cdl_and_3d_lut,
    identity_cube_lut,
    legalize_broadcast_rgb_ebu_r103,
    parse_cube_lut,
    render_physical_precipitation_layer,
    serialize_cube_lut,
)


def test_precipitation_physical_streak_attenuation_and_determinism() -> None:
    receipt = render_physical_precipitation_layer(
        48,
        40,
        800.0,
        4.0,
        8.0,
        frame_rate_fps=24.0,
        shutter_angle_deg=180.0,
        wind_velocity_m_s=(1.0, 0.5),
        particle_count=20,
        extinction_per_m=0.2,
        seed=31,
    )
    exposure = 0.5 / 24.0
    expected_vertical = 800.0 * 8.0 * exposure / 4.0
    expected_total = np.hypot(800 * exposure / 4, 800 * 8.5 * exposure / 4)
    assert receipt.vertical_streak_length_px == pytest.approx(expected_vertical)
    assert receipt.total_streak_length_px == pytest.approx(expected_total)
    assert receipt.beer_lambert_attenuation == pytest.approx(np.exp(-0.8))
    assert receipt.rendered_particle_count == 20
    assert receipt.layer.shape == (40, 48, 3)
    assert receipt.layer.sum() > 0
    same = render_physical_precipitation_layer(
        48,
        40,
        800,
        4,
        8,
        frame_rate_fps=24,
        particle_count=20,
        extinction_per_m=0.2,
        seed=31,
    )
    assert np.array_equal(
        same.layer,
        render_physical_precipitation_layer(
            48,
            40,
            800,
            4,
            8,
            frame_rate_fps=24,
            particle_count=20,
            extinction_per_m=0.2,
            seed=31,
        ).layer,
    )
    assert same.layer.shape == receipt.layer.shape
    assert (
        render_physical_precipitation_layer(
            8, 6, 400, 3, 0, frame_rate_fps=30, particle_count=1
        ).vertical_streak_length_px
        == 0.0
    )


def test_precipitation_invalid_controls() -> None:
    with pytest.raises(KernelError, match="physical precipitation"):
        render_physical_precipitation_layer(0, 4, 100, 2, 3, frame_rate_fps=24)
    with pytest.raises(KernelError, match="physical precipitation"):
        render_physical_precipitation_layer(4, 4, 100, 0, 3, frame_rate_fps=24)
    with pytest.raises(KernelError, match="physical precipitation"):
        render_physical_precipitation_layer(4, 4, 100, 2, -1, frame_rate_fps=24)
    with pytest.raises(KernelError, match="physical precipitation"):
        render_physical_precipitation_layer(4, 4, 100, 2, 3, frame_rate_fps=0)
    with pytest.raises(KernelError, match="physical precipitation"):
        render_physical_precipitation_layer(
            4, 4, 100, 2, 3, frame_rate_fps=24, shutter_angle_deg=361
        )
    with pytest.raises(KernelError, match="physical precipitation"):
        render_physical_precipitation_layer(
            4, 4, 100, 2, 3, frame_rate_fps=24, wind_velocity_m_s=(1.0,)
        )
    with pytest.raises(KernelError, match="physical precipitation"):
        render_physical_precipitation_layer(
            4, 4, 100, 2, 3, frame_rate_fps=24, particle_rgb=(1, -1, 1)
        )


def test_cube_lut_identity_roundtrip_trilinear_and_cdl_math() -> None:
    lut = identity_cube_lut(5)
    encoded = serialize_cube_lut(lut, title='studio "identity"')
    parsed = parse_cube_lut(encoded)
    sample = np.random.default_rng(9).random((17, 13, 3))
    reconstructed = apply_asc_cdl_and_3d_lut(sample, lut=parsed)
    assert np.max(np.abs(reconstructed - sample)) < 1e-7
    assert parsed.size == 5
    assert serialize_cube_lut(parsed) == serialize_cube_lut(
        parse_cube_lut(serialize_cube_lut(parsed))
    )

    raw = np.array([[[0.2, 0.3, 0.4]], [[-0.5, 0.5, 2.0]]])
    result = apply_asc_cdl_and_3d_lut(
        raw,
        slope=(2.0, 1.0, 0.5),
        offset=(-0.1, 0.2, 0.0),
        power=(2.0, 1.0, 1.0),
        saturation=0.0,
    )
    cdl = np.maximum(raw * np.array([2.0, 1.0, 0.5]) + np.array([-0.1, 0.2, 0]), 0) ** np.array(
        [2, 1, 1]
    )
    expected_luma = cdl @ np.array([0.2126, 0.7152, 0.0722])
    assert result == pytest.approx(np.repeat(expected_luma[..., None], 3, axis=-1))

    offset_lut = identity_cube_lut(2)
    values = np.asarray(offset_lut.values).copy()
    values[..., 0] = 1.0 - values[..., 0]
    custom = CubeLUT(2, values)
    mapped = apply_asc_cdl_and_3d_lut(np.array([[[0.2, 0.4, 0.8]]]), lut=custom)
    assert mapped[0, 0, 0] == pytest.approx(0.8)
    with pytest.raises(KernelError, match="at least two"):
        identity_cube_lut(1)


def test_cube_lut_parser_and_cdl_refusals() -> None:
    for content in (
        "LUT_1D_SIZE 2\n",
        "LUT_3D_SIZE nope\n",
        "LUT_3D_SIZE 2\n0 0 0\n",
        "LUT_3D_SIZE 2\nDOMAIN_MIN 0 0\n",
        "LUT_3D_SIZE 2\n" + "0 0 x\n" * 8,
        "LUT_3D_SIZE 2\nDOMAIN_MIN 1 1 1\nDOMAIN_MAX 0 0 0\n" + "0 0 0\n" * 8,
    ):
        with pytest.raises(KernelError):
            parse_cube_lut(content)
    with pytest.raises(KernelError, match="duplicate"):
        parse_cube_lut("LUT_3D_SIZE 2\nLUT_3D_SIZE 2\n" + "0 0 0\n" * 8)
    with pytest.raises(KernelError, match="samples"):
        parse_cube_lut("LUT_3D_SIZE 2\n" + "0 0 0\n" * 7)
    with pytest.raises(KernelError, match="invalid 3D cube LUT"):
        serialize_cube_lut(CubeLUT(2, np.zeros((2, 2, 2, 2))))
    image = np.ones((2, 2, 3))
    for kwargs in (
        {"slope": (1.0, 1.0)},
        {"slope": (-1.0, 1.0, 1.0)},
        {"power": (1.0, 0.0, 1.0)},
        {"saturation": -1.0},
    ):
        with pytest.raises(KernelError, match="ASC-CDL"):
            apply_asc_cdl_and_3d_lut(image, **kwargs)
    with pytest.raises(KernelError, match="ASC-CDL"):
        apply_asc_cdl_and_3d_lut(np.full((2, 2, 3), np.nan))


def test_ebu_r103_legality_hue_compression_and_structure() -> None:
    ramp = np.linspace(-0.03, 1.03, 96)
    image = np.broadcast_to(ramp[None, :, None], (32, 96, 3)).copy()
    image[8:12, 30:35] = np.array([1.2, 0.2, -0.1])
    result = legalize_broadcast_rgb_ebu_r103(image)
    assert result.illegal_pixel_fraction_before > 0
    assert result.illegal_pixel_fraction_after == 0.0
    assert np.all((result.image >= 0) & (result.image <= 1))
    assert result.sobel_structure_correlation >= 0.99
    assert result.image.shape == image.shape
    # Pixel chroma is compressed toward its luminance, not independently clipped.
    input_pixel, output_pixel = image[9, 31], result.image[9, 31]
    assert np.linalg.norm(output_pixel - output_pixel.mean()) < np.linalg.norm(
        input_pixel - input_pixel.mean()
    )
    legal = legalize_broadcast_rgb_ebu_r103(np.full((4, 4, 3), 0.5))
    assert legal.illegal_pixel_fraction_before == legal.illegal_pixel_fraction_after == 0.0
    assert legal.sobel_structure_correlation == 1.0
    with pytest.raises(KernelError, match="finite H x W"):
        legalize_broadcast_rgb_ebu_r103(np.ones((4, 4)))
    with pytest.raises(KernelError, match="finite H x W"):
        legalize_broadcast_rgb_ebu_r103(np.full((4, 4, 3), np.inf))


def test_wave6_weather_grading_port_handlers_use_disk(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.chronos import build_default_chronos_handlers
    from nexus_ai_agent.creative.chronos.chronos_port import ChronosCommand, ChronosPort

    port = ChronosPort(build_default_chronos_handlers())
    precipitation_path = tmp_path / "rain.npy"
    precipitation = port.execute(
        ChronosCommand(
            "weather.precipitation",
            ("parameters",),
            {
                "output": str(precipitation_path),
                "width_px": "16",
                "height_px": "12",
                "focal_length_y_px": "500",
                "particle_depth_m": "3",
                "terminal_velocity_m_s": "7",
                "frame_rate_fps": "24",
                "particle_count": "4",
                "seed": "11",
            },
        )
    )
    assert np.load(precipitation_path, allow_pickle=False).shape == (12, 16, 3)
    assert float(precipitation.metadata["vertical_streak_length_px"]) > 0

    cube_source, cube_target = tmp_path / "identity.cube", tmp_path / "roundtrip.cube"
    cube_source.write_text(serialize_cube_lut(identity_cube_lut(3)), encoding="utf-8")
    cube_receipt = port.execute(
        ChronosCommand("grading.serialize_cube", (str(cube_source),), {"output": str(cube_target)})
    )
    assert cube_receipt.metadata["size"] == "3"
    image_path, graded_path = tmp_path / "input.npy", tmp_path / "graded.npy"
    pixels = np.random.default_rng(2).random((4, 5, 3))
    np.save(image_path, pixels)
    graded = port.execute(
        ChronosCommand(
            "grading.apply_cdl_lut",
            (str(image_path), str(cube_target)),
            {"output": str(graded_path), "slope": "1,1,1", "offset": "0,0,0", "power": "1,1,1"},
        )
    )
    assert graded.metadata["lut_size"] == "3"
    assert np.max(np.abs(np.load(graded_path, allow_pickle=False) - pixels)) < 1e-7
    illegal_path, legal_path = tmp_path / "illegal.npy", tmp_path / "legal.npy"
    np.save(illegal_path, np.array([[[-0.1, 0.3, 1.2]]]))
    legal = port.execute(
        ChronosCommand(
            "grading.legalize_ebu_r103", (str(illegal_path),), {"output": str(legal_path)}
        )
    )
    assert legal.metadata["illegal_after"] == "0.0"
    assert np.all(
        (np.load(legal_path, allow_pickle=False) >= 0)
        & (np.load(legal_path, allow_pickle=False) <= 1)
    )
