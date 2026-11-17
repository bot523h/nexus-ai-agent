"""WAVE 27 — tests for diegetic modal foley."""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.atlas import diegetic_modal_foley as f


def test_materials_have_matching_modes_and_decays() -> None:
    for m in f.MATERIALS.values():
        assert len(m.mode_ratios) == len(m.decay_times)


def test_impact_is_deterministic() -> None:
    a = f.synthesise_impact(f.MATERIALS["glass"], seed=1)
    b = f.synthesise_impact(f.MATERIALS["glass"], seed=1)
    assert np.array_equal(a.samples, b.samples)


def test_impact_changes_with_seed() -> None:
    a = f.synthesise_impact(f.MATERIALS["glass"], seed=1)
    b = f.synthesise_impact(f.MATERIALS["glass"], seed=2)
    assert not np.array_equal(a.samples, b.samples)


def test_impact_peak_equals_amplitude() -> None:
    imp = f.synthesise_impact(f.MATERIALS["wood"], amplitude=0.5)
    assert imp.peak == pytest.approx(0.5)
    assert np.abs(imp.samples).max() == pytest.approx(0.5)


def test_impact_mode_frequencies_are_ratios_of_base() -> None:
    imp = f.synthesise_impact(f.MATERIALS["metal"])
    np.testing.assert_allclose(
        imp.frequencies,
        f.MATERIALS["metal"].base_frequency * np.array(f.MATERIALS["metal"].mode_ratios),
    )


def test_impact_energy_decays_over_time() -> None:
    imp = f.synthesise_impact(f.MATERIALS["stone"], duration=1.0)
    early = float((imp.samples[:2000] ** 2).mean())
    late = float((imp.samples[-2000:] ** 2).mean())
    assert early > late


def test_impact_sample_count() -> None:
    imp = f.synthesise_impact(f.MATERIALS["wood"], sample_rate=8000, duration=0.5)
    assert imp.samples.size == 4000


def test_higher_excitation_increases_brightness() -> None:
    dull = f.synthesise_impact(f.MATERIALS["metal"], excitation=0.5, duration=0.3)
    bright = f.synthesise_impact(f.MATERIALS["metal"], excitation=1.5, duration=0.3)

    # Compare high-frequency energy via spectral centroid.
    def centroid(x: np.ndarray) -> float:
        spec = np.abs(np.fft.rfft(x))
        freqs = np.fft.rfftfreq(x.size, d=1.0 / 48000)
        return float((spec * freqs).sum() / (spec.sum() + 1e-12))

    assert centroid(bright.samples) > centroid(dull.samples)


def test_placement_applies_sound_delay() -> None:
    ev = [f.DiegeticEvent("thunder", 0.0, 343.0, "stone")]
    rep = f.place_foley_events(ev, duration=2.0)
    assert rep.delays[0] == pytest.approx(1.0)
    assert rep.heard_times[0] == pytest.approx(1.0)


def test_placement_skips_events_outside_timeline() -> None:
    ev = [f.DiegeticEvent("late", 100.0, 0.0, "wood")]
    rep = f.place_foley_events(ev, duration=1.0)
    assert rep.placed_labels == ()
    assert rep.peak == 0.0


def test_placement_mixes_multiple_events() -> None:
    ev = [f.DiegeticEvent("a", 0.0, 0.0, "wood"), f.DiegeticEvent("b", 0.5, 0.0, "glass")]
    rep = f.place_foley_events(ev, duration=2.0)
    assert set(rep.placed_labels) == {"a", "b"}
    assert rep.mix.size == 96000


def test_placement_is_deterministic() -> None:
    ev = [f.DiegeticEvent("a", 0.0, 10.0, "wood", seed=3)]
    a = f.place_foley_events(ev, duration=2.0)
    b = f.place_foley_events(ev, duration=2.0)
    assert np.array_equal(a.mix, b.mix)


def test_ir_has_unit_energy() -> None:
    ir = f.exponential_ir(rt60=0.5)
    assert float((ir**2).sum()) == pytest.approx(1.0)


def test_ir_decays_by_60db() -> None:
    ir = f.exponential_ir(rt60=1.0, seed=0, pre_delay=0.0)
    # Envelope ratio from start to end should be ~1e-3 (-60 dB).
    half = ir.size // 2
    early = float(np.sqrt((ir[:half] ** 2).mean()))
    late = float(np.sqrt((ir[half:] ** 2).mean()))
    assert late < early


def test_ir_pre_delay() -> None:
    ir = f.exponential_ir(rt60=0.1, pre_delay=0.05, sample_rate=1000)
    assert np.allclose(ir[:50], 0.0)


def test_convolution_length_and_determinism() -> None:
    s = np.ones(100)
    h = np.array([1.0, 0.5, 0.25])
    out = f.convolve_reverb(s, h)
    assert out.size == 102
    np.testing.assert_allclose(out[:3], [1.0, 1.5, 1.75])


def test_convolution_with_delta_is_identity() -> None:
    s = np.arange(10.0)
    delta = np.array([1.0])
    np.testing.assert_allclose(f.convolve_reverb(s, delta), s)


# --- adversarial / refusal --------------------------------------------------------


def test_refuse_empty_material_name() -> None:
    with pytest.raises(f.FoleyError):
        f.Material("", (1.0,), (1.0,), 100.0)


