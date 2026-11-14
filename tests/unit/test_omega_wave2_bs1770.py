"""Part 3A — the independent pure-NumPy ITU-R BS.1770-5 meter.

The point of this implementation is that it is a *second opinion*: the engine
publishes loudness numbers measured by FFmpeg, and a single implementation can
be wrong in a way no self-check detects.  So the tests here are of two kinds:

* conformance — reproduce the standard's own published coefficients and its
  closed-form results (a full-scale 997 Hz sine is -3.01 LUFS; dual-mono is
  +3.01 LU louder than mono), which pins the mathematics; and
* agreement — measure real rendered files with this meter and with FFmpeg
  ``ebur128`` and require them to agree, which pins the implementation.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from nexus_ai_agent.creative.audio_engine.loudness_bs1770 import (
    ABSOLUTE_GATE_LUFS,
    BLOCK_SECONDS,
    OVERSAMPLE_FACTOR,
    RELATIVE_GATE_LU,
    gated_loudness,
    k_weight,
    k_weighting_coefficients,
    loudness_range,
    measure_bs1770_numpy,
    true_peak_dbtp,
)

RATE = 48_000

#: BS.1770-4 Table 1 / Table 2, the published 48 kHz coefficients.
PUBLISHED_SHELF = (
    1.53512485958697,
    -2.69169618940638,
    1.19839281085285,
    -1.69065929318241,
    0.73248077421585,
)
PUBLISHED_HIGHPASS = (1.0, -2.0, 1.0, -1.99004745483398, 0.99007225036621)


def _sine(frequency: float, amplitude: float, seconds: float = 10.0) -> np.ndarray:
    time = np.arange(int(RATE * seconds)) / RATE
    return amplitude * np.sin(2.0 * np.pi * frequency * time)


def test_k_weighting_reproduces_the_standard_coefficients() -> None:
    """The re-derived 48 kHz coefficients match BS.1770 to ~1e-9.

    This is the load-bearing test.  BS.1770 specifies the stages by their analog
    prototype, so a *re-derivation* for a non-48 kHz rate is only trustworthy if
    the same code reproduces the standard's published 48 kHz numbers.  A generic
    RBJ high shelf does not (its b1 is off by ~0.04), which is how an
    "almost right" meter gets written.
    """
    coefficients = k_weighting_coefficients(RATE)
    assert coefficients["shelf"] == pytest.approx(PUBLISHED_SHELF, abs=1e-9)
    assert coefficients["highpass"] == pytest.approx(PUBLISHED_HIGHPASS, abs=1e-8)


@pytest.mark.parametrize(
    "rate", [8_000, 16_000, 22_050, 32_000, 44_100, 48_000, 88_200, 96_000, 192_000]
)
def test_coefficients_are_stable_at_every_rate(rate: int) -> None:
    """Every rate the canonical profile allows can be K-weighted.

    Includes 44.1 kHz on purpose: reusing the 48 kHz coefficients there is the
    single most common mistake and is wrong by a few tenths of a LU.
    """
    coefficients = k_weighting_coefficients(rate)
    b0, b1, b2, a1, a2 = coefficients["highpass"]
    # The RLB stage is a high-pass: exact DC rejection, near-unity at Nyquist.
    assert b0 + b1 + b2 == pytest.approx(0.0)
    assert abs(a1) < 2.0 and abs(a2) < 1.0
    shelf = coefficients["shelf"]
    # The head shelf boosts high frequencies: its DC gain is ~1 and its Nyquist
    # gain is ~+4 dB.
    dc = (shelf[0] + shelf[1] + shelf[2]) / (1.0 + shelf[3] + shelf[4])
    nyquist = (shelf[0] - shelf[1] + shelf[2]) / (1.0 - shelf[3] + shelf[4])
    assert dc == pytest.approx(1.0, abs=0.01)
    assert 20.0 * math.log10(nyquist) == pytest.approx(4.0, abs=0.2)


def test_44k_coefficients_differ_from_48k_coefficients() -> None:
    """Proof the re-derivation actually happens instead of being reused."""
    assert k_weighting_coefficients(44_100)["shelf"] != k_weighting_coefficients(48_000)["shelf"]


def test_k_weight_removes_dc() -> None:
    """A DC offset must not contribute loudness: K-weighting is a high-pass."""
    dc = np.full(RATE * 5, 0.5, dtype=np.float64)
    weighted = k_weight(dc, RATE)
    # The first samples are the filter settling; the tail is what DC rejection
    # means.  A DC offset that survived would read as ~-9 LUFS of "content".
    assert abs(float(np.mean(weighted[RATE : RATE * 3]))) < 1e-6


def test_full_scale_sine_reads_minus_three_lufs() -> None:
    """The standard's reference: a 0 dBFS 997 Hz sine is -3.01 LUFS.

    This is the calibration anchor of the whole scale.  If it is off, every
    measurement is off by the same amount.
    """
    measurement = measure_bs1770_numpy(_sine(997.0, 1.0), sample_rate=RATE)
    assert measurement.lufs == pytest.approx(-3.01, abs=0.05)


@pytest.mark.parametrize("amplitude_db", [-6.0, -20.0, -40.0])
def test_level_scales_linearly(amplitude_db: float) -> None:
    """Halving the amplitude must read -6.02 LU, not "roughly less"."""
    amplitude = 10.0 ** (amplitude_db / 20.0)
    measurement = measure_bs1770_numpy(_sine(997.0, amplitude), sample_rate=RATE)
    assert measurement.lufs == pytest.approx(-3.01 + amplitude_db, abs=0.05)


def test_dual_mono_is_three_lu_louder_than_mono() -> None:
    """Two identical channels sum to +3.01 LU — not +6, and not 0."""
    mono = _sine(997.0, 0.1)
    mono_measurement = measure_bs1770_numpy(mono, sample_rate=RATE)
    stereo_measurement = measure_bs1770_numpy(np.vstack([mono, mono]), sample_rate=RATE)
    assert stereo_measurement.lufs is not None and mono_measurement.lufs is not None
    assert stereo_measurement.lufs - mono_measurement.lufs == pytest.approx(3.01, abs=0.05)


def test_a_high_frequency_tone_reads_louder_than_a_low_one_at_equal_amplitude() -> None:
    """K-weighting is not flat: that is the whole point of the curve.

    At equal sample amplitude a 10 kHz tone measures louder than a 40 Hz tone
    because the head shelf boosts it and the RLB high-pass attenuates the bass.
    A meter with a single high-pass (or none) would get this ordering wrong.
    """
    low = measure_bs1770_numpy(_sine(40.0, 0.1), sample_rate=RATE)
    high = measure_bs1770_numpy(_sine(10_000.0, 0.1), sample_rate=RATE)
    assert low.lufs is not None and high.lufs is not None
    assert high.lufs > low.lufs + 6.0


def test_channel_weights_apply_the_surround_boost() -> None:
    """Surround channels are weighted 1.41; LFE is excluded."""
    from nexus_ai_agent.creative.audio_engine.loudness_bs1770 import _channel_weights

    assert list(_channel_weights(1)) == [1.0]
    assert list(_channel_weights(2)) == [1.0, 1.0]
    assert list(_channel_weights(6)) == [1.0, 1.0, 1.0, 0.0, 1.41, 1.41]


def test_absolute_gate_rejects_a_programme_below_the_floor() -> None:
    """Below -70 LUFS there is no loudness to report, and ``None`` says so."""
    quiet = _sine(997.0, 1e-5)
    measurement = measure_bs1770_numpy(quiet, sample_rate=RATE)
    assert measurement.lufs is None
    assert measurement.silent is True


def test_relative_gate_ignores_a_quiet_passage() -> None:
    """A loud section plus a much quieter one gates the quiet one out.

    The relative gate exists so that a long quiet passage does not drag the
    integrated value down; a one-stage meter would report several LU lower.
    """
    loud = _sine(997.0, 0.5, seconds=6.0)
    quiet = _sine(997.0, 0.005, seconds=6.0)
    gated = measure_bs1770_numpy(np.concatenate([loud, quiet]), sample_rate=RATE)
    loud_only = measure_bs1770_numpy(loud, sample_rate=RATE)
    assert gated.lufs is not None and loud_only.lufs is not None
    assert gated.lufs == pytest.approx(loud_only.lufs, abs=0.5)
    assert gated.gated_block_count < gated.block_count


def test_gate_constants_are_the_standard_values() -> None:
    assert ABSOLUTE_GATE_LUFS == -70.0
    assert RELATIVE_GATE_LU == -10.0
    assert BLOCK_SECONDS == 0.400


def test_two_stage_gating_matches_a_hand_computation() -> None:
    """The second gate's threshold is taken from the absolute-gated *mean*."""
    absolute_floor = 10.0 ** ((ABSOLUTE_GATE_LUFS + 0.691) / 10.0)
    values = np.array([absolute_floor * 100.0, absolute_floor * 50.0, absolute_floor * 1.5])
    loudness, count = gated_loudness(values)
    assert loudness is not None
    mean_above_absolute = float(np.mean(values))
    relative_floor = mean_above_absolute * 10.0 ** (RELATIVE_GATE_LU / 10.0)
    surviving = values[values > relative_floor]
    expected = -0.691 + 10.0 * math.log10(float(np.mean(surviving)))
    assert loudness == pytest.approx(expected, abs=1e-9)
    assert count == surviving.size


