"""Sample-count-preserving splice smoothing and deterministic dialogue ducking."""

from __future__ import annotations

import wave
from collections.abc import Sequence
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from .contracts import KernelError
from .timeline import _wav_signature

FloatArray = NDArray[np.float64]


def _decode_pcm(data: bytes, width: int) -> FloatArray:
    if width == 1:
        x = np.frombuffer(data, dtype=np.uint8).astype(np.float64) - 128
        return x / 128.0
    if width == 2:
        return np.frombuffer(data, dtype="<i2").astype(np.float64) / 32768.0
    if width == 3:
        b = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3)
        n = (
            b[:, 0].astype(np.int32)
            | (b[:, 1].astype(np.int32) << 8)
            | (b[:, 2].astype(np.int32) << 16)
        )
        n = (n ^ 0x800000) - 0x800000
        return n.astype(np.float64) / 8388608.0
    if width == 4:
        return np.frombuffer(data, dtype="<i4").astype(np.float64) / 2147483648.0
    raise KernelError("PCM sample widths 1-4 bytes are supported")


def _encode_pcm(x: FloatArray, width: int) -> bytes:
    bits = width * 8
    lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    values = np.rint(np.clip(x, -1, hi / (hi + 1)) * (1 << (bits - 1))).astype(np.int64)
    values = np.clip(values, lo, hi)
    if width == 1:
        return (values + 128).astype(np.uint8).tobytes()
    if width == 2:
        return values.astype("<i2").tobytes()
    if width == 4:
        return values.astype("<i4").tobytes()
    out = np.empty((len(values), 3), dtype=np.uint8)
    v = values.astype(np.int32)
    out[:, 0], out[:, 1], out[:, 2] = v & 255, (v >> 8) & 255, (v >> 16) & 255
    return out.tobytes()


def declick_pcm_wav_splices(
    source: str | Path,
    destination: str | Path,
    splice_frames: Sequence[int],
    *,
    bridge_frames: int = 32,
) -> dict[str, object]:
    """Smooth short windows around splice positions; preserves frame count exactly."""
    source, destination = Path(source), Path(destination)
    if bridge_frames < 2 or destination.exists():
        raise KernelError("bridge must be >=2 frames and destination must be new")
    try:
        with wave.open(str(source), "rb") as reader:
            signature = _wav_signature(reader)
            channels, width, rate, _ = signature
            frames = reader.getnframes()
            pcm = reader.readframes(frames)
    except (wave.Error, OSError, EOFError) as exc:
        raise KernelError("invalid PCM WAV") from exc
    samples = _decode_pcm(pcm, width).reshape(frames, channels)
    before: list[float] = []
    after: list[float] = []
    slope_before: list[float] = []
    slope_after: list[float] = []
    half = bridge_frames // 2
    for boundary in splice_frames:
        if not half <= boundary < frames - half:
            raise KernelError("splice has insufficient bridge context")
        before.append(float(np.max(np.abs(samples[boundary] - samples[boundary - 1]))))
        slope_before.append(
            float(
                np.max(
                    np.abs(samples[boundary + 1] - 2 * samples[boundary] + samples[boundary - 1])
                )
            )
        )
        left, right = samples[boundary - half - 1].copy(), samples[boundary + half].copy()
        t = np.linspace(0.0, 1.0, 2 * half + 2)
        weights_l, weights_r = np.cos(t * np.pi / 2), np.sin(t * np.pi / 2)
        bridge = weights_l[:, None] * left + weights_r[:, None] * right
        samples[boundary - half : boundary + half] = bridge[1:-1]
        after.append(float(np.max(np.abs(samples[boundary] - samples[boundary - 1]))))
        slope_after.append(
            float(
                np.max(
                    np.abs(samples[boundary + 1] - 2 * samples[boundary] + samples[boundary - 1])
                )
            )
        )
    output = _encode_pcm(samples.reshape(-1), width)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(destination), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(output)
    with wave.open(str(destination), "rb") as check:
        if _wav_signature(check) != signature or check.getnframes() != frames:
            raise KernelError("de-click output changed WAV format or frame count")
    return {
        "sample_frames": frames,
        "duration": (frames, rate),
        "boundary_steps_before": tuple(before),
        "boundary_steps_after": tuple(after),
        "max_step_before": max(before, default=0.0),
        "max_step_after": max(after, default=0.0),
        "max_slope_jump_before": max(slope_before, default=0.0),
        "max_slope_jump_after": max(slope_after, default=0.0),
    }


def duck_ambience_under_dialogue(
    dialogue: FloatArray,
    ambience: FloatArray,
    sample_rate: int,
    *,
    threshold_db: float = -35.0,
    attenuation_db: float = -9.0,
    attack_ms: float = 8.0,
    release_ms: float = 120.0,
) -> tuple[FloatArray, float]:
    """Lookahead RMS sidechain gain envelope; returns ducked signal and SNR gain dB."""
    d, a = np.asarray(dialogue, dtype=np.float64), np.asarray(ambience, dtype=np.float64)
    if d.ndim != 1 or a.shape != d.shape or sample_rate <= 0 or min(attack_ms, release_ms) <= 0:
        raise KernelError("dialogue/ambience must be same-length mono signals with valid rates")
    window = max(1, round(sample_rate * attack_ms / 1000))
    power = np.convolve(d * d, np.ones(window) / window, mode="same")
    threshold = 10 ** (threshold_db / 10)
    target = np.where(power >= threshold, 10 ** (attenuation_db / 20), 1.0)
    gain = np.empty_like(target)
    attack = np.exp(-1 / (sample_rate * attack_ms / 1000))
    release = np.exp(-1 / (sample_rate * release_ms / 1000))
    prev = 1.0
    for i, value in enumerate(target):
        coefficient = attack if value < prev else release
        prev = coefficient * prev + (1 - coefficient) * value
        gain[i] = prev
    out = a * gain
    voice = float(np.mean(d * d))
    snr_before = 10 * np.log10((voice + 1e-15) / (float(np.mean(a * a)) + 1e-15))
    snr_after = 10 * np.log10((voice + 1e-15) / (float(np.mean(out * out)) + 1e-15))
    return np.asarray(out, dtype=np.float64), float(snr_after - snr_before)
