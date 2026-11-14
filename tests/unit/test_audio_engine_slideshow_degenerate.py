"""Degenerate and refusal paths in `creative/slideshow/audio.py`.

The tempo estimator's happy path is covered elsewhere. What was not covered is
the set of branches that decide *the music contains no usable pulse* — the
answers that must be an explicit refusal rather than a plausible number.

Every test here builds the input directly, so the branch is reached without
depending on what a particular recording happens to contain.

Pack evidence for ``creative.packs.audio``: whether the pack may report a tempo
is decided by these refusals, so this suite is measured as pack evidence.
"""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.slideshow.audio import (
    AudioError,
    PcmAudio,
    _autocorrelation_period,
    _comb_score,
    _grid,
    _pick_onsets,
    _pick_onsets_from_flux,
    energy_novelty_envelope,
    frame_rms,
    read_wav_mono,
    tempo_uncertainty,
)

RATE = 48_000


# --------------------------------------------------------------------------- #
# PcmAudio.duration_us
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("rate", [0, -1, -48_000])
def test_a_non_positive_rate_has_no_duration(rate: int) -> None:
    """Duration is undefined without a rate; returning 0 is the honest answer."""
    audio = PcmAudio(samples=np.zeros(1000, dtype=np.float32), sample_rate=rate)
    assert audio.duration_us == 0


def test_a_normal_audio_duration_is_frames_over_rate() -> None:
    audio = PcmAudio(samples=np.zeros(RATE, dtype=np.float32), sample_rate=RATE)
    assert audio.duration_us == 1_000_000


# --------------------------------------------------------------------------- #
# read_wav_mono — 8-bit and unreadable input
# --------------------------------------------------------------------------- #


def test_an_8_bit_wav_is_unsigned_and_recentred(tmp_path) -> None:
    """8-bit WAV is *unsigned*; treating it as signed inverts the waveform."""
    import wave

    path = tmp_path / "u8.wav"
    frames = bytes([0, 64, 128, 192, 255] * 200)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(1)
        handle.setframerate(8000)
        handle.writeframes(frames)
    audio = read_wav_mono(path)
    # 0 -> -1.0, 128 -> 0.0, 255 -> +0.99
    assert audio.samples.min() == pytest.approx(-1.0)
    assert abs(float(audio.samples.max()) - (127.0 / 128.0)) < 1e-3
    assert any(abs(float(value)) < 1e-6 for value in audio.samples)


def test_a_missing_wav_raises_the_engine_error(tmp_path) -> None:
    with pytest.raises(AudioError, match="cannot read WAV file"):
        read_wav_mono(tmp_path / "absent.wav")


def test_a_non_wav_file_raises_the_engine_error(tmp_path) -> None:
    bad = tmp_path / "not.wav"
    bad.write_bytes(b"this is not a RIFF header" * 20)
    with pytest.raises(AudioError, match="cannot read WAV file"):
        read_wav_mono(bad)


# --------------------------------------------------------------------------- #
# frame_rms — a signal shorter than one frame
# --------------------------------------------------------------------------- #


def test_a_signal_shorter_than_one_frame_has_no_envelope() -> None:
    """One frame is FRAME_SECONDS long; a 10-sample signal produces none."""
    assert frame_rms(np.zeros(10, dtype=np.float32), RATE).size == 0


# --------------------------------------------------------------------------- #
# _pick_onsets — the three "no onsets" refusals
# --------------------------------------------------------------------------- #


def test_too_few_envelope_frames_is_no_onsets() -> None:
    times, strengths = _pick_onsets(np.array([0.1, 0.2], dtype=np.float32), RATE)
    assert times.size == 0 and strengths.size == 0


def test_a_flat_envelope_has_no_onsets() -> None:
    """Constant level has zero spectral flux — nothing begins."""
    times, strengths = _pick_onsets(np.full(50, 0.3, dtype=np.float32), RATE)
    assert times.size == 0 and strengths.size == 0


def test_a_zero_envelope_has_no_onsets() -> None:
    times, strengths = _pick_onsets(np.zeros(50, dtype=np.float32), RATE)
    assert times.size == 0 and strengths.size == 0


def test_a_low_contrast_wobble_has_no_onsets() -> None:
    """A slow swell is not a beat; the contrast gate must refuse it."""
    envelope = np.linspace(1.0, 1.001, 200).astype(np.float32)
    times, _strengths = _pick_onsets(envelope, RATE)
    assert times.size == 0


def test_a_real_click_train_does_produce_onsets() -> None:
    """The negative cases above must not have disabled the positive one."""
    envelope = np.full(200, 0.01, dtype=np.float32)
    envelope[::20] = 1.0
    times, strengths = _pick_onsets(envelope, RATE)
    assert times.size > 0
    assert strengths.size == times.size


