"""Deterministic decoder and tempo edge coverage, including real 24-bit PCM."""

from __future__ import annotations

import wave
from contextlib import nullcontext
from types import SimpleNamespace

import numpy as np
import pytest

from nexus_ai_agent.creative.slideshow import audio as audio


def _wav(path, values, width, channels=1):
    if width == 1:
        raw = bytes(values)
    elif width == 3:
        raw = b"".join((int(value) & 0xFFFFFF).to_bytes(3, "little") for value in values)
    else:
        raw = b"".join(int(value).to_bytes(width, "little", signed=True) for value in values)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(48000)
        handle.writeframes(raw)
    return path


@pytest.mark.parametrize(
    ("width", "values", "expected"),
    [
        (1, [0, 128, 255], [-1, 0, 127 / 128]),
        (2, [-32768, 0, 32767], [-1, 0, 32767 / 32768]),
        (
            3,
            [-8388608, -4194304, -1, 0, 1, 4194304, 8388607],
            [-1, -0.5, -1 / 8388608, 0, 1 / 8388608, 0.5, 8388607 / 8388608],
        ),
        (4, [-2147483648, 0, 2147483647], [-1, 0, 2147483647 / 2147483648]),
    ],
)
def test_every_integer_pcm_width_decodes_to_the_actual_normalized_samples(
    tmp_path, width, values, expected
):
    decoded = audio.read_wav_mono(_wav(tmp_path / "samples.wav", values, width))
    np.testing.assert_allclose(decoded.samples, expected, atol=1e-8)
    assert decoded.samples.dtype == np.float32
    assert decoded.sample_rate == 48000
    assert np.max(np.abs(decoded.samples)) <= 1


def test_stereo_downmix_and_real_file_entry_points(tmp_path):
    source = _wav(tmp_path / "stereo.wav", [-32768, 32767, 0, 16384], 2, 2)
    pcm = audio.read_wav_mono(source)
    np.testing.assert_allclose(pcm.samples, [-1 / 65536, 0.25])
    assert audio.beat_grid_for_file(source, fallback_bpm=120).source_path == str(source)
    assert audio.tempo_evidence_for_file(source).onset_count == 0
    assert audio.tempo_evidence_for_file(None, pcm=pcm).duration_us == pcm.duration_us
    assert audio.PcmAudio(np.ones(8), 0).duration_us == 0
    with pytest.raises(audio.AudioError, match="cannot read WAV"):
        audio.read_wav_mono(tmp_path / "absent.wav")
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not a wave file")
    with pytest.raises(audio.AudioError):
        audio.read_wav_mono(bad)


@pytest.mark.parametrize(("width", "raw"), [(3, b"\x00\x00\x40x"), (5, b"x" * 5)])
def test_truncated_triplet_and_unsupported_width_are_explicit(monkeypatch, width, raw):
    handle = SimpleNamespace(
        getnchannels=lambda: 1,
        getsampwidth=lambda: width,
        getframerate=lambda: 48000,
        getnframes=lambda: 2,
        readframes=lambda count: raw,
    )
    monkeypatch.setattr(wave, "open", lambda *a, **k: nullcontext(handle))
    if width == 5:
        with pytest.raises(audio.AudioError, match="unsupported WAV sample width"):
            audio.read_wav_mono("fake.wav")
    else:
        np.testing.assert_array_equal(audio.read_wav_mono("fake.wav").samples, [0.5])


def test_envelope_refusals_and_peak_spacing():
    assert audio.frame_rms(np.zeros(0), 48000).size == 0
    np.testing.assert_allclose(audio.frame_rms(np.ones(8), 1), 1)
    for envelope in (np.zeros(2), np.ones(10), np.linspace(100, 101, 20)):
        assert audio._pick_onsets(envelope, 48000)[0].size == 0
    envelope = np.zeros(100)
    envelope[[10, 12, 40]] = [2, 3, 2]
    times, strengths = audio._pick_onsets(envelope, 48000)
    np.testing.assert_allclose(times, [0.1, 0.4])
    assert strengths.size == 2
    novelty, hop = audio.energy_novelty_envelope(np.zeros(1), 48000)
    assert novelty.size == 0 and hop == audio.HOP_SECONDS
    novelty, _ = audio.energy_novelty_envelope(np.r_[np.zeros(4800), np.ones(4800)], 48000)
    assert np.max(novelty) > 0 and np.all(novelty >= 0)


def test_spectral_flux_degenerate_frames_and_nyquist_bands():
    for samples, rate in ((np.ones(2048), 48000), (np.ones(4096), 0), (np.zeros(3), 48000)):
        assert audio.spectral_flux_envelope(samples, rate)[0].size == 0
    flux, hop = audio.spectral_flux_envelope(np.ones(4096), 200)
    assert np.isfinite(flux).all() and hop > 0
    for flux, hop in ((np.zeros(3), 0.1), (np.ones(2), 0.1), (np.ones(4), 0)):
        assert audio._pick_onsets_from_flux(flux, hop)[0].size == 0
    flux = np.zeros(100)
    flux[[10, 12, 45]] = [2, 3, 2]
    np.testing.assert_allclose(audio._pick_onsets_from_flux(flux, 0.01)[0], [0.1, 0.45])


def test_autocorrelation_refuses_non_evidence_and_interpolates_a_period():
    kwargs = dict(hop_seconds=0.01, min_period=0.3, max_period=1.2)
    for strength in (np.ones(3), np.ones(100)):
        assert audio._autocorrelation_period(strength, **kwargs) is None
    assert audio._autocorrelation_period(np.arange(100), **(kwargs | {"hop_seconds": 0})) is None
    assert audio._autocorrelation_period(np.arange(100), **(kwargs | {"min_period": 2})) is None
    signal = np.cos(2 * np.pi * np.arange(1000) / 50.2)
    period = audio._autocorrelation_period(signal, **kwargs)
    assert period == pytest.approx(0.502, abs=0.002)
    assert (
        audio._autocorrelation_period(
            np.array([1.0, -1.0, 1.0, -1.0]), hop_seconds=1, min_period=1, max_period=1.1
        )
        is None
    )


