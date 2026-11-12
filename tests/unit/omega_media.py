"""Synthetic, adversarial media generated at test time (no binary fixtures).

Every fixture is a real file written by numpy + the standard library ``wave``
module, so the suite stays reproducible and the repository stores no media.  The
signals are chosen to *break* naive implementations:

* :func:`write_sine` — a quiet steady tone (normalization must lift it);
* :func:`write_clipped` — a hard-clipped, over-hot mix (peak limiting must act);
* :func:`write_click_track` — a percussive pulse at a known BPM (the detector
  must find *that* tempo, not a default);
* :func:`write_noisy_voice` — speech-like bursts buried in stationary noise
  (denoise must lower the noise floor without destroying the bursts);
* :func:`write_silence` — digital silence (normalization must refuse);
* :func:`write_corrupt` — a truncated RIFF header (decoding must fail loudly);
* :func:`write_with_noise_floor` — steady tone plus constant hiss, for
  signature/continuity comparisons.
"""

from __future__ import annotations

import struct
import wave
from pathlib import Path

import numpy as np

__all__ = [
    "write_sine",
    "write_clipped",
    "write_click_track",
    "write_noisy_voice",
    "write_silence",
    "write_corrupt",
    "write_with_noise_floor",
    "write_multichannel",
    "read_wav_samples",
]

DEFAULT_RATE = 48_000


def _write(path: Path, samples: np.ndarray, *, rate: int, channels: int = 1) -> Path:
    pcm = np.clip(np.asarray(samples, dtype=np.float64), -1.0, 1.0)
    interleaved = np.repeat(pcm[:, None], channels, axis=1).reshape(-1) if channels > 1 else pcm
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes((interleaved * 32767.0).astype("<i2").tobytes())
    return path


def _time(seconds: float, rate: int) -> np.ndarray:
    return np.arange(int(round(seconds * rate)), dtype=np.float64) / rate


def write_sine(
    path: Path,
    *,
    seconds: float = 6.0,
    amplitude: float = 0.1,
    frequency: float = 440.0,
    rate: int = DEFAULT_RATE,
) -> Path:
    """A quiet steady tone: −23.7 LUFS at the default amplitude."""
    return _write(path, amplitude * np.sin(2 * np.pi * frequency * _time(seconds, rate)), rate=rate)


def write_clipped(path: Path, *, seconds: float = 5.0, rate: int = DEFAULT_RATE) -> Path:
    """A hot mix driven 6 dB into the ceiling: hard clipping, true peak 0 dBFS."""
    t = _time(seconds, rate)
    signal = 2.0 * np.sin(2 * np.pi * 330.0 * t)
    return _write(path, np.clip(signal, -1.0, 1.0), rate=rate)


def write_click_track(
    path: Path,
    *,
    bpm: float = 96.0,
    seconds: float = 12.0,
    rate: int = DEFAULT_RATE,
    noise: float = 0.0005,
) -> Path:
    """A 4/4 click track at ``bpm`` (accented downbeat, 50 ms decay)."""
    if bpm <= 0:
        raise ValueError("bpm must be positive")
    t = _time(seconds, rate)
    samples = noise * np.random.default_rng(3).standard_normal(t.size)
    for beat, start in enumerate(np.arange(0.0, seconds, 60.0 / bpm)):
        index = int(start * rate)
        length = int(0.05 * rate)
        if index + length >= samples.size:
            break
        envelope = np.exp(-np.linspace(0.0, 12.0, length))
        tone = np.sin(
            2.0 * np.pi * (180.0 if beat % 4 == 0 else 320.0) * np.arange(length) / rate
        )
        samples[index : index + length] += (0.9 if beat % 4 == 0 else 0.5) * envelope * tone
    return _write(path, samples, rate=rate)


def write_noisy_voice(
    path: Path, *, seconds: float = 8.0, noise: float = 0.03, rate: int = DEFAULT_RATE
) -> Path:
    """Three speech-like bursts (220 Hz, Hann-windowed) over stationary hiss."""
    t = _time(seconds, rate)
    samples = noise * np.random.default_rng(7).standard_normal(t.size)
    for start in (2.0, 4.0, 6.0):
        index = int(start * rate)
        length = int(1.2 * rate)
        envelope = np.hanning(length)
        samples[index : index + length] += (
            0.5 * envelope * np.sin(2.0 * np.pi * 220.0 * np.arange(length) / rate)
        )
    return _write(path, samples, rate=rate)


def write_silence(path: Path, *, seconds: float = 5.0, rate: int = DEFAULT_RATE) -> Path:
    return _write(path, np.zeros(int(seconds * rate)), rate=rate)


def write_corrupt(path: Path, *, declared: int = 4096, actual: int = 220) -> Path:
    """A RIFF/WAVE header that promises far more data than the file contains."""
    payload = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + declared,
        b"WAVE",
        b"fmt ",
        16,
        1,  # PCM
        1,  # mono
        DEFAULT_RATE,
        DEFAULT_RATE * 2,
        2,
        16,
        b"data",
        declared,
    )
    path.write_bytes(payload + b"\x00" * actual)
    return path


def write_with_noise_floor(
    path: Path,
    *,
    seconds: float = 6.0,
    frequency: float = 200.0,
    noise: float = 0.002,
    rate: int = DEFAULT_RATE,
) -> Path:
    """A steady tone plus constant hiss (for signature determinism tests)."""
    t = _time(seconds, rate)
    rng = np.random.default_rng(11)
    return _write(
        path,
        0.25 * np.sin(2 * np.pi * frequency * t) + noise * rng.standard_normal(t.size),
        rate=rate,
    )


def write_multichannel(
    path: Path, *, seconds: float = 3.0, rate: int = DEFAULT_RATE
) -> Path:
    tone = 0.2 * np.sin(2 * np.pi * 440.0 * _time(seconds, rate))
    return _write(path, tone, rate=rate, channels=2)


def read_wav_samples(path: Path) -> tuple[np.ndarray, int]:
    """Read a 16-bit WAV back as ``(mono float64, rate)`` (a test-side decoder)."""
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    data = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data, rate
