"""Deterministic text-to-speech alignment primitives; no external model required."""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

_VOWELS = set("aeiouy")
_VISEMES = {
    "sil": 0,
    "a": 1,
    "e": 2,
    "i": 3,
    "o": 4,
    "u": 5,
    "m": 6,
    "f": 7,
    "th": 8,
    "sh": 9,
    "rest": 0,
}


@dataclass(frozen=True)
class AcousticFeatures:
    energy: np.ndarray
    spectral_centroid: np.ndarray
    zero_crossing_rate: np.ndarray
    sample_rate: int
    hop_size: int


@dataclass(frozen=True)
class PhonemeAlignment:
    symbol: str
    start: float
    end: float
    viseme: int
    aperture: float


@dataclass(frozen=True)
class WordAlignment:
    word: str
    start: float
    end: float
    phonemes: tuple[PhonemeAlignment, ...]
    confidence: float = 1.0


@dataclass(frozen=True)
class AlignmentResult:
    words: tuple[WordAlignment, ...]
    cost: float


def tokenize(text: str) -> list[str]:
    return re.findall(r"[\w]+(?:['’-][\w]+)*", text, flags=re.UNICODE)


def phonemize(word: str) -> tuple[str, ...]:
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return ("sil",)
    out: list[str] = []
    i = 0
    while i < len(w):
        if w[i : i + 2] in ("th", "sh", "ch", "ph", "ng"):
            out.append(w[i : i + 2])
            i += 2
        else:
            out.append(w[i])
            i += 1
    return tuple(out)


def phoneme_to_viseme(symbol: str) -> int:
    s = symbol.lower()
    if s in _VISEMES:
        return _VISEMES[s]
    if s[0:1] in _VOWELS:
        return _VISEMES.get(s[0], 1)
    if s.startswith(("b", "p", "m")):
        return 6
    if s.startswith(("f", "v")):
        return 7
    if s.startswith(("s", "z", "c")):
        return 9
    return 8


def extract_acoustic_features(
    audio: np.ndarray, sample_rate: int, frame_size: int = 400, hop_size: int = 160
) -> AcousticFeatures:
    x = np.asarray(audio, dtype=np.float32).reshape(-1)
    if x.size < frame_size:
        x = np.pad(x, (0, frame_size - x.size))
    frames = np.stack([x[i : i + frame_size] for i in range(0, x.size - frame_size + 1, hop_size)])
    win = np.hanning(frame_size)
    spec = np.abs(np.fft.rfft(frames * win))
    freqs = np.fft.rfftfreq(frame_size, 1 / sample_rate)
    energy = np.sqrt(np.mean(frames * frames, axis=1) + 1e-10)
    centroid = (spec * freqs).sum(1) / (spec.sum(1) + 1e-10)
    zcr = np.mean(np.diff(np.signbit(frames), axis=1), axis=1)
    return AcousticFeatures(energy, centroid, zcr, sample_rate, hop_size)


def banded_dtw(cost: np.ndarray, band: int | None = None) -> tuple[float, list[tuple[int, int]]]:
    c = np.asarray(cost, dtype=float)
    if c.ndim != 2 or not c.size:
        raise ValueError("cost must be a non-empty 2-D matrix")
    if not bool(np.all(np.isfinite(c))):
        raise ValueError("cost must be finite (no NaN or Inf)")
    n, m = c.shape
    band = max(n, m) if band is None else max(abs(n - m), int(band))
    inf = float("inf")
    dp = np.full((n, m), inf)
    prev = np.full((n, m, 2), -1, dtype=int)
    dp[0, 0] = c[0, 0]
    for i in range(n):
        for j in range(max(0, i - band), min(m, i + band + 1)):
            if i == j == 0:
                continue
            opts = []
            if i:
                opts.append((dp[i - 1, j], (i - 1, j)))
            if j:
                opts.append((dp[i, j - 1], (i, j - 1)))
            if i and j:
                opts.append((dp[i - 1, j - 1], (i - 1, j - 1)))
            best, p = min(opts, key=lambda z: (z[0], z[1]))
            dp[i, j] = c[i, j] + best
            prev[i, j] = p
    if not np.isfinite(dp[-1, -1]):
        raise ValueError(
            "band does not connect endpoints"
        )  # pragma: no cover - unreachable: band >= |n-m| keeps endpoints connected
    path = []
    i, j = n - 1, m - 1
    while i >= 0 and j >= 0:
        path.append((i, j))
        p = prev[i, j]
        if i == j == 0:
            break
        i, j = int(p[0]), int(p[1])
    return float(dp[-1, -1]), path[::-1]


def align_tokens(
    text: str, duration: float, features: AcousticFeatures | None = None, band: int = 4
) -> AlignmentResult:
    words = tokenize(text)
    if not words or duration <= 0:
        return AlignmentResult((), 0.0)
    weights = np.array([max(1, len(phonemize(w))) for w in words], float)
    edges = np.r_[0, np.cumsum(weights) / weights.sum() * duration]
    out = []
    for i, w in enumerate(words):
        ps = phonemize(w)
        span = float(edges[i + 1] - edges[i])
        step = span / len(ps)
        ph = tuple(
            PhonemeAlignment(
                p,
                float(edges[i] + k * step),
                float(edges[i] + (k + 1) * step),
                phoneme_to_viseme(p),
                float(1.0 if p[0:1] in _VOWELS else 0.35),
            )
            for k, p in enumerate(ps)
        )
        out.append(WordAlignment(w, float(edges[i]), float(edges[i + 1]), ph, 1.0))
    return AlignmentResult(tuple(out), 0.0)


forced_align = align_tokens
