"""K2 numerical references, constrained reconstruction, late-echo and 5 dB/15% benchmark."""

from __future__ import annotations

import inspect

import numpy as np
import pytest

from nexus_ai_agent.creative.audio_engine import spectral_voice_restoration as k2


def test_exponential_integral_matches_independent_tabulated_values_at_the_branch_boundary():
    # DLMF E1 definition; values independent of this implementation/denoiser.
    x = np.array([1e-6, 0.01, 0.1, 0.5, 1, 2, 5, 10, 100])
    expected = np.array(
        [
            13.23829589306249,
            4.037929576538114,
            1.8229239584193906,
            0.5597735947761608,
            0.21938393439552027,
            0.04890051070806112,
            0.0011482955912753257,
            4.156968929685325e-6,
            3.683597761682032e-46,
        ]
    )
    np.testing.assert_allclose(k2.exponential_integral_e1(x), expected, rtol=3e-13)
    boundary = k2.exponential_integral_e1(np.array([1 - 1e-9, 1 + 1e-9]))
    assert abs(boundary[0] - boundary[1]) < 8e-10
    assert k2.exponential_integral_e1(np.array([1000.0]))[0] == 0
    assert k2.exponential_integral_e1(np.array([])).size == 0
    for invalid in (0, -1, np.nan, np.inf):
        with pytest.raises(ValueError):
            k2.exponential_integral_e1(np.array([invalid]))


@pytest.mark.parametrize(("size", "overlap"), [(0, 0.75), (1, 0.75), (100, 0.5), (12345, 0.75)])
def test_wola_is_exact_even_at_boundaries_and_never_changes_duration(size, overlap):
    x = np.random.default_rng(41).normal(size=size)
    frames = k2.wola_analyse(x, overlap=overlap)
    y = k2.wola_synthesise(frames.spectrum, frames)
    assert y.shape == x.shape
    np.testing.assert_allclose(y, x, atol=2e-15)


def test_transform_shape_nonfinite_and_window_refusals():
    for x in (np.zeros((2, 2)), np.array([np.nan])):
        with pytest.raises(ValueError):
            k2.wola_analyse(x)
    for frame in (0, 15, 1023, 20000):
        with pytest.raises(ValueError):
            k2.wola_analyse(np.zeros(20), frame_length=frame)
    with pytest.raises(ValueError):
        k2.wola_analyse(np.ones(20), overlap=0.3)
    frames = k2.wola_analyse(np.ones(2000))
    for bad in (np.zeros((2, 2)), np.full_like(frames.spectrum, np.nan)):
        with pytest.raises(ValueError):
            k2.wola_synthesise(bad, frames)


def test_lsa_gain_uses_exponential_integral_not_wiener_and_has_a_musical_noise_floor():
    gain = k2.mmse_lsa_gain(np.array([1.0]), np.array([2.0]))
    assert gain[0] == pytest.approx(0.5 * np.exp(0.5 * 0.21938393439552027), abs=1e-15)
    assert gain[0] != 0.5
    np.testing.assert_array_equal(k2.mmse_lsa_gain(np.zeros(10), np.zeros(10), floor=0.04), 0.04)
    assert np.isfinite(k2.mmse_lsa_gain(np.array([1e12]), np.array([1e12]))).all()
    for kwargs in ({"floor": 0}, {"floor": 1}):
        with pytest.raises(ValueError):
            k2.mmse_lsa_gain(np.ones(2), np.ones(2), **kwargs)
    for xi, gamma in ((-1, 1), (1, -1), (np.nan, 1), (1, np.inf)):
        with pytest.raises(ValueError):
            k2.mmse_lsa_gain(np.array([xi]), np.array([gamma]))