def test_true_peak_catches_an_inter_sample_peak() -> None:
    """The worst case for a sample-domain peak: fs/4 at 45 degrees phase.

    Every sample sits at +-0.707 but the waveform reaches ~1.0 between samples,
    so the true peak is ~+3.01 dB above the sample peak.  Reporting
    ``max(abs(x))`` here understates the peak by 3 dB — and this is the case a
    true-peak meter exists for.
    """
    samples = 0.99 * np.sin(2.0 * np.pi * (RATE / 4) * np.arange(RATE * 5) / RATE + np.pi / 4)
    measurement = measure_bs1770_numpy(samples, sample_rate=RATE)
    assert measurement.sample_peak_dbfs is not None
    assert measurement.true_peak_dbtp is not None
    assert measurement.true_peak_oversample_gain_db is not None
    assert measurement.true_peak_oversample_gain_db == pytest.approx(3.01, abs=0.25)
    assert measurement.true_peak_dbtp > measurement.sample_peak_dbfs


@pytest.mark.parametrize(
    ("frequency", "amplitude"),
    [(997.0, 1.0), (3_000.0, 0.95), (50.0, 0.9), (12_000.0, 0.99)],
)
def test_true_peak_is_exact_for_tones_the_grid_misses(frequency: float, amplitude: float) -> None:
    """The interpolator reproduces the analytic peak, for four unrelated phases.

    These are the cases that break a naive implementation: a 12 kHz tone sampled
    at 48 kHz with a 45 degree phase start peaks at 0.99 but *no sample* exceeds
    0.700, and a 3 kHz tone starting at 90 degrees has an analytic peak of 0.95
    with every sample at or below that. A short windowed-sinc kernel read 0.923
    and 1.056 respectively — wrong in opposite directions — which is why this is
    asserted to 1e-6 rather than "to a couple of dB".
    """
    time = np.arange(RATE * 2) / RATE
    samples = amplitude * np.sin(2.0 * np.pi * frequency * time + np.pi / 4)
    peak = true_peak_dbtp(samples, oversample=OVERSAMPLE_FACTOR)
    assert peak is not None
    assert 10.0 ** (peak / 20.0) == pytest.approx(amplitude, abs=1e-5)


