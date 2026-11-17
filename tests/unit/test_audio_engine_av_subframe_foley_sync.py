"""K3: independent timestamp/physics oracles, not rounded-frame self-comparisons."""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.audio_engine import av_subframe_foley_sync as k3


def _events(t):
    return sum(
        a * np.exp(-0.5 * ((t - c) / w) ** 2)
        for c, w, a in [(0.7, 0.09, 1), (1.45, 0.12, 0.8), (2.3, 0.09, 0.9)]
    )


@pytest.mark.parametrize("lag", [-377.43, -3.76, 0, 2.37, 280.14])
def test_gcc_phat_recovers_signed_fractional_delay_not_an_integer_bin(lag):
    data = np.random.default_rng(81).normal(size=16000)
    data[:2000] = data[-2000:] = 0
    delayed = k3.fractional_time_shift(data, lag)
    result = k3.gcc_phat(data, delayed, sample_rate=16000, max_lag_seconds=0.05)
    assert abs(result.offset_samples - lag) < 0.06
    assert abs(result.offset_seconds - lag / 16000) < 0.0005
    assert 0 <= result.peak_margin <= 1
    assert result.confidence_kind.endswith("not_probability")


@pytest.mark.parametrize("fps", [24.0, 30000 / 1001, 60.0])
@pytest.mark.parametrize("lag_ms", [-67.35, -12.7, 4.65, 37.29])
def test_visual_interpolation_and_correction_reach_sub_half_ms_on_bounded_events(fps, lag_ms):
    fs = 16000
    t = np.arange(fs * 3) / fs
    # Analytic timestamps are independent of the estimator/corrector. Smooth
    # events fit within the visual Nyquist; no 24 fps claim for unseen clicks.
    visual = _events(np.arange(int(3 * fps)) / fps)
    feature = _events(np.arange(3000) / 1000 - lag_ms / 1000)
    pcm = _events(t - lag_ms / 1000) * np.sin(2 * np.pi * 1000 * t)
    result = k3.synchronize_audio_to_visual(
        pcm, visual, sample_rate=fs, frame_rate=fps, audio_feature=feature
    )
    assert abs(result.input_offset_ms - lag_ms) <= 0.5
    assert abs(result.residual_offset_ms) <= 0.5
    assert abs(result.applied_delay_samples + lag_ms * fs / 1000) <= fs * 0.0005
    # Independently recover the corrected PCM envelope; don't merely assert
    # `initial - correction == zero` using the same two numbers.
    measured = k3.rms_envelope(result.samples, sample_rate=fs)
    aligned = k3.estimate_av_offset(visual, measured, frame_rate=fps)
    assert abs(aligned.offset_seconds) <= 0.0005
    assert result.input_within_itu_window == (-15 <= lag_ms <= 5)


def test_default_rms_path_remeasures_actual_corrected_pcm():
    fs = 16000
    t = np.arange(fs * 3) / fs
    visual = _events(np.arange(90) / 30)
    stereo = np.vstack([_events(t - 0.02371) * np.cos(2 * np.pi * 1000 * t)] * 2)
    result = k3.synchronize_audio_to_visual(stereo, visual, sample_rate=fs, frame_rate=30)
    assert result.samples.shape == stereo.shape
    assert abs(result.input_offset_ms - 23.71) < 0.5
    assert abs(result.residual_offset_ms) < 0.5
    np.testing.assert_allclose(result.samples[0], result.samples[1], atol=0, rtol=0)


def test_visual_frames_are_timestamped_exactly_and_itu_bounds_are_asymmetric():
    visual = np.array([0.0, 1.0, 3.0, 2.0, 0.0])
    result = k3.interpolate_visual_features(visual, frame_rate=25, envelope_rate=1000, length=200)
    np.testing.assert_array_equal(result[[0, 40, 80, 120]], visual[:-1])
    assert np.all(result[161:] == 0)
    assert k3.within_itu_window(-15) and k3.within_itu_window(5)
    assert not k3.within_itu_window(-15.0001) and not k3.within_itu_window(5.0001)
    with pytest.raises(ValueError):
        k3.within_itu_window(float("nan"))


def test_shift_is_zero_extended_and_integer_collisions_never_wrap():
    impulse = np.zeros(32)
    impulse[2] = 1
    delayed = k3.fractional_time_shift(impulse, 3)
    assert np.argmax(delayed) == 5
    assert abs(delayed[-1]) < 1e-15
    assert np.max(abs(k3.fractional_time_shift(impulse, -40))) == 0
    assert k3.fractional_time_shift(np.zeros(0), 2).size == 0
    with pytest.raises(ValueError):
        k3.fractional_time_shift(impulse, float("inf"))


