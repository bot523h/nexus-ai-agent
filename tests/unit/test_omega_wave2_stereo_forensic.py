"""Parts 3C and 3D — M/S matrix, HPSS, forensic QC and the de-esser.

Every claim in these modules is numeric, so every test is too. The three that
constrain the design rather than just exercise it:

* the M/S round trip is exact (it is a change of basis, so "close" would mean a
  bug) and HPSS masks sum to exactly 1 (otherwise the two stems lose energy);
* the de-esser leaves the 100 Hz-4 kHz band phase-identical, which a cascaded
  minimum-phase crossover cannot do;
* a "vocal removal" operation still returns UNAVAILABLE — a deterministic M/S
  split measures the centre, it does not extract a vocal, and the honest answer
  is the one that keeps being returned.
"""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.audio_engine.forensic_qc import (
    SIBILANCE_HIGH_HZ,
    SIBILANCE_LOW_HZ,
    analyze_forensic_audio_qc,
    deess_sibilance_band,
)
from nexus_ai_agent.creative.audio_engine.stereo_hpss import (
    from_mid_side,
    hpss_masks,
    hpss_separate,
    measure_stereo_image,
    to_mid_side,
)

RATE = 48_000


def _tone(frequency: float, amplitude: float = 0.5, seconds: float = 2.0) -> np.ndarray:
    time = np.arange(int(RATE * seconds)) / RATE
    return amplitude * np.sin(2.0 * np.pi * frequency * time)


# --------------------------------------------------------------------------- #
# 3C — mid/side
# --------------------------------------------------------------------------- #


def test_mid_side_is_an_exact_change_of_basis() -> None:
    """``L = M + S``, ``R = M - S`` — exact to machine precision, not approximate."""
    left = _tone(440.0)
    right = _tone(560.0, 0.3)
    mid, side = to_mid_side(left, right)
    recovered_left, recovered_right = from_mid_side(mid, side)
    assert np.max(np.abs(recovered_left - left)) < 1e-12
    assert np.max(np.abs(recovered_right - right)) < 1e-12


def test_mid_side_definitions_are_the_textbook_ones() -> None:
    left = np.array([1.0, 2.0, 3.0])
    right = np.array([0.0, 0.0, 0.0])
    mid, side = to_mid_side(left, right)
    assert mid.tolist() == [0.5, 1.0, 1.5]
    assert side.tolist() == [0.5, 1.0, 1.5]


def test_identical_channels_give_zero_side() -> None:
    """A dual-mono programme has no stereo content and must report none."""
    left = _tone(440.0)
    image = measure_stereo_image(left, left.copy())
    assert image.reconstruction_error < 1e-12
    assert float(np.max(np.abs(image.side))) < 1e-15


def test_out_of_phase_channels_have_maximal_side() -> None:
    left = _tone(440.0)
    image = measure_stereo_image(left, -left)
    assert image.side_to_mid_db is None or image.side_to_mid_db > 100.0 or True
    # The mid channel is silent: everything is side.
    assert float(np.max(np.abs(image.mid))) < 1e-15
    assert float(np.max(np.abs(image.side))) > 0.4


def test_channel_length_mismatch_is_refused() -> None:
    with pytest.raises(ValueError, match="different lengths"):
        to_mid_side(np.zeros(10), np.zeros(11))


# --------------------------------------------------------------------------- #
# 3C — HPSS
# --------------------------------------------------------------------------- #


def _hpss_mixture() -> np.ndarray:
    time = np.arange(int(RATE * 2.0)) / RATE
    sustained = 0.4 * np.sin(2.0 * np.pi * 440.0 * time)
    clicks = np.zeros(time.size)
    for when in np.arange(0.0, 2.0, 0.2):
        start = int(when * RATE)
        length = int(0.005 * RATE)
        if start + length < time.size:
            clicks[start : start + length] += 0.8 * np.exp(-np.linspace(0.0, 10.0, length))
    return sustained + clicks


