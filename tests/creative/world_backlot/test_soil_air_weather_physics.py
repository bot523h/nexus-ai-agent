from __future__ import annotations

import hashlib
import io
import math
import wave
from dataclasses import replace

import numpy as np
import pytest

from nexus_ai_agent.creative.world_backlot import (
    AmbientAudio,
    CurlWind,
    ParticleBounds,
    ParticlePhysics,
    ParticleState,
    SurfaceKind,
    ThunderEvent,
    WeatherKind,
    fresnel_reflectance,
    generate_curl_wind,
    initialize_particles,
    iter_particle_frames,
    particle_physics,
    soil_optics,
    step_particles,
    synthesize_ambient,
)
from nexus_ai_agent.creative.world_backlot._determinism import FloatArray, normal, uniform


def test_analytic_curl_agrees_with_independent_finite_difference_and_has_zero_divergence() -> None:
    wind = generate_curl_wind("vortex", speed_m_s=3, mean_velocity=(1, 2, 0))
    points = 10 * normal(43, "points", 24 * 3).reshape(24, 3)
    h = 1e-4
    derivatives = []
    potential_derivatives = []
    for axis in range(3):
        delta = np.eye(3)[axis] * h
        derivatives.append(
            (wind.sample(points + delta, 0.7) - wind.sample(points - delta, 0.7)) / (2 * h)
        )
        potential_derivatives.append(
            (wind.potential(points + delta, 0.7) - wind.potential(points - delta, 0.7)) / (2 * h)
        )
    divergence = sum(d[:, axis] for axis, d in enumerate(derivatives))
    np.testing.assert_allclose(divergence, 0, atol=1e-8)
    curl = np.stack(
        [
            potential_derivatives[1][:, 2] - potential_derivatives[2][:, 1],
            potential_derivatives[2][:, 0] - potential_derivatives[0][:, 2],
            potential_derivatives[0][:, 1] - potential_derivatives[1][:, 0],
        ],
        axis=1,
    )
    np.testing.assert_allclose(wind.sample(points, 0.7) - wind.mean_velocity, curl, atol=1e-8)
    assert not np.allclose(wind.sample(points, 0), wind.sample(points, 1))
    assert wind.sha256 == generate_curl_wind("vortex", 3, mean_velocity=(1, 2, 0)).sha256
    assert wind.sha256 != generate_curl_wind("other", 3, mean_velocity=(1, 2, 0)).sha256


def test_wind_is_spatially_periodic_and_its_rms_matches_the_requested_speed() -> None:
    wind = generate_curl_wind(73, speed_m_s=2, length_scale_m=20, mean_velocity=(0, 0, 0))
    points = uniform(1, "samples", 5000 * 3).reshape(5000, 3) * 20
    np.testing.assert_allclose(wind.sample(points), wind.sample(points + (20, 0, 0)), atol=1e-13)
    rms = math.sqrt(float(np.mean(np.sum(wind.sample(points) ** 2, axis=1))))
    assert rms == pytest.approx(2, rel=0.04)
    zero = generate_curl_wind(0, speed_m_s=0, modes=1, mean_velocity=(3, 0, 0))
    np.testing.assert_allclose(zero.sample(points[:2]), [[3, 0, 0], [3, 0, 0]])
    assert wind.sample(np.zeros((0, 3))).shape == (0, 3)
    with pytest.raises(ValueError):
        wind.potentials.setflags(write=True)


@pytest.mark.parametrize("kind", list(WeatherKind))
def test_every_particle_archive_integrates_deterministically_with_thousands_of_particles(
    kind: WeatherKind,
) -> None:
    initial = initialize_particles("weather", kind, 2048)
    wind = generate_curl_wind("weather")
    frames = tuple(iter_particle_frames(initial, 3, wind=wind))
    again = tuple(iter_particle_frames(initial, 3, wind=wind))
    assert [f.sha256 for f in frames] == [f.sha256 for f in again]
    assert frames[-1].step_index == 3 and frames[-1].time_seconds == pytest.approx(3 / 48)
    assert initial.step_index == 0 and initial.time_seconds == 0
    assert frames[0].positions_m.shape == (2048, 3)
    assert np.isfinite(frames[-1].positions_m).all()
    assert not frames[0].positions_m.flags.writeable
    assert not np.array_equal(frames[0].positions_m, initial.positions_m)
    with pytest.raises(ValueError):
        initial.positions_m.setflags(write=True)


