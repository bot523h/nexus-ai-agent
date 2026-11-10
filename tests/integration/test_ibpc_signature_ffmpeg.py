"""Real-FFmpeg integration tests for the NAGAR IBPC Scope Signature (Spec §3).

Every clip is produced by the real ffmpeg binary and every signature is
computed from real decoded pixels; nothing is mocked. The calibrated facts
proven here:

* the 64-bin L*a*b* histogram is L2-normalised, finite and non-negative;
* motion magnitude is a *motion* metric, not a brightness one — two clips with
  (near-)identical mean luma but different temporal variation get very
  different motion magnitudes;
* a black clip is finite and normalised (no NaN / Inf);
* the stable digest is identical across two real runs (``computed_at`` is
  excluded from it);
* :class:`ScopeMatchEngine` ranks an identical scope highest and filters a
  genuinely dissimilar one at the default 0.80 threshold.

FFmpeg availability is an environment gate: no binary → the module skips
(UNAVAILABLE ≠ PASSED).
"""

from __future__ import annotations

import math
import subprocess
from pathlib import Path

import pytest

from nexus_ai_agent.creative.slideshow.ffmpeg import (
    FfmpegUnavailableError,
    resolve_ffmpeg_bin,
)
from nexus_ai_agent.nagar.ibpc.models import VideoTimeScope
from nexus_ai_agent.nagar.ibpc.signature import (
    ScopeMatchEngine,
    SignatureError,
    compute_scope_signature,
    cosine_similarity,
)

pytestmark = pytest.mark.integration

_SIZE = "160x90"
_RATE = 10
_DURATION_US = 2_000_000
_TESTSRC_LAVFI = f"testsrc=duration=2:size={_SIZE}:rate={_RATE}"
_GREY_LAVFI = f"color=c=0x808080:s={_SIZE}:r={_RATE}:d=2"
_BLACK_LAVFI = f"color=c=black:s={_SIZE}:r={_RATE}:d=2"


def _ffmpeg_bin() -> str:
    try:
        return resolve_ffmpeg_bin()
    except FfmpegUnavailableError as exc:  # environment gate — never a pass
        pytest.skip(f"FFmpeg unavailable: {exc}")


def _clip(tmp_path: Path, lavfi: str, *, name: str, video_filter: str | None = None) -> Path:
    path = tmp_path / name
    cmd = [
        _ffmpeg_bin(),
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        lavfi,
    ]
    if video_filter is not None:
        cmd += ["-vf", video_filter]
    cmd += [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=180)
    if proc.returncode != 0:
        pytest.skip(f"could not synthesize clip: {proc.stderr.decode('utf-8', 'replace')[-200:]}")
    return path


def _scope(
    start_us: int = 0, end_us: int = _DURATION_US, *, asset_id: str = "src"
) -> VideoTimeScope:
    return VideoTimeScope(asset_id=asset_id, effect="keep", start_us=start_us, end_us=end_us)


def _mean_luma(path: Path, samples: int = 8) -> float:
    """Mean Rec.601 luma of *samples* decoded gray frames — independent of the module."""
    binary = _ffmpeg_bin()
    values: list[float] = []
    for index in range(samples):
        ts = (index * _DURATION_US) // samples / 1_000_000.0
        raw = subprocess.run(
            [
                binary,
                "-nostdin",
                "-ss",
                f"{ts:.6f}",
                "-i",
                str(path),
                "-vframes",
                "1",
                "-vf",
                "format=gray",
                "-f",
                "rawvideo",
                "pipe:1",
            ],
            capture_output=True,
            timeout=60,
        ).stdout
        if raw:
            values.append(sum(raw) / len(raw))
    return sum(values) / len(values) if values else 0.0


