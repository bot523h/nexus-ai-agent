"""K1 falsification: coordinates, group laws, HRTF cues and independently measured decay."""

from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
import pytest

from nexus_ai_agent.creative.audio_engine import ambisonics_binaural_fdn as k1


def test_foa_acn_sn3d_coordinate_basis_is_w_y_z_x():
    azimuth = np.array([0, np.pi / 2, 0, np.pi])
    elevation = np.array([0, 0, np.pi / 2, 0])
    harmonics = k1.sn3d_harmonics(1, azimuth, elevation)
    np.testing.assert_allclose(
        harmonics, [[1, 1, 1, 1], [0, 1, 0, 0], [0, 0, 1, 0], [1, 0, 0, -1]], atol=1e-15
    )
    signals = np.eye(4)
    np.testing.assert_allclose(
        k1.encode_ambisonics(signals, azimuth=azimuth, elevation=elevation), harmonics
    )
    assert k1.encode_ambisonics(np.ones(4), azimuth=0).shape == (4, 4)


@pytest.mark.parametrize("order", [0, 1, 2, 3])
def test_hoa_rotation_is_orthogonal_directional_and_invertible(order):
    rng = np.random.default_rng(510 + order)
    az = rng.uniform(-np.pi, np.pi, 100)
    el = np.arcsin(rng.uniform(-1, 1, 100))
    harmonics = k1.sn3d_harmonics(order, az, el)
    for degree in range(order + 1):
        np.testing.assert_allclose(
            np.sum(harmonics[degree**2 : (degree + 1) ** 2] ** 2, axis=0), 1, atol=3e-15
        )
    angles = dict(yaw=0.72, pitch=-0.43, roll=0.29)
    matrix = k1.ambisonic_rotation_matrix(order, **angles)
    np.testing.assert_allclose(matrix.T @ matrix, np.eye((order + 1) ** 2), atol=3e-15)
    so3 = k1.euler_so3(**angles).T
    cartesian = np.vstack((np.cos(el) * np.cos(az), np.cos(el) * np.sin(az), np.sin(el)))
    turned = so3 @ cartesian
    expected = k1.sn3d_harmonics(order, np.arctan2(turned[1], turned[0]), np.arcsin(turned[2]))
    np.testing.assert_allclose(matrix @ harmonics, expected, atol=4e-15)
    arbitrary = rng.normal(size=((order + 1) ** 2, 4096))
    rotated = k1.rotate_ambisonics(arbitrary, **angles)
    assert abs(np.linalg.norm(rotated) - np.linalg.norm(arbitrary)) < 1e-12
    np.testing.assert_allclose(matrix.T @ rotated, arbitrary, atol=8e-15)
    inverse = k1.ambisonic_rotation_matrix(order, camera_coordinates=False, **angles)
    np.testing.assert_allclose(inverse, matrix.T, atol=3e-15)
    composed = k1.ambisonic_rotation_matrix(order, yaw=0.3) @ k1.ambisonic_rotation_matrix(
        order, yaw=0.4
    )
    np.testing.assert_allclose(composed, k1.ambisonic_rotation_matrix(order, yaw=0.7), atol=4e-15)


def test_camera_yaw_is_inverse_to_the_scene_and_roll_pitch_are_not_swapped():
    front = k1.encode_ambisonics(np.ones(1), azimuth=0)
    turned = k1.rotate_ambisonics(front, yaw=np.pi / 2)
    np.testing.assert_allclose(turned[:, 0], [1, -1, 0, 0], atol=1e-15)
    up = k1.rotate_ambisonics(front, pitch=np.pi / 2)
    np.testing.assert_allclose(up[:, 0], [1, 0, 1, 0], atol=1e-15)


@pytest.mark.parametrize("order", [-1, 4, True, 1.5])
def test_unsupported_orders_fail_closed(order):
    with pytest.raises(ValueError):
        k1.sn3d_harmonics(order, np.array([0]), np.array([0]))


@pytest.mark.parametrize("data", [np.zeros(4), np.zeros((3, 4)), np.full((4, 4), np.nan)])
def test_bad_b_format_cannot_be_rotated_or_decoded(data):
    for function in (k1.rotate_ambisonics, k1.binaural_decode):
        with pytest.raises(ValueError):
            function(data)