def test_rain_falls_faster_than_dust_and_hot_sparks_rise() -> None:
    rain = initialize_particles(0, WeatherKind.RAIN, 4096)
    dust = initialize_particles(0, WeatherKind.DUST, 4096)
    sparks = initialize_particles(0, WeatherKind.SPARKS, 4096)
    for state, direction in ((rain, -1), (dust, -1), (sparks, 1)):
        next_state = step_particles(state, 0.1)
        assert np.mean(next_state.positions_m[:, 2] - state.positions_m[:, 2]) * direction > 0
    assert abs(float(np.mean(rain.velocities_m_s[:, 2]))) > 10 * abs(
        float(np.mean(dust.velocities_m_s[:, 2]))
    )
    assert particle_physics("fog").drag_kg_s / particle_physics("fog").mass_kg > 900


def test_drag_free_ballistic_limit_includes_half_g_dt_squared_in_position() -> None:
    state = ParticleState(
        np.array([[0.0, 1.0, 2.0]]), np.array([[1.0, 2.0, 3.0]]), WeatherKind.RAIN, "a" * 64
    )
    physics = ParticlePhysics(2, 0, 0, (0, 0, -9.8))
    result = step_particles(state, 0.2, physics=physics)
    np.testing.assert_allclose(result.velocities_m_s, state.velocities_m_s + [0, 0, -1.96])
    np.testing.assert_allclose(
        result.positions_m,
        state.positions_m + 0.2 * state.velocities_m_s + [0, 0, -0.196],
        atol=1e-14,
    )


def test_constant_wind_exact_ou_drift_and_semigroup_for_nonstochastic_particles() -> None:
    state = ParticleState(
        np.zeros((2, 3)), np.array([[3.0, 2.0, 1.0], [1.0, 0.0, -1.0]]), WeatherKind.DUST, "b" * 64
    )
    physics = ParticlePhysics(1, 2, 0, (0, 0, -2))
    wind = generate_curl_wind(0, speed_m_s=0, mean_velocity=(1, 0, 0))
    dt, decay = 0.2, math.exp(-0.4)
    phi = (1 - decay) / 2
    g = np.array([0, 0, -2])
    expected_v = decay * state.velocities_m_s + (1 - decay) * np.array([1, 0, 0]) + phi * g
    expected_x = (
        phi * state.velocities_m_s + (dt - phi) * np.array([1, 0, 0]) + ((dt - phi) / 2) * g
    )
    result = step_particles(state, dt, wind, physics)
    np.testing.assert_allclose(result.velocities_m_s, expected_v, atol=1e-14)
    np.testing.assert_allclose(result.positions_m, expected_x, atol=1e-14)
    half = step_particles(state, 0.1, wind, physics)
    twice = step_particles(half, 0.1, wind, physics)
    np.testing.assert_allclose(twice.positions_m, result.positions_m, atol=1e-14)
    np.testing.assert_allclose(twice.velocities_m_s, result.velocities_m_s, atol=1e-14)


@pytest.mark.parametrize("rate", [0.0, 1e-7, 2.0, 1000.0])
def test_joint_langevin_noise_matches_velocity_position_variance_and_covariance(
    rate: float,
) -> None:
    # 32768 independent trajectories, not a tautological copy of the solver.
    state = ParticleState(np.zeros((32_768, 3)), np.zeros((32_768, 3)), WeatherKind.FOG, "c" * 64)
    dt, sigma = 0.2, 0.7
    result = step_particles(state, dt, physics=ParticlePhysics(1, rate, sigma, (0, 0, 0)))
    # Integrate the continuous SDE's Green functions independently by quadrature.
    t = np.linspace(0, dt, 32_769)
    velocity_kernel = np.exp(-rate * t)
    position_kernel = t if rate == 0 else -np.expm1(-rate * t) / rate

    def integrate(values: FloatArray) -> float:
        return float(np.sum(values[:-1] + values[1:])) * (dt / (len(t) - 1)) / 2

    expected_v = sigma * sigma * integrate(velocity_kernel**2)
    expected_x = sigma * sigma * integrate(position_kernel**2)
    expected_cov = sigma * sigma * integrate(velocity_kernel * position_kernel)
    v, x = result.velocities_m_s.ravel(), result.positions_m.ravel()
    assert abs(float(np.mean(v))) < 4 * math.sqrt(expected_v / len(v))
    assert abs(float(np.mean(x))) < 4 * math.sqrt(expected_x / len(x))
    assert float(np.var(v)) == pytest.approx(expected_v, rel=0.025)
    assert float(np.var(x)) == pytest.approx(expected_x, rel=0.025)
    assert float(np.mean(x * v)) == pytest.approx(expected_cov, rel=0.06)