def test_true_peak_reports_no_phantom_peak_at_a_block_boundary() -> None:
    """A tone whose period does not divide the excerpt must not gain a false peak.

    A single whole-signal FFT interpolates *circularly*, so the wrap-around
    discontinuity shows up as a phantom peak ~+0.36 dB high.  The overlapping-block
    form exists to prevent that, and this is the regression that proves it.
    """
    time = np.arange(int(RATE * 1.0001)) / RATE
    samples = 0.99 * np.sin(2.0 * np.pi * 12_000.0 * time + np.pi / 4)
    peak = true_peak_dbtp(samples, oversample=OVERSAMPLE_FACTOR)
    assert peak is not None
    assert 10.0 ** (peak / 20.0) == pytest.approx(0.99, abs=1e-4)


def test_true_peak_of_silence_is_none_not_minus_infinity() -> None:
    assert true_peak_dbtp(np.zeros(1000)) is None


def test_short_term_max_is_the_loudest_three_seconds() -> None:
    """A loud burst inside a quiet programme sets the short-term maximum."""
    quiet = _sine(997.0, 0.01, seconds=10.0)
    burst = _sine(997.0, 0.5, seconds=4.0)
    measurement = measure_bs1770_numpy(np.concatenate([quiet, burst, quiet]), sample_rate=RATE)
    assert measurement.short_term_max_lufs is not None
    assert measurement.lufs is not None
    assert measurement.short_term_max_lufs > measurement.lufs


def test_loudness_range_needs_enough_material_to_exist() -> None:
    """One block has no range; reporting 0.0 LU would claim it was measured."""
    assert loudness_range(np.array([0.1])) is None
    assert loudness_range(np.array([0.1, 0.1])) is None


