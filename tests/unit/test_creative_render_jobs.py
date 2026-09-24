"""Worker-side ``creative_render`` adapter tests (task-166, P0-B).

Proves the queue side of the canonical chain end-to-end:

    validated payload → packs runtime registry (capability lookup)
    → CommandBus dispatch (typed operation) → render lane (FFmpeg)
    → measured artifact (probe + sha256)

The render tests encode a real 2-second clip with the imageio-ffmpeg wheel
binary (the dev/test convenience in the documented binary resolution chain),
so no codec, filter or timing claim is mocked. Caption tests fake only the
``CaptionEnginePort`` (the engine itself is an optional extra).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.creative.render_jobs import (
    CREATIVE_RENDER_JOB_TYPE,
    CreativeRenderPayload,
    creative_render_job,
)
from nexus_ai_agent.creative.slideshow.ffmpeg import resolve_ffmpeg_bin


def _payload(workspace: Path, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "command": "edit",
        "operation": "trim",
        "args": ["0", "1"],
        "workspace_dir": str(workspace),
        "input_path": str(workspace / "input.mp4"),
        "media_duration_us": 2_000_000,
        "user_id": 42,
        "chat_id": 4242,
        "lang": "fa",
        "idempotency_key": "creative:42:4242:111",
    }
    base.update(overrides)
    return base


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A job workspace under a private creative temp root (payload trust boundary)."""
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(tmp_path))
    settings_module.get_settings.cache_clear()
    wd = tmp_path / "creative_ab12cd34ef56"
    wd.mkdir(parents=True)
    yield wd  # type: ignore[misc]
    settings_module.get_settings.cache_clear()


def _make_clip(path: Path, *, seconds: int = 2) -> None:
    """Encode a deterministic test clip with the resolved FFmpeg binary."""
    binary = resolve_ffmpeg_bin()
    subprocess.run(
        [
            binary,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={seconds}:size=320x240:rate=15",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(path),
        ],
        check=True,
        timeout=120,
    )


