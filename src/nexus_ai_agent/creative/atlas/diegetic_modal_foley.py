"""WAVE 27 — diegetic waveweide: modal foley synthesis and placement.

Two coupled facilities:

* **Modal impact synthesis** — a struck object is modelled as a bank of damped
  modes ``sum_n A_n exp(-t / tau_n) sin(2 pi f_n t + phi_n)``. Mode ratios and
  damping come from published idealisations (free-free bar, Kirchhoff circular
  plate) and per-material presets; the synthesis is a closed-form sum, so the
  output is exactly reproducible and its energy decays monotonically.
* **Diegetic placement ("waveweide")** — foley events are scheduled on a
  timeline against on-screen actions, with an optional propagation delay from
  source distance (``t = d / c_air``) so the sound is *diegetic*: it is
  positioned in the same world as the picture, not merely mixed on top.

The module also provides a bounded convolution reverb (exponential-decay
impulse response) and a foley report that states exactly which events were
placed, their delays, and the peak sample so an unplaced event is never
mistaken for a placed one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np

__all__ = [
    "FoleyError",
    "SPEED_OF_SOUND",
    "Material",
    "MATERIALS",
    "ModalImpact",
    "synthesise_impact",
    "DiegeticEvent",
    "FoleyReport",
    "place_foley_events",
    "convolve_reverb",
    "exponential_ir",
    "MAX_SAMPLE_RATE",
    "MAX_SAMPLES",
]

#: Speed of sound in air at 20 C (m/s).
SPEED_OF_SOUND: Final[float] = 343.0
MAX_SAMPLE_RATE: Final[int] = 192_000
MAX_SAMPLES: Final[int] = 48_000 * 600  # 10 minutes at 48 kHz


class FoleyError(ValueError):
    """Raised for invalid materials, events, or out-of-range parameters."""


@dataclass(frozen=True)
class Material:
    """A struck material: mode frequency ratios, damping times and brightness."""

    name: str
    mode_ratios: tuple[float, ...]
    decay_times: tuple[float, ...]
    base_frequency: float

    def __post_init__(self) -> None:
        if not self.name:
            raise FoleyError("material name must be non-empty")
        if len(self.mode_ratios) != len(self.decay_times):
            raise FoleyError("mode_ratios and decay_times must have equal length")
        if len(self.mode_ratios) == 0:
            raise FoleyError("material must have at least one mode")
        for r in self.mode_ratios:
            if not np.isfinite(r) or r <= 0.0:
                raise FoleyError("mode ratios must be finite and positive")
        for d in self.decay_times:
            if not np.isfinite(d) or d <= 0.0:
                raise FoleyError("decay times must be finite and positive")
        if not np.isfinite(self.base_frequency) or self.base_frequency <= 0.0:
            raise FoleyError("base_frequency must be finite and positive")


#: Free-free bar ratios (1, 2.756, 5.404, 8.933) and a plate-like set; decay
#: times shorten with mode order as higher modes radiate faster.
MATERIALS: Final[dict[str, Material]] = {
    "glass": Material(
        "glass", (1.0, 2.756, 5.404, 8.933, 13.34), (1.2, 0.6, 0.35, 0.2, 0.12), 900.0
    ),
    "wood": Material("wood", (1.0, 2.756, 5.404, 8.933), (0.35, 0.2, 0.12, 0.08), 180.0),
    "metal": Material(
        "metal", (1.0, 2.756, 5.404, 8.933, 13.34, 18.64), (2.5, 1.6, 1.1, 0.8, 0.55, 0.4), 420.0
    ),
    "stone": Material("stone", (1.0, 3.011, 6.209), (0.25, 0.14, 0.08), 260.0),
}


@dataclass(frozen=True)
class ModalImpact:
    """A synthesised impact: the sample array and its mode frequencies."""

    samples: np.ndarray
    sample_rate: int
    frequencies: np.ndarray
    peak: float


def synthesise_impact(
    material: Material,
    *,
    sample_rate: int = 48_000,
    duration: float = 0.8,
    amplitude: float = 1.0,
    excitation: float = 1.0,
    seed: int = 0,
) -> ModalImpact:
    """Synthesise a modal impact of ``material`` as a sum of damped sinusoids.

    ``amplitude`` scales the whole hit; ``excitation`` scales the brightness
    (relative level of higher modes). Phases are seeded, so a given
    ``(material, seed)`` reproduces identical samples.
    """
    if sample_rate < 1 or sample_rate > MAX_SAMPLE_RATE:
        raise FoleyError(f"sample_rate must be in [1, {MAX_SAMPLE_RATE}]")
    if duration <= 0.0 or not np.isfinite(duration):
        raise FoleyError("duration must be finite and positive")
    if amplitude <= 0.0 or not np.isfinite(amplitude):
        raise FoleyError("amplitude must be finite and positive")
    if excitation <= 0.0 or not np.isfinite(excitation):
        raise FoleyError("excitation must be finite and positive")
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
        raise FoleyError("seed must be a non-negative int")
    n = int(round(sample_rate * duration))
    if n > MAX_SAMPLES:
        raise FoleyError(f"too many samples (>{MAX_SAMPLES})")

    t = np.arange(n, dtype=np.float64) / sample_rate
    rng = np.random.default_rng(seed)
    ratios = np.array(material.mode_ratios)
    decays = np.array(material.decay_times)
    freqs = material.base_frequency * ratios
    if np.any(freqs >= sample_rate / 2.0):
        raise FoleyError("mode frequency exceeds the Nyquist limit; raise sample_rate")
    out = np.zeros(n, dtype=np.float64)
    for i, (fr, tau) in enumerate(zip(freqs, decays, strict=True)):
        phase = rng.uniform(0.0, 2.0 * np.pi)
        # Higher modes are excited more by a brighter excitation.
        level = amplitude * excitation**i
        out += level * np.exp(-t / tau) * np.sin(2.0 * np.pi * fr * t + phase)
    peak = float(np.abs(out).max())
    # A non-empty sum of damped sinusoids is non-zero, so normalisation always
    # applies; peak becomes exactly ``amplitude``.
    out = out / peak * amplitude
    return ModalImpact(samples=out, sample_rate=sample_rate, frequencies=freqs, peak=amplitude)


@dataclass(frozen=True)
class DiegeticEvent:
    """A foley event tied to an on-screen action.

    ``screen_time`` is when the action is visible (seconds); ``source_distance``
    is how far the source is from the camera/listener, so the heard time is
    ``screen_time + source_distance / speed_of_sound``.
    """

    label: str
    screen_time: float
    source_distance: float
    material_name: str
    amplitude: float = 1.0
    seed: int = 0

    def __post_init__(self) -> None:
        if not self.label:
            raise FoleyError("event label must be non-empty")
        if not np.isfinite(self.screen_time) or self.screen_time < 0.0:
            raise FoleyError("screen_time must be finite and non-negative")
        if not np.isfinite(self.source_distance) or self.source_distance < 0.0:
            raise FoleyError("source_distance must be finite and non-negative")
        if self.material_name not in MATERIALS:
            raise FoleyError(f"unknown material {self.material_name!r}")
        if not np.isfinite(self.amplitude) or self.amplitude <= 0.0:
            raise FoleyError("amplitude must be finite and positive")
        if not isinstance(self.seed, int) or isinstance(self.seed, bool) or self.seed < 0:
            raise FoleyError("seed must be a non-negative int")


@dataclass(frozen=True)
class FoleyReport:
    """The result of placing foley events: a mix plus per-event placement data."""

    mix: np.ndarray
    sample_rate: int
    placed_labels: tuple[str, ...]
    heard_times: tuple[float, ...]
    delays: tuple[float, ...]
    peak: float


def place_foley_events(
    events: list[DiegeticEvent],
    *,
    duration: float,
    sample_rate: int = 48_000,
    max_events: int = 256,
) -> FoleyReport:
    """Mix diegetic foley events onto a timeline of ``duration`` seconds.

    Each event is synthesised from its material and placed at
    ``screen_time + distance / c``. Events that fall outside the timeline are
    skipped and reported by omission (never silently placed at zero).
    """
    if duration <= 0.0 or not np.isfinite(duration):
        raise FoleyError("duration must be finite and positive")
    if sample_rate < 1 or sample_rate > MAX_SAMPLE_RATE:
        raise FoleyError(f"sample_rate must be in [1, {MAX_SAMPLE_RATE}]")
    if len(events) > max_events:
        raise FoleyError(f"too many events (>{max_events})")

    n = int(round(sample_rate * duration))
    if n > MAX_SAMPLES:
        raise FoleyError(f"too many samples (>{MAX_SAMPLES})")
    mix = np.zeros(n, dtype=np.float64)
    placed: list[str] = []
    heard: list[float] = []
    delays: list[float] = []
    for ev in events:
        delay = ev.source_distance / SPEED_OF_SOUND
        heard_time = ev.screen_time + delay
        start = int(round(heard_time * sample_rate))
        if start < 0 or start >= n:
            continue  # outside the timeline: honestly not placed
        material = MATERIALS[ev.material_name]
        impact = synthesise_impact(
            material,
            sample_rate=sample_rate,
            duration=min(0.8, duration),
            amplitude=ev.amplitude,
            seed=ev.seed,
        )
        end = min(n, start + impact.samples.size)
        mix[start:end] += impact.samples[: end - start]
        placed.append(ev.label)
        heard.append(heard_time)
        delays.append(delay)
    peak = float(np.abs(mix).max())
    return FoleyReport(
        mix=mix,
        sample_rate=sample_rate,
        placed_labels=tuple(placed),
        heard_times=tuple(heard),
        delays=tuple(delays),
        peak=peak,
    )


def exponential_ir(
    *, sample_rate: int = 48_000, rt60: float = 1.0, seed: int = 0, pre_delay: float = 0.02
) -> np.ndarray:
    """A synthetic room impulse response: seeded noise under an exponential
    decay whose energy drops by 60 dB over ``rt60`` seconds."""
    if sample_rate < 1 or sample_rate > MAX_SAMPLE_RATE:
        raise FoleyError(f"sample_rate must be in [1, {MAX_SAMPLE_RATE}]")
    if rt60 <= 0.0 or not np.isfinite(rt60):
        raise FoleyError("rt60 must be finite and positive")
    if pre_delay < 0.0 or not np.isfinite(pre_delay):
        raise FoleyError("pre_delay must be finite and non-negative")
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
        raise FoleyError("seed must be a non-negative int")
    n = int(round(sample_rate * rt60))
    if n > MAX_SAMPLES:
        raise FoleyError(f"too many samples (>{MAX_SAMPLES})")
    rng = np.random.default_rng(seed)
    t = np.arange(n, dtype=np.float64) / sample_rate
    env = 10.0 ** (-3.0 * t / rt60)  # -60 dB at t = rt60
    ir = rng.normal(0.0, 1.0, n) * env
    pre = int(round(pre_delay * sample_rate))
    if pre > 0:
        ir = np.concatenate([np.zeros(pre), ir])
    # A non-empty seeded noise burst always has non-zero energy.
    norm = float(np.sqrt(np.sum(ir * ir)))
    return ir / norm


def convolve_reverb(signal: np.ndarray, ir: np.ndarray) -> np.ndarray:
    """Convolve ``signal`` with a reverb impulse response (full convolution)."""
    s = np.asarray(signal, dtype=np.float64)
    h = np.asarray(ir, dtype=np.float64)
    if s.ndim != 1 or h.ndim != 1:
        raise FoleyError("signal and ir must be 1-D")
    if s.size == 0 or h.size == 0:
        raise FoleyError("signal and ir must be non-empty")
    if not (np.all(np.isfinite(s)) and np.all(np.isfinite(h))):
        raise FoleyError("signal and ir contain NaN/Inf")
    out: np.ndarray = np.convolve(s, h, mode="full")
    return out