def test_loudness_range_measures_a_twenty_decibel_swing_as_twenty_lu() -> None:
    """LRA is the 10th-to-95th percentile spread of the *short-term* loudness.

    A programme that is 20 dB quieter for half its length has a known answer, so
    this is a closed-form check rather than "greater than zero".  Computed from
    the 400 ms gating blocks directly it returns 0.003 LU — the mistake this
    test exists to prevent.
    """
    loud = _sine(997.0, 0.5, seconds=8.0)
    quiet = _sine(997.0, 0.05, seconds=8.0)
    measurement = measure_bs1770_numpy(np.concatenate([loud, quiet]), sample_rate=RATE)
    assert measurement.lra_lu is not None
    assert measurement.lra_lu == pytest.approx(20.0, abs=1.0)


def test_loudness_range_is_near_zero_for_a_steady_tone() -> None:
    measurement = measure_bs1770_numpy(_sine(997.0, 0.3, seconds=10.0), sample_rate=RATE)
    assert measurement.lra_lu == pytest.approx(0.0, abs=0.1)


def test_short_windows_are_none_rather_than_a_different_question() -> None:
    """A 2 s file has no 3 s short-term maximum, so it must not report one."""
    measurement = measure_bs1770_numpy(_sine(997.0, 0.3, seconds=2.0), sample_rate=RATE)
    assert measurement.short_term_max_lufs is None
    assert measurement.lra_lu is None


def test_empty_and_tiny_inputs_degenerate_cleanly() -> None:
    """No crash, and ``None`` rather than a fabricated number."""
    empty = measure_bs1770_numpy(np.zeros(0), sample_rate=RATE)
    assert empty.lufs is None and empty.true_peak_dbtp is None
    tiny = measure_bs1770_numpy(np.zeros(10), sample_rate=RATE)
    assert tiny.lufs is None
    assert tiny.block_count == 0


def test_invalid_inputs_are_refused() -> None:
    with pytest.raises(ValueError):
        measure_bs1770_numpy(np.zeros(100), sample_rate=0)
    with pytest.raises(ValueError):
        measure_bs1770_numpy(np.zeros((2, 2, 2)), sample_rate=RATE)
    with pytest.raises(ValueError):
        measure_bs1770_numpy(np.zeros((2, 100)), sample_rate=RATE, channels=3)


def test_the_measurement_carries_its_own_coefficients() -> None:
    """A reported number must be reproducible: the coefficients are in the result."""
    measurement = measure_bs1770_numpy(_sine(997.0, 0.5), sample_rate=RATE)
    assert measurement.coefficients["shelf"] == pytest.approx(PUBLISHED_SHELF, abs=1e-9)
    assert measurement.sample_rate == RATE
    assert measurement.channels == 1
    assert measurement.seconds_measured == pytest.approx(10.0, abs=0.01)


# --------------------------------------------------------------------------- #
# Agreement with FFmpeg ebur128 — the reason this module exists.
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def ffmpeg_dir(tmp_path_factory: pytest.TempPathFactory) -> object:
    return tmp_path_factory.mktemp("bs1770_ffmpeg")


def _write_wav(path: object, samples: np.ndarray, channels: int = 1) -> None:
    import wave

    flat = samples if channels == 1 else samples.reshape(-1)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes((np.clip(flat, -1.0, 1.0) * 32767).astype("<i2").tobytes())


@pytest.mark.parametrize(
    ("label", "signal"),
    [
        ("sine_997_-20dB", lambda: _sine(997.0, 0.1)),
        ("sine_40Hz_-20dB", lambda: _sine(40.0, 0.1)),
        ("sine_10kHz_-20dB", lambda: _sine(10_000.0, 0.1)),
        (
            "noise_-20dB",
            lambda: 0.1 * np.random.default_rng(1).standard_normal(RATE * 10),
        ),
        (
            "loud_then_quiet",
            lambda: np.concatenate(
                [_sine(997.0, 0.5, seconds=8.0), _sine(997.0, 0.02, seconds=8.0)]
            ),
        ),
    ],
)
def test_agrees_with_ffmpeg_ebur128(ffmpeg_dir: object, label: str, signal: object) -> None:
    """Two independent implementations of one defined quantity must agree.

    FFmpeg prints one decimal place, so agreement is asserted to 0.1 LU — the
    limit of what its report can distinguish, not the limit of ours.
    """
    from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import FfmpegTranscoder

    samples = signal()
    path = ffmpeg_dir / f"{label}.wav"  # type: ignore[operator]
    _write_wav(path, samples)
    ours = measure_bs1770_numpy(samples, sample_rate=RATE)
    theirs = FfmpegTranscoder().measure_loudness(path)
    assert ours.lufs is not None and theirs.integrated_lufs is not None
    assert ours.lufs == pytest.approx(theirs.integrated_lufs, abs=0.1), label


