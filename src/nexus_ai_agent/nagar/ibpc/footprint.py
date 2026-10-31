"""Independent footprint verifier: proves intended change + preservation from
the **decoded content** of the real artifact — never from renderer metadata.

Method (video, time-range scope):

1. probe + decode source and artifact with the same allow-listed FFmpeg binary
   (read-only ``-f rawvideo`` decode to stdout; no shell, one process per call,
   timeouts + frame/byte caps — a verifier never writes media);
2. discover the artifact→source frame mapping **by content** (downsampled
   mean-abs-diff cost matrix, argmin per artifact frame), then require it to be
   strictly increasing;
3. preservation check — the kept window's frames (every mapped pair) must be
   **bit-exact** for PASS; a per-frame mean|Δ| above ``NOISE_MEAN_MAX`` is a
   provable change inside the preserved window (FAIL); anything in between is
   codec noise → ``INCONCLUSIVE`` (a lossy encode can never earn a PASS);
4. intended-change check — for ``keep`` (the canonical ``timeline.trim``
   semantics: in/out points are a KEEP window): every artifact frame must map
   into the kept source window (±1 boundary frame of codec rounding), every
   unmatched source frame must lie OUTSIDE the kept window, and the output frame
   count must equal the kept window's frame count (±1); for ``modify``: frames
   inside the scope must differ beyond noise while the count stays equal.

A perceptual score is never used as preservation proof. Verdict aggregation:
FAIL > INCONCLUSIVE > PASS; overall PASS requires every check PASS.

Calibration (2026-10-31, ffmpeg 7.0.2, ``testsrc`` 320x240@15): unchanged-content
re-encode at crf 20 gives per-frame mean|Δ| ≤ 0.47 (max 63); a real global
brightness change gives mean|Δ| ≈ 31. ``NOISE_MEAN_MAX = 4.0`` sits between with
~8× margin on both sides (see docs/engineering/NAGAR_LIVE_EDITING_LIVE_AUDIT.md §4).
"""

from __future__ import annotations

import math
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from nexus_ai_agent.creative.slideshow.ffmpeg import (
    RenderError,
    probe_video,
    resolve_ffmpeg_bin,
    sha256_file,
)
from nexus_ai_agent.nagar.ibpc.models import (
    CheckVerdict,
    FootprintCheck,
    VerifierReport,
    VideoTimeScope,
    worst_verdict,
)

#: Per-frame mean|Δ| above which a preserved-region frame counts as provably
#: changed (FAIL). Calibrated — see module docstring.
NOISE_MEAN_MAX = 4.0
MAX_FRAMES = 2048
MAX_DECODE_BYTES = 384 * 1024 * 1024
DECODE_TIMEOUT_S = 300
#: Downsample stride for the mapping cost matrix (full-res diffs are computed
#: only on the discovered mapping).
_MAP_STRIDE = 4

_FPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*fps")


class FootprintVerifyError(RuntimeError):
    """Environment-level failure (no binary, unreadable file). The public
    verify functions convert this into an INCONCLUSIVE report instead."""


@dataclass(frozen=True)
class DecodedVideo:
    width: int
    height: int
    fps: float
    duration_us: int
    frames: tuple[np.ndarray, ...]  # uint8 (H, W, 3) rgb24


def _run_capture(args: list[str], *, timeout: int) -> subprocess.CompletedProcess[bytes]:
    # argv list, shell=False, allow-listed binary — same discipline as the lane.
    return subprocess.run(args, capture_output=True, timeout=timeout, check=False)


def _probe_fps(path: Path, binary: str) -> float:
    proc = _run_capture(
        [
            binary,
            "-hide_banner",
            "-nostdin",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-f",
            "null",
            "-",
        ],
        timeout=60,
    )
    match = _FPS_RE.search(proc.stderr.decode("utf-8", "replace"))
    if not match:
        raise FootprintVerifyError(f"could not parse fps for {path}")
    return float(match.group(1))


def probe_media(path: Path, *, binary: str | None = None) -> tuple[Any, float]:
    """``probe_video`` + parsed fps with the same allow-listed binary."""
    resolved = resolve_ffmpeg_bin(binary)
    info = probe_video(path, binary=resolved)
    return info, _probe_fps(path, resolved)