def test_lsa_strength_zero_silence_short_input_and_noise_only_reference_contract():
    x = np.random.default_rng(53).normal(size=16000) * 0.1
    result = k2.mmse_lsa_denoise(x, sample_rate=16000, strength=0)
    np.testing.assert_allclose(result.samples, x, atol=2e-16)
    assert result.reconstruction_error < 1e-15
    assert k2.mmse_lsa_denoise(np.zeros(0), sample_rate=16000).samples.size == 0
    for signal in (np.zeros(1), np.zeros(16000), np.ones(100) * 0.1):
        result = k2.mmse_lsa_denoise(signal, sample_rate=16000)
        assert np.isfinite(result.samples).all()
        assert result.samples.shape == signal.shape
    with pytest.raises(ValueError, match="noise-only reference"):
        k2.mmse_lsa_denoise(x, sample_rate=16000, noise_reference=np.zeros(20))
    for kwargs in ({"sample_rate": 0}, {"smoothing": 1}, {"strength": -1}, {"floor": 0}):
        with pytest.raises(ValueError):
            k2.mmse_lsa_denoise(x, **({"sample_rate": 16000} | kwargs))
    assert "clean_reference" not in inspect.signature(k2.mmse_lsa_denoise).parameters


def test_declip_pocs_exact_known_samples_and_positive_negative_constraints():
    fs = 16000
    t = np.arange(fs) / fs
    clean = 0.8 * np.sin(2 * np.pi * 440 * t)
    clipped = np.clip(clean, -0.5, 0.5)
    result = k2.consistent_declip(clipped, sample_rate=fs, threshold=0.5, bandwidth_hz=1200)
    mask = result.clipped_mask
    np.testing.assert_array_equal(result.samples[~mask], clipped[~mask])
    assert np.all(result.samples[clipped >= 0.5] >= 0.5)
    assert np.all(result.samples[clipped <= -0.5] <= -0.5)
    assert np.max(result.samples) > 0.65 and np.min(result.samples) < -0.65
    before = np.sum((clipped - clean) ** 2)
    after = np.sum((result.samples - clean) ** 2)
    assert after < before * 0.1
    assert 0 < result.iterations <= 100


def test_declip_does_not_normalize_reliable_audio_or_invent_fully_missing_peaks():
    x = np.zeros(100)
    result = k2.consistent_declip(x, sample_rate=16000)
    np.testing.assert_array_equal(result.samples, x)
    assert result.iterations == 0
    with pytest.raises(ValueError, match="insufficient reliable"):
        k2.consistent_declip(np.ones(100), sample_rate=16000)
    for kwargs in (
        {"threshold": 0},
        {"bandwidth_hz": 8000},
        {"sample_rate": 0},
        {"iterations": 0},
        {"iterations": 501},
    ):
        with pytest.raises(ValueError):
            k2.consistent_declip(x, **({"sample_rate": 16000} | kwargs))
    # A full-band alternating sequence lies in the projection subspace and
    # reaches the convergence criterion without running an arbitrary 100 loops.
    result = k2.consistent_declip(
        np.r_[np.zeros(1000), 0.5, np.zeros(1000)],
        sample_rate=16000,
        threshold=0.5,
        bandwidth_hz=7999,
    )
    assert result.iterations < 100


def degraded_voice_fixture(seed=42):
    """Reproducible voiced AM surrogate, not a natural-speech/listening claim.

    Noise is 5 dB BEFORE hard clipping; exactly 15% of samples hit the plateau.
    Only an independent noise-only prefix is given to the estimator.
    """
    fs = 16000
    t = np.arange(fs * 3) / fs
    envelope = np.sin(np.pi * np.clip((t - 0.3) / 2.4, 0, 1)) ** 2 * (
        0.65 + 0.35 * np.sin(2 * np.pi * 3 * t) ** 2
    )
    clean = envelope * (
        0.45 * np.sin(2 * np.pi * 180 * t)
        + 0.18 * np.sin(2 * np.pi * 540 * t)
        + 0.1 * np.sin(2 * np.pi * 900 * t)
    )
    noise = np.random.default_rng(seed).normal(size=t.size)
    noise *= np.sqrt(np.mean(clean**2) / (10**0.5 * np.mean(noise**2)))
    noisy = clean + noise
    threshold = float(np.quantile(np.abs(noisy), 0.85))
    clipped = np.clip(noisy, -threshold, threshold)
    return fs, clean, noise, noisy, threshold, clipped