def test_directions_angles_sample_rates_and_empty_audio():
    with pytest.raises(ValueError):
        k1.sn3d_harmonics(1, np.array([np.nan]), np.array([0]))
    with pytest.raises(ValueError):
        k1.sn3d_harmonics(1, np.array([0]), np.array([2]))
    with pytest.raises(ValueError):
        k1.euler_so3(np.nan, 0, 0)
    with pytest.raises(ValueError):
        k1.binaural_decode(np.zeros((4, 20)), sample_rate=8000)
    assert k1.binaural_decode(np.zeros((4, 0))).shape == (2, 0)
    assert k1.encode_ambisonics(np.zeros(0), azimuth=0).shape == (4, 0)


def test_woodworth_delay_and_lagrange_fractional_delay_have_physical_sign_and_no_wrap():
    expected = 0.0875 / 343 * (1 + np.pi / 2)
    assert k1.woodworth_itd(np.pi / 2) == pytest.approx(expected, abs=1e-15)
    assert k1.woodworth_itd(-np.pi / 2) == pytest.approx(-expected, abs=1e-15)
    assert k1.woodworth_itd(0) == 0
    assert abs(k1.woodworth_itd(np.pi / 2, np.pi / 2)) < 1e-15
    impulse = np.r_[1.0, np.zeros(100)]
    delayed = k1.lagrange_fractional_delay(impulse, 12.25, order=5)
    assert np.all(delayed[:12] == 0) and np.all(delayed[18:] == 0)
    assert np.sum(delayed) == pytest.approx(1, abs=1e-15)
    assert np.arange(delayed.size) @ delayed == pytest.approx(12.25, abs=1e-14)
    np.testing.assert_array_equal(k1.lagrange_fractional_delay(impulse, 0), impulse)
    assert not np.any(k1.lagrange_fractional_delay(impulse, 200))
    for kwargs in ({"head_radius": 0}, {"sound_speed": 0}, {"elevation": 2}, {"azimuth": np.inf}):
        with pytest.raises(ValueError):
            k1.woodworth_itd(**({"azimuth": 0} | kwargs))
    for delay, order in [(-1, 3), (np.inf, 3), (1, 0), (1, 9)]:
        with pytest.raises(ValueError):
            k1.lagrange_fractional_delay(impulse, delay, order=order)


def test_binaural_cues_mirror_exactly_and_pinna_notch_moves_with_elevation():
    impulse = np.r_[1.0, np.zeros(2047)]
    front = k1.binaural_decode(k1.encode_ambisonics(impulse, azimuth=0, order=3))
    np.testing.assert_allclose(front[0], front[1], atol=2e-15)
    left = k1.binaural_decode(k1.encode_ambisonics(impulse, azimuth=np.pi / 2, order=3))
    right = k1.binaural_decode(k1.encode_ambisonics(impulse, azimuth=-np.pi / 2, order=3))
    np.testing.assert_allclose(left, right[::-1], atol=2e-15)
    assert np.sum(left[0] ** 2) > 2 * np.sum(left[1] ** 2)
    assert np.argmax(abs(left[1])) > np.argmax(abs(left[0]))
    direct = k1._hrtf_impulse(np.pi / 2, 0, 48000)
    lag = np.argmax(abs(direct[1])) - np.argmax(abs(direct[0]))
    assert abs(lag - k1.woodworth_itd(np.pi / 2) * 48000) < 1
    low = abs(np.fft.rfft(k1._hrtf_impulse(0, -0.5, 48000)[0], n=8192))
    high = abs(np.fft.rfft(k1._hrtf_impulse(0, 0.5, 48000)[0], n=8192))
    assert np.argmin(high) > np.argmin(low)
    assert np.isfinite(left).all() and left.shape == (2, impulse.size)


