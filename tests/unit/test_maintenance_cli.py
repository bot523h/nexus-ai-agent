"""Maintenance CLI operator-truth contract.

Regression for the 2026-09-26 finding: ``nexus maintenance housekeeping
--dry-run`` printed only the (always empty) mutation result lists — an
operator asking "what WOULD you delete?" was shown nothing at all, the
worst possible dry-run preview.

The preview must list the planned selections in dry-run mode, and the
mutation result lists only after real runs.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

import nexus_ai_agent.config.settings as settings_module
from nexus_ai_agent.cli import app


def _make_stale(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("old", encoding="utf-8")
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).timestamp()
    os.utime(path, (old, old))


def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(tmp_path / "temp"))
    monkeypatch.setenv("NEXUS_DB_PATH", str(tmp_path / "data" / "app.sqlite"))
    monkeypatch.setenv("NEXUS_CHECKPOINT_PATH", str(tmp_path / "data" / "langgraph.sqlite"))
    cache_clear = getattr(settings_module.get_settings, "cache_clear", None)
    if callable(cache_clear):
        cache_clear()


def test_dry_run_lists_planned_candidates_and_mutates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_env(monkeypatch, tmp_path)
    stale = tmp_path / "temp" / "old.bin"
    _make_stale(stale)

    result = CliRunner().invoke(
        app,
        ["maintenance", "housekeeping", "--dry-run", "--temp-max-age-hours", "1"],
    )

    assert result.exit_code == 0, result.output
    assert "temp files planned for removal: 1" in result.output
    assert str(stale) in result.output
    assert "R2 backups planned for deletion: 0" in result.output
    assert "dry-run: nothing was changed" in result.output
    # Zero-mutation contract at the operator surface too:
    assert stale.exists()
    # The mutation vocabulary must not masquerade as a preview:
    assert "temp files removed" not in result.output


def test_real_run_reports_only_observed_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_env(monkeypatch, tmp_path)
    stale = tmp_path / "temp" / "old.bin"
    _make_stale(stale)

    result = CliRunner().invoke(
        app,
        ["maintenance", "housekeeping", "--temp-max-age-hours", "1"],
    )

    assert result.exit_code == 0, result.output
    assert "temp files removed: 1" in result.output
    assert "planned" not in result.output
    assert not stale.exists()
