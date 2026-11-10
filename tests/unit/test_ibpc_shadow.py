"""Unit tests: Shadow Preview — visual keyframe preview from actual pixels."""
from __future__ import annotations

import base64
import hashlib
import unittest.mock as mock
from pathlib import Path

import pytest
from pydantic import ValidationError


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------

class TestShadowModels:
    def test_keyframe_rejects_bad_digest(self) -> None:
        from nexus_ai_agent.nagar.ibpc.shadow import ShadowKeyframe
        with pytest.raises(ValidationError):
            ShadowKeyframe(timestamp_us=0, jpeg_b64="AA==", frame_digest="not_a_digest")

    def test_keyframe_accepts_valid(self) -> None:
        from nexus_ai_agent.nagar.ibpc.shadow import ShadowKeyframe
        kf = ShadowKeyframe(
            timestamp_us=500_000,
            jpeg_b64="AA==",
            frame_digest="sha256:" + "a" * 64,
        )
        assert kf.timestamp_us == 500_000

    def test_keyframe_is_frozen(self) -> None:
        from nexus_ai_agent.nagar.ibpc.shadow import ShadowKeyframe
        kf = ShadowKeyframe(
            timestamp_us=0,
            jpeg_b64="AA==",
            frame_digest="sha256:" + "b" * 64,
        )
        with pytest.raises(Exception):
            kf.timestamp_us = 999  # type: ignore[misc]

    def test_shadow_render_error_has_reason(self) -> None:
        from nexus_ai_agent.nagar.ibpc.shadow import ShadowRenderError
        err = ShadowRenderError("ffmpeg_not_found")
        assert err.reason == "ffmpeg_not_found"
        assert isinstance(err, RuntimeError)

    def test_shadow_render_error_with_detail(self) -> None:
        from nexus_ai_agent.nagar.ibpc.shadow import ShadowRenderError
        err = ShadowRenderError("empty_frame", "at t=1.5s")
        assert "empty_frame" in str(err)
        assert "at t=1.5s" in str(err)

    def test_manifest_has_shadow_digest_property(self) -> None:
        from nexus_ai_agent.nagar.ibpc.shadow import ShadowKeyframe, ShadowPreviewManifest
        kf = ShadowKeyframe(
            timestamp_us=0,
            jpeg_b64="AA==",
            frame_digest="sha256:" + "c" * 64,
        )
        m = ShadowPreviewManifest(
            plan_digest="sha256:" + "d" * 64,
            scope_digest="sha256:" + "e" * 64,
            source_duration_us=5_000_000,
            scope_start_us=0,
            scope_end_us=2_000_000,
            keyframes=(kf,),
            composite_digest="sha256:" + "f" * 64,
        )
        assert m.shadow_digest.startswith("sha256:")
        assert len(m.shadow_digest) == 71  # "sha256:" + 64 hex chars


# ---------------------------------------------------------------------------
# build_shadow_preview tests (with mocked ffmpeg)
# ---------------------------------------------------------------------------

SCOPE_START = 0
SCOPE_END = 2_000_000
DURATION = 5_000_000
PLAN_DIGEST = "sha256:" + "b" * 64
FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 50 + b"\xff\xd9"  # minimal valid JPEG bytes


def _make_scope(start_us: int = SCOPE_START, end_us: int = SCOPE_END):
    from nexus_ai_agent.nagar.ibpc.models import VideoTimeScope
    return VideoTimeScope(asset_id="a1", effect="keep", start_us=start_us, end_us=end_us)


def _fake_ffmpeg_success(*args, **kwargs):
    result = mock.Mock()
    result.stdout = FAKE_JPEG
    result.returncode = 0
    return result


def _fake_ffmpeg_empty(*args, **kwargs):
    result = mock.Mock()
    result.stdout = b""
    result.returncode = 0
    return result


