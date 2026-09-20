"""The ``/version`` command must report the real running version.

Regression cover for the v3.0.0 hardcoded string that survived until
v3.10.0.  The distribution metadata is the primary source (the ``VERSION``
file at the repository root is not shipped in a wheel); the repository file
is a fallback for an uninstalled checkout.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nexus_ai_agent.bot import update_handlers
from nexus_ai_agent.bot.update_handlers import running_version, version_cmd

REPO_ROOT = Path(__file__).parents[2]


class _FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs: Any) -> None:
        self.replies.append(text)


class _FakeUpdate:
    def __init__(self) -> None:
        self.message = _FakeMessage()


def test_running_version_matches_installed_distribution() -> None:
    assert running_version() == f"v{distribution_version('nexus-ai-agent')}"
    assert "3.0.0" not in running_version()


def test_running_version_matches_the_release_files() -> None:
    """`VERSION`, `pyproject.toml` and the installed metadata must agree."""
    file_version = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert running_version() == f"v{file_version}"


def test_running_version_falls_back_to_the_repo_version_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _missing(_name: str) -> str:
        raise PackageNotFoundError(_name)

    monkeypatch.setattr(update_handlers, "_distribution_version", _missing)
    expected = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert running_version() == f"v{expected}"


def test_running_version_reports_unknown_only_when_nothing_is_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _missing(_name: str) -> str:
        raise PackageNotFoundError(_name)

    monkeypatch.setattr(update_handlers, "_distribution_version", _missing)
    monkeypatch.setattr(update_handlers, "_version_from_repo_checkout", lambda: None, raising=True)
    assert running_version() == "v0.0.0+unknown"


async def test_version_cmd_replies_with_the_running_version() -> None:
    update = _FakeUpdate()
    context = SimpleNamespace(bot=SimpleNamespace())
    await version_cmd(update, context)  # type: ignore[arg-type]
    assert update.message.replies == [f"🤖 نسخه فعلی: {running_version()}"]
