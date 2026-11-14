"""The mux resolves its encoder through the ladder — *after* it validates inputs.

Two contracts, both proved here without relying on a machine-wide FFmpeg:

1. **Resolution is the shared ladder.** ``mux_mp4_ffmpeg`` must obtain the
   encoder from
   :func:`nexus_ai_agent.creative.slideshow.ffmpeg.resolve_ffmpeg_bin` (explicit
   argument → ``NEXUS_FFMPEG_BIN`` → ``PATH`` → the bundled ``imageio-ffmpeg``
   wheel) and run *that* binary.  Before the fix it defaulted to the literal
   string ``"ffmpeg"``, i.e. a PATH lookup that ignored the rest of the ladder;
   the resolver is patched here to prove the call happens and that its return
   value — not a hard-coded name — reaches ``argv[0]``.
2. **Input errors outrank encoder errors.** A frame pattern that matches
   nothing, or an audio path that does not exist, is the caller's mistake; the
   failure must name *that*, not the deployment's missing encoder.  The resolver
   is replaced with one that fails the test if it is reached at all.

Both tests patch ``subprocess.run`` inside the module instead of encoding real
video, so they are hermetic: no FFmpeg, no media, no 120-second budget.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

from nexus_ai_agent.creative.morpheus import morpheus_real_media_spine as spine


def _frames(tmp_path: Path, count: int = 2) -> str:
    """Write *count* lossless PNG frames and return the printf pattern."""
    for index in range(count):
        spine.write_png_frame(
            np.full((8, 8, 3), fill_value=index * 40, dtype=np.uint8),
            tmp_path / f"frame_{index:04d}.png",
        )
    return str(tmp_path / "frame_%04d.png")


def _audio(tmp_path: Path) -> Path:
    path = tmp_path / "audio.wav"
    spine.write_wav_pcm16(np.zeros(2400, dtype=np.int16), path, sample_rate=48000)
    return path


def _capture_subprocess(monkeypatch: pytest.MonkeyPatch, produced: bytes = b"\x00" * 64) -> dict:
    """Replace ``subprocess.run`` with a recorder that writes the output file."""
    recorded: dict = {}

    def _fake_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        recorded["cmd"] = list(cmd)
        recorded["kwargs"] = kwargs
        Path(cmd[-1]).write_bytes(produced)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(spine.subprocess, "run", _fake_run)
    return recorded


def test_the_mux_runs_the_binary_the_ladder_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``argv[0]`` is the resolved encoder, and resolution happens exactly once."""
    resolved: list[str | None] = []

    def _fake_resolver(explicit: str | None = None) -> str:
        resolved.append(explicit)
        return "/opt/ladder/bin/ffmpeg-from-the-wheel"

    monkeypatch.setattr(spine, "resolve_ffmpeg_bin", _fake_resolver)
    recorded = _capture_subprocess(monkeypatch)

    out_mp4 = tmp_path / "video.mp4"
    spine.mux_mp4_ffmpeg(_frames(tmp_path), _audio(tmp_path), out_mp4, fps=24)

    assert resolved == [None], "the mux must call the shared resolver once, with the caller's value"
    assert recorded["cmd"][0] == "/opt/ladder/bin/ffmpeg-from-the-wheel", (
        "argv[0] must be the resolved binary, never a literal 'ffmpeg'"
    )
    assert "+faststart" in recorded["cmd"], "the deterministic mux flags must survive"
    assert str(out_mp4) == recorded["cmd"][-1]
    assert out_mp4.is_file() and out_mp4.stat().st_size > 0


def test_the_default_argument_really_goes_through_the_real_ladder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no explicit binary the *real* resolver decides — not the string "ffmpeg"."""
    seen: list[str | None] = []
    real_resolver = spine.resolve_ffmpeg_bin

    def _spy(explicit: str | None = None) -> str:
        seen.append(explicit)
        return real_resolver(explicit)

    monkeypatch.setattr(spine, "resolve_ffmpeg_bin", _spy)
    recorded = _capture_subprocess(monkeypatch)

    spine.mux_mp4_ffmpeg(_frames(tmp_path, count=1), _audio(tmp_path), tmp_path / "v.mp4")

    assert seen == [None]
    binary = recorded["cmd"][0]
    assert binary != "ffmpeg", (
        "a bare 'ffmpeg' means the ladder was bypassed: the default must be None so the "
        "resolver can reach NEXUS_FFMPEG_BIN and the bundled wheel"
    )
    assert Path(binary).is_file(), f"the resolved binary must exist on disk: {binary!r}"


def test_a_pattern_matching_no_frames_is_refused_before_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty frame set is the caller's error, reported as one."""

    def _must_not_resolve(explicit: str | None = None) -> str:
        raise AssertionError("the encoder was resolved before the input was validated")

    monkeypatch.setattr(spine, "resolve_ffmpeg_bin", _must_not_resolve)
    with pytest.raises(FileNotFoundError, match="No frames match the pattern"):
        spine.mux_mp4_ffmpeg(str(tmp_path / "frame_%04d.png"), _audio(tmp_path), tmp_path / "v.mp4")


def test_a_missing_audio_file_is_refused_before_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Frames are present, audio is not: still an input error, not an encoder error."""

    def _must_not_resolve(explicit: str | None = None) -> str:
        raise AssertionError("the encoder was resolved before the input was validated")

    monkeypatch.setattr(spine, "resolve_ffmpeg_bin", _must_not_resolve)
    with pytest.raises(FileNotFoundError, match="Audio WAV missing"):
        spine.mux_mp4_ffmpeg(_frames(tmp_path), tmp_path / "absent.wav", tmp_path / "v.mp4")


def test_an_empty_frame_pattern_is_refused(tmp_path: Path) -> None:
    """A blank pattern names no frame and must never reach the encoder."""
    with pytest.raises(FileNotFoundError, match="No frames match the pattern"):
        spine.mux_mp4_ffmpeg("   ", _audio(tmp_path), tmp_path / "v.mp4")


def test_the_pattern_expansion_follows_ffmpeg_semantics(tmp_path: Path) -> None:
    """``frame_files`` expands what FFmpeg's pattern would expand, sorted."""
    for index in (0, 1, 7):
        spine.write_png_frame(
            np.zeros((4, 4, 3), dtype=np.uint8), tmp_path / f"frame_{index:04d}.png"
        )
    (tmp_path / "notes.txt").write_text("not a frame")

    found = spine.frame_files(str(tmp_path / "frame_%04d.png"))
    assert [path.name for path in found] == ["frame_0000.png", "frame_0001.png", "frame_0007.png"]
    # The unpadded form FFmpeg also accepts, and the cases that name nothing.
    assert spine.frame_files(str(tmp_path / "frame_%d.png"))
    assert spine.frame_files(str(tmp_path / "absent_%04d.png")) == []
    assert spine.frame_files("") == []
