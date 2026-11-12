"""§4.7 — octave-stable tempo estimation with GUM measurement uncertainty.

These tests exist because the previous estimator could silently return an
octave-flipped tempo (120 reported as 60 or 240) with no way for a caller to
know, and reported a bare ``confidence`` scalar that looked like precision but
was a heuristic margin.  Every assertion below is a *measured* quantity.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from nexus_ai_agent.creative.slideshow.audio import (
    COVERAGE_FACTOR_95,
    PcmAudio,
    collect_tempo_candidates,
    spectral_flux_envelope,
    tempo_uncertainty,
)

SAMPLE_RATE = 48_000


def _click_track(
    bpm: float,
    *,
    seconds: float = 12.0,
    accent_every: int | None = None,
    tone_hz: float = 320.0,
) -> PcmAudio:
    """Exponentially decaying tonal clicks at ``bpm`` — the classic tempo probe."""
    count = int(SAMPLE_RATE * seconds)
    samples = np.zeros(count, dtype=np.float64)
    decay = np.exp(-np.linspace(0.0, 12.0, int(0.05 * SAMPLE_RATE)))
    onset = int(0.05 * SAMPLE_RATE)
    for beat, when in enumerate(np.arange(0.0, seconds, 60.0 / bpm)):
        start = int(when * SAMPLE_RATE)
        if start + onset >= count:
            break
        amplitude = 0.9 if (accent_every and beat % accent_every == 0) else 0.5
        samples[start : start + onset] += (
            amplitude * decay * np.sin(2.0 * np.pi * tone_hz * np.arange(onset) / SAMPLE_RATE)
        )
    return PcmAudio(samples=samples.astype(np.float32), sample_rate=SAMPLE_RATE)


def _backbeat(bpm: float, *, seconds: float = 12.0) -> PcmAudio:
    """Kick/snare backbeat with 8th-note hats — the classic octave trap."""
    count = int(SAMPLE_RATE * seconds)
    samples = np.zeros(count, dtype=np.float64)
    hit = int(0.06 * SAMPLE_RATE)
    decay = np.exp(-np.linspace(0.0, 14.0, hit))
    kick = np.sin(2.0 * np.pi * 55.0 * np.arange(hit) / SAMPLE_RATE) * decay
    snare = (
        np.random.default_rng(3).standard_normal(hit) * 0.55 * np.exp(-np.linspace(0.0, 20.0, hit))
    )
    hat_len = int(0.02 * SAMPLE_RATE)
    hat = np.random.default_rng(9).standard_normal(hat_len) * np.exp(
        -np.linspace(0.0, 18.0, hat_len)
    )
    beat = 60.0 / bpm
    for index, when in enumerate(np.arange(0.0, seconds, beat)):
        start = int(when * SAMPLE_RATE)
        if start + hit >= count:
            break
        position = index % 4
        if position in (0, 2):
            samples[start : start + hit] += 0.85 * kick
        if position in (1, 3):
            samples[start : start + hit] += 0.6 * snare
        if start + hat_len < count:
            samples[start : start + hat_len] += 0.25 * hat
        off = start + int(beat * 0.5 * SAMPLE_RATE)
        if off + hat_len < count:
            samples[off : off + hat_len] += 0.18 * hat
    return PcmAudio(samples=np.clip(samples, -1.0, 1.0).astype(np.float32), sample_rate=SAMPLE_RATE)


@pytest.mark.parametrize(
    ("bpm", "tolerance"),
    [(96.0, 1.0), (120.0, 1.0), (140.0, 1.0), (60.0, 0.5), (180.0, 2.0)],
)
def test_tempo_locks_onto_the_true_rate(bpm: float, tolerance: float) -> None:
    """The detected BPM is within ``tolerance`` of the truth — and it is reported."""
    evidence = collect_tempo_candidates(_click_track(bpm))
    assert evidence.status == "detected"
    assert evidence.tempo_bpm is not None
    assert abs(evidence.tempo_bpm - bpm) <= tolerance


def test_an_isochronous_fast_pulse_is_not_folded_down() -> None:
    """Every click the same loudness at 240 BPM *is* 240 BPM — and stays 240.

    The tempting "nobody taps 240, so halve it" rule is wrong here: the onset
    stream contains no evidence of a slower beat, because every onset sits on
    the fast grid at equal strength.  Folding an isochronous stream to half its
    rate is an octave error in the *other* direction, and the estimator must
    not commit it just because the number looks high.
    """
    evidence = collect_tempo_candidates(_click_track(240.0))
    assert evidence.status == "detected"
    assert evidence.tempo_bpm == pytest.approx(240.0, abs=3.0)
    assert evidence.octave_relation == "as_is"


def test_subdivisions_under_a_backbeat_are_folded_onto_the_beat() -> None:
    """The real octave case: 8th-note hats over a kick/snare backbeat.

    The onset stream has twice as many hats as beats.  Without the subdivision
    test the estimator locks onto the hat rate and reports double time; the
    measured strength ratio (leftover onsets ≈ 0.65–0.71 of the explained ones)
    is what identifies them as subdivisions.  The reported relation must say the
    period was doubled, i.e. the answer was folded down deliberately.
    """
    evidence = collect_tempo_candidates(_backbeat(120.0))
    assert evidence.status == "detected"
    assert evidence.tempo_bpm == pytest.approx(120.0, abs=2.5)
    assert evidence.octave_relation == "double"


@pytest.mark.parametrize("bpm", [80.0, 90.0, 100.0, 110.0, 120.0])
def test_backbeat_tempo_is_recovered_across_the_range(bpm: float) -> None:
    """The folding is not tuned to one number; it holds across the range."""
    evidence = collect_tempo_candidates(_backbeat(bpm))
    assert evidence.status == "detected"
    assert evidence.tempo_bpm == pytest.approx(bpm, abs=2.5)


def test_half_time_source_is_not_pushed_up_an_octave() -> None:
    """A genuine 60 BPM is left alone: the prior must not double a real pulse."""
    evidence = collect_tempo_candidates(_click_track(60.0))
    assert evidence.status == "detected"
    assert evidence.tempo_bpm == pytest.approx(60.0, abs=0.5)
    assert evidence.octave_relation == "as_is"


def test_syncopated_two_bar_accent_does_not_flip_the_octave() -> None:
    """A 4-beat accent cycle is a common octave-flip trigger; 120 stays 120."""
    evidence = collect_tempo_candidates(_click_track(120.0, accent_every=4))
    assert evidence.status == "detected"
    assert evidence.tempo_bpm == pytest.approx(120.0, abs=2.0)
    assert evidence.tempo_bpm == pytest.approx(120.0, abs=2.5)


def test_uncertainty_is_reported_and_expanded_by_the_declared_coverage_factor() -> None:
    """``bpm_ci95_half_width`` is exactly ``1.96 · u_c`` — not a rounded guess."""
    evidence = collect_tempo_candidates(_click_track(120.0))
    assert evidence.status == "detected"
    assert evidence.bpm_std_uncertainty > 0.0
    assert evidence.bpm_ci95_half_width == pytest.approx(
        COVERAGE_FACTOR_95 * evidence.bpm_std_uncertainty, rel=1e-4
    )
    # Type A and Type B combine in quadrature: neither may vanish, because a
    # zero component would mean the model silently dropped a physical effect.
    assert evidence.bpm_uncertainty_type_a > 0.0
    assert evidence.bpm_uncertainty_type_b > 0.0
    assert evidence.bpm_std_uncertainty == pytest.approx(
        math.hypot(evidence.bpm_uncertainty_type_a, evidence.bpm_uncertainty_type_b),
        rel=1e-4,
    )


def test_uncertainty_is_zero_when_no_tempo_was_detected() -> None:
    """No detection means no uncertainty — never a small number implying precision."""
    silence = PcmAudio(samples=np.zeros(SAMPLE_RATE, dtype=np.float32), sample_rate=SAMPLE_RATE)
    evidence = collect_tempo_candidates(silence)
    assert evidence.status != "detected"
    assert evidence.bpm_std_uncertainty == 0.0
    assert evidence.bpm_ci95_half_width == 0.0


def test_type_a_follows_the_gum_formula_exactly() -> None:
    """``u_A = (60/τ̄²)·s_τ/√M`` — verified against a hand computation."""
    period = 0.5
    intervals = np.array([0.48, 0.51, 0.50, 0.52, 0.49], dtype=np.float64)
    mean = float(intervals.mean())
    dispersion = float(intervals.std(ddof=1))
    type_a, type_b, combined, expanded, _skip = tempo_uncertainty(
        intervals, mean_period=mean, hop_seconds=0.01
    )
    expected = (60.0 / mean**2) * dispersion / math.sqrt(intervals.size)
    assert type_a == pytest.approx(expected, rel=1e-12)
    assert type_b > 0.0
    assert combined == pytest.approx(math.hypot(type_a, type_b), rel=1e-12)
    assert expanded == pytest.approx(1.96 * combined, rel=1e-12)
    assert period > 0  # silence the unused-variable linter without changing maths


def test_type_b_follows_the_hop_quantisation_model() -> None:
    """``u_B = (60/(k̄τ̄²))·Δt/√12`` with the rectangular distribution."""
    hop = 1.0 / 93.75  # 512 samples at 48 kHz
    intervals = np.full(8, 0.5, dtype=np.float64)
    _a, type_b, _c, _e, mean_skip = tempo_uncertainty(
        np.full(16, 1.0), mean_period=1.0, hop_seconds=hop
    )
    assert mean_skip == 1.0
    assert type_b == pytest.approx(60.0 / (1.0 * 1.0**2) * hop / math.sqrt(12.0), rel=1e-12)
    # More intervals must never inflate the Type-B term: it is a property of the
    # instrument (the hop), not of how many samples we averaged.
    _a2, type_b2, _c2, _e2, _ = tempo_uncertainty(intervals, mean_period=0.5, hop_seconds=hop)
    assert type_b2 == pytest.approx(type_b * 4.0, rel=1e-9)


def test_spectral_flux_envelope_separates_quiet_high_attacks_from_a_loud_bass() -> None:
    """Multi-band flux sees a quiet hat that full-band energy flux would miss.

    The low band is driven hard, then a quiet high transient is added.  A
    full-band energy flux would be dominated by the bass; the per-band
    log-magnitude sum registers the hat as a distinct onset.
    """
    seconds = 2.0
    count = int(SAMPLE_RATE * seconds)
    time = np.arange(count) / SAMPLE_RATE
    bass = 0.8 * np.sin(2.0 * np.pi * 60.0 * time)
    hat = np.where(
        (time > 1.0) & (time < 1.002), 0.05 * np.random.default_rng(7).standard_normal(count), 0.0
    )
    flux, hop_seconds = spectral_flux_envelope(bass + hat, SAMPLE_RATE)
    assert flux.size > 10
    assert hop_seconds == pytest.approx(512 / SAMPLE_RATE)
    before = flux[: int(0.9 / hop_seconds)]
    after = flux[int(0.95 / hop_seconds) : int(1.05 / hop_seconds)]
    assert after.max() > 3.0 * max(before.max(), 1e-9)


def test_short_signal_degenerates_cleanly() -> None:
    """A signal shorter than one STFT window yields an empty envelope, no crash."""
    flux, hop_seconds = spectral_flux_envelope(np.zeros(100, dtype=np.float32), SAMPLE_RATE)
    assert flux.size == 0
    assert hop_seconds == pytest.approx(512 / SAMPLE_RATE)
