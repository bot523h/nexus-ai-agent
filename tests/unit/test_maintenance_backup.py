"""Backup verifiability (task-167, P0-C).

Reproduced on main: ``create_backup()`` declared success the moment the
upload returned, with no integrity evidence at all — a truncated, corrupt
or wrong-key artifact reported ``uploaded: True`` identically to a healthy
one, and nothing recorded *when* the successful run happened (so "nightly
backup" status was a silent-failure blindspot: the scheduled workflow could
fail 3/3 runs while the docs claimed nightly backups exist).

These tests pin the release-level contract:

* artifact exists AND non-empty AND sha256-measured,
* integrity verified *by round-trip*: re-download into an isolated temp
  SQLite DB and run ``PRAGMATIC integrity_check`` + table inventory
  (empty/trivial dumps can never pass),
* failure ⇒ non-zero and truthfully flagged (never mislabeled success),
* a SUCCESS summary carries a timestamp.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.maintenance.backup import create_backup


@pytest.fixture(autouse=True)
def _uncached_settings():
    settings_module.get_settings.cache_clear()
    yield
    settings_module.get_settings.cache_clear()


class _FakeR2:
    """In-memory R2 provider: stores objects, can be told to corrupt them."""

    name = "r2"

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.configured = True
        self.corrupt_on_upload: bytes | None = None

    def is_configured(self) -> bool:
        return self.configured

    async def upload(self, *, local_path: Path, remote_key: str) -> None:
        data = local_path.read_bytes()
        if self.corrupt_on_upload is not None:
            data = self.corrupt_on_upload
        self.objects[remote_key] = data

    async def download(self, *, remote_key: str, local_path: Path) -> None:
        local_path.write_bytes(self.objects[remote_key])


def _settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_rows: int = 3) -> Any:
    from nexus_ai_agent.config.settings import Settings

    db = tmp_path / "app.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, payload TEXT)")
        for i in range(db_rows):
            conn.execute("INSERT INTO jobs VALUES (?, ?)", (f"job-{i}", "x" * 128))
        conn.commit()
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    return Settings(db_path=str(db))


def _provider(settings: Any, fake: _FakeR2, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus_ai_agent.maintenance.backup._build_provider", lambda settings: fake)


# ── the core contract: success must be verified, not asserted ────────────────


def test_backup_summary_is_verified_by_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    fake = _FakeR2()
    _provider(settings, fake, monkeypatch)

    summary = create_backup(settings=settings)

    assert summary["uploaded"] is True
    assert summary["verified"] is True, "success without verification is the main bug"
    assert summary["size_bytes"] > 0, "empty artifact must never read as success"
    assert (
        summary["sha256"]
        == hashlib.sha256(
            fake.objects[summary["key"]].read_bytes()
            if isinstance(fake.objects[summary["key"]], Path)
            else fake.objects[summary["key"]]
        ).hexdigest()
    )
    assert summary.get("timestamp"), "success must be timestamped"
    assert summary["verification"]["integrity"] == "ok"
    assert summary["verification"]["tables"]["jobs"] == 3
    assert summary["verification"]["roundtrip"] == "byte-identical"


def test_backup_fails_loudly_when_artifact_is_corrupt_in_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silent-success elimination: bytes that come back wrong must hard-fail."""
    settings = _settings(tmp_path, monkeypatch)
    fake = _FakeR2()
    fake.corrupt_on_upload = b"truncated-garbage"
    _provider(settings, fake, monkeypatch)

    with pytest.raises(RuntimeError):
        create_backup(settings=settings)


def test_backup_refuses_official_success_on_empty_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch, db_rows=0)
    # no tables at all → a zero-content backup is never "success"-grade
    db = Path(settings.db_path)
    db.write_bytes(b"")
    fake = _FakeR2()
    _provider(settings, fake, monkeypatch)

    with pytest.raises(RuntimeError):
        create_backup(settings=settings)