class TestBuildShadowPreview:
    def test_returns_manifest(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.shadow import build_shadow_preview
        src = tmp_path / "v.mp4"
        src.write_bytes(b"fake")
        with mock.patch("subprocess.run", side_effect=_fake_ffmpeg_success), \
             mock.patch("nexus_ai_agent.nagar.ibpc.shadow.resolve_ffmpeg_bin", return_value="ffmpeg"):
            m = build_shadow_preview(
                src, _make_scope(), PLAN_DIGEST,
                max_keyframes=3, source_duration_us=DURATION,
            )
        assert len(m.keyframes) == 3
        assert m.plan_digest == PLAN_DIGEST
        assert m.scope_digest == _make_scope().scope_digest
        assert m.composite_digest.startswith("sha256:")

    def test_composite_digest_deterministic(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.shadow import build_shadow_preview
        src = tmp_path / "v.mp4"
        src.write_bytes(b"fake")
        with mock.patch("subprocess.run", side_effect=_fake_ffmpeg_success), \
             mock.patch("nexus_ai_agent.nagar.ibpc.shadow.resolve_ffmpeg_bin", return_value="ffmpeg"):
            m1 = build_shadow_preview(src, _make_scope(), PLAN_DIGEST, max_keyframes=2, source_duration_us=DURATION)
            m2 = build_shadow_preview(src, _make_scope(), PLAN_DIGEST, max_keyframes=2, source_duration_us=DURATION)
        assert m1.composite_digest == m2.composite_digest

    def test_keyframe_digest_matches_jpeg_bytes(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.shadow import build_shadow_preview
        src = tmp_path / "v.mp4"
        src.write_bytes(b"fake")
        with mock.patch("subprocess.run", side_effect=_fake_ffmpeg_success), \
             mock.patch("nexus_ai_agent.nagar.ibpc.shadow.resolve_ffmpeg_bin", return_value="ffmpeg"):
            m = build_shadow_preview(src, _make_scope(), PLAN_DIGEST, max_keyframes=1, source_duration_us=DURATION)
        kf = m.keyframes[0]
        expected_digest = "sha256:" + hashlib.sha256(FAKE_JPEG).hexdigest()
        assert kf.frame_digest == expected_digest
        assert base64.b64decode(kf.jpeg_b64) == FAKE_JPEG

    def test_empty_frame_raises(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.shadow import build_shadow_preview, ShadowRenderError
        src = tmp_path / "v.mp4"
        src.write_bytes(b"fake")
        with mock.patch("subprocess.run", side_effect=_fake_ffmpeg_empty), \
             mock.patch("nexus_ai_agent.nagar.ibpc.shadow.resolve_ffmpeg_bin", return_value="ffmpeg"), \
             pytest.raises(ShadowRenderError) as exc:
            build_shadow_preview(src, _make_scope(), PLAN_DIGEST, max_keyframes=1, source_duration_us=DURATION)
        assert exc.value.reason == "empty_frame"

    def test_ffmpeg_not_found_raises(self) -> None:
        from nexus_ai_agent.nagar.ibpc.shadow import build_shadow_preview, ShadowRenderError
        with mock.patch("nexus_ai_agent.nagar.ibpc.shadow.resolve_ffmpeg_bin", side_effect=FileNotFoundError), \
             pytest.raises(ShadowRenderError) as exc:
            build_shadow_preview(Path("/nonexistent.mp4"), _make_scope(), PLAN_DIGEST, source_duration_us=DURATION)
        assert exc.value.reason == "ffmpeg_not_found"

    def test_max_keyframes_too_low_raises(self) -> None:
        from nexus_ai_agent.nagar.ibpc.shadow import build_shadow_preview
        with pytest.raises(ValueError):
            build_shadow_preview(Path("/x.mp4"), _make_scope(), PLAN_DIGEST, max_keyframes=0, source_duration_us=DURATION)

    def test_max_keyframes_too_high_raises(self) -> None:
        from nexus_ai_agent.nagar.ibpc.shadow import build_shadow_preview
        with pytest.raises(ValueError):
            build_shadow_preview(Path("/x.mp4"), _make_scope(), PLAN_DIGEST, max_keyframes=17, source_duration_us=DURATION)

    def test_scope_stored_in_manifest(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.shadow import build_shadow_preview
        src = tmp_path / "v.mp4"
        src.write_bytes(b"fake")
        scope = _make_scope(500_000, 3_000_000)
        with mock.patch("subprocess.run", side_effect=_fake_ffmpeg_success), \
             mock.patch("nexus_ai_agent.nagar.ibpc.shadow.resolve_ffmpeg_bin", return_value="ffmpeg"):
            m = build_shadow_preview(src, scope, PLAN_DIGEST, max_keyframes=2, source_duration_us=DURATION)
        assert m.scope_start_us == 500_000
        assert m.scope_end_us == 3_000_000