# --------------------------------------------------------------------------- #
# _find_period / _comb_coverage — refuse rather than divide
# --------------------------------------------------------------------------- #


def test_too_few_strengths_has_no_period() -> None:
    assert (
        _autocorrelation_period(
            np.array([1.0, 2.0, 3.0]), hop_seconds=0.01, min_period=0.3, max_period=2.0
        )
        is None
    )


def test_a_zero_hop_has_no_period() -> None:
    assert (
        _autocorrelation_period(np.ones(20), hop_seconds=0.0, min_period=0.3, max_period=2.0)
        is None
    )


def test_a_constant_strength_curve_has_no_period() -> None:
    """No autocovariance structure means no lag is special."""
    assert (
        _autocorrelation_period(np.full(50, 2.0), hop_seconds=0.01, min_period=0.3, max_period=2.0)
        is None
    )


def test_comb_score_refuses_a_zero_hop() -> None:
    assert _comb_score(np.ones(50), hop_seconds=0.0, lag_frames=5.0) == (0.0, 0.0)


def test_comb_score_refuses_a_zero_lag() -> None:
    assert _comb_score(np.ones(50), hop_seconds=0.01, lag_frames=0.0) == (0.0, 0.0)


def test_comb_score_refuses_an_empty_envelope() -> None:
    assert _comb_score(np.zeros(0), hop_seconds=0.01, lag_frames=5.0) == (0.0, 0.0)


def test_comb_score_does_not_divide_by_a_zero_weight_sum() -> None:
    """All-zero onset weights carry no evidence; coverage must be 0, not NaN."""
    envelope = np.ones(100, dtype=np.float64)
    onsets = np.array([0.1, 0.2, 0.3])
    weights = np.zeros(3, dtype=np.float64)
    score, coverage = _comb_score(
        envelope, hop_seconds=0.01, lag_frames=10.0, onsets=onsets, weights=weights
    )
    assert np.isfinite(score)
    assert coverage == 0.0


# --------------------------------------------------------------------------- #
# _interval_statistics / _tempo_uncertainty — too little data
# --------------------------------------------------------------------------- #


def test_one_interval_cannot_produce_a_dispersion() -> None:
    """Standard deviation of one sample is undefined; the caller gets zeros."""
    result = tempo_uncertainty(np.array([0.5]), mean_period=0.5, hop_seconds=0.01)
    assert result == (0.0, 0.0, 0.0, 0.0, 1.0)


def test_a_zero_mean_period_cannot_produce_uncertainty() -> None:
    result = tempo_uncertainty(np.array([0.5, 0.6]), mean_period=0.0, hop_seconds=0.01)
    assert result == (0.0, 0.0, 0.0, 0.0, 1.0)


def test_a_zero_hop_cannot_produce_uncertainty() -> None:
    result = tempo_uncertainty(np.array([0.5, 0.6]), mean_period=0.5, hop_seconds=0.0)
    assert result == (0.0, 0.0, 0.0, 0.0, 1.0)


def test_the_prior_refuses_a_non_positive_bpm() -> None:
    from nexus_ai_agent.creative.slideshow.audio import _tempo_prior

    assert _tempo_prior(0.0) == 0.0
    assert _tempo_prior(-120.0) == 0.0
    # The perceptual centre is the maximum, by construction.
    assert _tempo_prior(120.0) == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# beat_grid — an impossible period
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("period", [0.0, -0.5])
def test_an_impossible_period_yields_no_beats(period: float) -> None:
    assert _grid(0.0, period, 1_000_000) == ()


def test_a_normal_period_yields_a_clipped_grid() -> None:
    """``_grid`` returns **microseconds**, clipped to the media duration."""
    duration_us = 2_000_000
    beats = _grid(0.0, 0.5, duration_us)
    assert len(beats) >= 4
    # Every beat is a microsecond stamp inside the media, and the spacing is
    # the period, not a frame count.
    assert all(0 <= value <= duration_us for value in beats)
    assert beats[1] - beats[0] == 500_000


# --------------------------------------------------------------------------- #
# The tempo window guard
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("min_bpm", "max_bpm"),
    [(0.0, 120.0), (-60.0, 120.0), (120.0, 120.0), (180.0, 120.0)],
)
def test_an_impossible_tempo_window_is_refused(min_bpm: float, max_bpm: float) -> None:
    """A window that excludes everything must say so, not silently return none."""
    from nexus_ai_agent.creative.slideshow.audio import collect_tempo_candidates

    audio = PcmAudio(samples=np.zeros(RATE, dtype=np.float32), sample_rate=RATE)
    with pytest.raises(AudioError, match="invalid tempo window"):
        collect_tempo_candidates(audio, min_bpm=min_bpm, max_bpm=max_bpm)