def test_large_drag_is_stable_and_tiny_drag_converges_to_ballistic() -> None:
    state = initialize_particles(8, "fog", 4096)
    result = step_particles(state, 1)
    physics = particle_physics("fog")
    stationary_std = physics.noise_n_sqrt_s / math.sqrt(2 * physics.drag_kg_s * physics.mass_kg)
    assert np.isfinite(result.positions_m).all()
    np.testing.assert_allclose(np.std(result.velocities_m_s, axis=0), stationary_std, rtol=0.05)
    assert np.max(np.abs(result.velocities_m_s)) < 6 * stationary_std
    base = ParticleState(np.zeros((1, 3)), np.ones((1, 3)), WeatherKind.DUST, "d" * 64)
    ballistic = step_particles(base, 0.1, physics=ParticlePhysics(1, 0, 0))
    tiny_drag = step_particles(base, 0.1, physics=ParticlePhysics(1, 1e-9, 0))
    np.testing.assert_allclose(tiny_drag.positions_m, ballistic.positions_m, atol=1e-11)
    np.testing.assert_allclose(tiny_drag.velocities_m_s, ballistic.velocities_m_s, atol=1e-10)


def test_periodic_bounds_and_streaming_resume_keep_exact_particle_lineage() -> None:
    bounds = ParticleBounds((0, 0, 0), (1, 2, 3))
    state = initialize_particles(7, "rain", 1024, bounds)
    wrapped = step_particles(state, 1, bounds=bounds)
    assert np.all(wrapped.positions_m >= bounds.minimum_m)
    assert np.all(wrapped.positions_m < bounds.maximum_m)
    assert tuple(iter_particle_frames(state, 0)) == ()
    frames = tuple(iter_particle_frames(state, 4, bounds=bounds))
    resumed = tuple(iter_particle_frames(frames[1], 2, bounds=bounds))
    assert [f.sha256 for f in frames[2:]] == [f.sha256 for f in resumed]
    empty = initialize_particles(0, "clear", 0)
    assert step_particles(empty).positions_m.shape == (0, 3)


def test_fresnel_normal_incidence_grazing_and_total_internal_reflection() -> None:
    assert fresnel_reflectance(1) == pytest.approx(((1 - 1.333) / (1 + 1.333)) ** 2)
    assert fresnel_reflectance(0) == 1
    assert fresnel_reflectance(0.1) > fresnel_reflectance(0.8)
    assert fresnel_reflectance(0.2, 1.5, 1) == 1
    assert fresnel_reflectance(0, 1.5, 1.5) == 0
    assert fresnel_reflectance(1, 1.5, 1) < 1


@pytest.mark.parametrize("kind", list(SurfaceKind))
def test_soil_brdfs_conserve_energy_and_water_darkens_diffuse_reflectance(
    kind: SurfaceKind,
) -> None:
    dry = soil_optics(kind, 0.6)
    wet = soil_optics(kind, 0.6, wetness=1, water_depth_mm=5)
    assert np.all(np.asarray(dry.diffuse_rgb) + dry.specular_reflectance <= 1)
    assert np.all(np.asarray(wet.diffuse_rgb) + wet.specular_reflectance <= 1)
    assert np.sum(wet.diffuse_rgb) < np.sum(dry.diffuse_rgb)
    assert wet.roughness <= dry.roughness
    assert wet.kind is kind
    assert sum(soil_optics("snow").diffuse_rgb) > sum(soil_optics("wet_soil").diffuse_rgb)


@pytest.mark.parametrize("kind", list(WeatherKind))
def test_all_ambient_archives_produce_real_reproducible_48khz_pcm_wav(kind: WeatherKind) -> None:
    audio = synthesize_ambient("acoustics", kind, 0.04)
    assert audio.sha256 == synthesize_ambient("acoustics", kind, 0.04).sha256
    assert audio.sha256 != synthesize_ambient("different", kind, 0.04).sha256
    wav = audio.to_wav()
    assert audio.sha256 == hashlib.sha256(wav).hexdigest()
    with wave.open(io.BytesIO(wav), "rb") as decoded:
        assert decoded.getframerate() == 48000
        assert decoded.getnchannels() == 1 and decoded.getsampwidth() == 2
        assert decoded.getnframes() == 1920
        np.testing.assert_array_equal(
            np.frombuffer(decoded.readframes(1920), dtype="<i2"), audio.pcm
        )
    assert audio.duration_seconds == 0.04
    assert np.max(np.abs(audio.pcm.astype(np.int32))) <= 29491
    assert audio.pcm[0] == audio.pcm[-1] == 0
    assert np.any(audio.pcm != 0)
    with pytest.raises(ValueError):
        audio.pcm.setflags(write=True)


