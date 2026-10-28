"""Restore-drill truth (mission: production/DR — BACKUP MUST MEAN RECOVERY).

Reproduced on main: the repository had zero restore capability — backups
were verified artifacts, but nothing ever restored one, so "DB dies
tomorrow" had no executable answer. ``maintenance/restore.py`` closes the
chain BACKUP → VERIFIED ARTIFACT → RESTORE → REAL RECOVERY.

Pinned contracts (kill-mutations like "raise → warn" or "verify target →
skip" must turn tests here red):

Happy path
* local sqlite artifact: drill (verify-only) records the full chain
  FETCHED → CHECKSUM_* → INTEGRITY_VERIFIED → RESTORE_PROVEN and ``ok``.
* apply restores REAL data (table contents read back from the target), is
  atomic (temp + os.replace), leaves a timestamped safety copy when
  replacing, and re-verifies the target after restore.
* ``--expect-app-schema`` requires the app's ORM tables on the target.

Chaos (operator path)
* missing local file → plan error naming the cause.
* empty (0-byte) artifact → verification error, never "restorable".
* truncated/corrupt sqlite header → integrity failure, never applied.
* wrong --expected-sha256 → checksum mismatch fails the drill.
* sidecar manifest that disagrees with the bytes → corruption/tampering error.
* existing target without --force → refused; directory target → refused.
* R2 unconfigured → ProviderUnavailable naming the exact env vars.
* --latest with an empty bucket → plan error; with objects → newest wins
  and sidecar keys never poison the selection.
* download failure → actionable MaintenanceOperationError (not a bare raise).
* postgres structural leg proves footer/bytes; the live pg_restore leg
  requires --target-url + --force and names the external dependency —
  it is never faked.

Chaos policy: errors are typed (RestorePlanError / RestoreVerificationError
/ MaintenanceOperationError / ProviderUnavailable) with operator actions —
never bare RuntimeError.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.maintenance import restore as restore_module
from nexus_ai_agent.maintenance.backup import _sha256
from nexus_ai_agent.maintenance.restore import (
    DrillReport,
    MaintenanceOperationError,
    RestorePlanError,
    RestoreVerificationError,
    restore_drill,
)
from nexus_ai_agent.storage.providers.base import ProviderUnavailable, StorageError


@pytest.fixture(autouse=True)
def _uncached_settings():
    settings_module.get_settings.cache_clear()
    yield
    settings_module.get_settings.cache_clear()


def _settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Settings without R2 and without pg DSN (unit isolation by default)."""
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("R2_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("R2_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("R2_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("R2_BUCKET", raising=False)
    from nexus_ai_agent.config.settings import Settings

    return Settings(db_path=str(tmp_path / "app.sqlite3"))


def _sqlite_artifact(path: Path, table: str = "jobs", rows: int = 3) -> Path:
    """A real backup-shaped sqlite artifact (same function the backup uses)."""
    from nexus_ai_agent.maintenance.backup import _dump_sqlite

    source = path.parent / f"{path.stem}-source.sqlite3"
    with sqlite3.connect(source) as conn:
        conn.execute(f"CREATE TABLE {table} (id TEXT PRIMARY KEY, payload TEXT)")
        for i in range(rows):
            conn.execute(f"INSERT INTO {table} VALUES (?, ?)", (f"row-{i}", "payload-" + "x" * 32))
        conn.commit()
    _dump_sqlite(source, path)
    return path


def _table_rows(db: Path, table: str) -> list[tuple]:
    with sqlite3.connect(db) as conn:
        return conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()


class TestLocalDrillHappyPath:
    def test_verify_only_drill_proves_chain_without_target(self, tmp_path, monkeypatch):
        artifact = _sqlite_artifact(tmp_path / "backup.sqlite3")

        report = restore_drill(
            settings=_settings(tmp_path, monkeypatch),
            from_local=artifact,
            expected_sha256=_sha256(artifact),
        )

        assert report.ok is True
        assert report.engine == "sqlite"
        assert [s["state"] for s in report.chain] == [
            "FETCHED",
            "CHECKSUM_VERIFIED",
            "INTEGRITY_VERIFIED",
            "RESTORE_PROVEN",
        ]
        assert report.artifact["tables"]["jobs"] == 3
        assert report.artifact["sha256"] == _sha256(artifact)

    def test_structural_only_is_honestly_labeled_without_checksum(self, tmp_path, monkeypatch):
        artifact = _sqlite_artifact(tmp_path / "backup.sqlite3")
        report = restore_drill(settings=_settings(tmp_path, monkeypatch), from_local=artifact)
        assert report.ok is True
        assert report.chain[1]["state"] == "CHECKSUM_STRUCTURAL_ONLY"
        # Honest downgrade is visible in the proof line, never silent:
        assert "structural only" in report.chain[-1]["detail"]

    def test_apply_restores_real_data_atomically(self, tmp_path, monkeypatch):
        artifact = _sqlite_artifact(tmp_path / "backup.sqlite3", rows=4)
        target = tmp_path / "restored" / "nested" / "app.sqlite3"

        report = restore_drill(
            settings=_settings(tmp_path, monkeypatch),
            from_local=artifact,
            apply=True,
            target=target,
            expect_app_schema=False,
        )

        assert report.ok is True
        assert [s["state"] for s in report.chain][-1] == "RESTORE_PROVEN"
        # REAL recovery = the data is actually queryable at the target path:
        assert _table_rows(target, "jobs") == [
            (f"row-{i}", "payload-" + "x" * 32) for i in range(4)
        ]
        # byte-for-byte recovery from the artifact:
        assert target.read_bytes() == artifact.read_bytes()
        assert "POST_RESTORE_INTEGRITY" in [s["state"] for s in report.chain]
        assert report.restored["safety_copy"] is None  # nothing existed before

    def test_apply_replaces_with_timestamped_safety_copy(self, tmp_path, monkeypatch):
        artifact = _sqlite_artifact(tmp_path / "backup.sqlite3", rows=2)
        target = tmp_path / "app.sqlite3"
        target.write_bytes(b"old-database-bytes")

        report = restore_drill(
            settings=_settings(tmp_path, monkeypatch),
            from_local=artifact,
            apply=True,
            target=target,
            force=True,
        )

        assert report.ok is True
        safety = Path(report.restored["safety_copy"])
        assert safety.exists() and safety.read_bytes() == b"old-database-bytes"
        assert ".pre-restore-" in safety.name
        assert _table_rows(target, "jobs") == [
            (f"row-{i}", "payload-" + "x" * 32) for i in range(2)
        ]

    def test_apply_expect_app_schema_accepts_app_shaped_db(self, tmp_path, monkeypatch):
        # Build an artifact with the REAL application schema (the same
        # metadata the app boots with) — the success leg of app-schema proof.
        from nexus_ai_agent.maintenance.backup import _dump_sqlite
        from nexus_ai_agent.storage.db import create_all_tables

        source = tmp_path / "app-shaped.sqlite3"
        import asyncio

        asyncio.run(create_all_tables(str(source)))
        artifact = tmp_path / "backup.sqlite3"
        _dump_sqlite(source, artifact)

        report = restore_drill(
            settings=_settings(tmp_path, monkeypatch),
            from_local=artifact,
            apply=True,
            target=tmp_path / "restored.sqlite3",
            expect_app_schema=True,
        )
        assert report.ok is True
        states = [s["state"] for s in report.chain]
        assert "APP_SCHEMA_READABLE" in states
        assert report.restored["size_bytes"] > 0

    def test_cli_module_entry_returns_zero_and_json(self, tmp_path, monkeypatch, capsys):
        artifact = _sqlite_artifact(tmp_path / "backup.sqlite3")
        exit_code = restore_module.main(
            ["--from-local", str(artifact), "--expected-sha256", _sha256(artifact), "--json"]
        )
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["chain"][-1]["state"] == "RESTORE_PROVEN"


class TestChaosLocal:
    def test_missing_local_file_is_a_plan_error_naming_the_path(self, tmp_path, monkeypatch):
        with pytest.raises(RestorePlanError, match="does not exist"):
            restore_drill(
                settings=_settings(tmp_path, monkeypatch),
                from_local=tmp_path / "nope.sqlite3",
            )

    def test_empty_artifact_is_rejected_before_integrity(self, tmp_path, monkeypatch):
        empty = tmp_path / "backup.sqlite3"
        empty.touch()
        with pytest.raises(RestoreVerificationError, match="backup artifact is empty") as exc_info:
            restore_drill(settings=_settings(tmp_path, monkeypatch), from_local=empty)
        # The early-stage identity matters operationally: an empty object in
        # the bucket means the BACKUP/UPLOAD pipeline produced nothing —
        # investigate THAT, not "why does the dump not verify". The drill
        # must surface it before structural probing:
        assert "not a backup" in str(exc_info.value)

    def test_corrupt_sqlite_bytes_fail_integrity_and_never_apply(self, tmp_path, monkeypatch):
        corrupt = tmp_path / "backup.sqlite3"
        # Plenty of non-zero bytes but not a database: header is readable as
        # sqlite (name-based sniff would pass) yet PRAGMA integrity trembles.
        corrupt.write_bytes(b"SQLite format 3\x02" + b"\x7f" * 4096)
        with pytest.raises(RestoreVerificationError):
            restore_drill(
                settings=_settings(tmp_path, monkeypatch),
                from_local=corrupt,
                apply=True,
                target=tmp_path / "must-not-be-created.sqlite3",
            )
        assert not (tmp_path / "must-not-be-created.sqlite3").exists()

    def test_wrong_expected_sha256_fails_as_tamper_evidence(self, tmp_path, monkeypatch):
        artifact = _sqlite_artifact(tmp_path / "backup.sqlite3")
        with pytest.raises(RestoreVerificationError, match="checksum MISMATCH") as exc_info:
            restore_drill(
                settings=_settings(tmp_path, monkeypatch),
                from_local=artifact,
                expected_sha256="0" * 64,
            )
        assert "do not restore" in str(exc_info.value)

    def test_disagreeing_sidecar_means_corruption_not_warning(self, tmp_path, monkeypatch):
        artifact = _sqlite_artifact(tmp_path / "backup.sqlite3")
        # Attach a sidecar NEAR the local source: the drill reads the local
        # copy's sibling manifest when provider fetch supplies one — emulate
        # by staging through the checksum stage directly (unit-level).
        report = DrillReport(mode="drill")
        sidecar = tmp_path / "backup.sqlite3.meta.json"
        sidecar.write_text(json.dumps({"sha256": "f" * 64, "size_bytes": artifact.stat().st_size}))
        with pytest.raises(RestoreVerificationError, match="sidecar"):
            restore_module._verify_checksum(artifact, report, expected_sha256=None, sidecar=sidecar)

    def test_agreeing_sidecar_upgrades_proof_to_checksum_verified(self, tmp_path, monkeypatch):
        artifact = _sqlite_artifact(tmp_path / "backup.sqlite3")
        report = DrillReport(mode="drill")
        sidecar = tmp_path / "backup.sqlite3.meta.json"
        sidecar.write_text(
            json.dumps({"sha256": _sha256(artifact), "size_bytes": artifact.stat().st_size})
        )
        restore_module._verify_checksum(artifact, report, expected_sha256=None, sidecar=sidecar)
        assert report.chain[0]["state"] == "CHECKSUM_VERIFIED"

    def test_existing_target_requires_force(self, tmp_path, monkeypatch):
        artifact = _sqlite_artifact(tmp_path / "backup.sqlite3")
        target = tmp_path / "app.sqlite3"
        target.write_bytes(b"keep-me")
        with pytest.raises(RestorePlanError, match="--force"):
            restore_drill(
                settings=_settings(tmp_path, monkeypatch),
                from_local=artifact,
                apply=True,
                target=target,
            )
        assert target.read_bytes() == b"keep-me"  # untouched, never half-written

    def test_directory_target_is_refused(self, tmp_path, monkeypatch):
        artifact = _sqlite_artifact(tmp_path / "backup.sqlite3")
        with pytest.raises(RestorePlanError, match="directory"):
            restore_drill(
                settings=_settings(tmp_path, monkeypatch),
                from_local=artifact,
                apply=True,
                target=tmp_path,  # a directory
            )

    def test_apply_without_any_target_is_a_plan_error(self, tmp_path, monkeypatch):
        artifact = _sqlite_artifact(tmp_path / "backup.sqlite3")
        with pytest.raises(RestorePlanError, match="target"):
            restore_drill(
                settings=_settings(tmp_path, monkeypatch),
                from_local=artifact,
                apply=True,
            )

    def test_apply_missing_app_tables_fails_identified(self, tmp_path, monkeypatch):
        artifact = _sqlite_artifact(tmp_path / "backup.sqlite3", table="not_an_app_table")
        with pytest.raises(RestoreVerificationError, match="missing application tables"):
            restore_drill(
                settings=_settings(tmp_path, monkeypatch),
                from_local=artifact,
                apply=True,
                target=tmp_path / "restored.sqlite3",
                expect_app_schema=True,
            )

    def test_cli_returns_one_and_actionable_error_on_failure(self, tmp_path, monkeypatch, capsys):
        missing = tmp_path / "ghost.sqlite3"
        exit_code = restore_module.main(["--from-local", str(missing)])
        assert exit_code == 1
        assert "restore drill failed" in capsys.readouterr().err


class _FakeR2:
    """In-memory stand-in for the repository boundary (seam used ONLY to
    unit-test OUR orchestration/classification — never to claim live proof;
    live proof stays owner-side by design)."""

    name = "r2"

    def __init__(self, objects: dict[str, bytes] | None = None, configured: bool = True):
        self.objects = objects or {}
        self.configured = configured
        self.download_fails_with: Exception | None = None

    def is_configured(self) -> bool:
        return self.configured

    async def list_files(self, *, prefix: str = "") -> list[str]:
        return [k for k in self.objects if k.startswith(prefix)]

    async def download(self, *, remote_key: str, local_path: Path) -> None:
        if self.download_fails_with is not None and remote_key in self.download_targets:
            raise self.download_fails_with
        if remote_key not in self.objects:
            raise StorageError("remote: NoSuchKey")
        local_path.write_bytes(self.objects[remote_key])

    download_targets: set[str] = set()


def _r2_seam(fake: _FakeR2, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(restore_module, "_build_provider", lambda settings: fake)


class TestR2Leverage:
    def test_unconfigured_r2_is_provider_unavailable_with_env_names(self, tmp_path, monkeypatch):
        _r2_seam(_FakeR2(configured=False), monkeypatch)
        with pytest.raises(ProviderUnavailable, match="R2 is not configured") as exc_info:
            restore_drill(settings=_settings(tmp_path, monkeypatch), latest=True)
        message = str(exc_info.value)
        assert "R2_ACCOUNT_ID" in message and "R2_BUCKET" in message
        # Actionable alternative is named for the operator:
        assert "--from-local" in message

    def test_latest_picks_newest_and_ignores_sidecars(self, tmp_path, monkeypatch):
        older = _sqlite_artifact(tmp_path / "older.sqlite3").read_bytes()
        newer = _sqlite_artifact(tmp_path / "newer.sqlite3").read_bytes()
        fake = _FakeR2(
            {
                "backups/db/20250101-000000/nexus-sqlite-20250101-000000.sqlite3": older,
                "backups/db/20250101-000000/nexus-sqlite-20250101-000000.sqlite3.meta.json": b"{}",
                "backups/db/20250303-030303/nexus-sqlite-20250303-030303.sqlite3": newer,
                "backups/db/20250303-030303/nexus-sqlite-20250303-030303.sqlite3.meta.json": (
                    json.dumps({"sha256": hashlib.sha256(newer).hexdigest()}).encode()
                ),
                # junk keys must never poison discovery:
                "backups/db/not-a-stamp-thing": b"x",
            }
        )
        _r2_seam(fake, monkeypatch)

        report = restore_drill(settings=_settings(tmp_path, monkeypatch), latest=True)

        assert report.ok is True
        states = [s["state"] for s in report.chain]
        assert states[0] == "LATEST_RESOLVED" and "20250303" in report.chain[0]["detail"]
        assert "SIDECAR_FETCHED" in states
        assert "CHECKSUM_VERIFIED" in states
        assert states[-1] == "RESTORE_PROVEN"

    def test_latest_on_empty_bucket_is_actionable(self, tmp_path, monkeypatch):
        _r2_seam(_FakeR2({}), monkeypatch)
        with pytest.raises(RestorePlanError, match="no restorable backup") as exc_info:
            restore_drill(settings=_settings(tmp_path, monkeypatch), latest=True)
        assert "manual backup" in str(exc_info.value)

    def test_missing_sidecar_is_an_honest_warning_not_silence(self, tmp_path, monkeypatch):
        artifact_bytes = _sqlite_artifact(tmp_path / "x.sqlite3").read_bytes()
        fake = _FakeR2(
            {"backups/db/20250202-020202/nexus-sqlite-20250202-020202.sqlite3": artifact_bytes}
        )
        _r2_seam(fake, monkeypatch)

        report = restore_drill(
            settings=_settings(tmp_path, monkeypatch),
            from_r2_key="backups/db/20250202-020202/nexus-sqlite-20250202-020202.sqlite3",
        )

        assert report.ok is True  # legacy artifact: provable, just structurally
        assert any("no sidecar manifest" in w for w in report.warnings)
        assert "CHECKSUM_STRUCTURAL_ONLY" in [s["state"] for s in report.chain]

    def test_download_failure_maps_to_actionable_maintenance_error(self, tmp_path, monkeypatch):
        # S3-style throttling error (fields reproduced without botocore —
        # the taxonomy works on the field shape, not the import).
        class _Throttling(StorageError):
            response = {"Error": {"Code": "SlowDown", "Message": "too many requests"}}

        fake = _FakeR2(
            {"backups/db/20250101-000000/a.sqlite3": b"irrelevant"},
        )
        fake.download_fails_with = _Throttling("SlowDown")
        fake.download_targets = {"backups/db/20250101-000000/a.sqlite3"}
        _r2_seam(fake, monkeypatch)

        with pytest.raises(MaintenanceOperationError) as exc_info:
            restore_drill(
                settings=_settings(tmp_path, monkeypatch),
                from_r2_key="backups/db/20250101-000000/a.sqlite3",
            )
        failure = exc_info.value.failure
        assert failure.category == "transient"
        assert failure.retryable is True
        # Failure must be actionable: what/where/why + concrete next step:
        assert "backoff" in failure.operator_action
        assert "backups/db/20250101-000000/a.sqlite3" in failure.render()

    def test_engine_detection_from_sniffed_bytes(self, tmp_path, monkeypatch):
        # Rename-hiding: a sqlite artifact named without the suffix must
        # still be recognized by its header.
        artifact = _sqlite_artifact(tmp_path / "artifact-no-suffix")
        report = restore_drill(settings=_settings(tmp_path, monkeypatch), from_local=artifact)
        assert report.engine == "sqlite"


class TestPostgresLegTruth:
    def test_structural_drill_for_pg_dump_is_proven(self, tmp_path, monkeypatch, capsys):
        artifact = tmp_path / "backup.sql"
        artifact.write_text(
            "-- PostgreSQL database dump\nCREATE TABLE t (id int);\n"
            "-- PostgreSQL database dump complete\n"
        )
        report = restore_drill(settings=_settings(tmp_path, monkeypatch), from_local=artifact)
        assert report.ok is True
        assert report.engine == "postgres"
        assert report.chain[-1]["state"] == "RESTORE_PROVEN"

    def test_truncated_pg_dump_fails_integrity(self, tmp_path, monkeypatch):
        artifact = tmp_path / "backup.sql"
        artifact.write_text("-- PostgreSQL database dump\nCREATE TABLE t (id int);")
        with pytest.raises(RestoreVerificationError):
            restore_drill(settings=_settings(tmp_path, monkeypatch), from_local=artifact)

    def test_apply_pg_requires_target_url_and_force(self, tmp_path, monkeypatch):
        import shutil

        if shutil.which("pg_restore") is None:
            # Missing binary must be the named blocker — honest, never faked.
            artifact = tmp_path / "backup.sql"
            artifact.write_text(
                "-- PostgreSQL database dump\nSELECT 1;\n-- PostgreSQL database dump complete\n"
            )
            with pytest.raises(RestorePlanError, match="pg_restore not found"):
                restore_drill(
                    settings=_settings(tmp_path, monkeypatch),
                    from_local=artifact,
                    apply=True,
                    target_url="postgresql://example/db",
                    force=True,
                )
        else:
            artifact = tmp_path / "backup.sql"
            artifact.write_text(
                "-- PostgreSQL database dump\nSELECT 1;\n-- PostgreSQL database dump complete\n"
            )
            with pytest.raises(RestorePlanError, match="--force"):
                restore_drill(
                    settings=_settings(tmp_path, monkeypatch),
                    from_local=artifact,
                    apply=True,
                    target_url="postgresql://example/db",
                )
            with pytest.raises(RestorePlanError, match="target-url"):
                restore_drill(
                    settings=_settings(tmp_path, monkeypatch),
                    from_local=artifact,
                    apply=True,
                )
