"""Fail-closed paths of the maintenance self-test and the prune helper.

``test_maintenance_selftest_isolation.py`` proves the lane cannot reach a cloud
tier. This module completes the *other* half of the contract: when a leg is
wrong — a non-WAL scratch database, an unverified artifact, a prune that did not
happen, a reconciler that reports divergence — the self-test says so and exits
non-zero. A CI step that silently passes on a broken maintenance lane is worse
than no step at all.

Every failure is injected at a real boundary (a collaborator function, the
``restore`` CLI, an on-disk artifact), never by stubbing the function under test.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.maintenance import backup, ci_selftest, housekeeping


# ---------------------------------------------------------------------------
# The scratch database must really be WAL
# ---------------------------------------------------------------------------
def test_an_in_memory_database_cannot_pass_the_wal_requirement() -> None:
    """``:memory:`` reports ``journal_mode=memory``; the leg must refuse it."""
    with pytest.raises(ci_selftest.SelfTestError) as excinfo:
        ci_selftest.prepare_database(Path(":memory:"))
    assert "expected a WAL database" in str(excinfo.value)


def test_an_empty_schema_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A file that opens in WAL but has no application tables is not a database."""
    db_path = tmp_path / "empty.sqlite3"

    async def _no_tables(_path: str = "") -> None:
        sqlite3.connect(str(db_path)).close()  # a real, empty, WAL-capable file

    monkeypatch.setattr("nexus_ai_agent.storage.db.create_all_tables", _no_tables)
    with pytest.raises(ci_selftest.SelfTestError) as excinfo:
        ci_selftest.prepare_database(db_path)
    assert "schema looks empty" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The backup leg must not accept an unverified artifact
# ---------------------------------------------------------------------------
def _run_backup_with(monkeypatch: pytest.MonkeyPatch, result: dict[str, Any], tmp_path: Path):
    monkeypatch.setattr("nexus_ai_agent.maintenance.backup.create_backup", lambda **_: dict(result))
    return ci_selftest.run_backup(tmp_path / "db.sqlite3", tmp_path / "tier")


def test_an_unverified_backup_fails_the_leg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(ci_selftest.SelfTestError) as excinfo:
        _run_backup_with(monkeypatch, {"verified": False}, tmp_path)
    assert "was not verified" in str(excinfo.value)


def test_a_backup_on_another_tier_fails_the_leg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(ci_selftest.SelfTestError) as excinfo:
        _run_backup_with(monkeypatch, {"verified": True, "tier": "r2", "key": "k"}, tmp_path)
    assert "expected 'local_cache'" in str(excinfo.value)


def test_a_backup_missing_from_the_local_tier_fails_the_leg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(ci_selftest.SelfTestError) as excinfo:
        _run_backup_with(
            monkeypatch,
            {"verified": True, "tier": "local_cache", "key": "backups/db/x/nexus.sqlite3"},
            tmp_path,
        )
    assert "missing from the local tier" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The drill and the reconciler must fail closed
# ---------------------------------------------------------------------------
def test_a_drill_on_a_corrupt_artifact_fails(tmp_path: Path) -> None:
    artifact = tmp_path / "not-a-dump.sqlite3"
    artifact.write_bytes(b"this is not a sqlite database")
    with pytest.raises(ci_selftest.SelfTestError) as excinfo:
        ci_selftest.run_drill(artifact, tmp_path / "restore")
    assert "restore drill failed" in str(excinfo.value)


def test_a_reconciler_divergence_fails_the_leg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from nexus_ai_agent.maintenance import reconciler

    monkeypatch.setattr(reconciler, "main", lambda _argv: 1)
    with pytest.raises(ci_selftest.SelfTestError) as excinfo:
        ci_selftest.run_reconcile(tmp_path / "db.sqlite3", tmp_path)
    assert "reconciliation reported a divergence" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The housekeeping leg must prove the prune happened
# ---------------------------------------------------------------------------
def test_a_prune_that_skipped_the_seed_fails_the_leg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        housekeeping,
        "run_housekeeping",
        lambda **_: {"dry_run": False, "backups_deleted": [], "backups_planned": []},
    )
    with pytest.raises(ci_selftest.SelfTestError) as excinfo:
        ci_selftest.run_housekeeping_leg(database=tmp_path / "db.sqlite3", tier=tmp_path / "tier")
    assert "did not prune the seeded backup" in str(excinfo.value)


def test_a_prune_that_deleted_nothing_on_disk_fails_the_leg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A report of a delete the filesystem does not confirm is not a success."""
    seeded = f"{backup.BLOB_BACKUP_PREFIX}/20000101-000000/nexus.sqlite3"
    monkeypatch.setattr(
        housekeeping,
        "run_housekeeping",
        lambda **_: {"dry_run": False, "backups_deleted": [seeded], "backups_planned": [seeded]},
    )
    with pytest.raises(ci_selftest.SelfTestError) as excinfo:
        ci_selftest.run_housekeeping_leg(database=tmp_path / "db.sqlite3", tier=tmp_path / "tier")
    assert "reported a delete that did not happen" in str(excinfo.value)


def test_a_failing_leg_is_reported_as_a_clean_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``main`` turns a ``SelfTestError`` into exit 1 and one actionable line."""

    def _boom(*_: Any, **__: Any) -> dict[str, Any]:
        raise ci_selftest.SelfTestError("synthetic leg failure")

    monkeypatch.setattr(ci_selftest, "run", _boom)
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    assert ci_selftest.main(["--only", "backup"]) == 1
    out = capsys.readouterr().out
    assert "❌ maintenance self-test failed: synthetic leg failure" in out
    assert "Traceback" not in out


