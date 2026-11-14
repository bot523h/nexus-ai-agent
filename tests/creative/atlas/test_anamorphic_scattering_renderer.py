"""WAVE 23 — tests for the anamorphic volumetric scattering renderer.

Checks the Rayleigh ``lambda^-4`` ratios, the Henyey-Greenstein sphere
normalisation, Beer-Lambert transmittance, the energy-preserving anamorphic
bokeh kernel, and the acoustic / relativistic-optical Doppler ratios (including
the sonic-boom and luminal refusals).
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from nexus_ai_agent.creative.atlas import anamorphic_scattering_renderer as asr

# --- Rayleigh / Mie volumetric field ---------------------------------------------


def test_henyey_greenstein_normalises_to_one() -> None:
    field = asr.compute_volumetric_mie_rayleigh_field(g=0.6, extinction_coefficient=0.1, depth=10.0)
    assert abs(field.phase_normalisation - 1.0) < 1e-10


@pytest.mark.parametrize("g", [-0.9, -0.5, 0.0, 0.5, 0.9])
def test_henyey_greenstein_normalisation_across_g(g: float) -> None:
    field = asr.compute_volumetric_mie_rayleigh_field(g=g, extinction_coefficient=0.2, depth=5.0)
    assert abs(field.phase_normalisation - 1.0) < 1e-9


@pytest.mark.parametrize("g", [1.0, -1.0, 1.5])
def test_henyey_greenstein_rejects_out_of_range_g(g: float) -> None:
    with pytest.raises(asr.ScatteringError):
        asr.henyey_greenstein(np.array([0.0]), g)


def test_henyey_greenstein_rejects_bad_cosine() -> None:
    with pytest.raises(asr.ScatteringError):
        asr.henyey_greenstein(np.array([2.0]), 0.5)


def test_rayleigh_ratios_follow_lambda_minus_four() -> None:
    field = asr.compute_volumetric_mie_rayleigh_field(g=0.5, extinction_coefficient=0.1, depth=1.0)
    ratios = field.rayleigh_ratios
    assert ratios["B"] > ratios["G"] > ratios["R"]
    expected = (asr.RGB_WAVELENGTHS_NM["R"] / asr.RGB_WAVELENGTHS_NM["B"]) ** 4
    assert ratios["B"] / ratios["R"] == pytest.approx(expected)


def test_transmittance_is_beer_lambert() -> None:
    field = asr.compute_volumetric_mie_rayleigh_field(g=0.3, extinction_coefficient=0.5, depth=2.0)
    assert field.transmittance == pytest.approx(math.exp(-1.0))
    assert 0.0 < field.transmittance <= 1.0


def test_field_rejects_bad_quadrature_nodes() -> None:
    with pytest.raises(asr.ScatteringError):
        asr.compute_volumetric_mie_rayleigh_field(
            g=0.5, extinction_coefficient=0.1, depth=1.0, quadrature_nodes=4
        )


def test_field_rejects_negative_extinction() -> None:
    with pytest.raises(asr.ScatteringError):
        asr.compute_volumetric_mie_rayleigh_field(g=0.5, extinction_coefficient=-0.1, depth=1.0)


def test_field_rejects_negative_depth() -> None:
    with pytest.raises(asr.ScatteringError):
        asr.compute_volumetric_mie_rayleigh_field(g=0.5, extinction_coefficient=0.1, depth=-1.0)


def test_field_serialises() -> None:
    field = asr.compute_volumetric_mie_rayleigh_field(g=0.5, extinction_coefficient=0.1, depth=1.0)
    payload = field.to_dict()
    assert payload["kind"] == "atlas.volumetric_scattering_field"
    assert payload["phase_g"] == 0.5


# --- anamorphic bokeh ------------------------------------------------------------


def test_circle_of_confusion_is_zero_at_focus() -> None:
    assert asr.circle_of_confusion_radius(5.0, 5.0, 2.0, 50.0) == 0.0


def test_circle_of_confusion_grows_with_defocus() -> None:
    near = asr.circle_of_confusion_radius(4.0, 5.0, 2.0, 50.0)
    far = asr.circle_of_confusion_radius(2.0, 5.0, 2.0, 50.0)
    assert far > near


def test_circle_of_confusion_rejects_bad_f_number() -> None:
    with pytest.raises(asr.ScatteringError):
        asr.circle_of_confusion_radius(5.0, 5.0, 0.0, 50.0)


def test_bokeh_kernel_is_energy_normalised() -> None:
    kernel = asr.render_anamorphic_cinema_frame(coc_radius=4.0, squeeze=2.0, side=33)
    assert kernel.energy == pytest.approx(1.0, abs=1e-9)


def test_bokeh_elliptical_axes() -> None:
    kernel = asr.render_anamorphic_cinema_frame(coc_radius=4.0, squeeze=2.0, side=33)
    assert kernel.semi_axis_y == pytest.approx(4.0)
    assert kernel.semi_axis_x == pytest.approx(2.0)


def test_bokeh_squeeze_one_is_circular() -> None:
    kernel = asr.render_anamorphic_cinema_frame(coc_radius=3.0, squeeze=1.0, side=33)
    assert kernel.semi_axis_x == pytest.approx(kernel.semi_axis_y)


def test_bokeh_streak_preserves_normalisation() -> None:
    kernel = asr.render_anamorphic_cinema_frame(
        coc_radius=4.0, squeeze=2.0, side=33, streak_length=8.0
    )
    assert kernel.energy == pytest.approx(1.0, abs=1e-9)
    assert kernel.streak_energy == pytest.approx(0.15)


@pytest.mark.parametrize("squeeze", asr.ANAMORPHIC_SQUEEZES)
def test_bokeh_accepts_standard_squeezes(squeeze: float) -> None:
    kernel = asr.render_anamorphic_cinema_frame(coc_radius=2.0, squeeze=squeeze, side=17)
    assert kernel.squeeze == squeeze


def test_bokeh_rejects_unknown_squeeze() -> None:
    with pytest.raises(asr.ScatteringError):
        asr.render_anamorphic_cinema_frame(coc_radius=2.0, squeeze=1.5, side=17)


def test_bokeh_rejects_even_side() -> None:
    with pytest.raises(asr.ScatteringError):
        asr.render_anamorphic_cinema_frame(coc_radius=2.0, squeeze=1.0, side=16)


def test_bokeh_rejects_tiny_side() -> None:
    with pytest.raises(asr.ScatteringError):
        asr.render_anamorphic_cinema_frame(coc_radius=2.0, squeeze=1.0, side=3)


def test_bokeh_rejects_non_positive_coc() -> None:
    with pytest.raises(asr.ScatteringError):
        asr.render_anamorphic_cinema_frame(coc_radius=0.0, squeeze=1.0, side=17)


def test_bokeh_rejects_negative_streak() -> None:
    with pytest.raises(asr.ScatteringError):
        asr.render_anamorphic_cinema_frame(coc_radius=2.0, squeeze=1.0, side=17, streak_length=-1.0)


def test_bokeh_serialises() -> None:
    kernel = asr.render_anamorphic_cinema_frame(coc_radius=2.0, squeeze=1.8, side=17)
    payload = kernel.to_dict()
    assert payload["kind"] == "atlas.anamorphic_bokeh_kernel"
    assert payload["squeeze"] == 1.8


# --- Doppler ---------------------------------------------------------------------


def test_acoustic_doppler_ratio() -> None:
    result = asr.compute_doppler_shifted_signal(
        source_frequency=440.0, source_velocity=50.0, mode="acoustic"
    )
    expected = (asr.SPEED_OF_SOUND + 0.0) / (asr.SPEED_OF_SOUND - 50.0)
    assert result.ratio == pytest.approx(expected)


def test_acoustic_doppler_with_observer_motion() -> None:
    result = asr.compute_doppler_shifted_signal(
        source_frequency=440.0, source_velocity=10.0, observer_velocity=20.0, mode="acoustic"
    )
    expected = (asr.SPEED_OF_SOUND + 20.0) / (asr.SPEED_OF_SOUND - 10.0)
    assert result.ratio == pytest.approx(expected)


def test_acoustic_doppler_refuses_sonic_boom() -> None:
    with pytest.raises(asr.ScatteringError):
        asr.compute_doppler_shifted_signal(
            source_frequency=440.0, source_velocity=asr.SPEED_OF_SOUND, mode="acoustic"
        )


def test_acoustic_doppler_refuses_fast_observer() -> None:
    with pytest.raises(asr.ScatteringError):
        asr.compute_doppler_shifted_signal(
            source_frequency=440.0,
            source_velocity=1.0,
            observer_velocity=asr.SPEED_OF_SOUND + 1.0,
            mode="acoustic",
        )


def test_optical_doppler_ratio() -> None:
    beta = 0.5
    result = asr.compute_doppler_shifted_signal(
        source_frequency=440.0, source_velocity=beta * asr.SPEED_OF_LIGHT, mode="optical"
    )
    assert result.ratio == pytest.approx(math.sqrt((1.0 + beta) / (1.0 - beta)))


def test_optical_doppler_redshift_is_less_than_one() -> None:
    result = asr.compute_doppler_shifted_signal(
        source_frequency=440.0, source_velocity=-0.3 * asr.SPEED_OF_LIGHT, mode="optical"
    )
    assert result.ratio < 1.0


def test_optical_doppler_refuses_luminal_speed() -> None:
    with pytest.raises(asr.ScatteringError):
        asr.compute_doppler_shifted_signal(
            source_frequency=440.0, source_velocity=asr.SPEED_OF_LIGHT, mode="optical"
        )


def test_doppler_rejects_unknown_mode() -> None:
    with pytest.raises(asr.ScatteringError):
        asr.compute_doppler_shifted_signal(source_frequency=440.0, source_velocity=1.0, mode="warp")


def test_doppler_rejects_bad_frequency() -> None:
    with pytest.raises(asr.ScatteringError):
        asr.compute_doppler_shifted_signal(
            source_frequency=0.0, source_velocity=1.0, mode="acoustic"
        )


def test_doppler_rejects_bad_amplitude() -> None:
    with pytest.raises(asr.ScatteringError):
        asr.compute_doppler_shifted_signal(
            source_frequency=440.0, source_velocity=1.0, amplitude=1.5
        )


def test_doppler_synthesises_phase_continuous_waveform() -> None:
    result = asr.compute_doppler_shifted_signal(
        source_frequency=440.0, source_velocity=10.0, duration=0.1, mode="acoustic"
    )
    assert result.waveform is not None
    assert result.waveform.shape == (4800,)
    assert np.all(np.abs(result.waveform) <= 1.0)


def test_doppler_waveform_starts_at_zero_phase() -> None:
    result = asr.compute_doppler_shifted_signal(
        source_frequency=440.0, source_velocity=10.0, duration=0.01, mode="acoustic"
    )
    assert result.waveform is not None
    assert result.waveform[0] == pytest.approx(0.0, abs=1e-12)


def test_doppler_refuses_beyond_nyquist() -> None:
    with pytest.raises(asr.ScatteringError):
        asr.compute_doppler_shifted_signal(
            source_frequency=30_000.0, source_velocity=100.0, duration=0.01, mode="acoustic"
        )


def test_doppler_result_serialises() -> None:
    result = asr.compute_doppler_shifted_signal(
        source_frequency=440.0, source_velocity=10.0, mode="acoustic"
    )
    payload = result.to_dict()
    assert payload["kind"] == "atlas.doppler_shift"
    assert payload["mode"] == "acoustic"
