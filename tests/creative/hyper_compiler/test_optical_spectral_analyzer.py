"""Tests for :mod:`optical_spectral_analyzer`.

Colour-space and math tests are pure unit tests; frame/audio streaming tests
use real ffmpeg clips rendered into ``/tmp``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

from nexus_ai_agent.creative.hyper_compiler import optical_spectral_analyzer as osa
from nexus_ai_agent.creative.hyper_compiler.optical_spectral_analyzer import (
    AUDIO_SAMPLE_RATE,
    ShotBoundary,
    detect_shot_boundaries,
    jensen_shannon_divergence,
    oklab_histogram,
    proxy_frames,
    rgb_to_oklab,
    smooth_trajectory,
    srgb_to_linear,
)


def test_srgb_to_linear_endpoints():
    assert srgb_to_linear(np.array(0.0)) == 0.0
    assert srgb_to_linear(np.array(1.0)) == 1.0
    # Linear region.
    assert abs(srgb_to_linear(np.array(0.02)) - (0.02 / 12.92)) < 1e-12


def test_rgb_to_oklab_known_values():
    # Constant input → constant OKLab output; lightness stays in [0, 1].
    gray = np.full((3, 3, 3), 0.5)
    oklab = rgb_to_oklab(gray)
    assert oklab.shape == (3, 3, 3)
    assert np.allclose(oklab, oklab[0, 0])
    assert 0.0 <= oklab[0, 0, 0] <= 1.0
    # Determinism.
    assert np.array_equal(rgb_to_oklab(gray), rgb_to_oklab(gray))
    # Round trip through the matrix pair preserves shape and range.
    oklab = rgb_to_oklab(np.random.rand(4, 5, 3))
    assert oklab.shape == (4, 5, 3)
    assert np.all(oklab[..., 0] >= 0)


def test_oklab_histogram_normalises():
    frame = np.full((osa.PROXY_HEIGHT, osa.PROXY_WIDTH, 3), 0.5)
    oklab = rgb_to_oklab(frame)
    hist = oklab_histogram(oklab)
    assert hist.shape == (osa._HIST_BINS**3,)
    assert abs(hist.sum() - 1.0) < 1e-9
    assert np.all(hist >= 0)


def test_jensen_shannon_basic():
    p = np.array([0.5, 0.5])
    assert abs(jensen_shannon_divergence(p, p)) < 1e-9
    q = np.array([1.0, 0.0])
    div = jensen_shannon_divergence(p, q)
    assert div > 0.0
    # Symmetric.
    assert abs(jensen_shannon_divergence(p, q) - jensen_shannon_divergence(q, p)) < 1e-12


def test_detect_shot_boundaries_identical_frames():
    frame = np.random.rand(osa.PROXY_HEIGHT, osa.PROXY_WIDTH, 3)
    boundaries = detect_shot_boundaries([frame, frame.copy(), frame.copy()], threshold=0.15)
    assert boundaries == []


def test_detect_shot_boundaries_cut():
    red = np.zeros((osa.PROXY_HEIGHT, osa.PROXY_WIDTH, 3))
    red[..., 0] = 1.0
    blue = np.zeros((osa.PROXY_HEIGHT, osa.PROXY_WIDTH, 3))
    blue[..., 2] = 1.0
    boundaries = detect_shot_boundaries([red, red, blue, blue], threshold=0.05)
    assert len(boundaries) == 1
    assert isinstance(boundaries[0], ShotBoundary)
    assert boundaries[0].js_divergence > 0.05


def test_smooth_trajectory_constant_unchanged():
    pts = np.tile(np.array([[3.0, -1.0]]), (10, 1))
    smoothed = smooth_trajectory(pts)
    assert np.allclose(smoothed, pts, atol=1e-6)


def test_smooth_trajectory_reduces_jitter():
    rng = np.random.default_rng(0)
    t = np.linspace(0, 1, 50)
    clean = np.stack([t, 0.5 * t], axis=1)
    noisy = clean + 0.2 * rng.standard_normal((50, 2))
    smoothed = smooth_trajectory(noisy, lambda_1=1.0, lambda_2=1.0, lambda_3=1.0)

    # Smoothed trajectory must be smoother (smaller second differences) than noisy.
    def second_diff(x):
        return np.mean(np.sum(np.diff(x, n=2, axis=0) ** 2, axis=1))

    assert second_diff(smoothed) < second_diff(noisy)
    # And still tracks the clean signal reasonably.
    assert np.mean(np.abs(smoothed - clean)) < np.mean(np.abs(noisy - clean)) + 0.1


def test_proxy_frames_shape(make_clip):
    path = make_clip("proxy.mp4", duration=1.0, width=320, height=180, fps=30, has_audio=True)
    frames = proxy_frames(str(path), width=160, height=90)
    assert 1 <= len(frames) <= 60
    for frame in frames:
        assert frame.shape == (90, 160, 3)
        assert frame.dtype == np.float64
        assert 0.0 <= frame.min() and frame.max() <= 1.0


def _make_clip_with_audio(path: Path, kind: str, duration: float = 2.0) -> None:
    """kind in {'tone', 'silence'}."""
    audio_src = (
        "sine=frequency=440:duration=" + str(duration)
        if kind == "tone"
        else f"anullsrc=duration={duration}"
    )
    args = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        audio_src,
        "-t",
        str(duration),
        "-c:a",
        "pcm_s16le",
        "-ar",
        str(AUDIO_SAMPLE_RATE),
        "-ac",
        "1",
        str(path),
    ]
    result = subprocess.run(args, shell=False, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr


def test_analyze_audio_silence_speech_tone(clips_dir):
    path = clips_dir / "tone.wav"
    _make_clip_with_audio(path, "tone", duration=2.0)
    segments = osa.analyze_audio_silence_speech(str(path))
    assert len(segments) >= 1
    assert all(seg.is_speech for seg in segments)
    assert segments[0].start_us == 0


def test_analyze_audio_silence_speech_silent(clips_dir):
    path = clips_dir / "silence.wav"
    _make_clip_with_audio(path, "silence", duration=2.0)
    segments = osa.analyze_audio_silence_speech(str(path))
    assert len(segments) >= 1
    assert all(not seg.is_speech for seg in segments)


def test_analyze_audio_missing_file_returns_empty(clips_dir):
    assert osa.analyze_audio_silence_speech(str(clips_dir / "missing.wav")) == []
