"""Divergence-free Fourier curl wind, integrated Langevin particles and PCM.

SI units throughout. The OU velocity AND its correlated position increment are
integrated analytically with the wind held at the beginning of each time step.
This is exact for constant wind, weakly first order in spatially varying wind,
and stable even for fog's large drag/mass ratio. A named SHA-256 noise stream
per step makes continuation deterministic, without a mutable global RNG.
"""

from __future__ import annotations

import hashlib
import math
import struct
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum

import numpy as np

from ._determinism import (
    MAX_AUDIO_SECONDS,
    MAX_POINTS,
    RGB,
    SAMPLE_RATE,
    FloatArray,
    PCMArray,
    Seed,
    Vec3,
    array_digest,
    digest,
    finite,
    frozen_floats,
    frozen_pcm,
    integer,
    normal,
    points_array,
    seed_bytes,
    uniform,
    vector,
)


class WeatherKind(str, Enum):
    CLEAR = "clear"
    WIND = "wind"
    RAIN = "rain"
    STORM = "storm"
    SNOW = "snow"
    DUST = "dust"
    ASH = "ash"
    LEAVES = "leaves"
    FOG = "fog"
    SPARKS = "sparks"


@dataclass(frozen=True, slots=True)
class CurlWind:
    wave_vectors: FloatArray
    potentials: FloatArray
    phases: FloatArray
    angular_speeds: FloatArray
    mean_velocity: Vec3 = (1.5, 0.0, 0.0)
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        k = frozen_floats(self.wave_vectors)
        count = len(k)
        integer(count, "wind modes", 1, 64)
        arrays = (
            k,
            frozen_floats(self.potentials),
            frozen_floats(self.phases),
            frozen_floats(self.angular_speeds),
        )
        if any(a.shape != ((count, 3) if i < 2 else (count,)) for i, a in enumerate(arrays)):
            raise ValueError("inconsistent curl wind coefficients")
        for name, a in zip(
            ("wave_vectors", "potentials", "phases", "angular_speeds"), arrays, strict=True
        ):
            object.__setattr__(self, name, a)
        object.__setattr__(self, "mean_velocity", vector(self.mean_velocity, "mean wind"))
        object.__setattr__(
            self,
            "sha256",
            digest(
                (
                    [array_digest(a) for a in arrays],
                    self.mean_velocity,
                )
            ),
        )

    def sample(self, points: FloatArray, time_seconds: float = 0.0) -> FloatArray:
        points = points_array(points)
        finite(time_seconds, "wind time", -1e9, 1e9)
        phase = points @ self.wave_vectors.T + self.phases + time_seconds * self.angular_speeds
        curl_coefficients = np.cross(self.wave_vectors, self.potentials)
        return np.asarray(
            np.cos(phase) @ curl_coefficients + np.asarray(self.mean_velocity), dtype=np.float64
        )

    def potential(self, points: FloatArray, time_seconds: float = 0.0) -> FloatArray:
        points = points_array(points)
        finite(time_seconds, "wind time", -1e9, 1e9)
        phase = points @ self.wave_vectors.T + self.phases + time_seconds * self.angular_speeds
        return np.sin(phase) @ self.potentials


def generate_curl_wind(
    seed: Seed,
    speed_m_s: float = 2.0,
    length_scale_m: float = 20.0,
    modes: int = 12,
    mean_velocity: Vec3 = (1.5, 0.0, 0.0),
) -> CurlWind:
    finite(speed_m_s, "wind speed", 0, 100)
    finite(length_scale_m, "wind length scale", 0.1, 10000)
    integer(modes, "wind modes", 1, 64)
    k = np.floor(uniform(seed, "wind/wave-vectors", modes * 3).reshape(modes, 3) * 7) - 3
    k[:, 0] = np.where(np.all(k == 0, axis=1), 1, k[:, 0])
    k *= 2 * math.pi / length_scale_m
    potentials = normal(seed, "wind/vector-potential", modes * 3).reshape(modes, 3)
    energy = float(np.sum(np.cross(k, potentials) ** 2)) / 2
    potentials *= speed_m_s / math.sqrt(max(energy, 1e-30))
    return CurlWind(
        k,
        potentials,
        uniform(seed, "wind/phases", modes) * 2 * math.pi,
        0.1 + uniform(seed, "wind/frequencies", modes),
        mean_velocity,
    )


