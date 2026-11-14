"""Tests for the ATLAS FFmpeg binary resolver (``atlas_ffmpeg``).

The resolver decides whether the ATLAS render/verify plane can touch real media.
It must (a) honour an explicit ``NEXUS_FFMPEG_BIN`` override, (b) prefer the
system binary on ``PATH``, (c) fall back to the ``imageio-ffmpeg`` wheel, and
(d) never fabricate a path — a machine with nothing available must report
``None`` so the operators fail closed.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator

import pytest

from nexus_ai_agent.creative.atlas import atlas_ffmpeg


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    atlas_ffmpeg.clear_cache()
    yield
    atlas_ffmpeg.clear_cache()


def test_override_is_honoured_when_it_is_a_file(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "ffmpeg"  # type: ignore[operator]
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("NEXUS_FFMPEG_BIN", str(fake))
    assert atlas_ffmpeg.ffmpeg_binary() == str(fake)


def test_override_pointing_at_a_missing_file_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_FFMPEG_BIN", "/no/such/ffmpeg")
    assert atlas_ffmpeg.ffmpeg_binary() is None


def test_path_binary_wins_over_the_wheel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXUS_FFMPEG_BIN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    assert atlas_ffmpeg.ffmpeg_binary() == "/usr/bin/ffmpeg"


def test_wheel_is_the_last_resort(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXUS_FFMPEG_BIN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(atlas_ffmpeg, "_bundled_ffmpeg", lambda: "/wheel/ffmpeg")
    assert atlas_ffmpeg.ffmpeg_binary() == "/wheel/ffmpeg"


def test_no_binary_anywhere_reports_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXUS_FFMPEG_BIN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(atlas_ffmpeg, "_bundled_ffmpeg", lambda: None)
    assert atlas_ffmpeg.ffmpeg_binary() is None


def test_ffprobe_is_path_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        shutil, "which", lambda name: f"/usr/bin/{name}" if name == "ffprobe" else None
    )
    assert atlas_ffmpeg.ffprobe_binary() == "/usr/bin/ffprobe"
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert atlas_ffmpeg.ffprobe_binary() is None


def test_bundled_probe_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    class _Stub:
        @staticmethod
        def get_ffmpeg_exe() -> str:
            calls["n"] += 1
            return "/wheel/ffmpeg"

    monkeypatch.setitem(__import__("sys").modules, "imageio_ffmpeg", _Stub)
    atlas_ffmpeg.clear_cache()
    assert atlas_ffmpeg._bundled_ffmpeg() == "/wheel/ffmpeg"
    assert atlas_ffmpeg._bundled_ffmpeg() == "/wheel/ffmpeg"
    assert calls["n"] == 1


def test_public_surface_matches_declared_exports() -> None:
    assert set(atlas_ffmpeg.__all__) == {"ffmpeg_binary", "ffprobe_binary", "clear_cache"}
    for name in atlas_ffmpeg.__all__:
        assert hasattr(atlas_ffmpeg, name)