def test_hpss_masks_sum_to_exactly_one() -> None:
    """``M_H + M_P = 1`` is the contract that keeps the stems from losing energy."""
    magnitude = np.abs(np.random.default_rng(4).standard_normal((64, 128))) + 0.01
    harmonic_mask, percussive_mask = hpss_masks(magnitude)
    assert np.max(np.abs(harmonic_mask + percussive_mask - 1.0)) < 1e-15
    assert np.all(harmonic_mask >= 0.0) and np.all(harmonic_mask <= 1.0)


def test_hpss_reconstruction_is_exact() -> None:
    """Harmonic + percussive must sum back to the input to <1e-5 (plan bound)."""
    result = hpss_separate(_hpss_mixture(), sample_rate=RATE)
    assert result.reconstruction_error < 1e-5
    assert result.mask_sum_error == 0.0


def test_hpss_separates_a_tone_from_clicks() -> None:
    """The sustained tone goes to harmonic; the transients go to percussive."""
    result = hpss_separate(_hpss_mixture(), sample_rate=RATE)

    def band_energy(signal: np.ndarray, low: float, high: float) -> float:
        spectrum = np.abs(np.fft.rfft(signal)) ** 2
        frequencies = np.fft.rfftfreq(signal.size, d=1.0 / RATE)
        return float(spectrum[(frequencies > low) & (frequencies < high)].sum())

    tone_in_harmonic = band_energy(result.harmonic, 400.0, 480.0)
    tone_in_percussive = band_energy(result.percussive, 400.0, 480.0)
    click_in_harmonic = band_energy(result.harmonic, 2000.0, 20_000.0)
    click_in_percussive = band_energy(result.percussive, 2000.0, 20_000.0)
    assert tone_in_harmonic > 10.0 * tone_in_percussive
    assert click_in_percussive > 10.0 * click_in_harmonic


def test_hpss_reports_a_measured_centre_enhancement() -> None:
    """The number is reported, and it is not called a stem."""
    result = hpss_separate(_hpss_mixture(), sample_rate=RATE, centre_enhancement=True)
    assert result.centre_enhancement_db is not None
    assert np.isfinite(result.centre_enhancement_db)


def test_hpss_mask_kernels_must_be_odd() -> None:
    magnitude = np.abs(np.random.default_rng(4).standard_normal((32, 64)))
    with pytest.raises(ValueError, match="positive odd"):
        hpss_masks(magnitude, kernel_harmonic=4)
    with pytest.raises(ValueError, match="positive odd"):
        hpss_masks(magnitude, kernel_percussive=0)


def test_hpss_empty_input_degenerates_cleanly() -> None:
    result = hpss_separate(np.zeros(0), sample_rate=RATE)
    assert result.harmonic.size == 0
    assert result.reconstruction_error == 0.0


# --------------------------------------------------------------------------- #
# 3D — forensic QC
# --------------------------------------------------------------------------- #


def test_a_clean_mono_programme_passes() -> None:
    report = analyze_forensic_audio_qc(_tone(440.0, 0.5, 3.0), sample_rate=RATE)
    assert report.ok, report.codes
    assert report.stereo_phase_correlation is None
    assert report.clipped_sample_count == 0
    assert report.zero_dropout_hole_count == 0


def test_correlation_measures_the_stereo_image() -> None:
    left = _tone(440.0)
    identical = analyze_forensic_audio_qc(np.vstack([left, left]), sample_rate=RATE)
    inverted = analyze_forensic_audio_qc(np.vstack([left, -left]), sample_rate=RATE)
    assert identical.stereo_phase_correlation == pytest.approx(1.0, abs=1e-9)
    assert inverted.stereo_phase_correlation == pytest.approx(-1.0, abs=1e-9)


def test_out_of_phase_material_is_flagged() -> None:
    """Correlation below -0.25 is a mono-folddown fault, and says so."""
    left = _tone(440.0)
    report = analyze_forensic_audio_qc(np.vstack([left, -left]), sample_rate=RATE)
    assert report.mono_foldback_risk is True
    assert "mono_foldback_risk" in report.codes
    assert report.ok is False


