"""Colour-core regression tests: the display path must emit sRGB, not AP1 coordinates.

Before this suite the ACES grade ended at ACEScg (AP1, D60) and was encoded with the
sRGB OETF as if it were already sRGB (D65). Neutral greys survive that mistake, so
the older tests could not see it; saturated primaries came out washed-out. The tests
below pin the output transform against an independent derivation from the published
chromaticities, not against the module's own constants.
"""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.atlas import aces_hald_film_optic as film
from nexus_ai_agent.creative.atlas import physical_frame_stream as physical

# Published chromaticities (x, y).
SRGB_PRIMARIES = ((0.64, 0.33), (0.30, 0.60), (0.15, 0.06))
SRGB_WHITE_D65 = (0.3127, 0.3290)
AP1_PRIMARIES = ((0.713, 0.293), (0.165, 0.830), (0.128, 0.044))
AP1_WHITE_D60 = (0.32168, 0.33767)
BRADFORD = np.array(
    [
        [0.8951, 0.2664, -0.1614],
        [-0.7502, 1.7135, 0.0367],
        [0.0389, -0.0685, 1.0296],
    ]
)


def _xyz_of_white(xy: tuple[float, float]) -> np.ndarray:
    x, y = xy
    return np.array([x / y, 1.0, (1.0 - x - y) / y])


def _rgb_to_xyz(
    primaries: tuple[tuple[float, float], ...], white: tuple[float, float]
) -> np.ndarray:
    """Standard RGB->XYZ matrix from primaries and a white point (column convention)."""
    cols = np.array([[x / y, 1.0, (1.0 - x - y) / y] for x, y in primaries]).T
    scale = np.linalg.solve(cols, _xyz_of_white(white))
    return cols * scale[None, :]


def _independent_ap1_to_srgb_d65() -> np.ndarray:
    """AP1(D60) -> sRGB(D65) derived from chromaticities alone (no module constants)."""
    cone_d60 = BRADFORD @ _xyz_of_white(AP1_WHITE_D60)
    cone_d65 = BRADFORD @ _xyz_of_white(SRGB_WHITE_D65)
    cat = np.linalg.solve(BRADFORD, np.diag(cone_d65 / cone_d60) @ BRADFORD)
    ap1_to_xyz = _rgb_to_xyz(AP1_PRIMARIES, AP1_WHITE_D60)
    srgb_to_xyz = _rgb_to_xyz(SRGB_PRIMARIES, SRGB_WHITE_D65)
    return np.linalg.solve(srgb_to_xyz, cat @ ap1_to_xyz)


def test_output_matrix_matches_independent_chromaticity_derivation() -> None:
    expected = _independent_ap1_to_srgb_d65()
    assert np.max(np.abs(film.AP1_TO_SRGB_D65 - expected)) < 1e-5


def test_output_transform_is_exact_inverse_of_forward_chain() -> None:
    forward = film.AP0_TO_AP1 @ film.SRGB_D65_TO_AP0_D60
    assert np.max(np.abs(film.AP1_TO_SRGB_D65 @ forward - np.eye(3))) < 1e-12


def test_srgb_primaries_survive_the_colour_round_trip() -> None:
    primaries = np.eye(3)
    ap1 = film.aces_ap0_to_acescg(film.linear_srgb_d65_to_aces_ap0_d60(primaries))
    back = ap1 @ film.AP1_TO_SRGB_D65.T
    assert np.max(np.abs(back - primaries)) < 1e-12


@pytest.mark.parametrize("level", [0.01, 0.18, 0.5, 2.0, 8.0])
def test_neutral_axis_stays_neutral_through_the_display_core(level: float) -> None:
    out = film.scene_linear_srgb_to_display_linear(np.full(3, level))
    assert np.ptp(out) < 1e-9
    assert np.all((out >= 0.0) & (out <= 1.0))


def test_display_core_is_monotone_in_luminance() -> None:
    levels = np.linspace(0.0, 16.0, 257)
    greys = np.stack([levels, levels, levels], axis=-1)
    luma = film.scene_linear_srgb_to_display_linear(greys)[:, 0]
    assert np.all(np.diff(luma) >= -1e-12)


def test_display_core_rejects_non_finite_input() -> None:
    with pytest.raises(film.FilmOpticError):
        film.scene_linear_srgb_to_display_linear(np.array([[0.1, np.nan, 0.2]]))


def test_render_and_grade_paths_both_use_the_shared_core(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, ...]] = []
    original = film.scene_linear_srgb_to_display_linear

    def spy(rgb: np.ndarray) -> np.ndarray:
        calls.append(tuple(np.shape(rgb)))
        return original(rgb)

    monkeypatch.setattr(film, "scene_linear_srgb_to_display_linear", spy)
    state = physical.OceanState(fps=8.0)
    atmos = physical.AtmosphereState()
    physical.generate_physical_atlas_frame_rgb24(0, 1, 16, 16, state, atmos, seed=3)
    assert calls, "rgb24 render path must route through the shared colour core"
    render_calls = len(calls)
    assert render_calls >= 1
    calls.clear()
    from nexus_ai_agent.creative.atlas import olympus_master as om

    clut = film.hald_identity_clut(8)
    om._grade_physical_frame(np.full((4, 4, 3), 0.3), clut=clut, seed=1)
    assert len(calls) == 1, "grading must apply the colour core exactly once"


def test_render_path_has_no_private_tone_curve() -> None:
    assert not hasattr(physical, "_tone_map")


def test_pure_green_and_blue_crosstalk_and_luminance_monotonicity() -> None:
    """Characterise standard ACES RRT->ODT (Narkowicz in AP1 -> AP1_TO_SRGB_D65).

    Because the tone curve runs per-channel in AP1 before multiplying by
    ``AP1_TO_SRGB_D65``, saturated pure sRGB green ``(0, 1, 0)`` and blue
    ``(0, 0, 1)`` undergo controlled crosstalk/desaturation into adjacent sRGB
    channels while staying bounded in ``[0, 1]`` and strictly monotone with scene
    luminance.
    """
    levels = np.array([0.05, 0.18, 0.5, 1.0, 4.0, 12.0])
    for channel_idx, primary in ((1, np.array([0.0, 1.0, 0.0])), (2, np.array([0.0, 0.0, 1.0]))):
        samples = levels[:, None] * primary[None, :]
        out = film.scene_linear_srgb_to_display_linear(samples)
        assert np.all((out >= 0.0) & (out <= 1.0))
        # Dominant primary stays strictly greater than the crosstalk channels at every level
        other_max = np.max(np.delete(out, channel_idx, axis=1), axis=1)
        assert np.all(out[:, channel_idx] > other_max)
        # Dominant channel is monotonically non-decreasing (strict before clipping) and
        # total sRGB display luminance increases strictly with exposure.
        assert np.all(np.diff(out[:, channel_idx]) >= -1e-12)
        assert np.all(np.diff(out[:4, channel_idx]) > 0.0)
        luma = out @ np.array([0.2126, 0.7152, 0.0722])
        assert np.all(np.diff(luma) > 0.0)
        # Highlight desaturation: at high exposure, adjacent channels exhibit positive crosstalk
        assert np.all(out[-1, :] > 0.1)
