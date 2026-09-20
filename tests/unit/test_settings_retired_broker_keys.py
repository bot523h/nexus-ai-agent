"""Retired broker settings must not crash boot for operators' existing ``.env`` files.

``Settings`` forbids unknown dotenv keys (pre-existing behaviour, unchanged
here).  Removing ``redis_url`` / ``celery_broker_url`` /
``celery_result_backend`` would therefore turn a v3.4.0-era ``.env`` into a
boot failure; those three keys — and only those — are dropped with a
warning instead.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from nexus_ai_agent.config.settings import RETIRED_BROKER_KEYS, Settings


def _env_file(tmp_path: Path, body: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(body, encoding="utf-8")
    return path


def test_retired_broker_keys_are_dropped_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    env = _env_file(
        tmp_path,
        "TELEGRAM_BOT_TOKEN=1:a\n"
        "REDIS_URL=redis://redis:6379/0\n"
        "CELERY_BROKER_URL=redis://redis:6379/0\n"
        "CELERY_RESULT_BACKEND=redis://redis:6379/1\n",
    )
    with caplog.at_level(logging.WARNING):
        settings = Settings(_env_file=env)
    assert settings.telegram_bot_token == "1:a"
    assert not set(Settings.model_fields) & RETIRED_BROKER_KEYS
    assert any("retired broker settings" in record.getMessage() for record in caplog.records)


def test_unknown_keys_still_fail_fast(tmp_path: Path) -> None:
    """The shim is surgical: arbitrary unknown dotenv keys keep failing as before."""
    env = _env_file(tmp_path, "TELEGRAM_BOT_TOKEN=1:a\nTOTALLY_UNKNOWN_KEY=1\n")
    with pytest.raises(ValidationError):
        Settings(_env_file=env)


def test_no_warning_without_retired_keys(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    env = _env_file(tmp_path, "TELEGRAM_BOT_TOKEN=1:a\n")
    with caplog.at_level(logging.WARNING):
        Settings(_env_file=env)
    assert not any("retired broker settings" in record.getMessage() for record in caplog.records)


def test_retired_keys_are_exactly_the_removed_fields() -> None:
    assert RETIRED_BROKER_KEYS == frozenset(
        {"redis_url", "celery_broker_url", "celery_result_backend"}
    )