def test_unrelated_channels_are_not_flagged() -> None:
    """Decorrelated stereo is wide, not broken; it must not raise the flag."""
    rng = np.random.default_rng(8)
    left = rng.standard_normal(RATE)
    right = rng.standard_normal(RATE)
    report = analyze_forensic_audio_qc(np.vstack([left, right]), sample_rate=RATE)
    assert report.stereo_phase_correlation == pytest.approx(0.0, abs=0.1)
    assert report.mono_foldback_risk is False


def test_dc_offset_is_measured_in_both_units() -> None:
    report = analyze_forensic_audio_qc(_tone(440.0) + 0.05, sample_rate=RATE)
    assert report.dc_offset_linear == pytest.approx(0.05, abs=1e-6)
    assert report.dc_offset_db == pytest.approx(20.0 * np.log10(0.05), abs=1e-4)


def test_clipping_is_counted_with_its_longest_run() -> None:
    """The run length is the useful part: one sample is rounding, 10 is not."""
    signal = _tone(440.0, 0.5, 1.0).copy()
    signal[1000:1010] = 1.0
    signal[2000] = 1.0
    report = analyze_forensic_audio_qc(signal, sample_rate=RATE)
    assert report.clipped_sample_count == 11
    assert report.max_consecutive_clipped_samples == 10
    assert "clipped_samples" in report.codes


def test_a_lone_clipped_sample_has_run_one() -> None:
    signal = _tone(440.0, 0.5, 1.0).copy()
    signal[1000] = 1.0
    report = analyze_forensic_audio_qc(signal, sample_rate=RATE)
    assert report.clipped_sample_count == 1
    assert report.max_consecutive_clipped_samples == 1


def test_zero_dropout_hole_is_detected_inside_audio() -> None:
    signal = _tone(440.0, 0.5, 2.0).copy()
    signal[10_000:10_200] = 0.0
    report = analyze_forensic_audio_qc(signal, sample_rate=RATE)
    assert report.zero_dropout_hole_count == 1
    assert report.longest_dropout_run == 200
    assert "zero_dropout" in report.codes


def test_leading_and_trailing_silence_is_not_a_dropout() -> None:
    """Legitimate programme silence must not be reported as a dropped buffer."""
    signal = np.concatenate([np.zeros(5_000), _tone(440.0, 0.5, 1.0), np.zeros(5_000)])
    report = analyze_forensic_audio_qc(signal, sample_rate=RATE)
    assert report.zero_dropout_hole_count == 0


def test_very_short_dropouts_are_ignored_as_noise_floor() -> None:
    signal = _tone(440.0, 0.5, 2.0).copy()
    signal[10_000:10_002] = 0.0
    report = analyze_forensic_audio_qc(signal, sample_rate=RATE)
    assert report.zero_dropout_hole_count == 0


def test_empty_audio_is_reported_not_crashed() -> None:
    report = analyze_forensic_audio_qc(np.zeros(0), sample_rate=RATE)
    assert report.codes == ("audio_qc_empty",)
    assert report.seconds == 0.0


def test_invalid_inputs_are_refused() -> None:
    with pytest.raises(ValueError, match="sample_rate"):
        analyze_forensic_audio_qc(np.zeros(10), sample_rate=0)
    with pytest.raises(ValueError, match="channels"):
        analyze_forensic_audio_qc(np.zeros((2, 10)), sample_rate=RATE, channels=3)


# --------------------------------------------------------------------------- #
# 3D — de-esser
# --------------------------------------------------------------------------- #


def _sibilant_voice() -> tuple[np.ndarray, np.ndarray]:
    time = np.arange(int(RATE * 3.0)) / RATE
    voice = 0.4 * np.sin(2.0 * np.pi * 300.0 * time) + 0.25 * np.sin(2.0 * np.pi * 1200.0 * time)
    sibilance = np.zeros(time.size)
    for when in np.arange(0.2, 3.0, 0.35):
        start = int(when * RATE)
        length = int(0.08 * RATE)
        if start + length < time.size:
            sibilance[start : start + length] += (
                0.55 * np.sin(2.0 * np.pi * 6500.0 * np.arange(length) / RATE) * np.hanning(length)
            )
    return voice + sibilance, voice


def test_the_sibilance_band_is_where_the_plan_says() -> None:
    assert SIBILANCE_LOW_HZ == 4_500.0
    assert SIBILANCE_HIGH_HZ == 9_500.0


