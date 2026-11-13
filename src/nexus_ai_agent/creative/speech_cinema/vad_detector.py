"""Deterministic, dependency-light voice activity detection."""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import BinaryIO

import numpy as np


@dataclass(frozen=True)
class VADConfig:
    sample_rate: int = 16_000
    frame_ms: float = 25.0
    hop_ms: float = 10.0
    energy_threshold_db: float = -42.0
    entropy_threshold: float = 0.92
    min_speech_ms: float = 80.0
    min_silence_ms: float = 120.0
    smoothing_frames: int = 3


@dataclass(frozen=True)
class SpeechSegment:
    start: Fraction
    end: Fraction
    snr_db: float
    confidence: float = 1.0

    @property
    def duration(self) -> Fraction:
        return self.end - self.start


@dataclass(frozen=True)
class VADResult:
    segments: tuple[SpeechSegment, ...]
    sample_rate: int
    speech_mask: np.ndarray
    frame_energy_db: np.ndarray
    spectral_entropy: np.ndarray
    snr_db: float


def read_wav(source: str | Path | bytes | bytearray | BinaryIO) -> tuple[np.ndarray, int]:
    """Read PCM WAV into mono float32 samples in [-1, 1]."""
    if isinstance(source, (str, Path)):
        handle = wave.open(str(source), "rb")
        close = True
    else:
        handle = (
            wave.open(io.BytesIO(source), "rb")
            if isinstance(source, (bytes, bytearray))
            else wave.open(source, "rb")
        )
        close = True
    try:
        rate, channels, width, count = (
            handle.getframerate(),
            handle.getnchannels(),
            handle.getsampwidth(),
            handle.getnframes(),
        )
        raw = handle.readframes(count)
    finally:
        if close:
            handle.close()
    if width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128) / 128
    elif width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        vals = (
            b[:, 0].astype(np.int32)
            | (b[:, 1].astype(np.int32) << 8)
            | (b[:, 2].astype(np.int32) << 16)
        )
        vals = np.where(vals & 0x800000, vals - 0x1000000, vals)
        data = vals.astype(np.float32) / 8388608
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648
    else:
        raise ValueError(f"unsupported PCM width: {width} bytes")
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return np.asarray(data, dtype=np.float32), rate


def _coerce_audio(
    audio: np.ndarray | str | Path | bytes | bytearray | BinaryIO, sample_rate: int | None
) -> tuple[np.ndarray, int]:
    if isinstance(audio, np.ndarray):
        x = np.asarray(audio, dtype=np.float32)
        if x.ndim == 2:
            x = x.mean(axis=1)
        return x.reshape(-1), int(VADConfig.sample_rate if sample_rate is None else sample_rate)
    return read_wav(audio)


def _entropy(frame: np.ndarray) -> float:
    power = np.abs(np.fft.rfft(frame * np.hanning(len(frame)))) ** 2
    p = power / (power.sum() + 1e-12)
    return float(-(p * np.log(p + 1e-12)).sum() / np.log(max(2, len(p))))


def detect_voice(
    audio: np.ndarray | str | Path | bytes | bytearray | BinaryIO,
    sample_rate: int | None = None,
    config: VADConfig | None = None,
) -> VADResult:
    cfg = config or VADConfig()
    x, rate = _coerce_audio(audio, sample_rate)
    if rate <= 0:
        raise ValueError("sample_rate must be a positive integer")
    if x.size and not bool(np.all(np.isfinite(x))):
        raise ValueError("audio samples must be finite (no NaN or Inf)")
    if x.size == 0:
        return VADResult((), rate, np.zeros(0, dtype=bool), np.zeros(0), np.zeros(0), 0.0)
    frame = max(1, round(rate * cfg.frame_ms / 1000))
    hop = max(1, round(rate * cfg.hop_ms / 1000))
    if x.size < frame:
        x = np.pad(x, (0, frame - x.size))
    starts = np.arange(0, max(1, x.size - frame + 1), hop)
    frames = np.stack([x[s : s + frame] for s in starts])
    rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)
    energy_db = 20 * np.log10(rms + 1e-9)
    entropy = np.array([_entropy(f) for f in frames])
    raw = (energy_db >= cfg.energy_threshold_db) & (entropy <= cfg.entropy_threshold)
    # The window must not exceed the frame count: np.convolve(mode="same") returns
    # max(len(raw), width) elements, which desynchronises the mask from the frames.
    width = min(max(1, int(cfg.smoothing_frames)), int(raw.size))
    smooth = np.convolve(raw.astype(np.int32), np.ones(width, dtype=np.int32), mode="same") >= (
        width / 2
    )
    # Hysteresis-like cleanup: reject short speech islands and fill short gaps.
    min_on = max(1, round(cfg.min_speech_ms / cfg.hop_ms))
    min_off = max(1, round(cfg.min_silence_ms / cfg.hop_ms))
    for value, minimum in ((True, min_on), (False, min_off)):
        i = 0
        while i < len(smooth):
            if bool(smooth[i]) == value:
                j = i
                while j < len(smooth) and bool(smooth[j]) == value:
                    j += 1
                # Fill only silence bounded by speech on both sides.  A short leading or
                # trailing silence (or a whole short clip) must not be promoted to speech.
                bounded = i > 0 and j < len(smooth)
                if j - i < minimum and (value or bounded):
                    smooth[i:j] = not value
                i = j
            else:
                i += 1
    noise = float(np.percentile(rms, 20))
    signal = float(np.percentile(rms[smooth], 80)) if smooth.any() else noise
    snr = float(20 * np.log10((signal + 1e-9) / (noise + 1e-9)))
    segments: list[SpeechSegment] = []
    i = 0
    while i < len(smooth):
        if not smooth[i]:
            i += 1
            continue
        j = i
        while j < len(smooth) and smooth[j]:
            j += 1
        start = Fraction(int(starts[i]), rate)
        end = Fraction(int(min(x.size, starts[j - 1] + frame)), rate)
        conf = float(np.clip(np.mean((energy_db[i:j] - cfg.energy_threshold_db) / 30), 0, 1))
        segments.append(SpeechSegment(start, end, snr, conf))
        i = j
    return VADResult(tuple(segments), rate, smooth.astype(bool), energy_db, entropy, snr)


vad = detect_voice