@dataclass(frozen=True, slots=True)
class ParticlePhysics:
    mass_kg: float
    drag_kg_s: float
    noise_n_sqrt_s: float
    gravity_m_s2: Vec3 = (0.0, 0.0, -9.80665)

    def __post_init__(self) -> None:
        finite(self.mass_kg, "particle mass", 1e-15, 1e3)
        finite(self.drag_kg_s, "particle drag", 0, 1e3)
        finite(self.noise_n_sqrt_s, "particle noise", 0, 1e3)
        object.__setattr__(self, "gravity_m_s2", vector(self.gravity_m_s2, "gravity", 100))


_PRESETS: dict[WeatherKind, ParticlePhysics] = {
    WeatherKind.CLEAR: ParticlePhysics(1e-9, 1e-8, 0, (0, 0, 0)),
    WeatherKind.WIND: ParticlePhysics(1e-9, 1e-8, 2e-9, (0, 0, 0)),
    WeatherKind.RAIN: ParticlePhysics(4.2e-6, 5.2e-6, 3e-7),
    WeatherKind.STORM: ParticlePhysics(4.2e-6, 5.2e-6, 2e-6),
    WeatherKind.SNOW: ParticlePhysics(1e-6, 8e-6, 2e-6),
    WeatherKind.DUST: ParticlePhysics(1e-9, 4e-8, 1e-8),
    WeatherKind.ASH: ParticlePhysics(2e-9, 3e-8, 1e-8, (0, 0, -3)),
    WeatherKind.LEAVES: ParticlePhysics(2e-4, 1e-3, 3e-4),
    WeatherKind.FOG: ParticlePhysics(1e-12, 1e-9, 5e-11),
    WeatherKind.SPARKS: ParticlePhysics(1e-8, 2e-8, 8e-9, (0, 0, 3)),
}


def particle_physics(kind: WeatherKind | str) -> ParticlePhysics:
    return _PRESETS[WeatherKind(kind)]