def decode_video(path: Path, *, binary: str | None = None) -> DecodedVideo:
    """Decode all video frames to rgb24 rawvideo (read-only)."""
    resolved = resolve_ffmpeg_bin(binary)
    info = probe_video(path, binary=resolved)
    if info.width is None or info.height is None:
        raise FootprintVerifyError(f"no video stream / resolution in {path}")
    fps = _probe_fps(path, resolved)
    width, height = info.width, info.height
    frame_bytes = width * height * 3
    proc = _run_capture(
        [
            resolved,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        timeout=DECODE_TIMEOUT_S,
    )
    if proc.returncode != 0:
        raise FootprintVerifyError(
            f"decode failed for {path}: {proc.stderr.decode('utf-8', 'replace')[-400:]}"
        )
    raw = proc.stdout
    if not raw:
        raise FootprintVerifyError(f"decode produced no frames: {path}")
    if len(raw) % frame_bytes != 0:
        raise FootprintVerifyError(f"raw frame size mismatch for {path}")
    if len(raw) > MAX_DECODE_BYTES:
        raise FootprintVerifyError(f"decoded output exceeds the verifier budget: {path}")
    count = len(raw) // frame_bytes
    if count > MAX_FRAMES:
        raise FootprintVerifyError(f"frame count {count} exceeds verifier cap {MAX_FRAMES}")
    arr = np.frombuffer(raw, dtype=np.uint8).reshape(count, height, width, 3)
    frames = tuple(arr[i] for i in range(count))
    return DecodedVideo(
        width=width, height=height, fps=fps, duration_us=info.duration_us, frames=frames
    )


def _frame_stats(a: np.ndarray, b: np.ndarray) -> tuple[float, int]:
    """(mean abs diff, max abs diff) between two same-shape frames."""
    if a.shape != b.shape:
        raise FootprintVerifyError("frame shape mismatch")
    diff = np.abs(a.astype(np.int16) - b.astype(np.int16))
    return float(diff.mean()), int(diff.max())


def _discover_mapping(artifact: DecodedVideo, source: DecodedVideo) -> tuple[list[int], np.ndarray]:
    """Argmin mapping artifact→source over a downsampled cost matrix.

    Returns (best source index per artifact frame, downsampled mean-diff row
    minima). Full-resolution diffs are computed separately on the mapping.
    """
    stride = _MAP_STRIDE
    src_small = [f[::stride, ::stride].astype(np.int16) for f in source.frames]
    art_small = [f[::stride, ::stride].astype(np.int16) for f in artifact.frames]
    src_stack = np.stack(src_small)  # (n, h, w, 3)
    mapping: list[int] = []
    minima: list[float] = []
    for af in art_small:
        # (n, h, w, 3) diff against this artifact frame
        diff = np.abs(src_stack - af[None, ...]).mean(axis=(1, 2, 3))
        j = int(diff.argmin())
        mapping.append(j)
        minima.append(float(diff[j]))
    return mapping, np.array(minima, dtype=float)


def _classify_diffs(mean_diffs: list[float]) -> CheckVerdict:
    """PASS iff all bit-exact (max==0 handled by caller via exact flags)."""
    if not mean_diffs:
        return CheckVerdict.INCONCLUSIVE
    if max(mean_diffs) > NOISE_MEAN_MAX:
        return CheckVerdict.FAIL
    return CheckVerdict.INCONCLUSIVE


def _inconclusive_report(
    *, scope: VideoTimeScope, source_path: Path, artifact_path: Path, reason: str
) -> VerifierReport:
    check = FootprintCheck(
        name="decode",
        verdict=CheckVerdict.INCONCLUSIVE,
        evidence={"reason": reason},
    )
    return VerifierReport(
        scope_digest=scope.scope_digest,
        source_digest=_safe_digest(source_path),
        artifact_digest=_safe_digest(artifact_path),
        source_frames=0,
        artifact_frames=0,
        checks=(check,),
        verdict=CheckVerdict.INCONCLUSIVE,
    )


def _safe_digest(path: Path) -> str:
    try:
        return sha256_file(path)
    except Exception:  # noqa: BLE001 — digest failure degrades the report, never fakes it
        return "sha256:" + "0" * 64


def _expected_kept_frames(scope: VideoTimeScope, fps: float) -> tuple[int, int]:
    """Half-open frame bounds of the KEEP window at ``fps`` (codec-rounded)."""
    a = math.ceil(scope.start_us * fps / 1_000_000 - 1e-9)
    b = math.ceil(scope.end_us * fps / 1_000_000 - 1e-9)
    return a, b


def verify_keep(
    source_path: str | Path,
    artifact_path: str | Path,
    *,
    scope: VideoTimeScope,
    binary: str | None = None,
) -> VerifierReport:
    """Verify a ``keep``-scoped edit (canonical ``timeline.trim``: the window
    ``[start_us, end_us)`` is preserved, the complement is removed) on real
    decoded frames."""
    if scope.effect != "keep":
        raise ValueError("verify_keep expects a keep scope")
    source_path, artifact_path = Path(source_path), Path(artifact_path)
    try:
        resolved = resolve_ffmpeg_bin(binary)
        source = decode_video(source_path, binary=resolved)
        artifact = decode_video(artifact_path, binary=resolved)
    except (RenderError, FootprintVerifyError) as exc:
        return _inconclusive_report(
            scope=scope,
            source_path=source_path,
            artifact_path=artifact_path,
            reason=f"{type(exc).__name__}: {exc}",
        )

    if (source.width, source.height) != (artifact.width, artifact.height):
        return _inconclusive_report(
            scope=scope,
            source_path=source_path,
            artifact_path=artifact_path,
            reason=(
                f"resolution mismatch: source {source.width}x{source.height} vs "
                f"artifact {artifact.width}x{artifact.height}"
            ),
        )
    if abs(source.fps - artifact.fps) > 0.5:
        return _inconclusive_report(
            scope=scope,
            source_path=source_path,
            artifact_path=artifact_path,
            reason=f"fps mismatch: {source.fps} vs {artifact.fps}",
        )

    n_src = len(source.frames)
    n_art = len(artifact.frames)
    mapping, _ = _discover_mapping(artifact, source)

    # Strictly increasing mapping required — reordered content is a change.
    strictly_increasing = all(mapping[i] < mapping[i + 1] for i in range(len(mapping) - 1))

    # Full-resolution diffs on the discovered mapping.
    pair_max: list[int] = []
    pair_mean: list[float] = []
    for i, j in enumerate(mapping):
        mean, mx = _frame_stats(artifact.frames[i], source.frames[j])
        pair_mean.append(mean)
        pair_max.append(mx)

    a_exp, b_exp = _expected_kept_frames(scope, source.fps)
    # mapped frames get ±1 boundary tolerance; unmatched frames must lie strictly
    # outside the kept window [a_exp, b_exp)
    mapped_inside = all(a_exp - 1 <= j <= b_exp for j in mapping)
    unmatched = sorted(set(range(n_src)) - set(mapping))
    unmatched_outside = all(j < a_exp or j >= b_exp for j in unmatched)
    count_ok = abs(n_art - (b_exp - a_exp)) <= 1
    intended = (
        CheckVerdict.PASS
        if (strictly_increasing and mapped_inside and unmatched_outside and count_ok)
        else CheckVerdict.FAIL
    )

    exact = all(mx == 0 for mx in pair_max)
    if not strictly_increasing:
        preservation = CheckVerdict.FAIL
    elif exact:
        preservation = CheckVerdict.PASS
    else:
        preservation = _classify_diffs(pair_mean)

    checks = (
        FootprintCheck(
            name="intended_change",
            verdict=intended,
            evidence={
                "source_frames": n_src,
                "artifact_frames": n_art,
                "keep_window_frames": [a_exp, b_exp],
                "mapped_range": [min(mapping), max(mapping)] if mapping else None,
                "unmatched_source_frames": unmatched,
                "strictly_increasing_mapping": strictly_increasing,
            },
        ),
        FootprintCheck(
            name="preservation_of_keep_window",
            verdict=preservation,
            evidence={
                "mapped_pairs": n_art,
                "bit_exact_pairs": sum(1 for mx in pair_max if mx == 0),
                "max_pair_mean_diff": max(pair_mean) if pair_mean else None,
                "max_pair_abs_diff": max(pair_max) if pair_max else None,
                "noise_mean_max": NOISE_MEAN_MAX,
                "note": (
                    "PASS requires bit-exact preservation of the kept window; "
                    "lossy encoding yields INCONCLUSIVE, never PASS"
                ),
            },
        ),
    )
    return VerifierReport(
        scope_digest=scope.scope_digest,
        source_digest=sha256_file(source_path),
        artifact_digest=sha256_file(artifact_path),
        source_frames=n_src,
        artifact_frames=n_art,
        checks=checks,
        verdict=worst_verdict(tuple(c.verdict for c in checks)),
    )


def verify_range_modify(
    source_path: str | Path,
    artifact_path: str | Path,
    *,
    scope: VideoTimeScope,
    binary: str | None = None,
) -> VerifierReport:
    """Verify an in-place ``modify``-scoped edit (e.g. a graded segment)."""
    if scope.effect != "modify":
        raise ValueError("verify_range_modify expects a modify scope")
    source_path, artifact_path = Path(source_path), Path(artifact_path)
    try:
        resolved = resolve_ffmpeg_bin(binary)
        source = decode_video(source_path, binary=resolved)
        artifact = decode_video(artifact_path, binary=resolved)
    except (RenderError, FootprintVerifyError) as exc:
        return _inconclusive_report(
            scope=scope,
            source_path=source_path,
            artifact_path=artifact_path,
            reason=f"{type(exc).__name__}: {exc}",
        )
    if (source.width, source.height) != (artifact.width, artifact.height):
        return _inconclusive_report(
            scope=scope,
            source_path=source_path,
            artifact_path=artifact_path,
            reason="resolution mismatch",
        )

    n = min(len(source.frames), len(artifact.frames))
    if len(source.frames) != len(artifact.frames):
        intended = CheckVerdict.FAIL
    else:
        intended = CheckVerdict.PASS  # frame count preserved; content checked below

    sf = math.ceil(scope.start_us * source.fps / 1_000_000 - 1e-9)
    ef = math.ceil(scope.end_us * source.fps / 1_000_000 - 1e-9)
    outside_mean: list[float] = []
    inside_mean: list[float] = []
    for i in range(n):
        mean, mx = _frame_stats(artifact.frames[i], source.frames[i])
        if i < sf or i >= ef:
            outside_mean.append(mean)
        else:
            inside_mean.append(mean)

    if len(source.frames) != len(artifact.frames):
        preservation = CheckVerdict.FAIL
    elif not outside_mean:
        preservation = CheckVerdict.INCONCLUSIVE
    elif all(m == 0.0 for m in outside_mean):
        preservation = CheckVerdict.PASS
    else:
        preservation = _classify_diffs(outside_mean)

    if not inside_mean:
        intended = CheckVerdict.FAIL  # empty in-scope range: nothing could change
    elif max(inside_mean) > NOISE_MEAN_MAX:
        intended = intended if intended is CheckVerdict.PASS else CheckVerdict.FAIL
    elif all(m == 0.0 for m in inside_mean):
        intended = CheckVerdict.FAIL  # no change happened inside the scope
    else:
        intended = CheckVerdict.INCONCLUSIVE

    checks = (
        FootprintCheck(
            name="intended_change",
            verdict=intended,
            evidence={
                "scope_frames": [sf, ef],
                "max_inside_mean_diff": max(inside_mean) if inside_mean else None,
                "frame_count_equal": len(source.frames) == len(artifact.frames),
            },
        ),
        FootprintCheck(
            name="preservation_outside_scope",
            verdict=preservation,
            evidence={
                "outside_frames": len(outside_mean),
                "bit_exact_outside": sum(1 for m in outside_mean if m == 0.0),
                "max_outside_mean_diff": max(outside_mean) if outside_mean else None,
                "noise_mean_max": NOISE_MEAN_MAX,
            },
        ),
    )
    return VerifierReport(
        scope_digest=scope.scope_digest,
        source_digest=sha256_file(source_path),
        artifact_digest=sha256_file(artifact_path),
        source_frames=len(source.frames),
        artifact_frames=len(artifact.frames),
        checks=checks,
        verdict=worst_verdict(tuple(c.verdict for c in checks)),
    )


__all__ = [
    "DECODE_TIMEOUT_S",
    "FootprintVerifyError",
    "MAX_DECODE_BYTES",
    "MAX_FRAMES",
    "NOISE_MEAN_MAX",
    "DecodedVideo",
    "decode_video",
    "probe_media",
    "verify_keep",
    "verify_range_modify",
]
