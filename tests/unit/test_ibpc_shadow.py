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
        with pytest.raises(ValidationError):
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
        with (
            mock.patch("subprocess.run", side_effect=_fake_ffmpeg_success),
            mock.patch(
                "nexus_ai_agent.nagar.ibpc.shadow.resolve_ffmpeg_bin", return_value="ffmpeg"
            ),
        ):
            m = build_shadow_preview(
                src,
                _make_scope(),
                PLAN_DIGEST,
                max_keyframes=3,
                source_duration_us=DURATION,
            )
        assert len(m.keyframes) == 3
        assert m.plan_digest == PLAN_DIGEST
        assert m.scope_digest == _make_scope().scope_digest
        assert m.composite_digest.startswith("sha256:")

    def test_composite_digest_deterministic(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.shadow import build_shadow_preview

        src = tmp_path / "v.mp4"
        src.write_bytes(b"fake")
        with (
            mock.patch("subprocess.run", side_effect=_fake_ffmpeg_success),
            mock.patch(
                "nexus_ai_agent.nagar.ibpc.shadow.resolve_ffmpeg_bin", return_value="ffmpeg"
            ),
        ):
            m1 = build_shadow_preview(
                src, _make_scope(), PLAN_DIGEST, max_keyframes=2, source_duration_us=DURATION
            )
            m2 = build_shadow_preview(
                src, _make_scope(), PLAN_DIGEST, max_keyframes=2, source_duration_us=DURATION
            )
        assert m1.composite_digest == m2.composite_digest

    def test_keyframe_digest_matches_jpeg_bytes(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.shadow import build_shadow_preview

        src = tmp_path / "v.mp4"
        src.write_bytes(b"fake")
        with (
            mock.patch("subprocess.run", side_effect=_fake_ffmpeg_success),
            mock.patch(
                "nexus_ai_agent.nagar.ibpc.shadow.resolve_ffmpeg_bin", return_value="ffmpeg"
            ),
        ):
            m = build_shadow_preview(
                src, _make_scope(), PLAN_DIGEST, max_keyframes=1, source_duration_us=DURATION
            )
        kf = m.keyframes[0]
        expected_digest = "sha256:" + hashlib.sha256(FAKE_JPEG).hexdigest()
        assert kf.frame_digest == expected_digest
        assert base64.b64decode(kf.jpeg_b64) == FAKE_JPEG

    def test_empty_frame_raises(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.shadow import ShadowRenderError, build_shadow_preview

        src = tmp_path / "v.mp4"
        src.write_bytes(b"fake")
        with (
            mock.patch("subprocess.run", side_effect=_fake_ffmpeg_empty),
            mock.patch(
                "nexus_ai_agent.nagar.ibpc.shadow.resolve_ffmpeg_bin", return_value="ffmpeg"
            ),
            pytest.raises(ShadowRenderError) as exc,
        ):
            build_shadow_preview(
                src, _make_scope(), PLAN_DIGEST, max_keyframes=1, source_duration_us=DURATION
            )
        assert exc.value.reason == "empty_frame"

    def test_ffmpeg_not_found_raises(self) -> None:
        from nexus_ai_agent.creative.slideshow.ffmpeg import FfmpegUnavailableError
        from nexus_ai_agent.nagar.ibpc.shadow import ShadowRenderError, build_shadow_preview

        # the resolver raises FfmpegUnavailableError (a RuntimeError, NOT an OSError)
        with (
            mock.patch(
                "nexus_ai_agent.nagar.ibpc.shadow.resolve_ffmpeg_bin",
                side_effect=FfmpegUnavailableError("none"),
            ),
            pytest.raises(ShadowRenderError) as exc,
        ):
            build_shadow_preview(
                Path("/nonexistent.mp4"), _make_scope(), PLAN_DIGEST, source_duration_us=DURATION
            )
        assert exc.value.reason == "ffmpeg_not_found"

    def test_max_keyframes_too_low_raises(self) -> None:
        from nexus_ai_agent.nagar.ibpc.shadow import build_shadow_preview

        with pytest.raises(ValueError):
            build_shadow_preview(
                Path("/x.mp4"),
                _make_scope(),
                PLAN_DIGEST,
                max_keyframes=0,
                source_duration_us=DURATION,
            )

    def test_max_keyframes_too_high_raises(self) -> None:
        from nexus_ai_agent.nagar.ibpc.shadow import build_shadow_preview

        with pytest.raises(ValueError):
            build_shadow_preview(
                Path("/x.mp4"),
                _make_scope(),
                PLAN_DIGEST,
                max_keyframes=17,
                source_duration_us=DURATION,
            )

    def test_scope_stored_in_manifest(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.shadow import build_shadow_preview

        src = tmp_path / "v.mp4"
        src.write_bytes(b"fake")
        scope = _make_scope(500_000, 3_000_000)
        with (
            mock.patch("subprocess.run", side_effect=_fake_ffmpeg_success),
            mock.patch(
                "nexus_ai_agent.nagar.ibpc.shadow.resolve_ffmpeg_bin", return_value="ffmpeg"
            ),
        ):
            m = build_shadow_preview(
                src, scope, PLAN_DIGEST, max_keyframes=2, source_duration_us=DURATION
            )
        assert m.scope_start_us == 500_000
        assert m.scope_end_us == 3_000_000


# ---------------------------------------------------------------------------
# Spec §2 conformance: schema id, half-open sampling, guards, digest, security
# ---------------------------------------------------------------------------


class TestShadowSpecConformance:
    def test_manifest_has_schema_and_shadow_id(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.shadow import build_shadow_preview

        src = tmp_path / "v.mp4"
        src.write_bytes(b"fake")
        with (
            mock.patch("subprocess.run", side_effect=_fake_ffmpeg_success),
            mock.patch(
                "nexus_ai_agent.nagar.ibpc.shadow.resolve_ffmpeg_bin", return_value="ffmpeg"
            ),
        ):
            m = build_shadow_preview(
                src, _make_scope(), PLAN_DIGEST, max_keyframes=1, source_duration_us=DURATION
            )
        assert m.schema_id == "nexus.ibpc.shadow_preview.v1"
        assert m.shadow_id.startswith("shad_")

    def test_keyframe_and_manifest_live_in_models_module(self) -> None:
        from nexus_ai_agent.nagar.ibpc import models as models_mod

        assert "ShadowKeyframe" in models_mod.__all__
        assert "ShadowPreviewManifest" in models_mod.__all__

    def test_sampling_is_half_open(self) -> None:
        from nexus_ai_agent.nagar.ibpc.shadow import _sample_timestamps

        ts = _sample_timestamps(1_000_000, 5_000_000, 4)
        assert ts == [1_000_000, 2_000_000, 3_000_000, 4_000_000]
        assert all(t < 5_000_000 for t in ts)  # never at/after end

    def test_sampling_single_sample_is_inside(self) -> None:
        from nexus_ai_agent.nagar.ibpc.shadow import _sample_timestamps

        assert _sample_timestamps(1_000_000, 4_000_000, 1) == [1_000_000]

    def test_defaults_are_spec_values(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc import shadow as shadow_mod

        assert shadow_mod._DEFAULT_MAX_KEYFRAMES == 8
        assert shadow_mod._DEFAULT_JPEG_QUALITY == 23
        assert shadow_mod._DEFAULT_TARGET_HEIGHT == 480

    def test_empty_scope_raises(self, tmp_path: Path) -> None:
        from types import SimpleNamespace

        from nexus_ai_agent.nagar.ibpc.shadow import ShadowRenderError, build_shadow_preview

        src = tmp_path / "v.mp4"
        src.write_bytes(b"fake")
        bad = SimpleNamespace(
            start_us=2_000_000, end_us=2_000_000, scope_digest="sha256:" + "0" * 64
        )
        with (
            mock.patch(
                "nexus_ai_agent.nagar.ibpc.shadow.resolve_ffmpeg_bin", return_value="ffmpeg"
            ),
            pytest.raises(ShadowRenderError) as exc,
        ):
            build_shadow_preview(src, bad, PLAN_DIGEST, max_keyframes=1)
        assert exc.value.reason == "empty_scope"

    def test_timeout_translated_to_shadow_error(self, tmp_path: Path) -> None:
        import subprocess

        from nexus_ai_agent.nagar.ibpc.shadow import ShadowRenderError, build_shadow_preview

        src = tmp_path / "v.mp4"
        src.write_bytes(b"fake")
        with (
            mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ffmpeg", 30)),
            mock.patch(
                "nexus_ai_agent.nagar.ibpc.shadow.resolve_ffmpeg_bin", return_value="ffmpeg"
            ),
            pytest.raises(ShadowRenderError) as exc,
        ):
            build_shadow_preview(src, _make_scope(), PLAN_DIGEST, max_keyframes=1)
        assert exc.value.reason == "timeout"

    def test_composite_digest_matches_spec_formula(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.shadow import build_shadow_preview

        src = tmp_path / "v.mp4"
        src.write_bytes(b"fake")
        with (
            mock.patch("subprocess.run", side_effect=_fake_ffmpeg_success),
            mock.patch(
                "nexus_ai_agent.nagar.ibpc.shadow.resolve_ffmpeg_bin", return_value="ffmpeg"
            ),
        ):
            m = build_shadow_preview(
                src, _make_scope(), PLAN_DIGEST, max_keyframes=3, source_duration_us=DURATION
            )
        expected = (
            "sha256:"
            + hashlib.sha256(
                b"".join(sorted(fd.encode() for fd in (kf.frame_digest for kf in m.keyframes)))
            ).hexdigest()
        )
        assert m.composite_digest == expected

    def test_composite_digest_order_independent(self) -> None:
        from nexus_ai_agent.nagar.ibpc.models import ShadowKeyframe
        from nexus_ai_agent.nagar.ibpc.shadow import _compute_composite_digest

        a = ShadowKeyframe(timestamp_us=0, jpeg_b64="AA==", frame_digest="sha256:" + "a" * 64)
        b = ShadowKeyframe(timestamp_us=1, jpeg_b64="AA==", frame_digest="sha256:" + "b" * 64)
        assert _compute_composite_digest((a, b)) == _compute_composite_digest((b, a))

    def test_no_shell_true_and_list_args(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.shadow import build_shadow_preview

        src = tmp_path / "v.mp4"
        src.write_bytes(b"fake")
        captured: dict = {}

        def _spy(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return _fake_ffmpeg_success()

        with (
            mock.patch("subprocess.run", side_effect=_spy),
            mock.patch(
                "nexus_ai_agent.nagar.ibpc.shadow.resolve_ffmpeg_bin", return_value="ffmpeg"
            ),
        ):
            build_shadow_preview(src, _make_scope(), PLAN_DIGEST, max_keyframes=1)
        assert isinstance(captured["cmd"], list)
        assert captured["kwargs"].get("shell") is None  # never shell=True
        assert "timeout" in captured["kwargs"]

    def test_not_found_error_does_not_leak(self) -> None:
        from nexus_ai_agent.creative.slideshow.ffmpeg import FfmpegUnavailableError
        from nexus_ai_agent.nagar.ibpc.shadow import ShadowRenderError, build_shadow_preview

        with (
            mock.patch(
                "nexus_ai_agent.nagar.ibpc.shadow.resolve_ffmpeg_bin",
                side_effect=FfmpegUnavailableError("none"),
            ),
            pytest.raises(ShadowRenderError) as exc,
        ):
            build_shadow_preview(
                Path("/nonexistent.mp4"), _make_scope(), PLAN_DIGEST, max_keyframes=1
            )
        assert exc.value.reason == "ffmpeg_not_found"


# ---------------------------------------------------------------------------
# Stage 3 integrity (NAGAR-IBPC-TRIAD-CLOSURE-01): partial output, tampering,
# forged digests and the consumer-side verifier.
# ---------------------------------------------------------------------------

FAKE_JPEG_OTHER = b"\xff\xd8\xff\xe0" + b"\x01" * 50 + b"\xff\xd9"


def _ffmpeg_result(stdout: bytes, returncode: int):
    def _run(*args, **kwargs):
        result = mock.Mock()
        result.stdout = stdout
        result.returncode = returncode
        return result

    return _run


def _build_manifest(
    tmp_path: Path, *, scope=None, source_duration_us: int = DURATION, max_keyframes: int = 3
):
    from nexus_ai_agent.nagar.ibpc.shadow import build_shadow_preview

    src = tmp_path / "v.mp4"
    src.write_bytes(b"fake")
    with (
        mock.patch("subprocess.run", side_effect=_fake_ffmpeg_success),
        mock.patch("nexus_ai_agent.nagar.ibpc.shadow.resolve_ffmpeg_bin", return_value="ffmpeg"),
    ):
        return build_shadow_preview(
            src,
            scope or _make_scope(),
            PLAN_DIGEST,
            max_keyframes=max_keyframes,
            source_duration_us=source_duration_us,
        )


def _render_error_for(stdout: bytes, returncode: int, tmp_path: Path) -> str:
    from nexus_ai_agent.nagar.ibpc.shadow import ShadowRenderError, build_shadow_preview

    src = tmp_path / "v.mp4"
    src.write_bytes(b"fake")
    with (
        mock.patch("subprocess.run", side_effect=_ffmpeg_result(stdout, returncode)),
        mock.patch("nexus_ai_agent.nagar.ibpc.shadow.resolve_ffmpeg_bin", return_value="ffmpeg"),
        pytest.raises(ShadowRenderError) as exc,
    ):
        build_shadow_preview(src, _make_scope(), PLAN_DIGEST, max_keyframes=2)
    return exc.value.reason


class TestShadowIntegrity:
    def test_nonzero_exit_with_bytes_is_rejected_as_partial_output(self, tmp_path: Path) -> None:
        assert _render_error_for(FAKE_JPEG, 1, tmp_path) == "ffmpeg_failed"

    def test_truncated_jpeg_without_eoi_is_rejected(self, tmp_path: Path) -> None:
        truncated = b"\xff\xd8\xff\xe0" + b"x" * 40
        assert _render_error_for(truncated, 0, tmp_path) == "invalid_jpeg"

    def test_built_manifest_passes_its_own_verifier(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.shadow import verify_shadow_manifest

        verify_shadow_manifest(_build_manifest(tmp_path))

    def test_forged_frame_digest_is_refused(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.shadow import ShadowRenderError, verify_shadow_manifest

        m = _build_manifest(tmp_path)
        forged = m.keyframes[0].model_copy(update={"frame_digest": "sha256:" + "0" * 64})
        bad = m.model_copy(update={"keyframes": (forged,) + m.keyframes[1:]})
        with pytest.raises(ShadowRenderError) as exc:
            verify_shadow_manifest(bad)
        assert exc.value.reason == "frame_digest_mismatch"

    def test_swapped_frame_bytes_under_original_digest_are_refused(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.shadow import ShadowRenderError, verify_shadow_manifest

        m = _build_manifest(tmp_path)
        other_b64 = base64.b64encode(FAKE_JPEG_OTHER).decode("ascii")
        swapped = m.keyframes[0].model_copy(update={"jpeg_b64": other_b64})
        bad = m.model_copy(update={"keyframes": (swapped,) + m.keyframes[1:]})
        with pytest.raises(ShadowRenderError) as exc:
            verify_shadow_manifest(bad)
        assert exc.value.reason == "frame_digest_mismatch"

    def test_undecodable_base64_is_refused(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.shadow import ShadowRenderError, verify_shadow_manifest

        m = _build_manifest(tmp_path)
        broken = m.keyframes[0].model_copy(update={"jpeg_b64": "!!!not-base64!!!"})
        bad = m.model_copy(update={"keyframes": (broken,) + m.keyframes[1:]})
        with pytest.raises(ShadowRenderError) as exc:
            verify_shadow_manifest(bad)
        assert exc.value.reason == "tampered_frame"

    def test_forged_composite_digest_is_refused(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.shadow import ShadowRenderError, verify_shadow_manifest

        m = _build_manifest(tmp_path)
        bad = m.model_copy(update={"composite_digest": "sha256:" + "1" * 64})
        with pytest.raises(ShadowRenderError) as exc:
            verify_shadow_manifest(bad)
        assert exc.value.reason == "composite_digest_mismatch"

    def test_timestamp_at_scope_end_is_refused_half_open(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.shadow import ShadowRenderError, verify_shadow_manifest

        m = _build_manifest(tmp_path)
        late = m.keyframes[-1].model_copy(update={"timestamp_us": SCOPE_END})
        bad = m.model_copy(update={"keyframes": m.keyframes[:-1] + (late,)})
        with pytest.raises(ShadowRenderError) as exc:
            verify_shadow_manifest(bad)
        assert exc.value.reason == "timestamp_out_of_scope"

    def test_empty_manifest_is_refused(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.shadow import ShadowRenderError, verify_shadow_manifest

        m = _build_manifest(tmp_path)
        with pytest.raises(ShadowRenderError) as exc:
            verify_shadow_manifest(m.model_copy(update={"keyframes": ()}))
        assert exc.value.reason == "empty_manifest"

    def test_in_scope_retimestamp_is_refused_by_the_schedule(self, tmp_path: Path) -> None:
        """Retiming a keyframe inside the scope breaks the deterministic schedule.
        composite_digest (what approvals bind, Spec §2.4) covers frame bytes only, so it
        is unchanged by the retime; the verifier refuses the manifest on the schedule."""
        from nexus_ai_agent.nagar.ibpc.shadow import ShadowRenderError, verify_shadow_manifest

        m = _build_manifest(tmp_path)
        first = m.keyframes[0]
        retimed_first = first.model_copy(update={"timestamp_us": first.timestamp_us + 1})
        retimed = m.model_copy(update={"keyframes": (retimed_first,) + m.keyframes[1:]})
        with pytest.raises(ShadowRenderError) as exc:
            verify_shadow_manifest(retimed)
        assert exc.value.reason == "timestamp_off_schedule"
        assert retimed.composite_digest == m.composite_digest
        assert retimed.shadow_digest != m.shadow_digest

    def test_tiny_scope_never_produces_colliding_timestamps(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.shadow import verify_shadow_manifest

        m = _build_manifest(tmp_path, scope=_make_scope(0, 3), max_keyframes=8)
        ts = [kf.timestamp_us for kf in m.keyframes]
        assert len(ts) == 3
        assert ts == sorted(set(ts))
        assert all(0 <= t < 3 for t in ts)
        verify_shadow_manifest(m)

    def test_scope_beyond_known_source_duration_is_refused(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.shadow import ShadowRenderError

        with pytest.raises(ShadowRenderError) as exc:
            _build_manifest(tmp_path, scope=_make_scope(0, 2_000_000), source_duration_us=1_000_000)
        assert exc.value.reason == "scope_outside_source"
