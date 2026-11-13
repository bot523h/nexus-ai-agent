"""WAVE 29 — tests for the real FFmpeg media sine.

The encode/probe tests execute a real ``ffmpeg``/``ffprobe`` when both binaries
are on PATH (they are skipped otherwise). Error branches that depend on binary
availability or subprocess failure are exercised with a monkeypatched
``shutil.which``/``subprocess.run`` — this is the one place a mock is used, and
it is necessary because the failure modes cannot be produced deterministically
with a working FFmpeg install.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nexus_ai_agent.creative.atlas import media_sine as m
from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem

HAS_FFMPEG = m.ffmpeg_available()
requires_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not on PATH")


@pytest.fixture()
def workspace(tmp_path: Path) -> WorkspaceFilesystem:
    return WorkspaceFilesystem(tmp_path)


def test_ffmpeg_available_is_a_bool() -> None:
    assert isinstance(m.ffmpeg_available(), bool)


def test_even_rounds_down_and_validates() -> None:
    assert m._even(128, "width") == 128
    assert m._even(129, "width") == 128
    with pytest.raises(m.MediaSineError):
        m._even(1, "width")
    with pytest.raises(m.MediaSineError):
        m._even(m.MAX_DIMENSION + 1, "width")
    with pytest.raises(m.MediaSineError):
        m._even(True, "width")  # type: ignore[arg-type]


def test_require_raises_when_binary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(m.shutil, "which", lambda _name: None)
    with pytest.raises(m.MediaSineUnavailable):
        m.require_binary("ffmpeg")


def test_run_rejects_nonpositive_timeout() -> None:
    with pytest.raises(m.MediaSineError):
        m.run_process(["true"], timeout=0.0)


def test_synthesise_refuses_missing_binary(
    workspace: WorkspaceFilesystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(m.shutil, "which", lambda _name: None)
    with pytest.raises(m.MediaSineUnavailable):
        m.synthesise_test_clip(workspace, "clip.mp4")


def test_probe_refuses_missing_binary(
    workspace: WorkspaceFilesystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(m.shutil, "which", lambda _name: None)
    with pytest.raises(m.MediaSineUnavailable):
        m.probe_media(workspace, "clip.mp4")


def test_refuse_bad_frames(workspace: WorkspaceFilesystem) -> None:
    with pytest.raises(m.MediaSineError):
        m.synthesise_test_clip(workspace, "c.mp4", frames=0)
    with pytest.raises(m.MediaSineError):
        m.synthesise_test_clip(workspace, "c.mp4", frames=m.MAX_FRAMES + 1)


def test_refuse_bad_fps(workspace: WorkspaceFilesystem) -> None:
    with pytest.raises(m.MediaSineError):
        m.synthesise_test_clip(workspace, "c.mp4", fps=0)
    with pytest.raises(m.MediaSineError):
        m.synthesise_test_clip(workspace, "c.mp4", fps=241)


def test_refuse_bad_codec(workspace: WorkspaceFilesystem) -> None:
    with pytest.raises(m.MediaSineError):
        m.synthesise_test_clip(workspace, "c.mp4", codec="prores")


def test_refuse_ffmpeg_failure(
    workspace: WorkspaceFilesystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(m.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        m.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "boom"),
    )
    with pytest.raises(m.MediaSineError):
        m.synthesise_test_clip(workspace, "c.mp4")


def test_refuse_success_without_output_file(
    workspace: WorkspaceFilesystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(m.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        m.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", ""),
    )
    with pytest.raises(m.MediaSineError):
        m.synthesise_test_clip(workspace, "c.mp4")


def test_refuse_probe_of_missing_file(workspace: WorkspaceFilesystem) -> None:
    if not HAS_FFMPEG:
        pytest.skip("ffprobe not on PATH")
    with pytest.raises(m.MediaSineError):
        m.probe_media(workspace, "does_not_exist.mp4")


def test_refuse_ffprobe_failure(
    workspace: WorkspaceFilesystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    (workspace.root / "c.mp4").write_bytes(b"not a video")
    monkeypatch.setattr(m.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        m.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "nope"),
    )
    with pytest.raises(m.MediaSineError):
        m.probe_media(workspace, "c.mp4")


def test_refuse_ffprobe_no_stream(
    workspace: WorkspaceFilesystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    (workspace.root / "c.mp4").write_bytes(b"x")
    monkeypatch.setattr(m.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        m.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, '{"streams": []}', ""),
    )
    with pytest.raises(m.MediaSineError):
        m.probe_media(workspace, "c.mp4")


def test_refuse_ffprobe_missing_frame_count(
    workspace: WorkspaceFilesystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    (workspace.root / "c.mp4").write_bytes(b"x")
    monkeypatch.setattr(m.shutil, "which", lambda name: f"/usr/bin/{name}")
    payload = '{"streams": [{"codec_name": "h264", "width": 2, "height": 2}]}'
    monkeypatch.setattr(
        m.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, payload, ""),
    )
    with pytest.raises(m.MediaSineError):
        m.probe_media(workspace, "c.mp4")


# --- real end-to-end encode/probe (REAL DISK / REAL EXECUTION) --------------------


@requires_ffmpeg
def test_real_synthesise_and_probe(workspace: WorkspaceFilesystem) -> None:
    res = m.synthesise_test_clip(workspace, "out/clip.mp4", width=128, height=72, frames=12, fps=12)
    assert res.size_bytes > 0
    assert (workspace.root / "out" / "clip.mp4").is_file()
    assert res.probe.codec == "h264"
    assert (res.probe.width, res.probe.height) == (128, 72)
    assert res.probe.nb_frames == 12
    assert res.probe.pix_fmt == "yuv420p"


@requires_ffmpeg
def test_real_probe_reports_duration(workspace: WorkspaceFilesystem) -> None:
    m.synthesise_test_clip(workspace, "c.mp4", width=64, height=64, frames=24, fps=24)
    probe = m.probe_media(workspace, "c.mp4")
    assert probe.duration == 1


@requires_ffmpeg
def test_real_odd_dimensions_are_made_even(workspace: WorkspaceFilesystem) -> None:
    res = m.synthesise_test_clip(workspace, "c.mp4", width=65, height=73, frames=6, fps=6)
    assert (res.probe.width, res.probe.height) == (64, 72)


@requires_ffmpeg
def test_real_mpeg4_codec(workspace: WorkspaceFilesystem) -> None:
    res = m.synthesise_test_clip(
        workspace, "c.mp4", width=64, height=64, frames=6, fps=6, codec="mpeg4"
    )
    assert res.probe.codec == "mpeg4"


@requires_ffmpeg
def test_real_frame_count_mismatch_is_detected(
    workspace: WorkspaceFilesystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        m,
        "probe_media",
        lambda fs, rp, timeout=m.DEFAULT_TIMEOUT: m.MediaProbe(
            codec="h264",
            width=64,
            height=64,
            nb_frames=999,
            duration=m.Fraction(1),
            pix_fmt="yuv420p",
        ),
    )
    with pytest.raises(m.MediaSineError):
        m.synthesise_test_clip(workspace, "c.mp4", width=64, height=64, frames=12, fps=12)


@requires_ffmpeg
def test_real_geometry_mismatch_is_detected(
    workspace: WorkspaceFilesystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force a bogus probe result to prove the post-encode geometry assertion
    # actually fires (a real mismatch is otherwise hard to provoke).
    real_probe = m.probe_media
    monkeypatch.setattr(
        m,
        "probe_media",
        lambda fs, rp, timeout=m.DEFAULT_TIMEOUT: m.MediaProbe(
            codec="h264",
            width=999,
            height=999,
            nb_frames=12,
            duration=m.Fraction(1),
            pix_fmt="yuv420p",
        ),
    )
    with pytest.raises(m.MediaSineError):
        m.synthesise_test_clip(workspace, "c.mp4", width=64, height=64, frames=12, fps=12)
    monkeypatch.setattr(m, "probe_media", real_probe)