def test_unconfigured_provider_still_fails_closed_with_precise_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.storage.providers.base import ProviderUnavailable

    settings = _settings(tmp_path, monkeypatch)
    fake = _FakeR2()
    fake.configured = False
    assert not fake.is_configured()
    _provider(settings, fake, monkeypatch)

    with pytest.raises(ProviderUnavailable, match="R2"):
        create_backup(settings=settings)


def test_postgres_dump_verifies_via_sha256_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pg_dump route: no temp-DB restore, so verification = byte-identical
    round-trip + non-empty + dump-footer marker. No new tooling is imposed —
    the check uses the download primitive the provider already exposes."""
    from nexus_ai_agent.config.settings import Settings

    monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://u:p@h/db")
    from nexus_ai_agent.config import settings as settings_module

    settings_module.get_settings.cache_clear()
    settings = Settings(db_path=str(tmp_path / "unused.sqlite3"))
    fake = _FakeR2()
    _provider(settings, fake, monkeypatch)

    def _fake_pg_dump(url: str, dest: Path) -> None:
        dest.write_bytes(
            b"-- fake pg dump\nCREATE TABLE t(x);\n-- PostgreSQL database dump complete\n"
        )

    monkeypatch.setattr("nexus_ai_agent.maintenance.backup._dump_postgres", _fake_pg_dump)

    summary = create_backup(settings=settings)
    assert summary["verified"] is True
    assert summary["sha256"] == hashlib.sha256(fake.objects[summary["key"]]).hexdigest()
    assert summary["verification"]["roundtrip"] == "byte-identical"


def test_postgres_dump_rejects_truncated_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.config.settings import Settings

    monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://u:p@h/db")
    from nexus_ai_agent.config import settings as settings_module

    settings_module.get_settings.cache_clear()
    settings = Settings(db_path=str(tmp_path / "unused.sqlite3"))
    fake = _FakeR2()
    fake.corrupt_on_upload = b"-- fake pg dump\ntruncated"  # no completion marker
    _provider(settings, fake, monkeypatch)

    monkeypatch.setattr(
        "nexus_ai_agent.maintenance.backup._dump_postgres",
        lambda url, dest: dest.write_bytes(
            b"-- fake pg dump\n-- PostgreSQL database dump complete\n"
        ),
    )
    with pytest.raises(RuntimeError):
        create_backup(settings=settings)


# ── task-164: failure classification, preflight, restore proof ───────────────
#
# Forensics (maintenance run 36181105635, 2026-09-25, this branch): every one
# of R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_BUCKET /
# NEXUS_DATABASE_URL was ABSENT from the repository's Actions secrets, so the
# nightly job died in ~1 s inside the "not configured" branch — five nights
# in a row, indistinguishable in the UI from a real dump/upload failure, and
# with nothing naming which secret was missing.  These tests pin the fix:
# typed classification, per-variable naming, the CI SQLite refusal, and the
# restore proof against a scratch PostgreSQL.


def _r2_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> Any:
    from nexus_ai_agent.config.settings import Settings

    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    for env in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.delenv(env, raising=False)
    fields = {
        "r2_account_id": "acc",
        "r2_access_key_id": "key",
        "r2_secret_access_key": "secret",
        "r2_bucket": "bucket",
    }
    fields.update(overrides)
    return Settings(db_path=str(tmp_path / "missing.sqlite3"), **fields)


def test_preflight_names_each_missing_variable_individually(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.maintenance.backup import preflight

    settings = _r2_settings(tmp_path, monkeypatch, r2_bucket=None, r2_secret_access_key=None)
    report = preflight(settings)
    assert report["r2"] == {
        "R2_ACCOUNT_ID": True,
        "R2_ACCESS_KEY_ID": True,
        "R2_SECRET_ACCESS_KEY": False,
        "R2_BUCKET": False,
    }
    assert report["r2_complete"] is False
    assert report["database"] == "sqlite"
    assert "R2_SECRET_ACCESS_KEY is not set" in report["problems"]
    assert "R2_BUCKET is not set" in report["problems"]
    assert "R2_ACCOUNT_ID is not set" not in report["problems"]
    assert report["ok"] is False
    # booleans only — the values themselves must never appear in the report
    assert "acc" not in str(report) and "key" not in str(report["r2"])


def test_preflight_require_postgres_refuses_sqlite_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.maintenance.backup import preflight

    settings = _r2_settings(tmp_path, monkeypatch)
    report = preflight(settings, require_postgres=True)
    assert report["ok"] is False
    assert any("--require-postgres" in p for p in report["problems"])


def test_not_configured_is_typed_and_names_only_the_missing_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.maintenance.backup import BackupNotConfigured
    from nexus_ai_agent.storage.providers.base import ProviderUnavailable

    settings = _r2_settings(tmp_path, monkeypatch, r2_bucket=None)
    with pytest.raises(BackupNotConfigured) as info:
        create_backup(settings=settings)
    exc = info.value
    assert isinstance(exc, ProviderUnavailable)  # backwards compatible for callers
    assert exc.classification == "not_configured"
    assert "R2_BUCKET is not set" in str(exc)
    assert "R2_ACCOUNT_ID is not set" not in str(exc)
    assert exc.summary["status"] == "failed"  # type: ignore[attr-defined]
    assert exc.summary["classification"] == "not_configured"  # type: ignore[attr-defined]


def test_require_postgres_blocks_ci_sqlite_backup_even_with_r2_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.maintenance.backup import BackupNotConfigured

    settings = _settings(tmp_path, monkeypatch)  # real SQLite file with rows
    fake = _FakeR2()
    _provider(settings, fake, monkeypatch)
    with pytest.raises(BackupNotConfigured, match="require-postgres"):
        create_backup(settings=settings, require_postgres=True)
    assert fake.objects == {}  # nothing was uploaded


def test_missing_sqlite_source_is_a_dump_failure_not_a_config_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.maintenance.backup import BackupSourceError

    settings = _r2_settings(tmp_path, monkeypatch)  # db_path does not exist
    _provider(settings, _FakeR2(), monkeypatch)
    with pytest.raises(BackupSourceError) as info:
        create_backup(settings=settings)
    assert info.value.classification == "dump_failed"


def test_upload_exception_is_classified_and_never_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.maintenance.backup import BackupUploadError

    settings = _settings(tmp_path, monkeypatch)

    class _Exploding(_FakeR2):
        async def upload(self, *, local_path: Path, remote_key: str) -> None:
            raise OSError("boto: connection reset at postgresql://u:pw@host/db")

    _provider(settings, _Exploding(), monkeypatch)
    with pytest.raises(BackupUploadError) as info:
        create_backup(settings=settings)
    summary = info.value.summary  # type: ignore[attr-defined]
    assert summary["uploaded"] is False and summary["verified"] is False
    assert "pw@" not in summary["error"] and "://***@" in summary["error"]


def test_redact_strips_url_credentials() -> None:
    from nexus_ai_agent.maintenance.backup import redact

    assert (
        redact("x postgresql://alice:s3cr3t@ep.neon.tech/db y")
        == "x postgresql://***@ep.neon.tech/db y"
    )
    assert redact("no creds here") == "no creds here"


# ── restore proof (scratch PostgreSQL) with injected psql/inventory ──────────


class _FakePsql:
    """Records psql invocations; simulates CREATE / load / DROP."""

    def __init__(self, *, load_rc: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.load_rc = load_rc

    def __call__(self, args: list[str], *, timeout: int = 1800) -> Any:
        import subprocess

        self.calls.append(args)
        rc = self.load_rc if "-f" in args else 0
        return subprocess.CompletedProcess(args, rc, stdout="", stderr="boom" if rc else "")


def _pg_backup_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from nexus_ai_agent.config.settings import Settings

    monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://u:p@source/db")
    settings_module.get_settings.cache_clear()
    monkeypatch.setattr(
        "nexus_ai_agent.maintenance.backup._dump_postgres",
        lambda url, dest: dest.write_bytes(
            b"CREATE TABLE public.users(id int);\n-- PostgreSQL database dump complete\n"
        ),
    )
    return Settings(db_path=str(tmp_path / "unused.sqlite3"))


def test_restore_proof_creates_scratch_db_loads_with_on_error_stop_and_drops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _pg_backup_env(tmp_path, monkeypatch)
    fake_r2 = _FakeR2()
    _provider(settings, fake_r2, monkeypatch)
    psql = _FakePsql()
    monkeypatch.setattr("nexus_ai_agent.maintenance.backup._run_psql", psql)
    inventories = {
        "postgresql://u:p@source/db": {"public.users": 3, "public.jobs": 1},
    }

    def _inventory(url: str) -> dict[str, int]:
        return inventories.get(url, {"public.users": 3, "public.jobs": 1})

    monkeypatch.setattr("nexus_ai_agent.maintenance.backup._pg_inventory", _inventory)

    summary = create_backup(
        settings=settings, restore_target_url="postgresql://u:p@scratch/postgres"
    )
    assert summary["status"] == "success"
    assert summary["restore_proven"] is True
    v = summary["verification"]
    assert v["restore"] == "ok" and v["row_counts_match"] is True
    assert v["restore_target_database"].startswith("nexus_restore_")
    # exactly: CREATE DATABASE … TEMPLATE template0 → load → DROP
    assert "TEMPLATE template0" in psql.calls[0][-1]
    assert "--single-transaction" in psql.calls[1] and "-f" in psql.calls[1]
    assert psql.calls[1][0].endswith("/" + v["restore_target_database"])
    assert psql.calls[2][-1].startswith("DROP DATABASE IF EXISTS")


def test_restore_failure_is_typed_and_scratch_db_is_still_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.maintenance.backup import BackupRestoreError

    settings = _pg_backup_env(tmp_path, monkeypatch)
    _provider(settings, _FakeR2(), monkeypatch)
    psql = _FakePsql(load_rc=3)
    monkeypatch.setattr("nexus_ai_agent.maintenance.backup._run_psql", psql)
    monkeypatch.setattr(
        "nexus_ai_agent.maintenance.backup._pg_inventory", lambda url: {"public.users": 3}
    )
    with pytest.raises(BackupRestoreError, match="psql restore failed") as info:
        create_backup(settings=settings, restore_target_url="postgresql://u:p@scratch/postgres")
    assert info.value.classification == "restore_failed"
    assert psql.calls[-1][-1].startswith("DROP DATABASE IF EXISTS")
    summary = info.value.summary  # type: ignore[attr-defined]
    # the upload + round-trip DID happen; only recoverability is unproven
    assert summary["uploaded"] is True and summary["verified"] is True
    assert summary["restore_proven"] is False and summary["status"] == "failed"


def test_restore_with_missing_table_is_incomplete_and_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.maintenance.backup import BackupRestoreError

    settings = _pg_backup_env(tmp_path, monkeypatch)
    _provider(settings, _FakeR2(), monkeypatch)
    monkeypatch.setattr("nexus_ai_agent.maintenance.backup._run_psql", _FakePsql())

    def _inventory(url: str) -> dict[str, int]:
        if "source" in url:
            return {"public.users": 3, "public.jobs": 1}
        return {"public.users": 3}  # jobs table did not come back

    monkeypatch.setattr("nexus_ai_agent.maintenance.backup._pg_inventory", _inventory)
    with pytest.raises(BackupRestoreError, match="missing=\\['public.jobs'\\]"):
        create_backup(settings=settings, restore_target_url="postgresql://u:p@scratch/postgres")


def test_row_count_drift_is_reported_not_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _pg_backup_env(tmp_path, monkeypatch)
    _provider(settings, _FakeR2(), monkeypatch)
    monkeypatch.setattr("nexus_ai_agent.maintenance.backup._run_psql", _FakePsql())
    monkeypatch.setattr(
        "nexus_ai_agent.maintenance.backup._pg_inventory",
        lambda url: {"public.users": 4 if "source" in url else 3},
    )
    summary = create_backup(
        settings=settings, restore_target_url="postgresql://u:p@scratch/postgres"
    )
    v = summary["verification"]
    assert v["row_counts_match"] is False
    assert v["row_count_drift"] == {"public.users": {"source": 4, "restored": 3}}


def test_restore_drill_requires_identical_row_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus_ai_agent.maintenance.backup import BackupRestoreError, restore_drill

    monkeypatch.setattr(
        "nexus_ai_agent.maintenance.backup._dump_postgres",
        lambda url, dest: dest.write_bytes(b"x\n-- PostgreSQL database dump complete\n"),
    )
    monkeypatch.setattr("nexus_ai_agent.maintenance.backup._run_psql", _FakePsql())
    monkeypatch.setattr(
        "nexus_ai_agent.maintenance.backup._pg_inventory",
        lambda url: {"public.t": 2 if "src" in url else 1},
    )
    with pytest.raises(BackupRestoreError, match="row counts differ"):
        restore_drill(source_url="postgresql://u:p@src/db", target_url="postgresql://u:p@tgt/db")

    monkeypatch.setattr(
        "nexus_ai_agent.maintenance.backup._pg_inventory", lambda url: {"public.t": 2}
    )
    ok = restore_drill(source_url="postgresql://u:p@src/db", target_url="postgresql://u:p@tgt/db")
    assert ok["status"] == "success" and ok["verification"]["row_counts_match"] is True


# ── CLI: exit codes + evidence file ──────────────────────────────────────────


def test_cli_preflight_exit_2_and_evidence_json_without_secret_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from typer.testing import CliRunner

    from nexus_ai_agent.cli import app

    for env in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("R2_ACCOUNT_ID", "acc-value-must-not-leak")
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.setenv("NEXUS_DB_PATH", str(tmp_path / "nope.sqlite3"))
    settings_module.get_settings.cache_clear()
    evidence = tmp_path / "ev" / "preflight.json"

    result = CliRunner().invoke(
        app, ["maintenance", "backup", "--preflight", "--evidence-json", str(evidence)]
    )
    assert result.exit_code == 2, result.output
    assert "R2_ACCOUNT_ID: present" in result.output
    assert "R2_BUCKET: MISSING" in result.output
    assert "acc-value-must-not-leak" not in result.output
    data = json.loads(evidence.read_text())
    assert data["preflight"]["ok"] is False
    assert "acc-value-must-not-leak" not in evidence.read_text()


def test_cli_backup_exit_code_distinguishes_not_configured_from_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from typer.testing import CliRunner

    from nexus_ai_agent.cli import app

    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    for env in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("NEXUS_DB_PATH", str(tmp_path / "nope.sqlite3"))
    settings_module.get_settings.cache_clear()
    evidence = tmp_path / "backup.json"

    # not configured → 2, evidence says so
    result = CliRunner().invoke(app, ["maintenance", "backup", "--evidence-json", str(evidence)])
    assert result.exit_code == 2, result.output
    assert json.loads(evidence.read_text())["classification"] == "not_configured"
    assert "✅" not in result.output

    # configured but the source is missing → 1 (a real failure)
    for env in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.setenv(env, "x")
    settings_module.get_settings.cache_clear()
    result = CliRunner().invoke(app, ["maintenance", "backup", "--evidence-json", str(evidence)])
    assert result.exit_code == 1, result.output
    assert json.loads(evidence.read_text())["classification"] == "dump_failed"
