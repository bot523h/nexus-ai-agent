"""WAVE 29 — tests for the real FFmpeg media encode/probe layer.

The encode/probe tests execute a real ``ffmpeg``/``ffprobe`` when both binaries
are on PATH (they are skipped otherwise). Error branches that depend on binary
availability or on the subprocess channel are exercised with a monkeypatched
``shutil.which`` / ``subprocess.run`` / ``subprocess.Popen``: these failure modes
cannot be produced deterministically with a working FFmpeg install, so the mock is
the honest instrument for them.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.creative.atlas import media_sine as m
from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem

HAS_FFMPEG = m.ffmpeg_available()
requires_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not on PATH")


@pytest.fixture()
def workspace(tmp_path: Path) -> WorkspaceFilesystem:
    return WorkspaceFilesystem(tmp_path)


def _frame_bytes(index: int, width: int, height: int) -> bytes:
    """Deterministic, frame-dependent rgb24 content of the even-rounded geometry."""
    n = width * height * 3
    return bytes(((index * 37) + (j * 3)) & 0xFF for j in range(n))


def _encode(
    fs: WorkspaceFilesystem,
    rel: str,
    *,
    width: int = 128,
    height: int = 72,
    frames: int = 24,
    fps: int = 24,
    codec: str = "libx264",
    timeout: float = m.DEFAULT_TIMEOUT,
) -> m.MediaSineResult:
    w = width - (width % 2)
    h = height - (height % 2)
    return m.encode_rgb24_stream(
        fs,
        rel,
        lambda i: _frame_bytes(i, w, h),
        width=width,
        height=height,
        frames=frames,
        fps=fps,
        codec=codec,
        timeout=timeout,
    )


class _FakeStdin:
    def __init__(self, error: BaseException | None = None) -> None:
        self.data = bytearray()
        self._error = error

    def write(self, payload: bytes) -> int:
        if self._error is not None:
            raise self._error
        self.data.extend(payload)
        return len(payload)

    def close(self) -> None:
        return None


class _FakeProc:
    """A child process that keeps running until ``wait`` returns (or it is killed)."""

    def __init__(
        self,
        returncode: int = 0,
        *,
        write_error: BaseException | None = None,
        wait_error: BaseException | None = None,
    ) -> None:
        self.stdin = _FakeStdin(write_error)
        self.stdout = None
        self._returncode = returncode
        self._wait_error = wait_error
        self._finished = False
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        if self._wait_error is not None:
            error, self._wait_error = self._wait_error, None
            raise error
        self._finished = True
        return self._returncode

    def poll(self) -> int | None:
        return self._returncode if (self._finished or self.killed) else None

    def kill(self) -> None:
        self.killed = True


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


def test_encode_refuses_missing_binary(
    workspace: WorkspaceFilesystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(m.shutil, "which", lambda _name: None)
    with pytest.raises(m.MediaSineUnavailable):
        _encode(workspace, "clip.mp4")


def test_probe_refuses_missing_binary(
    workspace: WorkspaceFilesystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(m.shutil, "which", lambda _name: None)
    with pytest.raises(m.MediaSineUnavailable):
        m.probe_media(workspace, "clip.mp4")


def test_refuse_bad_frames(workspace: WorkspaceFilesystem) -> None:
    with pytest.raises(m.MediaSineError):
        _encode(workspace, "c.mp4", frames=0)
    with pytest.raises(m.MediaSineError):
        _encode(workspace, "c.mp4", frames=m.MAX_FRAMES + 1)


def test_refuse_bad_fps(workspace: WorkspaceFilesystem) -> None:
    with pytest.raises(m.MediaSineError):
        _encode(workspace, "c.mp4", fps=0)
    with pytest.raises(m.MediaSineError):
        _encode(workspace, "c.mp4", fps=241)


def test_refuse_bad_codec(workspace: WorkspaceFilesystem) -> None:
    with pytest.raises(m.MediaSineError):
        _encode(workspace, "c.mp4", codec="prores")


def test_refuse_nonpositive_encode_timeout(workspace: WorkspaceFilesystem) -> None:
    with pytest.raises(m.MediaSineError):
        _encode(workspace, "c.mp4", timeout=0.0)
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(m.MediaSineError):
            _encode(workspace, "c.mp4", timeout=bad)
        with pytest.raises(m.MediaSineError):
            m.run_process(["true"], timeout=bad)
        with pytest.raises(m.MediaSineError):
            m.probe_media(workspace, "c.mp4", timeout=bad)


def test_refuse_frame_of_wrong_size(
    workspace: WorkspaceFilesystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(m.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(m.subprocess, "Popen", lambda *a, **k: _FakeProc(0))
    with pytest.raises(m.MediaSineError, match="expected"):
        m.encode_rgb24_stream(
            workspace, "c.mp4", lambda i: b"short", width=8, height=8, frames=2, fps=2
        )


def test_frame_source_error_propagates_and_kills_ffmpeg(
    workspace: WorkspaceFilesystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    procs: list[_FakeProc] = []

    def _popen(*_a: object, **_k: object) -> _FakeProc:
        proc = _FakeProc(0)
        procs.append(proc)
        return proc

    def _source(index: int) -> bytes:
        if index == 1:
            raise RuntimeError("renderer failed")
        return bytes(8 * 8 * 3)

    monkeypatch.setattr(m.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(m.subprocess, "Popen", _popen)
    with pytest.raises(RuntimeError, match="renderer failed"):
        m.encode_rgb24_stream(workspace, "c.mp4", _source, width=8, height=8, frames=3, fps=3)
    assert procs and procs[0].killed


def test_broken_pipe_is_reported_as_encode_error(
    workspace: WorkspaceFilesystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(m.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        m.subprocess, "Popen", lambda *a, **k: _FakeProc(0, write_error=BrokenPipeError())
    )
    with pytest.raises(m.MediaSineError, match="closed its input"):
        m.encode_rgb24_stream(
            workspace, "c.mp4", lambda i: bytes(8 * 8 * 3), width=8, height=8, frames=2, fps=2
        )


def test_ffmpeg_that_never_finishes_is_killed_on_timeout(
    workspace: WorkspaceFilesystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    procs: list[_FakeProc] = []

    def _popen(*_a: object, **_k: object) -> _FakeProc:
        proc = _FakeProc(0, wait_error=subprocess.TimeoutExpired(cmd="ffmpeg", timeout=1.0))
        procs.append(proc)
        return proc

    monkeypatch.setattr(m.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(m.subprocess, "Popen", _popen)
    with pytest.raises(m.MediaSineError, match="wall-clock timeout"):
        m.encode_rgb24_stream(
            workspace, "c.mp4", lambda i: bytes(8 * 8 * 3), width=8, height=8, frames=2, fps=2
        )
    assert procs and procs[0].killed


def test_encode_exceeding_deadline_is_refused(
    workspace: WorkspaceFilesystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    procs: list[_FakeProc] = []

    def _popen(*_a: object, **_k: object) -> _FakeProc:
        proc = _FakeProc(0)
        procs.append(proc)
        return proc

    def _slow_source(index: int) -> bytes:
        time.sleep(0.05)  # the first frame is produced after the 10 ms budget expires
        return bytes(8 * 8 * 3)

    monkeypatch.setattr(m.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(m.subprocess, "Popen", _popen)
    with pytest.raises(m.MediaSineError, match="exceeded its wall-clock timeout"):
        m.encode_rgb24_stream(
            workspace,
            "c.mp4",
            _slow_source,
            width=8,
            height=8,
            frames=3,
            fps=3,
            timeout=0.01,
        )
    assert procs and procs[0].killed


def test_blocking_stdin_write_is_killed_by_watchdog_under_one_second(
    workspace: WorkspaceFilesystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real subprocess that never drains stdin fills the pipe buffer and blocks write();
    the watchdog must kill it and raise MediaSineError before 1 second elapses."""
    spawned: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def _sleeping_consumer(_argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        proc = real_popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            stdin=kwargs.get("stdin"),
            stdout=kwargs.get("stdout"),
            stderr=kwargs.get("stderr"),
        )
        spawned.append(proc)
        return proc

    monkeypatch.setattr(m.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(m.subprocess, "Popen", _sleeping_consumer)
    large_frame = b"\x5a" * (256 * 256 * 3)  # 192 KiB > OS pipe buffer; 8 frames = 1.5 MiB
    t0 = time.monotonic()
    with pytest.raises(m.MediaSineError, match="wall-clock timeout"):
        m.encode_rgb24_stream(
            workspace,
            "blocked_pipe.mp4",
            lambda _i: large_frame,
            width=256,
            height=256,
            frames=8,
            fps=8,
            timeout=0.15,
        )
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"watchdog took too long: {elapsed:.3f}s"
    assert spawned and spawned[0].poll() is not None


def test_probe_shares_remaining_deadline_budget(
    workspace: WorkspaceFilesystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen_probe_timeouts: list[float] = []
    out_file = workspace.root / "shared.mp4"

    class _WritingProc(_FakeProc):
        def wait(self, timeout: float | None = None) -> int:
            out_file.write_bytes(b"mp4")
            time.sleep(0.06)
            return super().wait(timeout=timeout)

    def _fake_probe(_fs: WorkspaceFilesystem, _rel: str, *, timeout: float = 0.0) -> m.MediaProbe:
        seen_probe_timeouts.append(timeout)
        return m.MediaProbe(
            codec="h264",
            width=8,
            height=8,
            nb_frames=2,
            duration=m.Fraction(1),
            pix_fmt="yuv420p",
        )

    monkeypatch.setattr(m.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(m.subprocess, "Popen", lambda *a, **k: _WritingProc(0))
    monkeypatch.setattr(m, "probe_media", _fake_probe)

    m.encode_rgb24_stream(
        workspace,
        "shared.mp4",
        lambda _i: bytes(8 * 8 * 3),
        width=8,
        height=8,
        frames=2,
        fps=2,
        timeout=0.30,
    )
    assert len(seen_probe_timeouts) == 1
    assert 0.0 < seen_probe_timeouts[0] < 0.28

    # When the budget is already exhausted before probe, probe_media must not be called.
    seen_probe_timeouts.clear()

    class _ExhaustingProc(_FakeProc):
        def wait(self, timeout: float | None = None) -> int:
            out_file.write_bytes(b"mp4")
            time.sleep(0.08)
            return super().wait(timeout=timeout)

    monkeypatch.setattr(m.subprocess, "Popen", lambda *a, **k: _ExhaustingProc(0))
    with pytest.raises(m.MediaSineError, match="wall-clock timeout"):
        m.encode_rgb24_stream(
            workspace,
            "shared.mp4",
            lambda _i: bytes(8 * 8 * 3),
            width=8,
            height=8,
            frames=2,
            fps=2,
            timeout=0.05,
        )
    assert seen_probe_timeouts == []


def test_refuse_ffmpeg_nonzero_exit(
    workspace: WorkspaceFilesystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(m.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(m.subprocess, "Popen", lambda *a, **k: _FakeProc(1))
    with pytest.raises(m.MediaSineError, match="ffmpeg failed"):
        _encode(workspace, "c.mp4")


def test_refuse_success_without_output_file(
    workspace: WorkspaceFilesystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(m.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(m.subprocess, "Popen", lambda *a, **k: _FakeProc(0))
    with pytest.raises(m.MediaSineError):
        _encode(workspace, "c.mp4")


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
def test_real_encode_and_probe(workspace: WorkspaceFilesystem) -> None:
    res = _encode(workspace, "out/clip.mp4", width=128, height=72, frames=12, fps=12)
    assert res.size_bytes > 0
    assert (workspace.root / "out" / "clip.mp4").is_file()
    assert res.probe.codec == "h264"
    assert (res.probe.width, res.probe.height) == (128, 72)
    assert res.probe.nb_frames == 12
    assert res.probe.pix_fmt == "yuv420p"


@requires_ffmpeg
def test_real_probe_reports_duration(workspace: WorkspaceFilesystem) -> None:
    _encode(workspace, "c.mp4", width=64, height=64, frames=24, fps=24)
    probe = m.probe_media(workspace, "c.mp4")
    assert probe.duration == 1


@requires_ffmpeg
def test_real_odd_dimensions_are_made_even(workspace: WorkspaceFilesystem) -> None:
    res = _encode(workspace, "c.mp4", width=65, height=73, frames=6, fps=6)
    assert (res.probe.width, res.probe.height) == (64, 72)


@requires_ffmpeg
def test_real_mpeg4_codec(workspace: WorkspaceFilesystem) -> None:
    res = _encode(workspace, "c.mp4", width=64, height=64, frames=6, fps=6, codec="mpeg4")
    assert res.probe.codec == "mpeg4"


@requires_ffmpeg
def test_real_ffmpeg_failure_on_unwritable_target(workspace: WorkspaceFilesystem) -> None:
    # A directory at the target path makes the real ffmpeg exit non-zero.
    (workspace.root / "blocked.mp4").mkdir()
    with pytest.raises(m.MediaSineError, match="ffmpeg failed"):
        _encode(workspace, "blocked.mp4", width=64, height=64, frames=3, fps=3)


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
        _encode(workspace, "c.mp4", width=64, height=64, frames=12, fps=12)


@requires_ffmpeg
def test_real_geometry_mismatch_is_detected(
    workspace: WorkspaceFilesystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force a bogus probe result to prove the post-encode geometry assertion
    # actually fires (a real mismatch is otherwise hard to provoke).
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
        _encode(workspace, "c.mp4", width=64, height=64, frames=12, fps=12)