# ---------------------------------------------------------------------------
# The guard restores an existing flag value instead of deleting it
# ---------------------------------------------------------------------------
def test_the_guard_restores_a_previous_flag_value(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    monkeypatch.setenv(backup.SELFTEST_FLAG_ENV, "0")
    with backup.forbid_remote_provider_factory():
        assert os.environ[backup.SELFTEST_FLAG_ENV] == "1"
    assert os.environ[backup.SELFTEST_FLAG_ENV] == "0"


# ---------------------------------------------------------------------------
# housekeeping helpers
# ---------------------------------------------------------------------------
def test_an_unparseable_backup_key_is_never_pruned() -> None:
    assert housekeeping._parse_backup_stamp("backups/db/not-a-stamp/file") is None
    assert housekeeping._parse_backup_stamp("backups/other/20200101-000000/file") is None
    assert housekeeping._parse_backup_stamp("short") is None
    assert housekeeping._parse_backup_stamp("backups/db/20200101-000000/f") is not None


def test_negative_retention_is_refused(tmp_path: Path) -> None:
    from nexus_ai_agent.config.settings import Settings

    settings = Settings.model_validate({}).model_copy(
        update={"creative_temp_dir": str(tmp_path / "temp")}
    )
    with pytest.raises(ValueError):
        housekeeping.run_housekeeping(settings=settings, temp_max_age_hours=-1)
    with pytest.raises(ValueError):
        housekeeping.run_housekeeping(settings=settings, backup_retention_days=-1)


def test_a_tier_with_nothing_expired_is_a_clean_noop(tmp_path: Path) -> None:
    from nexus_ai_agent.config.settings import Settings
    from nexus_ai_agent.storage.providers.local_cache import LocalCacheProvider

    provider = LocalCacheProvider(tmp_path / "tier")
    fresh = provider.path_for_key("backups/db/29991231-235959/nexus.sqlite3")
    fresh.parent.mkdir(parents=True, exist_ok=True)
    fresh.write_bytes(b"future")
    settings = Settings.model_validate({}).model_copy(
        update={"creative_temp_dir": str(tmp_path / "temp")}
    )
    result = housekeeping.run_housekeeping(settings=settings, provider=provider)
    assert result["backups_planned"] == []
    assert result["backups_deleted"] == []
    assert fresh.exists()


def test_an_unparsed_key_is_skipped_while_a_real_one_is_pruned(tmp_path: Path) -> None:
    from nexus_ai_agent.config.settings import Settings
    from nexus_ai_agent.storage.providers.local_cache import LocalCacheProvider

    provider = LocalCacheProvider(tmp_path / "tier")
    stale = provider.path_for_key("backups/db/20000101-000000/nexus.sqlite3")
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"old")
    odd = provider.path_for_key("backups/db/not-a-stamp/nexus.sqlite3")
    odd.parent.mkdir(parents=True, exist_ok=True)
    odd.write_bytes(b"odd")
    settings = Settings.model_validate({}).model_copy(
        update={"creative_temp_dir": str(tmp_path / "temp")}
    )
    result = housekeeping.run_housekeeping(
        settings=settings, backup_retention_days=0, provider=provider
    )
    assert result["backups_deleted"] == ["backups/db/20000101-000000/nexus.sqlite3"]
    assert odd.exists(), "an unparseable key is never pruned"


def test_a_boundary_refusal_is_not_reported_as_a_removal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A refused unlink must not be counted as a successful removal."""
    import datetime as dt

    from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem

    temp_dir = tmp_path / "temp"
    temp_dir.mkdir()
    stale_file = temp_dir / "stale.tmp"
    stale_file.write_bytes(b"x")
    import os

    old = os.stat(stale_file)
    os.utime(stale_file, (old.st_atime - 10_000, old.st_mtime - 10_000))

    def _refuse(self: WorkspaceFilesystem, relative: str) -> None:
        raise ValueError("outside the workspace boundary")

    monkeypatch.setattr(WorkspaceFilesystem, "unlink", _refuse)
    removed = housekeeping._clean_temp_dir(temp_dir, cutoff=dt.datetime.now(dt.timezone.utc))
    assert removed == []
    assert stale_file.exists()


def test_a_file_that_vanishes_between_listing_and_stat_is_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The listing/stat race is real; the guard must skip, never crash."""
    import datetime as dt
    import os

    temp_dir = tmp_path / "temp"
    temp_dir.mkdir()
    present = temp_dir / "present.tmp"
    present.write_bytes(b"x")
    vanishing = temp_dir / "vanishing.tmp"
    vanishing.write_bytes(b"x")
    stale = dt.datetime.now(dt.timezone.utc).timestamp() - 10_000
    os.utime(present, (stale, stale))
    os.utime(vanishing, (stale, stale))

    real_stat = Path.stat

    def _stat(self: Path, *args: Any, **kwargs: Any):
        if self.name == "vanishing.tmp":
            raise OSError("file vanished between listing and stat")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", _stat)
    removed = housekeeping._clean_temp_dir(temp_dir, cutoff=dt.datetime.now(dt.timezone.utc))
    assert [Path(item).name for item in removed] == ["present.tmp"], removed
    assert "vanishing.tmp" in os.listdir(temp_dir), "an unstatable file is never touched"


def test_the_housekeeping_leg_never_consults_the_factory(tmp_path: Path) -> None:
    """The leg must use the provider it was handed, not the ambient tier.

    With the guard armed, a leg that fell back to ``build_blob_provider`` would
    raise instead of pruning; an injected local provider keeps it working.
    """
    with backup.forbid_remote_provider_factory():
        result = ci_selftest.run_housekeeping_leg(
            database=tmp_path / "db.sqlite3", tier=tmp_path / "tier"
        )
    assert result["provider"] == "local_cache"
    assert result["backups_deleted"] == 1
