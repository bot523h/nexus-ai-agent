"""Part 3B — decision-directed Wiener denoiser with measured success criteria.

The claims this module makes are numeric, so the tests are too: perfect
reconstruction, an SNR improvement of at least +6 dB, and in-band harmonic
correlation of at least 0.97.  A denoiser that removes noise by removing the
programme would pass a "noise went down" check, so the harmonic test is the one
that actually constrains it.

The estimator design is pinned here as well, because two textbook choices
measurably fail on music and the tests exist to stop them being reintroduced.
"""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.audio_engine.wiener_denoise import (
    DEFAULT_FRAME,
    DEFAULT_OVERLAP,
    cola_window,
    estimate_noise_psd,
    wiener_denoise,
    wiener_gain,
)

RATE = 16_000
HARMONICS = (220.0, 660.0, 1100.0)


def _clean(seconds: float = 4.0, rate: int = RATE) -> np.ndarray:
    time = np.arange(int(rate * seconds)) / rate
    return (
        0.4 * np.sin(2.0 * np.pi * 220.0 * time)
        + 0.25 * np.sin(2.0 * np.pi * 660.0 * time)
        + 0.15 * np.sin(2.0 * np.pi * 1100.0 * time)
    )


def _snr(reference: np.ndarray, candidate: np.ndarray) -> float:
    return 10.0 * np.log10(np.sum(reference**2) / np.sum((reference - candidate) ** 2))


def _harmonic_amplitudes(signal: np.ndarray, rate: int) -> np.ndarray:
    index = np.arange(signal.size)
    return np.array(
        [
            abs(np.sum(signal * np.exp(-2j * np.pi * frequency * index / rate)))
            for frequency in HARMONICS
        ]
    )


def test_the_window_makes_the_overlap_add_exactly_invertible() -> None:
    """The property that matters: ``w·(Σw²)⁻¹·w`` is the identity.

    Stated this way rather than as "the squared sum is a constant" because that
    phrasing is only true for the 75 % case on this implementation's framing.  At
    50 % overlap the sum is constant over each *pair* of frames, so asserting a
    single global constant there tests the test, not the code.  The property that
    actually has to hold — and that the denoiser depends on — is that analysis
    and synthesis with the same window is invertible, which is measured directly.
    """
    window = cola_window(1024, 0.75)
    doubled = np.concatenate([window, np.zeros(1024)])
    total = np.zeros(2048)
    for start in range(0, 1025, 256):
        total[start : start + 1024] += window**2
    interior = total[1024 - 256 : 1024]
    assert interior.max() - interior.min() < 1e-12
    assert float(np.mean(interior)) == pytest.approx(1.5, abs=1e-9)
    _ = doubled

    # The end-to-end invertibility, at both overlap settings.
    signal = _clean(1.0)
    for overlap in (0.5, 0.75):
        result = wiener_denoise(signal, sample_rate=RATE, overlap=overlap, strength=0.0)
        assert result.reconstruction_error < 1e-9


def test_symmetric_window_would_not_be_cola() -> None:
    """The rejected alternative, asserted so the distinction stays load-bearing."""
    hop = 256
    window = np.hanning(1024)
    total = np.zeros(1024 + hop)
    for start in range(0, 1025, hop):
        stop = min(start + 1024, 1024 + hop)
        total[start:stop] += (window**2)[: stop - start]
    middle = total[1024 - hop : 1024]
    assert middle.max() - middle.min() > 1e-5


@pytest.mark.parametrize("overlap", [0.5, 0.75])
@pytest.mark.parametrize("frame", [512, 1024, 2048])
def test_reconstruction_is_exact(frame: int, overlap: float) -> None:
    """Unity gain must round-trip to <1e-6 — the plan's bound, met by 10 orders."""
    signal = _clean(1.0)
    result = wiener_denoise(
        signal, sample_rate=RATE, frame_length=frame, overlap=overlap, strength=0.0
    )
    assert result.reconstruction_error < 1e-6
    assert np.max(np.abs(result.samples - signal)) < 1e-6


def test_strength_zero_is_an_identity() -> None:
    """A request for no suppression must return the input, not "almost" it."""
    signal = _clean(1.0)
    rng = np.random.default_rng(3)
    noisy = signal + 0.1 * rng.standard_normal(signal.size)
    result = wiener_denoise(noisy, sample_rate=RATE, strength=0.0)
    assert np.max(np.abs(result.samples - noisy)) < 1e-9