@pytest.mark.parametrize("material", sorted(k3.MATERIALS))
def test_six_passive_materials_are_modal_contact_excited_and_impulse_linear(material):
    fs = 16000
    modes = k3.MATERIALS[material]
    sound = k3.synthesize_modal_foley(
        material, sample_rate=fs, contact_seconds=0, duration_seconds=1
    )
    double = k3.synthesize_modal_foley(material, sample_rate=fs, impulse_ns=2, contact_seconds=0)
    np.testing.assert_allclose(double, 2 * sound, rtol=0, atol=0)
    assert sound.shape == (fs,) and np.isfinite(sound).all()
    assert sound[0] == pytest.approx(1, abs=1e-15)
    assert np.mean(sound[-fs // 10 :] ** 2) < np.mean(sound[: fs // 10] ** 2)
    spectrum = abs(np.fft.rfft(sound))
    # Dominant measured peak must be one of the material's physical modes.
    hz = float(np.argmax(spectrum))
    assert min(abs(hz - f) for f in modes.frequencies_hz) < 8
    assert all(d > 0 for d in modes.decay_seconds)


def test_contact_duration_softens_higher_modes_and_friction_is_seeded():
    impulse = k3.synthesize_modal_foley("metal", sample_rate=16000, contact_seconds=0)
    soft = k3.synthesize_modal_foley("metal", sample_rate=16000, contact_seconds=0.02)
    frequencies = np.fft.rfftfreq(impulse.size, 1 / 16000)
    high = frequencies > 2000
    assert (
        np.sum(abs(np.fft.rfft(soft))[high] ** 2)
        < np.sum(abs(np.fft.rfft(impulse))[high] ** 2) * 0.1
    )
    a = k3.synthesize_modal_foley("cloth_rustle", sample_rate=16000, contact_seconds=0.1, seed=1)
    b = k3.synthesize_modal_foley("cloth_rustle", sample_rate=16000, contact_seconds=0.1, seed=2)
    assert not np.array_equal(a, b)
    np.testing.assert_array_equal(
        a, k3.synthesize_modal_foley("cloth_rustle", sample_rate=16000, contact_seconds=0.1, seed=1)
    )


def test_collision_placement_is_sample_exact_additive_and_truncates_only_the_tail():
    first = k3.FoleyCollision(123, "wood", contact_seconds=0, duration_seconds=0.1)
    last = k3.FoleyCollision(1999, "metal", impulse_ns=0.7, contact_seconds=0)
    rendered = k3.render_collision_foley((first, last), length_samples=2000, sample_rate=16000)
    np.testing.assert_array_equal(rendered[:123], np.zeros(123))
    assert rendered[123] == pytest.approx(1)
    expected = k3.synthesize_modal_foley(
        "wood", sample_rate=16000, contact_seconds=0, duration_seconds=0.1
    )
    np.testing.assert_allclose(rendered[123:1723], expected, atol=0, rtol=0)
    assert rendered[1999] == pytest.approx(0.7)
    doubled = k3.render_collision_foley((first, first), length_samples=2000, sample_rate=16000)
    np.testing.assert_allclose(doubled[123:1723], 2 * expected, atol=0, rtol=0)
    assert k3.render_collision_foley((), length_samples=0).size == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sample_rate": 0},
        {"sample_rate": float("nan")},
        {"max_lag_seconds": 0},
        {"max_lag_seconds": 1},
        {"interpolation": 1},
        {"interpolation": 2.5},
    ],
)
def test_gcc_refuses_invalid_measurement_geometry(kwargs):
    arguments = dict(sample_rate=1000, max_lag_seconds=0.01)
    arguments.update(kwargs)
    with pytest.raises(ValueError):
        k3.gcc_phat(np.arange(100), np.arange(100), **arguments)


def test_silent_insufficient_nonfinite_and_wrong_rank_features_refuse():
    for a, b in [
        (np.zeros(1000), np.ones(1000)),
        (np.ones(2), np.ones(2)),
        (np.zeros((2, 1000)), np.zeros(1000)),
        (np.array([np.nan] * 1000), np.ones(1000)),
    ]:
        with pytest.raises(ValueError):
            k3.gcc_phat(a, b, sample_rate=1000)
    for kwargs in [
        dict(frame_rate=0),
        dict(envelope_rate=1),
        dict(length=-1),
        dict(frame_rate=float("nan")),
    ]:
        arguments = dict(frame_rate=25, envelope_rate=1000, length=1000)
        arguments.update(kwargs)
        with pytest.raises(ValueError):
            k3.interpolate_visual_features(np.ones(5), **arguments)
    with pytest.raises(ValueError):
        k3.interpolate_visual_features(np.ones(3), frame_rate=25, envelope_rate=1000, length=100)
    with pytest.raises(ValueError):
        k3.rms_envelope(np.zeros(0), sample_rate=1000)
    with pytest.raises(ValueError):
        k3.synchronize_audio_to_visual(np.zeros(10), np.zeros(10), sample_rate=0, frame_rate=25)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"material": "plastic"},
        {"sample_rate": 8000},
        {"impulse_ns": float("nan")},
        {"impulse_ns": 0},
        {"contact_seconds": -0.1},
        {"duration_seconds": 6},
    ],
)
def test_unphysical_foley_is_refused(kwargs):
    arguments = dict(material="wood")
    arguments.update(kwargs)
    with pytest.raises(ValueError):
        k3.synthesize_modal_foley(**arguments)


def test_invalid_collision_timeline_is_not_rounded_or_accepted():
    for index in [-1, 100, True, 3.5]:
        with pytest.raises(ValueError):
            k3.render_collision_foley((k3.FoleyCollision(index, "wood"),), length_samples=100)
    with pytest.raises(ValueError):
        k3.render_collision_foley((), length_samples=-1)
    with pytest.raises(ValueError):
        k3.render_collision_foley((k3.FoleyCollision(1, "wood"),) * 1001, length_samples=100)