def test_autocorrelation_nonpositive_energy_is_not_normalized(monkeypatch):
    monkeypatch.setattr(np.fft, "irfft", lambda *a, **k: np.zeros(20))
    assert (
        audio._autocorrelation_period(np.arange(10), hop_seconds=0.01, min_period=0.1, max_period=1)
        is None
    )


@pytest.mark.parametrize(
    ("length", "lag", "hop"), [(0, 2, 0.1), (20, 0, 0.1), (20, 2, 0), (4, 10, 0.1), (4, 2, 0.1)]
)
def test_comb_cannot_score_empty_or_single_tooth_evidence(length, lag, hop):
    assert audio._comb_score(np.ones(length), lag_frames=lag, hop_seconds=hop) == (0, 0)


def test_comb_energy_and_weighted_coverage_have_distinct_meanings():
    kwargs = dict(lag_frames=10, hop_seconds=0.1)
    assert audio._comb_score(np.zeros(100), **kwargs) == (0, 0)
    assert audio._comb_score(np.ones(100), **kwargs) == (1, 1)
    onsets = np.array([1.0, 1.5, 2.0, 2.5])
    score, coverage = audio._comb_score(np.ones(100), onsets=onsets, **kwargs)
    assert score == coverage == 0.5
    score, coverage = audio._comb_score(np.ones(100), onsets=onsets, weights=np.zeros(4), **kwargs)
    assert score == 1 and coverage == 0
    a, b = audio._explained_split(envelope=np.ones(100), onsets=onsets, weights=None, **kwargs)
    assert a == b == 1
    assert audio._tempo_prior(0) == 0
    assert audio._tempo_prior(120) == 1


@pytest.mark.parametrize(
    ("length", "lag", "onsets"), [(0, 2, [1]), (10, 0, [1]), (10, 2, []), (4, 10, [1]), (4, 2, [1])]
)
def test_explained_split_degenerate_geometry(length, lag, onsets):
    assert audio._explained_split(
        envelope=np.ones(length),
        lag_frames=lag,
        hop_seconds=0.1,
        onsets=np.array(onsets),
        weights=None,
    ) == (0, 0)


def test_octave_refusals_and_zero_energy_do_not_invent_a_tempo():
    kwargs = dict(envelope=np.zeros(100), hop_seconds=0.01, min_period=0.3, max_period=1.2)
    assert audio._resolve_octave(0, **kwargs) == (0, "as_is", 0)
    assert audio._resolve_octave(0.5, **kwargs) == (0.5, "as_is", 0)
    result = audio._resolve_octave(0.4, onsets=np.array([0.8]), **kwargs)
    assert result == (0.4, "as_is", 2)
    assert audio.tempo_uncertainty(np.ones(1), mean_period=0.5, hop_seconds=0.01) == (0, 0, 0, 0, 1)
    assert audio._grid(0, 0, 1_000_000) == ()


def test_histogram_refusals_are_deterministic(monkeypatch):
    assert audio._dominant_period(np.arange(20) * 0.01) == (None, 1)
    monkeypatch.setattr(audio, "_interval_candidates", lambda *a: np.full(10, 0.34))
    assert audio._dominant_period(
        np.arange(20), min_period_seconds=0.3, max_period_seconds=0.32
    ) == (None, 1)
    monkeypatch.setattr(
        audio, "_interval_candidates", lambda *a: np.array([0.001, 0.001, 0.015, 0.015])
    )
    period, variation = audio._dominant_period(
        np.arange(20), min_period_seconds=0, max_period_seconds=0.02
    )
    assert period is None and variation > audio.MAX_PERIOD_VARIATION


@pytest.mark.parametrize(("min_bpm", "max_bpm"), [(0, 120), (120, 100)])
def test_invalid_tempo_windows_fail_before_analysis(min_bpm, max_bpm):
    with pytest.raises(audio.AudioError, match="invalid tempo window"):
        audio.collect_tempo_candidates(
            audio.PcmAudio(np.zeros(48000), 48000), min_bpm=min_bpm, max_bpm=max_bpm
        )


def test_empty_and_short_audio_have_no_fabricated_tempo():
    assert (
        audio.collect_tempo_candidates(audio.PcmAudio(np.zeros(0), 48000)).status == "empty_audio"
    )
    assert audio.collect_tempo_candidates(audio.PcmAudio(np.zeros(100), 48000)).tempo_bpm is None


def test_fallback_grid_retains_a_measured_anchor_and_detector_grid_confidence(monkeypatch):
    pcm = audio.PcmAudio(np.zeros(480000), 48000)
    onsets = np.arange(0.25, 10, 0.5)
    monkeypatch.setattr(audio, "_pick_onsets", lambda *a: (onsets, np.ones(onsets.size)))
    monkeypatch.setattr(audio, "_dominant_period", lambda *a, **k: (None, 0.5))
    grid = audio.estimate_beat_grid(pcm, fallback_bpm=120)
    assert grid.beats_us[0] == 250000
    assert grid.tempo_source == "fallback_bpm"
    monkeypatch.setattr(audio, "_dominant_period", lambda *a, **k: (0.5, 0))
    matched = audio.estimate_beat_grid(pcm, fallback_bpm=120)
    mismatched = audio.estimate_beat_grid(pcm, fallback_bpm=80)
    assert matched.confidence > mismatched.confidence
    assert matched.tempo_source == "detected"
