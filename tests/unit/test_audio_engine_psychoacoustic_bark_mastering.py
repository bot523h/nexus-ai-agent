"""K4: Bark physics, protected-band conservation and post-process metering."""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.audio_engine import psychoacoustic_bark_mastering as k4
from nexus_ai_agent.creative.audio_engine.loudness_bs1770 import (
    BS1770Measurement,
    measure_bs1770_numpy,
)


def _stems(fs=16000, seconds=2):
    t = np.arange(round(fs * seconds)) / fs
    fade = np.sin(np.pi * t / seconds) ** 2
    dialogue = fade * 0.06 * (np.sin(2 * np.pi * 1250 * t) + np.sin(2 * np.pi * 2500 * t))
    music = fade * (
        0.12 * (np.sin(2 * np.pi * 1250 * t) + np.sin(2 * np.pi * 2500 * t))
        + 0.03 * np.sin(2 * np.pi * 160 * t)
        + 0.03 * np.sin(2 * np.pi * 7000 * t)
    )
    sfx = fade * 0.01 * np.sin(2 * np.pi * 2500 * t)
    return dialogue, music, sfx


def _amplitude(samples, fs, frequency):
    t = np.arange(samples.size) / fs
    return 2 * abs(samples @ np.exp(-2j * np.pi * frequency * t)) / samples.size


def test_24_zwicker_bands_partition_every_bin_without_gaps_or_double_counting():
    frequencies = np.fft.rfftfreq(2048, 1 / 48000)
    weights = k4.bark_band_weights(frequencies)
    assert weights.shape == (24, 1025)
    np.testing.assert_array_equal(weights.sum(axis=0), np.ones(1025))
    assert len(k4.BARK_EDGES_HZ) == 25 and k4.BARK_EDGES_HZ[-1] == 15500
    bark = k4.zwicker_bark(np.array([0, 1000, 6000, 20000]))
    assert bark[0] == 0 and 8.5 < bark[1] < 8.6
    assert np.all(np.diff(bark) > 0)


def test_spreading_is_asymmetric_and_upper_slope_depends_on_actual_level():
    lower = k4.spreading_db(np.array([7, 6]), np.array([8, 8]), np.array([40, 100]))
    np.testing.assert_array_equal(lower, [-25, -50])
    upper = k4.spreading_db(np.array([9, 9, 9]), np.array([8, 8, 8]), np.array([40, 60, 110]))
    np.testing.assert_allclose(upper, [-27, -19.6, -10], atol=1e-14)
    assert k4.spreading_db(np.array([8]), np.array([8]), np.array([60]))[0] == 0


def test_conflicting_music_and_sfx_are_carved_to_speech_plus_6db_with_bass_air_preserved():
    voice, music, sfx = _stems()
    result = k4.carve_speech_masking(voice, music, sfx, sample_rate=16000)
    np.testing.assert_array_equal(result.dialogue[0], voice)
    assert result.margin_reached
    assert np.min(result.input_margin_db[result.active_speech_bands]) < -6
    assert np.min(result.output_margin_db[result.active_speech_bands]) >= 5.95
    assert result.band_gains.shape[1] == 24
    assert np.min(result.band_gains) < 0.5 and np.max(result.band_gains) <= 1
    for hz in (160, 7000):
        ratio = _amplitude(result.music[0], 16000, hz) / _amplitude(music, 16000, hz)
        assert abs(20 * np.log10(ratio)) < 0.01
    # Independently evaluate the output waveform at the known sinusoidal
    # frequencies, not just the model's internal gain/margin bookkeeping.
    for hz in (1250, 2500):
        voice_amplitude = _amplitude(voice, 16000, hz)
        competing = _amplitude(result.music[0] + result.sfx_foley[0], 16000, hz)
        assert 20 * np.log10(voice_amplitude / competing) >= 5.9
    assert result.spl_at_full_scale_db == 100


def test_nonconflicting_bands_and_absent_speech_are_not_broadband_ducked():
    voice, music, sfx = _stems()
    result = k4.carve_speech_masking(np.zeros_like(voice), music, sfx, sample_rate=16000)
    assert not np.any(result.active_speech_bands) and result.margin_reached
    np.testing.assert_array_equal(result.band_gains, np.ones_like(result.band_gains))
    np.testing.assert_allclose(result.music[0], music, rtol=0, atol=3e-16)
    np.testing.assert_allclose(result.sfx_foley[0], sfx, rtol=0, atol=1e-16)