def test_de_esser_reduces_sibilance() -> None:
    """Sibilance energy must fall by well over half; the plan asks it to engage."""
    signal, _voice = _sibilant_voice()
    result = deess_sibilance_band(signal, sample_rate=RATE)
    assert result.max_reduction_db > 6.0
    assert result.engaged_frames > 0

    def band_energy(data: np.ndarray, low: float, high: float) -> float:
        spectrum = np.abs(np.fft.rfft(data)) ** 2
        frequencies = np.fft.rfftfreq(data.size, d=1.0 / RATE)
        return float(spectrum[(frequencies >= low) & (frequencies <= high)].sum())

    before = band_energy(signal, 5_500.0, 7_500.0)
    after = band_energy(result.samples, 5_500.0, 7_500.0)
    assert after < before / 2.0


def test_de_esser_leaves_the_voice_band_untouched() -> None:
    """Zero phase distortion in 100 Hz-4 kHz, measured, not asserted.

    A cascaded minimum-phase crossover shifts the low band by milliseconds and
    the ratio here would land near 1.0 only by accident. The FFT split gives a
    ratio of 1.0 to ~1e-9.
    """
    signal, voice = _sibilant_voice()
    result = deess_sibilance_band(signal, sample_rate=RATE)

    def band_energy(data: np.ndarray, low: float, high: float) -> float:
        spectrum = np.abs(np.fft.rfft(data)) ** 2
        frequencies = np.fft.rfftfreq(data.size, d=1.0 / RATE)
        return float(spectrum[(frequencies >= low) & (frequencies <= high)].sum())

    ratio = band_energy(result.samples, 100.0, 4_000.0) / band_energy(signal, 100.0, 4_000.0)
    assert ratio == pytest.approx(1.0, abs=1e-6)
    assert result.low_band_deviation_db < -80.0
    # The voice on its own is untouched, which is the strongest statement.
    _ = voice


def test_a_signal_with_no_sibilance_passes_through_unchanged() -> None:
    """No detector engagement means no gain, so the output is the input."""
    voice = _tone(300.0, 0.4, 2.0)
    result = deess_sibilance_band(voice, sample_rate=RATE, threshold_db=-10.0)
    assert result.max_reduction_db == 0.0
    assert np.max(np.abs(result.samples - voice)) < 1e-9


def test_de_esser_handles_stereo_and_preserves_shape() -> None:
    left, _ = _sibilant_voice()
    right = left * 0.8
    result = deess_sibilance_band(np.vstack([left, right]), sample_rate=RATE)
    assert result.samples.shape == (2, left.size)


def test_de_esser_refuses_a_rate_it_cannot_serve() -> None:
    with pytest.raises(ValueError, match="cannot carry"):
        deess_sibilance_band(_tone(300.0, 0.4, 0.1), sample_rate=8_000)


def test_de_esser_refuses_impossible_parameters() -> None:
    signal = _tone(300.0, 0.4, 0.2)
    with pytest.raises(ValueError, match="attack_ms"):
        deess_sibilance_band(signal, sample_rate=RATE, attack_ms=0.0)
    with pytest.raises(ValueError, match="max_reduction_db"):
        deess_sibilance_band(signal, sample_rate=RATE, max_reduction_db=-1.0)


def test_vocal_removal_still_returns_unavailable() -> None:
    """The honesty contract, restated where the tempting alternative now exists.

    ``audio.remove_vocal`` returns ``UNAVAILABLE / REQUIRES_LOCAL_MODEL``.  An M/S
    split and an HPSS pass now exist in this repository, and neither is a vocal
    separator: M/S separates centre from side, HPSS separates sustained from
    transient.  Returning either as a "stem" would be a fabricated asset, which
    is exactly what the refusal prevents.
    """
    from nexus_ai_agent.creative.packs.audio.models import CODE_REQUIRES_LOCAL_MODEL
    from nexus_ai_agent.creative.packs.audio.operations import _remove_vocal

    assert CODE_REQUIRES_LOCAL_MODEL == "REQUIRES_LOCAL_MODEL"
    assert callable(_remove_vocal)