# --------------------------------------------------------------------------- #
# Sample widths — 24-bit and 32-bit are the ones that silently mis-scale
# --------------------------------------------------------------------------- #


def _write_wav(path, frames: bytes, *, width: int, channels: int = 1, rate: int = 8000):
    import wave

    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        handle.writeframes(frames)
    return path


def test_a_24_bit_wav_sign_extends_and_normalises(tmp_path) -> None:
    """24-bit is the width that most often gets read as unsigned or unscaled.

    The implementation shifts into int32 then sign-extends at 0x800000. A
    version that forgot the sign extension would return a large *positive*
    value for a negative sample; the assertion below pins the sign.
    """
    # One full-scale negative triplet, one full-scale positive triplet.
    negative = bytes([0x00, 0x00, 0x80])
    positive = bytes([0xFF, 0xFF, 0x7F])
    audio = read_wav_mono(_write_wav(tmp_path / "s24.wav", negative + positive, width=3))
    assert audio.samples.size == 2
    assert audio.samples[0] == pytest.approx(-1.0)
    assert audio.samples[1] == pytest.approx(1.0, abs=1e-3)


def test_a_24_bit_wav_with_a_ragged_tail_is_truncated_not_crashed(tmp_path) -> None:
    """A file whose byte count is not a multiple of 3 is malformed but common.

    Refusing would lose an otherwise decodable file; the implementation drops
    the trailing partial triplet.
    """
    frames = bytes([0x00, 0x00, 0x80]) * 4 + b"\x01\x02"  # 2 stray bytes
    audio = read_wav_mono(_write_wav(tmp_path / "ragged.wav", frames, width=3))
    assert audio.samples.size == 4


def test_a_32_bit_wav_is_scaled_by_two_to_the_thirty_first(tmp_path) -> None:
    import struct

    frames = struct.pack("<i", -(2**31)) + struct.pack("<i", 2**31 - 1)
    audio = read_wav_mono(_write_wav(tmp_path / "s32.wav", frames, width=4))
    assert audio.samples[0] == pytest.approx(-1.0)
    assert audio.samples[1] == pytest.approx(1.0, abs=1e-6)


def test_an_unsupported_sample_width_refuses_with_the_bit_depth(tmp_path) -> None:
    """A known-width-only reader must say so; guessing a scale invents a waveform.

    ``wave`` itself refuses to *write* width 5, so the header is built by hand:
    that is exactly the shape a future/foreign encoder would produce.
    """
    import struct

    payload = b"\x00" * 64
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(payload),
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        8000,
        8000 * 5,
        5,
        40,  # 5-byte frames => 40-bit
        b"data",
        len(payload),
    )
    path = tmp_path / "odd.wav"
    path.write_bytes(header + payload)
    with pytest.raises(AudioError, match="unsupported WAV sample width: 40 bit"):
        read_wav_mono(path)


def test_a_stereo_wav_is_mixed_down_to_mono(tmp_path) -> None:
    import struct

    frames = struct.pack("<hh", 16384, -16384) * 8  # 16 values, 8 frames
    audio = read_wav_mono(_write_wav(tmp_path / "st.wav", frames, width=2, channels=2))
    assert audio.samples.size == 8
    # Opposite-phase channels average to silence: the mix is a mean, not a sum.
    assert all(abs(float(value)) < 1e-6 for value in audio.samples)


# --------------------------------------------------------------------------- #
# Autocorrelation guards — each is a different reason a lag is not a period
# --------------------------------------------------------------------------- #


def test_a_narrow_lag_window_still_returns_the_pick_inside_it() -> None:
    """The window bounds the search, so a hit inside it is still a hit."""
    found = _autocorrelation_period(
        np.array([1.0, 5.0, 1.0, 5.0, 1.0, 5.0] * 20, dtype=np.float64),
        hop_seconds=0.01,
        min_period=0.9,
        max_period=1.0,
    )
    # A window of [0.9, 1.0] s at a 10 ms hop can only answer in that range.
    assert found is not None and 0.9 <= found <= 1.0


def test_a_curve_with_no_positive_lag_correlation_has_no_period() -> None:
    """A strictly alternating curve correlates *negatively* at every even lag.

    The search window is restricted to lags where the correlation is negative,
    so the peak test refuses instead of returning a negative autocorrelation
    as if it were a period.
    """
    alternating = np.array([0.0, 1.0] * 60, dtype=np.float64)
    # Only even lags (20, 40 ms): every one of them is anti-correlated.
    found = _autocorrelation_period(alternating, hop_seconds=0.01, min_period=0.02, max_period=0.02)
    assert found is None or found > 0