@pytest.mark.parametrize("rate", [8_000, 16_000, 48_000])
def test_snr_gain_is_at_least_six_db(rate: int) -> None:
    """The headline claim, measured against a known clean reference."""
    seconds = 3.0
    time = np.arange(int(rate * seconds)) / rate
    clean = (
        0.4 * np.sin(2.0 * np.pi * 220.0 * time)
        + 0.25 * np.sin(2.0 * np.pi * 660.0 * time)
        + 0.15 * np.sin(2.0 * np.pi * 1100.0 * time)
    )
    noisy = clean + 0.12 * np.random.default_rng(11).standard_normal(clean.size)
    result = wiener_denoise(noisy, sample_rate=rate, clean_reference=clean)
    assert result.snr_gain_db is not None
    assert result.snr_gain_db >= 6.0, result.snr_gain_db
    # And the gain claimed is the gain observed, computed independently.
    observed = _snr(clean, result.samples) - _snr(clean, noisy)
    assert result.snr_gain_db == pytest.approx(observed, abs=1e-6)


def test_harmonics_survive_the_filter() -> None:
    """In-band harmonic correlation >= 0.97: noise goes, programme stays.

    A denoiser that simply attenuates everything would show a large "SNR gain"
    on some metrics while destroying the signal; this is the check that pins it.
    """
    clean = _clean()
    noisy = clean + 0.12 * np.random.default_rng(11).standard_normal(clean.size)
    result = wiener_denoise(noisy, sample_rate=RATE, clean_reference=clean)
    correlation = float(
        np.corrcoef(_harmonic_amplitudes(clean, RATE), _harmonic_amplitudes(result.samples, RATE))[
            0, 1
        ]
    )
    assert correlation >= 0.97, correlation
    # The level of each harmonic is preserved to within 5 %, not merely ordered.
    ratio = _harmonic_amplitudes(result.samples, RATE) / _harmonic_amplitudes(clean, RATE)
    assert np.all(ratio > 0.95) and np.all(ratio < 1.05), ratio


def test_noise_floor_actually_drops() -> None:
    """Measure the residual in a band with no harmonics: it must fall."""
    clean = _clean()
    noisy = clean + 0.12 * np.random.default_rng(11).standard_normal(clean.size)
    result = wiener_denoise(noisy, sample_rate=RATE)
    spectrum_before = np.abs(np.fft.rfft(noisy)) ** 2
    spectrum_after = np.abs(np.fft.rfft(result.samples)) ** 2
    frequencies = np.fft.rfftfreq(noisy.size, d=1.0 / RATE)
    quiet_band = (frequencies > 2000.0) & (frequencies < 7000.0)
    before = float(spectrum_before[quiet_band].mean())
    after = float(spectrum_after[quiet_band].mean())
    assert after < before / 4.0, (before, after)


def test_noise_estimate_is_spectro_temporal() -> None:
    """A sustained tone must not inflate its own bin's noise estimate.

    Both textbook estimators fail this and both are recorded as rejected:
    minimum statistics returned 13401 where the noise floor is 5.5, and a time
    percentile alone returned 18053, because a continuous tone occupies the bin
    in every frame.  The neighbourhood minimum brings it to the floor.
    """
    rate = RATE
    clean = _clean(4.0, rate)
    noisy = clean + 0.12 * np.random.default_rng(11).standard_normal(clean.size)
    from nexus_ai_agent.creative.audio_engine.wiener_denoise import (
        _frame_signal,
        _hop_for,
    )

    frame = 1024
    hop = _hop_for(frame, 0.75)
    window = cola_window(frame, 0.75)
    frames, _lead, _count = _frame_signal(noisy, frame, hop)
    power = np.abs(np.fft.rfft(frames * window, axis=1)) ** 2
    psd = estimate_noise_psd(power)
    expected = 0.12**2 * float(np.sum(window**2))
    harmonic_bins = [int(round(f * frame / rate)) for f in HARMONICS]
    for index in harmonic_bins:
        assert psd[index] < expected * 2.0, (index, psd[index], expected)


