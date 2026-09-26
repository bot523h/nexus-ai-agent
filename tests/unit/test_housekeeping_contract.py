"""Destructive maintenance contract: previews must never delete local or remote data."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.maintenance import housekeeping as hk

NOW = dt.datetime(2026, 9, 26, 12, tzinfo=dt.timezone.utc)
OLD_KEY = "backups/db/20260801-120000/app.sqlite"
BOUNDARY_KEY = "backups/db/20260827-120000/app.sqlite"
NEW_KEY = "backups/db/20260926-120000/app.sqlite"
INVALID_KEY = "backups/db/not-a-date/app.sqlite"


@pytest.fixture()
def environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    class FrozenDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(hk.dt, "datetime", FrozenDateTime)
    settings = Settings(_env_file=None, creative_temp_dir=str(tmp_path / "creative"))
    # Use the validated Settings object without depending on ambient credentials.
    provider = Mock()
    provider.is_configured.return_value = True
    provider.list_files = AsyncMock(return_value=[OLD_KEY, BOUNDARY_KEY, NEW_KEY, INVALID_KEY])
    provider.delete_objects = AsyncMock()
    monkeypatch.setattr(hk.R2Provider, "from_settings", lambda settings: provider)
    return settings, provider


def make_file(root: Path, name: str, age_hours: int) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"must survive a preview")
    timestamp = (NOW - dt.timedelta(hours=age_hours)).timestamp()
    os.utime(path, (timestamp, timestamp))
    return path


@pytest.mark.parametrize("configured", [False, True])
def test_dry_run_preserves_real_files(environment, configured: bool) -> None:
    settings, provider = environment
    provider.is_configured.return_value = configured
    root = Path(settings.creative_temp_dir)
    old = make_file(root, "nested/old.bin", 49)
    fresh = make_file(root, "fresh.bin", 1)
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (old, fresh)}

    summary = hk.run_housekeeping(settings=settings, dry_run=True)

    assert old.exists(), "dry-run deleted a real stale file"
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (old, fresh)} == before
    assert summary["dry_run"] is True
    # Legacy summary names list candidates in preview mode, not actual deletions.
    assert summary["temp_files_removed"] == [str(old)]
    assert summary["backups_deleted"] == ([OLD_KEY] if configured else [])
    provider.delete_objects.assert_not_awaited()
    if configured:
        provider.list_files.assert_awaited_once_with(prefix="backups/db/")
    else:
        provider.list_files.assert_not_awaited()


def test_dry_run_does_not_even_attempt_unlink(environment, monkeypatch) -> None:
    settings, provider = environment
    old = make_file(Path(settings.creative_temp_dir), "old.bin", 49)

    def forbidden(*args, **kwargs):
        pytest.fail("dry-run attempted unlink")

    # An unlink followed by recreation would not satisfy zero mutation either.
    monkeypatch.setattr(Path, "unlink", forbidden)
    hk.run_housekeeping(settings=settings, dry_run=True)
    assert old.exists()
    provider.delete_objects.assert_not_awaited()


def test_real_cleanup_retention_boundary_and_repeat_are_correct(environment) -> None:
    settings, provider = environment
    root = Path(settings.creative_temp_dir)
    old = make_file(root, "nested/old.bin", 49)
    boundary = make_file(root, "boundary.bin", 48)
    fresh = make_file(root, "fresh.bin", 1)

    summary = hk.run_housekeeping(settings=settings)

    assert not old.exists()
    assert boundary.is_file() and fresh.is_file()
    assert summary["temp_files_removed"] == [str(old)]
    assert summary["backups_deleted"] == [OLD_KEY]
    provider.delete_objects.assert_awaited_once_with(keys=[OLD_KEY])
    provider.list_files.return_value = [BOUNDARY_KEY, NEW_KEY, INVALID_KEY]
    provider.delete_objects.reset_mock()
    repeated = hk.run_housekeeping(settings=settings)
    assert repeated["temp_files_removed"] == []
    assert repeated["backups_deleted"] == []
    provider.delete_objects.assert_not_awaited()


@pytest.mark.parametrize("dry_run", [False, True])
def test_absent_directory_is_not_created(environment, dry_run: bool) -> None:
    settings, provider = environment
    provider.is_configured.return_value = False
    root = Path(settings.creative_temp_dir)
    assert not root.exists()
    summary = hk.run_housekeeping(settings=settings, dry_run=dry_run)
    assert summary["temp_files_removed"] == []
    assert not root.exists()


def test_local_failure_is_not_success(environment, monkeypatch) -> None:
    settings, provider = environment
    make_file(Path(settings.creative_temp_dir), "old.bin", 49)

    def denied(*args, **kwargs):
        raise PermissionError("injected denial")

    monkeypatch.setattr(Path, "unlink", denied)
    with pytest.raises(PermissionError, match="injected denial"):
        hk.run_housekeeping(settings=settings)
    provider.delete_objects.assert_not_awaited()


@pytest.mark.parametrize("dry_run", [False, True])
def test_remote_listing_failure_is_not_success(environment, dry_run: bool) -> None:
    settings, provider = environment
    provider.list_files.side_effect = TimeoutError("injected timeout")
    with pytest.raises(TimeoutError, match="injected timeout"):
        hk.run_housekeeping(settings=settings, dry_run=dry_run)
    provider.delete_objects.assert_not_awaited()


def test_remote_delete_failure_is_not_success(environment) -> None:
    settings, provider = environment
    provider.delete_objects.side_effect = RuntimeError("injected deletion failure")
    with pytest.raises(RuntimeError, match="injected deletion failure"):
        hk.run_housekeeping(settings=settings)


@pytest.mark.parametrize(
    "key",
    [
        "backups/db/not-a-date/app.sqlite",
        "other/db/20260801-120000/app.sqlite",
        "backups/db/20260801-120000/nested/file",
    ],
)
def test_unrecognized_remote_keys_are_never_pruned(key: str) -> None:
    assert hk._parse_backup_stamp(key) is None