def test_protected_masker_can_make_the_margin_infeasible_and_that_is_reported():
    fs = 16000
    t = np.arange(fs) / fs
    voice = 0.00001 * np.sin(2 * np.pi * 850 * t)
    music = 0.8 * np.sin(2 * np.pi * 750 * t)  # protected below the 800 Hz carve corner
    result = k4.carve_speech_masking(voice, music, music * 0, sample_rate=fs)
    assert not result.margin_reached
    assert np.min(result.output_margin_db[result.active_speech_bands]) < 6
    assert _amplitude(result.music[0], fs, 750) / _amplitude(music, fs, 750) > 0.99


def test_stereo_gains_are_linked_preserving_channel_relationships():
    stems = _stems(seconds=1)
    stereo = [np.vstack([x, x * 0.37]) for x in stems]
    result = k4.carve_speech_masking(*stereo, sample_rate=16000)
    np.testing.assert_allclose(result.music[1], result.music[0] * 0.37, atol=2e-16)
    np.testing.assert_allclose(result.sfx_foley[1], result.sfx_foley[0] * 0.37, atol=2e-17)


def test_three_stem_master_measures_minus_24_lkfs_and_actual_true_peak():
    result = k4.master_dialogue_music_sfx(*_stems(seconds=1), sample_rate=16000)
    fresh = measure_bs1770_numpy(result.samples, sample_rate=16000)
    assert fresh.lufs is not None and abs(fresh.lufs + 24) <= 0.1
    assert fresh.true_peak_dbtp is not None and fresh.true_peak_dbtp <= -1
    assert result.measurement == fresh
    assert result.samples.shape == (1, 16000) and result.gain_db != 0


def test_high_crest_master_limits_peaks_without_lying_about_integrated_loudness():
    fs = 16000
    t = np.arange(fs) / fs
    voice = 0.01 * np.sin(2 * np.pi * 500 * t)
    voice[4000] = 4.0
    voice[12000] = -3.0
    result = k4.master_dialogue_music_sfx(voice, voice * 0, voice * 0, sample_rate=fs)
    assert result.limiter_passes > 0
    fresh = measure_bs1770_numpy(result.samples, sample_rate=fs)
    assert fresh.lufs is not None and abs(fresh.lufs + 24) <= 0.1
    assert fresh.true_peak_dbtp is not None and fresh.true_peak_dbtp <= -1


@pytest.mark.parametrize(
    "attack",
    [
        "frequency",
        "rank",
        "spreading",
        "geometry",
        "empty",
        "channels",
        "nonfinite",
        "rate",
        "calibration",
        "cut",
        "margin",
    ],
)
def test_invalid_masking_contracts_refuse(attack):
    stems = list(_stems(seconds=0.1))
    kwargs = dict(sample_rate=16000)
    if attack == "frequency":
        with pytest.raises(ValueError):
            k4.zwicker_bark(np.array([-1]))
        return
    if attack == "rank":
        with pytest.raises(ValueError):
            k4.bark_band_weights(np.ones((2, 2)))
        return
    if attack == "spreading":
        with pytest.raises(ValueError):
            k4.spreading_db(np.array([np.inf]), np.ones(1), np.ones(1))
        return
    if attack == "geometry":
        stems[1] = stems[1][:-1]
    if attack == "empty":
        stems = [np.zeros(0)] * 3
    if attack == "channels":
        stems[1] = np.zeros((3, 1600))
    if attack == "nonfinite":
        stems[0][0] = np.nan
    if attack == "rate":
        kwargs["sample_rate"] = 1000
    if attack == "calibration":
        kwargs["spl_at_full_scale_db"] = float("nan")
    if attack == "cut":
        kwargs["max_cut_db"] = 0
    if attack == "margin":
        kwargs["margin_db"] = 20
    with pytest.raises(ValueError):
        k4.carve_speech_masking(*stems, **kwargs)


def test_silence_and_too_short_programmes_cannot_invent_broadcast_measurements():
    with pytest.raises(ValueError, match="400 ms"):
        k4.master_dialogue_music_sfx(*_stems(seconds=0.1), sample_rate=16000)
    with pytest.raises(ValueError, match="silence/below-gate"):
        k4.master_dialogue_music_sfx(*[np.zeros(16000)] * 3, sample_rate=16000)


def test_nonconvergence_is_a_refusal_not_a_fabricated_pass(monkeypatch):
    measurement = BS1770Measurement(-24, None, None, 0, 0, 0)
    monkeypatch.setattr(k4, "measure_bs1770_numpy", lambda *a, **k: measurement)
    monkeypatch.setattr(k4, "true_peak_dbtp", lambda *a, **k: 0.0)
    with pytest.raises(ValueError, match="did not converge"):
        k4.master_dialogue_music_sfx(*_stems(seconds=0.5), sample_rate=16000)