def test_householder_is_unitary_and_eyring_geometry_matches_declared_absorption():
    matrix = k1.householder_matrix()
    np.testing.assert_array_equal(matrix.T @ matrix, np.eye(8))
    assert np.linalg.norm(matrix, 2) == pytest.approx(1, abs=1e-15)
    assert np.linalg.det(matrix) == pytest.approx(-1, abs=1e-15)
    room = k1.FdnRoom()
    for absorption, target in zip(room.eyring_absorption, (0.8, 0.6, 0.4), strict=True):
        measured = -0.161 * room.volume_m3 / (room.surface_area_m2 * math.log1p(-absorption))
        assert measured == pytest.approx(target, abs=1e-15)
    for lines in (1, 33, True):
        with pytest.raises(ValueError):
            k1.householder_matrix(lines)


@pytest.mark.parametrize("target", [0.3, 0.6, 1.1])
def test_schroeder_rt60_matches_target_within_eight_percent(target):
    room = k1.FdnRoom(rt60_low=target, rt60_mid=target, rt60_high=target)
    result = k1.fdn_reverb(np.array([1.0]), sample_rate=16000, room=room)
    measured = k1.schroeder_rt60(result.samples, sample_rate=16000)
    assert abs(measured / target - 1) <= 0.08
    assert result.maximum_feedback_gain < 1
    assert len(set(result.delays)) == 8
    assert all(math.gcd(a, b) == 1 for a in result.delays for b in result.delays if a != b)
    assert np.linalg.norm(result.samples[:, -1600:]) < np.linalg.norm(result.samples[:, :1600])


def test_frequency_dependent_decay_is_measured_with_causal_not_circular_filters():
    fs = 48000
    result = k1.fdn_reverb(np.array([1.0]), sample_rate=fs, tail_seconds=1.8)
    for hz, target in [(100, 0.8), (1000, 0.6), (10000, 0.4)]:
        length = 2 * round(6 * fs / hz) + 1
        n = np.arange(length) - (length - 1) / 2
        low, high = hz / np.sqrt(2), min(hz * np.sqrt(2), fs * 0.49)
        fir = (
            2 * high / fs * np.sinc(2 * high / fs * n) - 2 * low / fs * np.sinc(2 * low / fs * n)
        ) * np.hanning(length)
        size = 1 << (result.samples.shape[1] + length).bit_length()
        filtered = np.fft.irfft(
            np.fft.rfft(result.samples, n=size, axis=1) * np.fft.rfft(fir, n=size), n=size, axis=1
        )[:, : result.samples.shape[1] + length - 1]
        assert abs(k1.schroeder_rt60(filtered, sample_rate=fs) / target - 1) <= 0.08


@pytest.mark.parametrize(
    "changes",
    [
        {"volume_m3": 0},
        {"surface_area_m2": np.inf},
        {"rt60_high": 0.9},
        {"rt60_low": 11},
        {"volume_m3": 1e6},
    ],
)
def test_unphysical_rooms_refuse(changes):
    with pytest.raises(ValueError):
        replace(k1.FdnRoom(), **changes)


def test_fdn_and_rt60_invalid_evidence_is_not_a_plausible_measurement(monkeypatch):
    with pytest.raises(ValueError):
        k1.fdn_reverb(np.array([np.nan]))
    for kwargs in ({"sample_rate": 0}, {"tail_seconds": -1}, {"tail_seconds": np.inf}):
        with pytest.raises(ValueError):
            k1.fdn_reverb(np.ones(1), **kwargs)
    for samples, rate in (
        (np.zeros(0), 48000),
        (np.zeros(10), 0),
        (np.zeros(100), 48000),
        (np.array([1.0]), 48000),
    ):
        with pytest.raises(ValueError):
            k1.schroeder_rt60(samples, sample_rate=rate)
    monkeypatch.setattr(np, "polyfit", lambda *a, **k: [0, 0])
    with pytest.raises(ValueError, match="not stable"):
        k1.schroeder_rt60(np.exp(-np.arange(1000) / 100), sample_rate=8000)
    assert k1.fdn_reverb(np.zeros(0), tail_seconds=0).samples.shape == (2, 0)
    tiny = k1.fdn_reverb(
        np.zeros(0), sample_rate=8000, room=k1.FdnRoom(volume_m3=0.01), tail_seconds=0
    )
    assert len(set(tiny.delays)) == 8  # duplicate prime candidates are advanced