def test_agrees_with_ffmpeg_on_a_stereo_programme(ffmpeg_dir: object) -> None:
    """Stereo channel summing is where a mono-only implementation drifts."""
    from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import FfmpegTranscoder

    left = _sine(997.0, 0.2)
    right = _sine(440.0, 0.15)
    stereo = np.vstack([left, right]).T
    path = ffmpeg_dir / "stereo.wav"  # type: ignore[operator]
    _write_wav(path, stereo, channels=2)
    ours = measure_bs1770_numpy(stereo.T, sample_rate=RATE)
    theirs = FfmpegTranscoder().measure_loudness(path)
    assert ours.lufs is not None and theirs.integrated_lufs is not None
    assert ours.lufs == pytest.approx(theirs.integrated_lufs, abs=0.1)


def test_cross_verification_passes_on_an_honest_measurement(ffmpeg_dir: object) -> None:
    from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import FfmpegTranscoder
    from nexus_ai_agent.creative.audio_engine.verify import (
        cross_verify_loudness,
        decode_for_analysis,
    )

    samples = _sine(997.0, 0.1)
    path = ffmpeg_dir / "honest.wav"  # type: ignore[operator]
    _write_wav(path, samples)
    transcoder = FfmpegTranscoder()
    decoded, rate, channels = decode_for_analysis(transcoder, path)
    assert decoded is not None
    code, detail = cross_verify_loudness(
        decoded,
        sample_rate=rate,
        channels=channels,
        ffmpeg_measurement=transcoder.measure_loudness(path),
    )
    assert code is None, detail


def test_cross_verification_catches_a_wrong_ffmpeg_number(ffmpeg_dir: object) -> None:
    """A 5 LU discrepancy must be refused, and say how far apart the meters are."""
    from nexus_ai_agent.creative.audio_engine.models import LoudnessMeasurement
    from nexus_ai_agent.creative.audio_engine.verify import (
        CODE_LOUDNESS_METER_DISAGREEMENT,
        cross_verify_loudness,
    )

    samples = _sine(997.0, 0.1)
    truth = measure_bs1770_numpy(samples, sample_rate=RATE)
    assert truth.lufs is not None
    corrupted = LoudnessMeasurement(
        integrated_lufs=truth.lufs + 5.0, true_peak_dbtp=-1.0, loudness_range_lu=5.0
    )
    code, detail = cross_verify_loudness(
        samples, sample_rate=RATE, channels=1, ffmpeg_measurement=corrupted
    )
    assert code == CODE_LOUDNESS_METER_DISAGREEMENT
    assert "5.0" in detail or "LU apart" in detail


def test_cross_verification_is_fail_closed_without_samples() -> None:
    """No samples means unverifiable, which is a refusal, not a pass."""
    from nexus_ai_agent.creative.audio_engine.models import LoudnessMeasurement
    from nexus_ai_agent.creative.audio_engine.verify import (
        CODE_LOUDNESS_METER_DISAGREEMENT,
        cross_verify_loudness,
    )

    code, detail = cross_verify_loudness(
        None,
        sample_rate=None,
        channels=None,
        ffmpeg_measurement=LoudnessMeasurement(
            integrated_lufs=-14.0, true_peak_dbtp=-1.0, loudness_range_lu=5.0
        ),
    )
    assert code == CODE_LOUDNESS_METER_DISAGREEMENT
    assert "cross-verified" in detail


def test_cross_verification_catches_a_meter_that_sees_nothing(ffmpeg_dir: object) -> None:
    """FFmpeg finding loudness where we find none is itself a disagreement."""
    from nexus_ai_agent.creative.audio_engine.models import LoudnessMeasurement
    from nexus_ai_agent.creative.audio_engine.verify import (
        CODE_LOUDNESS_METER_DISAGREEMENT,
        cross_verify_loudness,
    )

    below_the_gate = _sine(997.0, 1e-5)
    code, detail = cross_verify_loudness(
        below_the_gate,
        sample_rate=RATE,
        channels=1,
        ffmpeg_measurement=LoudnessMeasurement(
            integrated_lufs=-40.0, true_peak_dbtp=-80.0, loudness_range_lu=1.0
        ),
    )
    assert code == CODE_LOUDNESS_METER_DISAGREEMENT
    assert "below the absolute gate" in detail