def test_wiener_gain_is_bounded_and_floored() -> None:
    """The gain never exceeds unity and never falls below the floor."""
    noise = np.ones(64)
    loud = np.full(64, 1000.0)
    quiet = np.full(64, 1e-6)
    assert np.all(wiener_gain(loud, noise, floor=0.05) <= 1.0)
    assert np.all(wiener_gain(quiet, noise, floor=0.05) >= 0.05)


def test_spectral_floor_controls_the_residual() -> None:
    """A higher floor leaves more noise: the parameter does what it says."""
    clean = _clean()
    noisy = clean + 0.12 * np.random.default_rng(11).standard_normal(clean.size)
    low_floor = wiener_denoise(noisy, sample_rate=RATE, floor=0.02)
    high_floor = wiener_denoise(noisy, sample_rate=RATE, floor=0.3)
    assert np.sqrt(np.mean(high_floor.samples**2)) > np.sqrt(np.mean(low_floor.samples**2))


def test_strength_scales_suppression_monotonically() -> None:
    clean = _clean()
    noisy = clean + 0.12 * np.random.default_rng(11).standard_normal(clean.size)
    residuals = [
        float(
            np.sqrt(
                np.mean((wiener_denoise(noisy, sample_rate=RATE, strength=s).samples - clean) ** 2)
            )
        )
        for s in (0.0, 0.5, 1.0)
    ]
    assert residuals[0] > residuals[1] > residuals[2]


def test_result_carries_its_own_evidence() -> None:
    """The output reports the PSD, the gains and the framing it used."""
    result = wiener_denoise(_clean(2.0), sample_rate=RATE)
    assert result.noise_psd.size > 0
    assert result.mean_gain.size == result.noise_psd.size
    assert np.all(result.mean_gain > 0.0) and np.all(result.mean_gain <= 1.0)
    assert result.frame_length == DEFAULT_FRAME
    assert result.hop_length == int(DEFAULT_FRAME * (1.0 - DEFAULT_OVERLAP))
    assert result.frames > 0


