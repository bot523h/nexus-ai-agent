"""A killed ffmpeg that will not reap must be logged, not silently ignored."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from nexus_ai_agent.creative.atlas import media_sine as m
from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem


class _BrokenStdin:
    def write(self, _payload: bytes) -> int:
        raise BrokenPipeError("pipe closed")

    def close(self) -> None:
        return None


class _StubbornProc:
    """Never exits on its own, and ``wait`` always times out, even after kill."""

    def __init__(self) -> None:
        self.stdin = _BrokenStdin()
        self.stdout = None
        self.pid = 4242
        self.killed = False

    def poll(self) -> int | None:
        return None

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=timeout or 0.0)


def test_unreapable_ffmpeg_after_kill_is_logged_with_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    proc = _StubbornProc()
    monkeypatch.setattr(m.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(m.subprocess, "Popen", lambda *_a, **_k: proc)
    caplog.set_level(logging.ERROR, logger=m.__name__)
    fs = WorkspaceFilesystem(tmp_path)
    with pytest.raises(m.MediaSineError):
        m.encode_rgb24_stream(
            fs,
            "leak.mp4",
            lambda _i: bytes(16 * 16 * 3),
            width=16,
            height=16,
            frames=1,
            fps=8,
        )
    assert proc.killed
    leaked = [r for r in caplog.records if "may be leaked" in r.getMessage()]
    assert leaked, "the leaked-process condition must be logged, not suppressed"
    assert "4242" in leaked[0].getMessage()
