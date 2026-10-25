"""Housekeeping contract: dry-run is a read-only simulation."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.maintenance.housekeeping import run_housekeeping


class _FakeR2:
    def __init__(self, keys: list[str]) -> None:
        self.keys = keys
        self.deleted: list[str] = []

    def is_configured(self) -> bool:
        return True

    async def list_files(self, *, prefix: str = "") -> list[str]:
        return [key for key in self.keys if key.startswith(prefix)]

    async def delete_objects(self, *, keys: list[str]) -> int:
        self.deleted.extend(keys)
        return len(keys)


def _settings(tmp_path: Path) -> Settings:
    return Settings(creative_temp_dir=str(tmp_path / "temp"))


def _make_stale(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("old", encoding="utf-8")
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).timestamp()
    path.touch()
    import os

    os.utime(path, (old, old))


def test_dry_run_does_not_delete_local_files_or_remote_backups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = tmp_path / "temp" / "old.bin"
    _make_stale(stale)
    fake = _FakeR2(["backups/db/20000101-000000/old.sqlite3"])
    monkeypatch.setattr(
        "nexus_ai_agent.maintenance.housekeeping.R2Provider.from_settings",
        lambda settings: fake,
    )

    result = run_housekeeping(
        settings=_settings(tmp_path),
        dry_run=True,
        temp_max_age_hours=1,
        backup_retention_days=1,
    )

    assert stale.exists(), "dry-run must not unlink"
    assert fake.deleted == [], "dry-run must not call remote delete"
    assert result["temp_files_planned"] == [str(stale)]
    assert result["temp_files_removed"] == []
    assert result["backups_planned"] == fake.keys
    assert result["backups_deleted"] == []


def test_apply_deletes_only_selected_local_and_remote_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = tmp_path / "temp" / "old.bin"
    fresh = tmp_path / "temp" / "fresh.bin"
    _make_stale(stale)
    fresh.write_text("new", encoding="utf-8")
    fake = _FakeR2(["backups/db/20000101-000000/old.sqlite3"])
    monkeypatch.setattr(
        "nexus_ai_agent.maintenance.housekeeping.R2Provider.from_settings",
        lambda settings: fake,
    )

    result = run_housekeeping(
        settings=_settings(tmp_path),
        dry_run=False,
        temp_max_age_hours=1,
        backup_retention_days=1,
    )

    assert not stale.exists()
    assert fresh.exists()
    assert result["temp_files_removed"] == [str(stale)]
    assert result["backups_deleted"] == fake.keys
    assert fake.deleted == fake.keys


def test_housekeeping_refuses_symlinked_temp_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    stale = outside / "secret.tmp"
    _make_stale(stale)
    link = tmp_path / "temp-link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - Windows without symlink privilege
        pytest.skip(f"symlinks unavailable: {exc}")

    result = run_housekeeping(
        settings=Settings(creative_temp_dir=str(link)),
        dry_run=False,
    )
    assert stale.exists()
    assert result["temp_files_removed"] == []