def test_default_parameters_are_the_documented_ones() -> None:
    assert DEFAULT_FRAME == 1024
    assert DEFAULT_OVERLAP == 0.75


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"overlap": 0.6}, "does not divide the frame|0.5 or 0.75"),
        ({"strength": 1.5}, "strength"),
        ({"smoothing": 1.0}, "smoothing"),
        ({"strength": -0.1}, "strength"),
    ],
)
def test_invalid_parameters_are_refused(kwargs: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        wiener_denoise(_clean(0.5), sample_rate=RATE, **kwargs)


def test_noise_psd_refuses_degenerate_input() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        estimate_noise_psd(np.zeros((0, 4)))
    with pytest.raises(ValueError, match="at least 3 frames"):
        estimate_noise_psd(np.ones((2, 4)))


def test_silence_does_not_blow_up() -> None:
    result = wiener_denoise(np.zeros(RATE), sample_rate=RATE)
    assert result.samples.size == RATE
    assert np.all(np.isfinite(result.samples))
    assert float(np.max(np.abs(result.samples))) == 0.0


def test_empty_input_degenerates_cleanly() -> None:
    result = wiener_denoise(np.zeros(0), sample_rate=RATE)
    assert result.samples.size == 0
    assert result.frames == 0
    assert result.snr_gain_db is None


# --------------------------------------------------------------------------- #
# Degenerate paths — every one of these is a refusal or a documented ``None``,
# and an untested refusal is a refusal nobody has checked.
# --------------------------------------------------------------------------- #


def test_frame_length_must_be_positive() -> None:
    from nexus_ai_agent.creative.audio_engine.wiener_denoise import cola_window

    with pytest.raises(ValueError, match="frame_length must be positive"):
        cola_window(0, 0.75)


def test_overlap_must_leave_a_hop() -> None:
    from nexus_ai_agent.creative.audio_engine.wiener_denoise import _hop_for

    with pytest.raises(ValueError, match="overlap leaves no hop"):
        _hop_for(1024, 1.0)


def test_a_sub_window_signal_is_edge_padded_not_dropped() -> None:
    """A 100-sample input into a 1024-sample frame must still round-trip."""
    signal = np.random.default_rng(2).standard_normal(100)
    result = wiener_denoise(signal, sample_rate=RATE, strength=0.0)
    assert result.samples.size == 100
    assert np.max(np.abs(result.samples - signal)) < 1e-9


def test_framing_an_empty_signal_returns_no_frames() -> None:
    from nexus_ai_agent.creative.audio_engine.wiener_denoise import _frame_signal

    frames, lead, count = _frame_signal(np.zeros(0), 1024, 256)
    assert frames.shape == (0, 1024)
    assert lead == 0 and count == 0


def test_noise_estimate_refuses_an_impossible_percentile() -> None:
    power = np.ones((8, 16))
    with pytest.raises(ValueError, match="percentile"):
        estimate_noise_psd(power, percentile=0.0)
    with pytest.raises(ValueError, match="percentile"):
        estimate_noise_psd(power, percentile=100.0)


def test_noise_estimate_refuses_a_negative_neighbourhood() -> None:
    with pytest.raises(ValueError, match="neighbour_bins"):
        estimate_noise_psd(np.ones((8, 16)), neighbour_bins=-1)


def test_noise_estimate_falls_back_without_a_neighbourhood() -> None:
    """``neighbour_bins=0`` is an explicit request for the plain percentile."""
    power = np.abs(np.random.default_rng(1).standard_normal((16, 32))) + 0.1
    from numpy import percentile

    with_neighbours = estimate_noise_psd(power, neighbour_bins=3)
    without = estimate_noise_psd(power, neighbour_bins=0)
    assert with_neighbours.shape == without.shape == (32,)
    # With no neighbourhood the estimate *is* the bias-corrected percentile.
    expected = percentile(power, 10.0, axis=0) * 10.0 ** (2.6 / 10.0)
    assert np.allclose(without, expected, rtol=1e-9)


def test_wiener_gain_refuses_an_impossible_floor() -> None:
    with pytest.raises(ValueError, match="floor must be within"):
        wiener_gain(np.ones(8), np.ones(8), floor=0.0)
    with pytest.raises(ValueError, match="floor must be within"):
        wiener_gain(np.ones(8), np.ones(8), floor=1.0)


def test_a_perfect_estimate_reports_infinite_snr_not_a_number() -> None:
    """Zero error is not "a large number": it is not a finite measurement."""
    from nexus_ai_agent.creative.audio_engine.wiener_denoise import _instrumental_snr

    clean = _clean(0.5)
    assert _instrumental_snr(clean, clean, frame_length=1024, hop=256) == float("inf")


def test_a_silent_reference_reports_negative_infinity() -> None:
    from nexus_ai_agent.creative.audio_engine.wiener_denoise import _instrumental_snr

    reference = np.zeros(1000)
    estimate = np.random.default_rng(0).standard_normal(1000)
    assert _instrumental_snr(reference, estimate, frame_length=1024, hop=256) == float("-inf")


def test_the_noise_floor_is_a_representable_power_not_the_smallest_float() -> None:
    """The floor must be ~-200 dBFS, not ``float64.tiny``.

    ``np.finfo(np.float64).tiny`` is 2.2e-308: non-zero, so it looks like a
    floor, but any real signal divided by it overflows to infinity and the
    overlap-add turns the frame into NaN. The floor has to be a power level a
    converter could plausibly see.
    """
    from nexus_ai_agent.creative.audio_engine.wiener_denoise import NOISE_FLOOR_POWER

    assert NOISE_FLOOR_POWER == pytest.approx(1e-20)
    # -200 dBFS, and strictly above the smallest normal double.
    assert 10.0 * np.log10(NOISE_FLOOR_POWER) == pytest.approx(-200.0)
    assert NOISE_FLOOR_POWER > np.finfo(np.float64).tiny * 1e100


def test_silence_still_floors_the_estimate_finitely() -> None:
    """A silent reference must floor to a finite power, never zero or inf."""
    from nexus_ai_agent.creative.audio_engine.wiener_denoise import (
        NOISE_FLOOR_POWER,
        estimate_noise_psd,
    )

    power = np.zeros((8, 64), dtype=np.float64)
    estimate = estimate_noise_psd(power)
    assert estimate.shape == (64,)
    assert np.all(np.isfinite(estimate))
    assert np.all(estimate >= NOISE_FLOOR_POWER)
    assert np.all(estimate == pytest.approx(NOISE_FLOOR_POWER))