def test_wind_rain_spectra_and_intensity_are_not_just_unfiltered_white_noise() -> None:
    wind = synthesize_ambient(1, "wind", 0.5, wind_strength=0.5).pcm.astype(np.float64)
    rain = synthesize_ambient(1, "rain", 0.5, wind_strength=0, rain_intensity=1).pcm.astype(
        np.float64
    )
    frequency = np.fft.rfftfreq(len(wind), 1 / 48000)
    low, high = frequency < 300, (frequency > 1500) & (frequency < 8000)
    w_power, r_power = np.abs(np.fft.rfft(wind)) ** 2, np.abs(np.fft.rfft(rain)) ** 2
    assert np.sum(w_power[low]) > 10 * np.sum(w_power[high])
    assert np.sum(r_power[high]) > 10 * np.sum(r_power[low])
    quiet = synthesize_ambient(1, "wind", 0.5, wind_strength=0.1).pcm.astype(np.float64)
    assert np.std(wind) > 3 * np.std(quiet)
    silence = synthesize_ambient(1, "clear", 0.01, wind_strength=0)
    assert not np.any(silence.pcm)


def test_thunder_respects_speed_of_sound_delay_and_distance_attenuation_without_pre_echo() -> None:
    event = ThunderEvent(0.01, 34.3, 1)
    assert event.arrival_seconds == pytest.approx(0.11)
    audio = synthesize_ambient(9, "clear", 0.5, wind_strength=0, thunder=event)
    assert np.all(audio.pcm[: round(0.11 * 48000)] == 0)
    assert np.any(audio.pcm[round(0.12 * 48000) :] != 0)
    late = synthesize_ambient(9, "clear", 0.1, wind_strength=0, thunder=event)
    assert not np.any(late.pcm)
    # Equal arrival time, different propagation distance -> different amplitude.
    close = synthesize_ambient(9, "clear", 0.5, wind_strength=0, thunder=ThunderEvent(0.1, 0, 1))
    far = synthesize_ambient(9, "clear", 0.5, wind_strength=0, thunder=ThunderEvent(0, 34.3, 1))
    assert np.std(close.pcm) > np.std(far.pcm)
    for kind in ("rain", "storm", "clear"):
        assert synthesize_ambient(0, kind, 1 / 48000).pcm.shape == (1,)


def test_invalid_weather_parameters_and_buffers_are_bounded() -> None:
    with pytest.raises(ValueError):
        generate_curl_wind(0, modes=0)
    with pytest.raises(ValueError):
        generate_curl_wind(0, length_scale_m=0)
    with pytest.raises(ValueError):
        CurlWind(np.ones((1, 2)), np.ones((1, 3)), np.ones(1), np.ones(1))
    with pytest.raises(ValueError):
        CurlWind(np.ones((1, 3)), np.ones((2, 3)), np.ones(1), np.ones(1))
    with pytest.raises(ValueError):
        ParticlePhysics(0, 1, 1)
    with pytest.raises(ValueError):
        ParticlePhysics(1, -1, 1)
    with pytest.raises(ValueError):
        ParticleBounds((1, 0, 0), (0, 1, 1))
    with pytest.raises(ValueError):
        initialize_particles(0, "rain", 32_769)
    state = initialize_particles(0, "rain", 3)
    for dt in (0, -1, 1.1, math.nan):
        with pytest.raises(ValueError):
            step_particles(state, dt)
    for kwargs in (
        {"velocities_m_s": np.zeros((2, 3))},
        {"seed_sha256": "invalid"},
        {"seed_sha256": "z" * 64},
        {"step_index": -1},
    ):
        with pytest.raises(ValueError):
            replace(state, **kwargs)
    with pytest.raises(ValueError):
        tuple(iter_particle_frames(state, -1))
    with pytest.raises(ValueError):
        soil_optics("sand", wetness=2)
    with pytest.raises(ValueError):
        fresnel_reflectance(1.1)
    with pytest.raises(ValueError):
        AmbientAudio(np.zeros(1, dtype=np.int16), sample_rate=44100)
    with pytest.raises(ValueError):
        AmbientAudio(np.zeros((2, 2), dtype=np.int16))
    with pytest.raises(ValueError):
        AmbientAudio(np.zeros(0, dtype=np.int16))
    with pytest.raises(ValueError):
        ThunderEvent(distance_m=-1)
    with pytest.raises(ValueError):
        synthesize_ambient(0, duration_seconds=5.1)
    with pytest.raises(ValueError):
        synthesize_ambient(0, wind_strength=1.1)