def test_histogram_is_64_bins_l2_normalised_and_finite(tmp_path: Path) -> None:
    source = _clip(tmp_path, _TESTSRC_LAVFI, name="src.mp4")
    scope = _scope()
    signature = compute_scope_signature(source, scope, max_frames=8)

    histogram = signature.color_histogram
    assert len(histogram) == 64
    assert all(math.isfinite(v) and v >= 0.0 for v in histogram)
    assert math.sqrt(sum(v * v for v in histogram)) == pytest.approx(1.0, abs=1e-9)

    assert signature.sample_frames == 8
    assert signature.scope_digest == scope.scope_digest
    assert signature.signature_digest.startswith("sha256:")


def test_equal_mean_brightness_does_not_hide_motion(tmp_path: Path) -> None:
    # Two clips with the same base grey (mean luma ~128) but very different
    # temporal variation: one is pixel-static, the other modulates its luma.
    static = _clip(tmp_path, _GREY_LAVFI, name="static.mp4")
    wave = _clip(
        tmp_path,
        _GREY_LAVFI,
        name="wave.mp4",
        video_filter="geq=lum='128+60*sin(2*PI*T)':cb=128:cr=128",
    )
    static_sig = compute_scope_signature(static, _scope(), max_frames=8)
    wave_sig = compute_scope_signature(wave, _scope(), max_frames=8)

    # brightness is (near-)identical ...
    assert abs(_mean_luma(static) - _mean_luma(wave)) < 5.0
    # ... yet motion is not: the static clip has none, the wave clip is high.
    assert static_sig.motion_magnitude == pytest.approx(0.0, abs=1.0)
    assert wave_sig.motion_magnitude > 20.0


def test_black_video_is_finite_and_normalised(tmp_path: Path) -> None:
    source = _clip(tmp_path, _BLACK_LAVFI, name="black.mp4")
    signature = compute_scope_signature(source, _scope(), max_frames=6)

    assert all(math.isfinite(v) and v >= 0.0 for v in signature.color_histogram)
    assert math.sqrt(sum(v * v for v in signature.color_histogram)) == pytest.approx(1.0, abs=1e-9)
    assert signature.motion_magnitude == pytest.approx(0.0, abs=1.0)
    assert signature.signature_digest.startswith("sha256:")


def test_stable_digest_is_identical_across_real_runs(tmp_path: Path) -> None:
    source = _clip(tmp_path, _TESTSRC_LAVFI, name="src.mp4")
    first = compute_scope_signature(source, _scope(), max_frames=6)
    second = compute_scope_signature(source, _scope(), max_frames=6)

    assert first.signature_digest == second.signature_digest
    assert first.color_histogram == second.color_histogram
    assert first.motion_magnitude == second.motion_magnitude


def test_scope_match_engine_ranks_identical_and_filters_dissimilar(tmp_path: Path) -> None:
    source = _clip(tmp_path, _TESTSRC_LAVFI, name="src.mp4")
    black = _clip(tmp_path, _BLACK_LAVFI, name="black.mp4")

    query = compute_scope_signature(source, _scope(0, 1_000_000))
    identical = compute_scope_signature(source, _scope(0, 1_000_000))
    dissimilar = compute_scope_signature(black, _scope(0, 2_000_000))

    engine = ScopeMatchEngine()
    matches = engine.find_similar(query, [dissimilar, identical])

    assert matches, "the identical scope must match"
    assert matches[0].candidate_scope_digest == identical.scope_digest
    assert matches[0].cosine_similarity == pytest.approx(1.0, abs=1e-9)
    # the black clip is genuinely dissimilar → filtered out at the default 0.80
    assert all(m.candidate_scope_digest != dissimilar.scope_digest for m in matches)
    assert cosine_similarity(query.color_histogram, dissimilar.color_histogram) < 0.80


def test_unresolvable_ffmpeg_binary_is_a_typed_error(tmp_path: Path) -> None:
    source = _clip(tmp_path, _TESTSRC_LAVFI, name="src.mp4")
    with pytest.raises(SignatureError) as error:
        compute_scope_signature(source, _scope(), ffmpeg_bin="/nonexistent/ffmpeg-xyz")
    assert error.value.reason == "ffmpeg_not_found"
