"""Fail-closed guards for non-finite audio: ``NaN``/``Inf`` must never be published.

Both modules used to *propagate* a non-finite sample instead of refusing it, and in
the loudness core that was not cosmetic: the gated loudness correctly came back
``None`` while ``true_peak_dbtp`` came back ``float("nan")``, and every downstream
ceiling is written as ``measured > limit`` — which is ``False`` for ``nan``. An
unmeasurable signal therefore *passed* a true-peak gate. These tests pin the
replacement contract: a typed ``ValueError`` at the boundary, and a finite result for
every input that is accepted.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from nexus_ai_agent.creative.audio_engine import loudness_bs1770, wiener_denoise

RATE = 16_000


def _noisy(seconds: float = 0.5) -> np.ndarray:
    rng = np.random.default_rng(7)
    time = np.arange(int(RATE * seconds)) / RATE
    return 0.3 * np.sin(2.0 * np.pi * 220.0 * time) + 0.05 * rng.standard_normal(time.size)


# --------------------------------------------------------------------------- #
# wiener_denoise: input, gain primitive, and the published output
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_wiener_denoise_refuses_non_finite_samples(bad: float) -> None:
    samples = _noisy()
    samples[123] = bad
    with pytest.raises(ValueError, match="non-finite value"):
        wiener_denoise.wiener_denoise(samples, sample_rate=RATE)


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_wiener_gain_refuses_non_finite_powers(bad: float) -> None:
    power = np.full(16, 2.0)
    power[3] = bad
    with pytest.raises(ValueError, match="frame_power contains"):
        wiener_denoise.wiener_gain(power, np.ones(16))
    psd = np.ones(16)
    psd[5] = bad
    with pytest.raises(ValueError, match="noise_psd contains"):
        wiener_denoise.wiener_gain(np.full(16, 2.0), psd)


@pytest.mark.parametrize(
    ("frame_power", "noise_psd", "message"),
    [
        (np.array([1.0, -1.0]), np.ones(2), "non-negative"),
        (np.ones(2), np.array([1.0, 0.0]), "strictly positive"),
        (np.ones(2), np.array([1.0, -1.0]), "strictly positive"),
        (np.ones(3), np.ones(2), "does not match"),
        (np.zeros(0), np.zeros(0), "non-empty"),
    ],
)
def test_wiener_gain_refuses_degenerate_powers(
    frame_power: np.ndarray, noise_psd: np.ndarray, message: str
) -> None:
    """A gain is a ratio: a zero PSD is ``inf``, a negative one is meaningless."""
    with pytest.raises(ValueError, match=message):
        wiener_denoise.wiener_gain(frame_power, noise_psd)


def test_wiener_denoise_never_publishes_a_non_finite_output() -> None:
    """The accepted path stays finite — silence, DC and loud material alike."""
    for samples in (_noisy(), np.zeros(RATE), np.full(RATE, 0.5), _noisy() * 1e6):
        result = wiener_denoise.wiener_denoise(samples, sample_rate=RATE)
        assert np.all(np.isfinite(result.samples))
        assert math.isfinite(result.reconstruction_error)
        assert result.samples.size == samples.size


# --------------------------------------------------------------------------- #
# BS.1770: the measurement that could be *passed* by being unmeasurable
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_measure_bs1770_refuses_non_finite_samples(bad: float) -> None:
    samples = _noisy()
    samples[10] = bad
    with pytest.raises(ValueError, match="non-finite sample"):
        loudness_bs1770.measure_bs1770_numpy(samples, sample_rate=RATE)
    stereo = np.vstack([samples, samples])
    with pytest.raises(ValueError, match="non-finite sample"):
        loudness_bs1770.measure_bs1770_numpy(stereo, sample_rate=RATE)


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_true_peak_refuses_non_finite_samples(bad: float) -> None:
    """``nan > ceiling`` is ``False``: an unmeasurable peak must not pass a gate."""
    samples = _noisy()
    samples[7] = bad
    with pytest.raises(ValueError, match="non-finite sample"):
        loudness_bs1770.true_peak_dbtp(samples)
    ceiling = -1.0
    with pytest.raises(ValueError):
        # The old behaviour returned ``nan`` here and this expression evaluated False,
        # i.e. "no violation" — the exact bypass this guard removes.
        assert loudness_bs1770.true_peak_dbtp(samples) > ceiling


def test_true_peak_of_finite_input_stays_finite() -> None:
    peak = loudness_bs1770.true_peak_dbtp(_noisy())
    assert peak is not None and math.isfinite(peak)


def test_silence_keeps_the_documented_none_contract() -> None:
    """The silence contract is unchanged: ``None``, not ``-inf`` and not ``nan``."""
    silent = np.zeros(RATE)
    measurement = loudness_bs1770.measure_bs1770_numpy(silent, sample_rate=RATE)
    assert measurement.lufs is None
    assert measurement.true_peak_dbtp is None
    assert measurement.sample_peak_dbfs is None
    assert loudness_bs1770.true_peak_dbtp(silent) is None


def test_empty_input_is_still_a_measurement_not_an_error() -> None:
    measurement = loudness_bs1770.measure_bs1770_numpy(np.zeros(0), sample_rate=RATE)
    assert measurement.lufs is None
    assert measurement.true_peak_dbtp is None


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_dbfs_of_a_non_finite_value_is_none(bad: float) -> None:
    """``nan <= 0.0`` is False, so the old guard fell through to ``20*log10(nan)``."""
    assert loudness_bs1770._dbfs(bad) is None