def test_refuse_mismatched_modes() -> None:
    with pytest.raises(f.FoleyError):
        f.Material("x", (1.0, 2.0), (1.0,), 100.0)


def test_refuse_empty_modes() -> None:
    with pytest.raises(f.FoleyError):
        f.Material("x", (), (), 100.0)


def test_refuse_negative_mode_ratio() -> None:
    with pytest.raises(f.FoleyError):
        f.Material("x", (-1.0,), (1.0,), 100.0)


def test_refuse_negative_decay() -> None:
    with pytest.raises(f.FoleyError):
        f.Material("x", (1.0,), (-1.0,), 100.0)


def test_refuse_bad_base_frequency() -> None:
    with pytest.raises(f.FoleyError):
        f.Material("x", (1.0,), (1.0,), 0.0)


def test_refuse_bad_sample_rate() -> None:
    with pytest.raises(f.FoleyError):
        f.synthesise_impact(f.MATERIALS["wood"], sample_rate=0)
    with pytest.raises(f.FoleyError):
        f.synthesise_impact(f.MATERIALS["wood"], sample_rate=f.MAX_SAMPLE_RATE + 1)


def test_refuse_bad_duration() -> None:
    with pytest.raises(f.FoleyError):
        f.synthesise_impact(f.MATERIALS["wood"], duration=0.0)


def test_refuse_bad_amplitude() -> None:
    with pytest.raises(f.FoleyError):
        f.synthesise_impact(f.MATERIALS["wood"], amplitude=0.0)


def test_refuse_bad_excitation() -> None:
    with pytest.raises(f.FoleyError):
        f.synthesise_impact(f.MATERIALS["wood"], excitation=0.0)


def test_refuse_bad_seed_impact() -> None:
    with pytest.raises(f.FoleyError):
        f.synthesise_impact(f.MATERIALS["wood"], seed=-1)


def test_refuse_nyquist_violation() -> None:
    # A low sample rate makes the high glass modes alias.
    with pytest.raises(f.FoleyError):
        f.synthesise_impact(f.MATERIALS["glass"], sample_rate=8000)


def test_refuse_unknown_material_in_event() -> None:
    with pytest.raises(f.FoleyError):
        f.DiegeticEvent("x", 0.0, 0.0, "unobtainium")


def test_refuse_empty_event_label() -> None:
    with pytest.raises(f.FoleyError):
        f.DiegeticEvent("", 0.0, 0.0, "wood")


def test_refuse_negative_screen_time() -> None:
    with pytest.raises(f.FoleyError):
        f.DiegeticEvent("x", -1.0, 0.0, "wood")


def test_refuse_negative_distance() -> None:
    with pytest.raises(f.FoleyError):
        f.DiegeticEvent("x", 0.0, -1.0, "wood")


def test_refuse_bad_event_amplitude() -> None:
    with pytest.raises(f.FoleyError):
        f.DiegeticEvent("x", 0.0, 0.0, "wood", amplitude=0.0)


def test_refuse_bad_event_seed() -> None:
    with pytest.raises(f.FoleyError):
        f.DiegeticEvent("x", 0.0, 0.0, "wood", seed=-1)


def test_refuse_bad_placement_duration() -> None:
    with pytest.raises(f.FoleyError):
        f.place_foley_events([], duration=0.0)


def test_refuse_bad_placement_sample_rate() -> None:
    with pytest.raises(f.FoleyError):
        f.place_foley_events([], duration=1.0, sample_rate=0)


def test_refuse_too_many_events() -> None:
    ev = [f.DiegeticEvent("x", 0.0, 0.0, "wood")]
    with pytest.raises(f.FoleyError):
        f.place_foley_events(ev * 3, duration=1.0, max_events=2)


def test_refuse_bad_rt60() -> None:
    with pytest.raises(f.FoleyError):
        f.exponential_ir(rt60=0.0)


def test_refuse_bad_pre_delay() -> None:
    with pytest.raises(f.FoleyError):
        f.exponential_ir(pre_delay=-1.0)


def test_refuse_bad_ir_seed() -> None:
    with pytest.raises(f.FoleyError):
        f.exponential_ir(seed=-1)


def test_refuse_bad_ir_sample_rate() -> None:
    with pytest.raises(f.FoleyError):
        f.exponential_ir(sample_rate=0)


def test_refuse_non_1d_convolution() -> None:
    with pytest.raises(f.FoleyError):
        f.convolve_reverb(np.zeros((4, 4)), np.ones(3))


def test_refuse_empty_convolution() -> None:
    with pytest.raises(f.FoleyError):
        f.convolve_reverb(np.array([]), np.ones(3))


def test_refuse_nan_convolution() -> None:
    with pytest.raises(f.FoleyError):
        f.convolve_reverb(np.array([1.0, np.nan]), np.ones(3))


def test_refuse_impact_too_many_samples() -> None:
    with pytest.raises(f.FoleyError):
        f.synthesise_impact(f.MATERIALS["wood"], sample_rate=f.MAX_SAMPLE_RATE, duration=200.0)


def test_refuse_placement_too_many_samples() -> None:
    with pytest.raises(f.FoleyError):
        f.place_foley_events([], duration=200.0, sample_rate=f.MAX_SAMPLE_RATE)


def test_refuse_ir_too_many_samples() -> None:
    with pytest.raises(f.FoleyError):
        f.exponential_ir(rt60=200.0, sample_rate=f.MAX_SAMPLE_RATE)