@dataclass(frozen=True, slots=True)
class ParticleState:
    positions_m: FloatArray
    velocities_m_s: FloatArray
    kind: WeatherKind
    seed_sha256: str
    step_index: int = 0
    time_seconds: float = 0.0
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        positions = points_array(self.positions_m)
        velocities = points_array(self.velocities_m_s)
        if positions.shape != velocities.shape:
            raise ValueError("particle positions and velocities must have the same shape")
        integer(self.step_index, "step index", 0, 1_000_000_000)
        finite(self.time_seconds, "particle time", 0, 1e9)
        if len(self.seed_sha256) != 64 or any(
            c not in "0123456789abcdef" for c in self.seed_sha256
        ):
            raise ValueError("particle seed must be a lowercase SHA-256 hex digest")
        object.__setattr__(self, "kind", WeatherKind(self.kind))
        object.__setattr__(self, "positions_m", frozen_floats(positions))
        object.__setattr__(self, "velocities_m_s", frozen_floats(velocities))
        object.__setattr__(
            self,
            "sha256",
            digest(
                (
                    array_digest(self.positions_m),
                    array_digest(self.velocities_m_s),
                    self.kind.value,
                    self.seed_sha256,
                    self.step_index,
                    self.time_seconds,
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class ParticleBounds:
    minimum_m: Vec3 = (-20.0, -20.0, 0.0)
    maximum_m: Vec3 = (20.0, 20.0, 30.0)

    def __post_init__(self) -> None:
        low, high = vector(self.minimum_m, "minimum"), vector(self.maximum_m, "maximum")
        if any(a >= b for a, b in zip(low, high, strict=True)):
            raise ValueError("bounds must have positive extent")
        object.__setattr__(self, "minimum_m", low)
        object.__setattr__(self, "maximum_m", high)


_DEFAULT_PARTICLE_BOUNDS = ParticleBounds()


def initialize_particles(
    seed: Seed,
    kind: WeatherKind | str,
    count: int = 1024,
    bounds: ParticleBounds = _DEFAULT_PARTICLE_BOUNDS,
) -> ParticleState:
    integer(count, "particle count", 0, MAX_POINTS)
    kind = WeatherKind(kind)
    low, high = np.asarray(bounds.minimum_m), np.asarray(bounds.maximum_m)
    positions = low + uniform(seed, f"particles/{kind.value}/position", count * 3).reshape(
        count, 3
    ) * (high - low)
    physics = particle_physics(kind)
    terminal = np.asarray(physics.gravity_m_s2) * physics.mass_kg / physics.drag_kg_s
    velocities = np.broadcast_to(terminal, (count, 3)).copy()
    return ParticleState(
        positions, velocities, kind, seed_bytes(seed, f"particles/{kind.value}").hex()
    )


def _ou_moments(rate: float, dt: float) -> tuple[float, float, float, float, float, float]:
    """Decay, phi, gravity integral, Var(v), Cov(x,v), Var(x) for unit noise."""
    z = rate * dt
    decay = math.exp(-z)
    if z < 1e-3:
        phi = dt * (1 - z / 2 + z * z / 6 - z**3 / 24 + z**4 / 120)
        gravity = dt * dt * (0.5 - z / 6 + z * z / 24 - z**3 / 120 + z**4 / 720)
        var_v = dt * (1 - z + 2 * z * z / 3 - z**3 / 3 + 2 * z**4 / 15)
        cov = dt * dt * (0.5 - z / 2 + 7 * z * z / 24 - z**3 / 8 + 31 * z**4 / 720)
        var_x = dt**3 * (1 / 3 - z / 4 + 7 * z * z / 60 - z**3 / 24 + 31 * z**4 / 2520)
    else:
        one_minus = -math.expm1(-z)
        phi = one_minus / rate
        gravity = (dt - phi) / rate
        var_v = -math.expm1(-2 * z) / (2 * rate)
        cov = one_minus**2 / (2 * rate * rate)
        var_x = (dt - 2 * phi + var_v) / (rate * rate)
    return decay, phi, gravity, var_v, cov, var_x


def step_particles(
    state: ParticleState,
    dt: float = 1 / 48,
    wind: CurlWind | None = None,
    physics: ParticlePhysics | None = None,
    bounds: ParticleBounds | None = None,
) -> ParticleState:
    finite(dt, "particle dt", 1e-6, 1.0)
    physics = physics if physics is not None else particle_physics(state.kind)
    velocity = state.velocities_m_s
    local_wind = (
        np.zeros_like(velocity)
        if wind is None
        else wind.sample(state.positions_m, state.time_seconds)
    )
    rate = physics.drag_kg_s / physics.mass_kg
    decay, phi, gravity_integral, var_v, cov, var_x = _ou_moments(rate, dt)
    gravity = np.asarray(physics.gravity_m_s2)
    next_velocity = decay * velocity + (1 - decay) * local_wind + phi * gravity
    next_position = (
        state.positions_m + phi * velocity + (dt - phi) * local_wind + gravity_integral * gravity
    )
    if physics.noise_n_sqrt_s:
        count = velocity.size
        seed = bytes.fromhex(state.seed_sha256)
        nv = normal(seed, f"langevin/{state.step_index}/velocity", count).reshape(velocity.shape)
        nx = normal(seed, f"langevin/{state.step_index}/position", count).reshape(velocity.shape)
        sigma = physics.noise_n_sqrt_s / physics.mass_kg
        sqrt_v = math.sqrt(var_v)
        correlated = cov / sqrt_v
        next_velocity += sigma * sqrt_v * nv
        next_position += sigma * (correlated * nv + math.sqrt(max(0.0, var_x - correlated**2)) * nx)
    if bounds is not None:
        low, high = np.asarray(bounds.minimum_m), np.asarray(bounds.maximum_m)
        next_position = low + np.remainder(next_position - low, high - low)
    return ParticleState(
        next_position,
        next_velocity,
        state.kind,
        state.seed_sha256,
        state.step_index + 1,
        state.time_seconds + dt,
    )


def iter_particle_frames(
    state: ParticleState,
    frame_count: int,
    dt: float = 1 / 48,
    wind: CurlWind | None = None,
    bounds: ParticleBounds | None = None,
) -> Iterator[ParticleState]:
    """Yield subsequent frames, keeping only one state unless the caller retains them."""
    integer(frame_count, "frame count", 0, 100_000)
    for _ in range(frame_count):
        state = step_particles(state, dt, wind, bounds=bounds)
        yield state


class SurfaceKind(str, Enum):
    DRY_SOIL = "dry_soil"
    WET_SOIL = "wet_soil"
    SAND = "sand"
    ROCK = "rock"
    SNOW = "snow"
    WET_ASPHALT = "wet_asphalt"


_SURFACES: dict[SurfaceKind, tuple[RGB, float, float]] = {
    SurfaceKind.DRY_SOIL: ((0.28, 0.16, 0.08), 0.92, 1.52),
    SurfaceKind.WET_SOIL: ((0.14, 0.075, 0.035), 0.30, 1.333),
    SurfaceKind.SAND: ((0.68, 0.49, 0.26), 0.84, 1.54),
    SurfaceKind.ROCK: ((0.30, 0.32, 0.34), 0.70, 1.55),
    SurfaceKind.SNOW: ((0.90, 0.94, 0.98), 0.80, 1.31),
    SurfaceKind.WET_ASPHALT: ((0.035, 0.040, 0.045), 0.12, 1.333),
}


def fresnel_reflectance(
    cos_incidence: float, ior_from: float = 1.0, ior_to: float = 1.333
) -> float:
    """Exact unpolarized dielectric Fresnel, including total internal reflection."""
    c = finite(cos_incidence, "incidence cosine", 0, 1)
    n1, n2 = finite(ior_from, "incident IOR", 1, 4), finite(ior_to, "transmitted IOR", 1, 4)
    if n1 == n2:
        return 0.0
    sin_t2 = (n1 / n2) ** 2 * (1 - c * c)
    if sin_t2 >= 1:
        return 1.0
    ct = math.sqrt(1 - sin_t2)
    rs = ((n1 * c - n2 * ct) / (n1 * c + n2 * ct)) ** 2
    rp = ((n1 * ct - n2 * c) / (n1 * ct + n2 * c)) ** 2
    return (rs + rp) / 2


@dataclass(frozen=True, slots=True)
class SurfaceResponse:
    kind: SurfaceKind
    diffuse_rgb: RGB
    specular_reflectance: float
    roughness: float


def soil_optics(
    kind: SurfaceKind | str,
    cos_incidence: float = 1.0,
    wetness: float = 0.0,
    water_depth_mm: float = 0.0,
) -> SurfaceResponse:
    kind = SurfaceKind(kind)
    finite(wetness, "wetness", 0, 1)
    finite(water_depth_mm, "water depth mm", 0, 100)
    albedo, roughness, ior = _SURFACES[kind]
    effective_wetness = max(
        wetness, 1.0 if kind in (SurfaceKind.WET_SOIL, SurfaceKind.WET_ASPHALT) else 0.0
    )
    reflectance = fresnel_reflectance(
        cos_incidence, 1, (1 - effective_wetness) * ior + effective_wetness * 1.333
    )
    attenuation = np.exp(-np.array([0.08, 0.035, 0.018]) * water_depth_mm)
    diffuse = np.asarray(albedo) * (1 - reflectance) * (1 - 0.5 * effective_wetness) * attenuation
    rgb = float(diffuse[0]), float(diffuse[1]), float(diffuse[2])
    return SurfaceResponse(
        kind, rgb, reflectance, (1 - effective_wetness) * roughness + effective_wetness * 0.12
    )


@dataclass(frozen=True, slots=True)
class ThunderEvent:
    emission_seconds: float = 0.0
    distance_m: float = 10.0
    intensity: float = 0.8

    def __post_init__(self) -> None:
        finite(self.emission_seconds, "thunder emission", 0, 3600)
        finite(self.distance_m, "thunder distance", 0, 100_000)
        finite(self.intensity, "thunder intensity", 0, 1)

    @property
    def arrival_seconds(self) -> float:
        return self.emission_seconds + self.distance_m / 343.0


@dataclass(frozen=True, slots=True)
class AmbientAudio:
    pcm: PCMArray
    sample_rate: int = SAMPLE_RATE
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.sample_rate != SAMPLE_RATE:
            raise ValueError("ambient audio must be mono PCM16 at 48000 Hz")
        pcm = frozen_pcm(self.pcm)
        if pcm.ndim != 1 or not 1 <= len(pcm) <= int(MAX_AUDIO_SECONDS * SAMPLE_RATE):
            raise ValueError("ambient clips must contain 1 sample to 5 seconds of mono PCM")
        object.__setattr__(self, "pcm", pcm)
        object.__setattr__(self, "sha256", hashlib.sha256(self.to_wav()).hexdigest())

    @property
    def duration_seconds(self) -> float:
        return len(self.pcm) / self.sample_rate

    def to_wav(self) -> bytes:
        data = self.pcm.tobytes()
        return (
            struct.pack(
                "<4sI4s4sIHHIIHH4sI",
                b"RIFF",
                36 + len(data),
                b"WAVE",
                b"fmt ",
                16,
                1,
                1,
                self.sample_rate,
                self.sample_rate * 2,
                2,
                16,
                b"data",
                len(data),
            )
            + data
        )


def synthesize_ambient(
    seed: Seed,
    kind: WeatherKind | str = WeatherKind.WIND,
    duration_seconds: float = 0.25,
    wind_strength: float = 0.4,
    rain_intensity: float = 0.5,
    thunder: ThunderEvent | None = None,
) -> AmbientAudio:
    """Bounded mono synthesis: aeroacoustic low-pass wind, resonant rain impacts,
    rustle/crackle and delayed, distance-attenuated low-frequency thunder.

    This is a physical-inspired sound effect, not a field recording or speech
    model. The full parameter set (including duration) defines the waveform.
    """
    kind = WeatherKind(kind)
    finite(duration_seconds, "audio duration", 1 / SAMPLE_RATE, MAX_AUDIO_SECONDS)
    finite(wind_strength, "wind strength", 0, 1)
    finite(rain_intensity, "rain intensity", 0, 1)
    count = max(1, round(duration_seconds * SAMPLE_RATE))
    t = np.arange(count) / SAMPLE_RATE
    frequency = np.fft.rfftfreq(count, 1 / SAMPLE_RATE)
    white = 2 * uniform(seed, "audio/air", count) - 1
    white -= float(np.mean(white))
    spectrum = np.fft.rfft(white)
    wind_filter = 1 / np.sqrt(1 + (frequency / 220) ** 4)
    wind = np.fft.irfft(spectrum * wind_filter, n=count)
    gust_phase = float(uniform(seed, "audio/gust", 1)[0]) * 2 * math.pi
    mix = 1.3 * wind_strength * wind * (0.7 + 0.3 * np.sin(2 * math.pi * 0.23 * t + gust_phase))
    if kind in (WeatherKind.RAIN, WeatherKind.STORM):
        hiss_filter = (
            (frequency / 600) ** 2
            / (1 + (frequency / 600) ** 2)
            / np.sqrt(1 + (frequency / 9000) ** 4)
        )
        mix += 0.23 * rain_intensity * np.fft.irfft(spectrum * hiss_filter, n=count)
        drops = uniform(seed, "audio/drops", count)
        impulses = np.where(drops < 650 * rain_intensity / SAMPLE_RATE, 1.0, 0.0)
        kernel_t = np.arange(min(count, 960)) / SAMPLE_RATE
        kernel = np.exp(-kernel_t / 0.003) * np.sin(2 * math.pi * 2400 * kernel_t)
        # Zero padding makes this a linear, not circular, impact convolution.
        nfft = 1 << (count + len(kernel) - 2).bit_length()
        impacts = np.fft.irfft(np.fft.rfft(impulses, nfft) * np.fft.rfft(kernel, nfft), nfft)
        mix += 0.10 * rain_intensity * impacts[:count]
    if kind in (WeatherKind.LEAVES, WeatherKind.ASH, WeatherKind.DUST, WeatherKind.SPARKS):
        rustle = np.fft.irfft(spectrum * frequency / (frequency + 1200), n=count)
        rustle_level = 0.18 if kind in (WeatherKind.LEAVES, WeatherKind.SPARKS) else 0.025
        mix += rustle_level * rustle * (0.5 + 0.5 * np.sin(2 * math.pi * 3.7 * t) ** 2)
    event = (
        thunder if thunder is not None else (ThunderEvent() if kind is WeatherKind.STORM else None)
    )
    if event is not None:
        elapsed = t - event.arrival_seconds
        envelope = np.where(elapsed >= 0, np.exp(-np.maximum(elapsed, 0) / 0.7), 0)
        rumble = np.fft.irfft(spectrum / np.sqrt(1 + (frequency / 90) ** 6), n=count)
        mix += 6 * event.intensity / (1 + event.distance_m / 500) * envelope * rumble
    # No whole-clip DC subtraction here: that would leak delayed thunder into
    # the silent interval before its physical arrival.
    fade_samples = min(round(0.005 * SAMPLE_RATE), count // 2)
    if fade_samples:
        fade = np.linspace(0, 1, fade_samples)
        mix[:fade_samples] *= fade
        mix[-fade_samples:] *= fade[::-1]
    pcm = np.rint(np.tanh(mix) * (0.9 * 32767)).astype("<i2")
    return AmbientAudio(pcm)
