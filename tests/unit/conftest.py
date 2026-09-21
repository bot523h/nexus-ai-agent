"""Pytest fixtures for the unit tests: Wave 2 slideshow media + feature-wiring fakes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from slideshow_media import SlideshowMedia, build_media_set, make_click_track
from surface_fakes import FakeBot


@pytest.fixture(scope="session")
def slideshow_media(tmp_path_factory: pytest.TempPathFactory) -> SlideshowMedia:
    """12 generated JPEGs (one blurred, one crisp) plus a 61 s 120 BPM WAV."""
    return build_media_set(tmp_path_factory.mktemp("slideshow"))


@pytest.fixture(scope="session")
def slideshow_silence(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("slideshow_silence")
    return make_click_track(directory / "silence.wav", bpm=120.0, seconds=30.0, active=False)


# ── feature-wiring batch (bot/surface) ───────────────────────────────


@pytest.fixture()
def feature_db(settings_override: Any) -> Any:
    """Temp SQLite with every SQLModel table, for the sync feature engines."""
    from sqlalchemy import create_engine
    from sqlmodel import SQLModel

    engine = create_engine(f"sqlite:///{settings_override.db_path}")
    SQLModel.metadata.create_all(engine)
    engine.dispose()
    return settings_override


@pytest.fixture()
def fake_bot() -> FakeBot:
    return FakeBot()
