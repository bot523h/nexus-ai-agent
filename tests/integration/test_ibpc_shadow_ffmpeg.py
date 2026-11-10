"""Real-FFmpeg integration tests for the NAGAR IBPC Shadow Preview (Spec §2).

FFmpeg is never mocked here: every clip is synthesized by the real binary and
every claim in the returned :class:`ShadowPreviewManifest` is re-derived
independently — the base64 decodes to bytes whose sha256 *is* the frame digest,
those bytes are a decodable JPEG, the timestamps sit strictly inside the
half-open scope, and the composite digest is recomputed from the sorted frame
digests.

FFmpeg availability is an environment gate: when no binary can be resolved the
module skips (UNAVAILABLE ≠ PASSED), it never silently passes.
"""

from __future__ import annotations

import base64
import hashlib
import io
import subprocess
from collections.abc import Iterable
from pathlib import Path

import pytest
from PIL import Image

from nexus_ai_agent.creative.slideshow.ffmpeg import (
    FfmpegUnavailableError,
    resolve_ffmpeg_bin,
)
from nexus_ai_agent.nagar.ibpc.models import ShadowKeyframe, VideoTimeScope
from nexus_ai_agent.nagar.ibpc.shadow import ShadowRenderError, build_shadow_preview

pytestmark = pytest.mark.integration

_SIZE = "160x90"
_RATE = 10
_SOURCE_LAVFI = f"testsrc=duration=2:size={_SIZE}:rate={_RATE}"
_PLAN_DIGEST = "sha256:" + "a" * 64


def _ffmpeg_bin() -> str:
    try:
        return resolve_ffmpeg_bin()
    except FfmpegUnavailableError as exc:  # environment gate — never a pass
        pytest.skip(f"FFmpeg unavailable: {exc}")


def _clip(tmp_path: Path, lavfi: str = _SOURCE_LAVFI, name: str = "src.mp4") -> Path:
    path = tmp_path / name
    proc = subprocess.run(
        [
            _ffmpeg_bin(),
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            lavfi,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        capture_output=True,
        timeout=180,
    )
    if proc.returncode != 0:
        pytest.skip(f"could not synthesize clip: {proc.stderr.decode('utf-8', 'replace')[-200:]}")
    return path


def _scope(start_us: int, end_us: int, *, asset_id: str = "src") -> VideoTimeScope:
    return VideoTimeScope(asset_id=asset_id, effect="keep", start_us=start_us, end_us=end_us)


def _composite_of(keyframes: Iterable[ShadowKeyframe]) -> str:
    return (
        "sha256:"
        + hashlib.sha256(b"".join(sorted(kf.frame_digest.encode() for kf in keyframes))).hexdigest()
    )


def test_keyframes_are_real_self_verifying_jpegs(tmp_path: Path) -> None:
    source = _clip(tmp_path)
    scope = _scope(200_000, 1_400_000)
    manifest = build_shadow_preview(
        source, scope, _PLAN_DIGEST, max_keyframes=5, source_duration_us=2_000_000
    )

    assert len(manifest.keyframes) == 5
    assert manifest.scope_digest == scope.scope_digest
    assert manifest.scope_start_us == 200_000
    assert manifest.scope_end_us == 1_400_000
    assert manifest.source_duration_us == 2_000_000

    for keyframe in manifest.keyframes:
        assert isinstance(keyframe, ShadowKeyframe)
        raw = base64.b64decode(keyframe.jpeg_b64)
        # the frame digest is the sha256 of the exact bytes ffmpeg emitted
        assert keyframe.frame_digest == "sha256:" + hashlib.sha256(raw).hexdigest()
        assert raw[:2] == b"\xff\xd8"  # JPEG SOI marker
        with Image.open(io.BytesIO(raw)) as image:
            assert image.format == "JPEG"
            assert image.height == 480  # default target height (Spec §2.3)
            image.load()

    # composite_digest is an independent, order-independent recomputation
    assert manifest.composite_digest == _composite_of(manifest.keyframes)
    # ...and the whole-manifest digest is a *different* concept (Spec §2.3)
    assert manifest.shadow_digest != manifest.composite_digest


def test_sampling_is_half_open_and_inside_the_scope(tmp_path: Path) -> None:
    source = _clip(tmp_path)
    scope = _scope(300_000, 1_700_000)
    manifest = build_shadow_preview(source, scope, _PLAN_DIGEST, max_keyframes=6)

    timestamps = [kf.timestamp_us for kf in manifest.keyframes]
    assert timestamps == sorted(timestamps)
    assert timestamps[0] == scope.start_us
    # half-open [start, end): no sample is ever taken at or beyond the end
    assert all(scope.start_us <= t < scope.end_us for t in timestamps)


def test_composite_digest_is_order_independent(tmp_path: Path) -> None:
    source = _clip(tmp_path)
    manifest = build_shadow_preview(source, _scope(0, 1_200_000), _PLAN_DIGEST, max_keyframes=4)
    assert manifest.composite_digest == _composite_of(reversed(manifest.keyframes))


def test_distinct_scopes_yield_distinct_manifests(tmp_path: Path) -> None:
    source = _clip(tmp_path)
    first = build_shadow_preview(source, _scope(0, 1_000_000), _PLAN_DIGEST, max_keyframes=4)
    second = build_shadow_preview(
        source, _scope(1_000_000, 2_000_000), _PLAN_DIGEST, max_keyframes=4
    )

    assert first.scope_digest != second.scope_digest
    assert first.composite_digest != second.composite_digest


def test_unresolvable_ffmpeg_binary_is_a_typed_error(tmp_path: Path) -> None:
    source = _clip(tmp_path)
    with pytest.raises(ShadowRenderError) as error:
        build_shadow_preview(
            source, _scope(0, 1_000_000), _PLAN_DIGEST, ffmpeg_bin="/nonexistent/ffmpeg-xyz"
        )
    assert error.value.reason == "ffmpeg_not_found"


def test_non_video_input_yields_a_typed_empty_frame_error(tmp_path: Path) -> None:
    bogus = tmp_path / "bogus.mp4"
    bogus.write_bytes(b"this is definitely not a video container" * 64)
    with pytest.raises(ShadowRenderError) as error:
        build_shadow_preview(bogus, _scope(0, 1_000_000), _PLAN_DIGEST, max_keyframes=1)
    assert error.value.reason == "empty_frame"