def _run(payload: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(creative_render_job(payload))


# ── payload contract (trust boundary: a hand-crafted queue row must fail) ──


def test_payload_rejects_extra_keys(workspace: Path) -> None:
    from pydantic import ValidationError

    data = _payload(workspace, sneaky="/etc/passwd")
    with pytest.raises(ValidationError):
        CreativeRenderPayload.model_validate(data)


def test_workspace_must_stay_under_creative_temp(workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path / ".." / "outside_ws"
    data = _payload(workspace, workspace_dir=str(outside))
    result = _run(data)
    assert result["success"] is False
    assert result["error_code"] == "invalid_request"


def test_unknown_operation_is_a_typed_failure(workspace: Path) -> None:
    (workspace / "input.mp4").write_bytes(b"not-a-video")
    result = _run(_payload(workspace, operation="nonsense"))
    assert result["success"] is False
    assert result["error_code"] == "unsupported_operation"


def test_missing_media_is_a_typed_failure(workspace: Path) -> None:
    result = _run(_payload(workspace, operation="trim", args=["0", "1"]))
    assert result["success"] is False
    assert result["error_code"] == "media_missing"


def test_job_type_constant_is_wired_to_the_worker_registry() -> None:
    from nexus_ai_agent.worker import default_job_handlers

    handlers = default_job_handlers()
    assert CREATIVE_RENDER_JOB_TYPE in handlers


# ── real render executions (trim / speed / reverse / exposure / proxy) ────────


@pytest.mark.parametrize(
    ("operation", "args", "expect_op"),
    [
        ("trim", ["0", "1"], "timeline.trim"),
        ("speed", ["2"], "timeline.speed_ramp"),
        ("reverse", [], "timeline.reverse_segment"),
    ],
)
def test_edit_operations_render_real_artifacts(
    workspace: Path, operation: str, args: list[str], expect_op: str
) -> None:
    _make_clip(workspace / "input.mp4")
    result = _run(_payload(workspace, operation=operation, args=args))
    assert result["success"] is True, result
    artifact = Path(result["artifact_path"])
    assert artifact.exists() and artifact.stat().st_size > 0
    assert result["artifact_kind"] == "video"
    assert result["operation"] == expect_op
    assert result["sha256"].startswith("sha256:")
    # duration algebra (integer microseconds): trim 0→1 s of a 2 s clip ≈ 1 s
    if operation == "trim":
        assert 700_000 <= result["duration_us"] <= 1_300_000
    elif operation == "speed":
        assert 700_000 <= result["duration_us"] <= 1_300_000  # 2 s at 2x ≈ 1 s
    else:
        assert 1_800_000 <= result["duration_us"] <= 2_200_000  # reverse ≈ source


def test_grade_exposure_renders(workspace: Path) -> None:
    _make_clip(workspace / "input.mp4")
    result = _run(
        _payload(
            workspace, command="grade", operation="exposure", args=["1.0"], allow_experimental=True
        )
    )
    assert result["success"] is True, result
    assert result["operation"] == "color.adjust_exposure"


def test_grade_proxy_renders_480p(workspace: Path) -> None:
    _make_clip(workspace / "input.mp4")
    result = _run(
        _payload(workspace, command="grade", operation="proxy", args=[], allow_experimental=True)
    )
    assert result["success"] is True, result
    assert result["operation"] == "delivery.make_proxy_480p"
    assert result.get("height") == 480


def test_experimental_pack_needs_the_payload_opt_in(workspace: Path) -> None:
    """The capability-lifecycle gate reaches the queue: a grade job without
    ``allow_experimental`` fails typed, naming the gate."""
    _make_clip(workspace / "input.mp4")
    result = _run(_payload(workspace, command="grade", operation="exposure", args=["1.0"]))
    assert result["success"] is False
    assert result["error_code"] == "invalid_request"
    assert "experimental" in result["error_detail"]


def test_trim_rejects_bad_points_as_typed_failure(workspace: Path) -> None:
    _make_clip(workspace / "input.mp4")
    result = _run(_payload(workspace, operation="trim", args=["5", "1"]))
    assert result["success"] is False
    assert result["error_code"] == "invalid_request"


def test_lut_is_not_silently_accepted(workspace: Path) -> None:
    """LUT grading was dropped from the surface: no .cube assets ship and the
    lane has no LUT op — a hand-crafted payload must fail typed, never fake it."""
    _make_clip(workspace / "input.mp4")
    result = _run(_payload(workspace, command="grade", operation="lut", args=[]))
    assert result["success"] is False
    assert result["error_code"] == "unsupported_operation"


# ── OTIO export: a real document artifact, no render needed ──────────────────


def test_grade_otio_writes_a_document(workspace: Path) -> None:
    _make_clip(workspace / "input.mp4")
    result = _run(
        _payload(workspace, command="grade", operation="otio", args=[], allow_experimental=True)
    )
    assert result["success"] is True, result
    artifact = Path(result["artifact_path"])
    assert artifact.suffix == ".otio" and artifact.exists()
    doc = json.loads(artifact.read_text(encoding="utf-8"))
    assert doc["OTIO_SCHEMA"] == "Timeline.1"
    assert doc["tracks"]["children"][0]["children"], "expected at least one clip"
    assert result["artifact_kind"] == "document"


# ── caption ops: fail-closed by default, real when an engine is present ──────


def test_caption_fails_closed_without_engine(workspace: Path) -> None:
    _make_clip(workspace / "input.mp4")
    result = _run(_payload(workspace, command="caption", operation="transcribe", args=[]))
    assert result["success"] is False
    assert result["error_code"] == "caption_profile_unavailable"


def test_caption_transcribe_with_engine_serves_srt(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With an available engine, the chain is canonical:
    caption.transcribe → caption.generate_srt → .srt artifact."""
    from nexus_ai_agent.creative.packs.caption.models import TranscriptRef, TranscriptSegment

    class _FakeEngine:
        def is_available(self) -> bool:
            return True

        async def transcribe(self, path: Any, *, language: str | None = None) -> TranscriptRef:
            return TranscriptRef(
                transcript_id="t-1",
                language=language or "fa",
                segments=(
                    TranscriptSegment(start_us=0, end_us=500_000, text="سلام دنیا"),
                    TranscriptSegment(start_us=500_000, end_us=1_000_000, text="تست دوم"),
                ),
            )

    monkeypatch.setattr(
        "nexus_ai_agent.creative.render_jobs._default_caption_engine", lambda: _FakeEngine()
    )
    _make_clip(workspace / "input.mp4")
    result = _run(_payload(workspace, command="caption", operation="transcribe", args=[]))
    assert result["success"] is True, result
    artifact = Path(result["artifact_path"])
    assert artifact.suffix == ".srt" and artifact.exists()
    text = artifact.read_text(encoding="utf-8")
    assert "سلام دنیا" in text and "00:00:00,000" in text
    assert result["artifact_kind"] == "document"


def test_caption_burnin_is_typed_unsupported(workspace: Path) -> None:
    """Burn-in needs a subtitles op in the lane IR — none exists. Typed, never faked."""
    _make_clip(workspace / "input.mp4")
    result = _run(_payload(workspace, command="caption", operation="burnin", args=[]))
    assert result["success"] is False
    assert result["error_code"] == "unsupported_operation"