@pytest.mark.parametrize("seed", [42, 43, 44])
def test_5db_noise_15percent_clipping_snr_crest_and_monotonic_modulation_benchmark(seed):
    fs, clean, noise, noisy, threshold, clipped = degraded_voice_fixture(seed)
    assert 10 * np.log10(np.sum(clean**2) / np.sum((noisy - clean) ** 2)) == pytest.approx(5)
    assert np.mean(np.abs(clipped) >= threshold) == pytest.approx(0.15)
    declipped = k2.consistent_declip(
        clipped,
        sample_rate=fs,
        threshold=threshold,
        bandwidth_hz=1800,
    ).samples
    correlations = [k2.modulation_correlation(clean, clipped, sample_rate=fs)]
    for strength in (0.25, 0.5, 0.75, 1):
        output = k2.mmse_lsa_denoise(
            declipped,
            sample_rate=fs,
            noise_reference=noise[: fs // 2],
            strength=strength,
        ).samples
        correlations.append(k2.modulation_correlation(clean, output, sample_rate=fs))
    assert np.all(np.diff(correlations) > 0)
    metrics = k2.restoration_metrics(clean, clipped, output, sample_rate=fs)
    assert metrics.snr_gain_db >= 9.0
    assert abs(metrics.output_crest_db - metrics.reference_crest_db) < abs(
        metrics.input_crest_db - metrics.reference_crest_db
    )
    assert metrics.output_modulation_correlation > metrics.input_modulation_correlation


def test_wpe_cancels_late_prediction_without_moving_early_reflections():
    fs = 16000
    dry = np.random.default_rng(71).normal(size=fs * 3) * 0.1
    reverberant = dry.copy()
    reverberant[768:] += 0.65 * dry[:-768]  # 48 ms, beyond the early-reflection window
    reverberant[1536:] += 0.4 * dry[:-1536]
    result = k2.wpe_dereverberate(reverberant, sample_rate=fs, taps=12, frame_length=512)
    metrics = k2.restoration_metrics(dry, reverberant, result.samples, sample_rate=fs)
    assert metrics.snr_gain_db > 4
    assert result.prediction_delay_ms >= 35
    np.testing.assert_array_equal(result.samples[:560], reverberant[:560])
    assert len(result.residual_power) == 3
    assert result.samples.shape == dry.shape
    stereo = np.vstack((reverberant, np.roll(reverberant, 7)))
    assert (
        k2.wpe_dereverberate(stereo, sample_rate=fs, frame_length=512).samples.shape == stereo.shape
    )
    short = k2.wpe_dereverberate(np.ones(100), sample_rate=fs)
    np.testing.assert_array_equal(short.samples, np.ones(100))
    assert short.residual_power == ()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sample_rate": 0},
        {"prediction_delay_ms": 34},
        {"taps": 0},
        {"iterations": 11},
        {"regularization": 0},
    ],
)
def test_wpe_invalid_contracts_fail_closed(kwargs):
    with pytest.raises(ValueError):
        k2.wpe_dereverberate(np.ones(1000), **({"sample_rate": 16000} | kwargs))


@pytest.mark.parametrize("x", [np.ones((5, 100)), np.ones((2, 2, 2)), np.full(10, np.nan)])
def test_wpe_rejects_unknown_or_nonfinite_geometry(x):
    with pytest.raises(ValueError):
        k2.wpe_dereverberate(x, sample_rate=16000)


def test_measurement_functions_refuse_unaligned_or_insufficient_reference_evidence():
    for a, b, fs in (
        (np.zeros(0), np.zeros(0), 16000),
        (np.zeros(10), np.zeros(11), 16000),
        (np.zeros(1000), np.zeros(1000), 0),
        (np.zeros(10), np.zeros(10), 16000),
    ):
        with pytest.raises(ValueError):
            k2.modulation_correlation(a, b, sample_rate=fs)
    for inputs in ((np.zeros(0),) * 3, (np.ones(10), np.ones(11), np.ones(10))):
        with pytest.raises(ValueError):
            k2.restoration_metrics(*inputs, sample_rate=16000)
    assert k2.modulation_correlation(np.ones(1000), np.ones(1000), sample_rate=16000) == 0
    m = k2.restoration_metrics(np.zeros(1000), np.zeros(1000), np.zeros(1000), sample_rate=16000)
    assert np.isfinite(m.snr_gain_db)