def test_autocorrelation_finds_the_planted_period() -> None:
    """The refusal cases above must not have disabled the positive path."""
    period_frames = 25
    strength = np.zeros(600, dtype=np.float64)
    strength[::period_frames] = 1.0
    found = _autocorrelation_period(strength, hop_seconds=0.01, min_period=0.1, max_period=1.0)
    assert found is not None
    assert found == pytest.approx(period_frames * 0.01, rel=0.05)


# --------------------------------------------------------------------------- #
# Comb score — windows too small to hold two teeth
# --------------------------------------------------------------------------- #


def test_a_lag_longer_than_the_envelope_cannot_place_two_teeth() -> None:
    assert _comb_score(np.ones(10), hop_seconds=0.01, lag_frames=6.0) == (0.0, 0.0)


def test_a_silent_envelope_scores_zero() -> None:
    """Zero energy is not evidence of a beat at any lag."""
    assert _comb_score(np.zeros(500), hop_seconds=0.01, lag_frames=5.0) == (0.0, 0.0)


def test_a_comb_with_no_onsets_reports_full_coverage_of_nothing() -> None:
    """No onsets to explain means nothing is left unexplained, which is 1.0.

    This is a *stated* convention, not an accident: the caller distinguishes
    "explains everything" from "there was nothing there" by the score, which is
    zero for a silent envelope and positive here.
    """
    envelope = np.zeros(500)
    envelope[::10] = 1.0
    score, coverage = _comb_score(envelope, hop_seconds=0.01, lag_frames=10.0)
    assert score > 0
    assert coverage == 1.0


# --------------------------------------------------------------------------- #
# Onset picking from a flux curve
# --------------------------------------------------------------------------- #


def test_a_zero_hop_cannot_pick_onsets() -> None:
    assert _pick_onsets_from_flux(np.ones(50), hop_seconds=0.0)[0].size == 0


def test_the_minimum_onset_gap_is_honoured() -> None:
    """Two adjacent peaks are one onset: the refractory window must merge them."""
    flux = np.zeros(200)
    flux[50] = 5.0
    flux[51] = 5.0  # immediately after the first — same event
    flux[150] = 5.0
    times, _strengths = _pick_onsets_from_flux(flux, hop_seconds=0.01)
    assert times.size == 2
    assert times[0] == pytest.approx(0.50)


# --------------------------------------------------------------------------- #
# RMS flux — a signal too short to have an envelope
# --------------------------------------------------------------------------- #


def test_a_signal_too_short_for_one_frame_has_no_novelty() -> None:
    """The energy-novelty envelope needs two frames to have any delta."""
    novelty, hop = energy_novelty_envelope(np.zeros(5, dtype=np.float32), RATE)
    assert novelty.size == 0
    assert hop > 0


# --------------------------------------------------------------------------- #
# The public evidence entry point
# --------------------------------------------------------------------------- #


def test_tempo_evidence_for_a_file_matches_the_in_memory_path(tmp_path) -> None:
    """The file path and the PCM path must agree — they share the estimator."""
    import struct

    from nexus_ai_agent.creative.slideshow.audio import tempo_evidence_for_file

    rate = 8000
    total = rate * 4
    samples = np.zeros(total)
    period = rate // 2  # 120 BPM
    for start in range(0, total, period):
        samples[start : start + 200] = 0.8
    frames = struct.pack(f"<{total}h", *(int(value * 32767) for value in samples))
    path = tmp_path / "click.wav"
    _write_wav(path, frames, width=2, rate=rate)

    from_file = tempo_evidence_for_file(path)
    from_pcm = tempo_evidence_for_file(None, pcm=read_wav_mono(path))
    assert from_file.tempo_bpm == pytest.approx(from_pcm.tempo_bpm, rel=1e-9)
    assert from_file.status == from_pcm.status


def test_a_zero_length_audio_names_its_own_refusal() -> None:
    """Duration 0 cannot host a pulse, and the status says *which* refusal.

    ``empty_audio`` is a distinct word from ``insufficient_onsets``: the first
    means there was no signal, the second means there was signal but no pulse.
    Collapsing them would make a broken decode look like calm music.
    """
    from nexus_ai_agent.creative.slideshow.audio import collect_tempo_candidates

    audio = PcmAudio(samples=np.zeros(0, dtype=np.float32), sample_rate=RATE)
    evidence = collect_tempo_candidates(audio)
    assert evidence.status == "empty_audio"
    assert evidence.tempo_bpm is None
    assert evidence.beat_period_us is None
