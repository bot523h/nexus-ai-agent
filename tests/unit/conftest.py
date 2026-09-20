"""Pytest fixtures for the Wave 2 slideshow tests (generated media, no binaries)."""

from __future__ import annotations

from pathlib import Path

import pytest
from slideshow_media import SlideshowMedia, build_media_set, make_click_track


@pytest.fixture(scope="session")
def slideshow_media(tmp_path_factory: pytest.TempPathFactory) -> SlideshowMedia:
    """12 generated JPEGs (one blurred, one crisp) plus a 61 s 120 BPM WAV."""
    return build_media_set(tmp_path_factory.mktemp("slideshow"))


@pytest.fixture(scope="session")
def slideshow_silence(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("slideshow_silence")
    return make_click_track(directory / "silence.wav", bpm=120.0, seconds=30.0, active=False)
