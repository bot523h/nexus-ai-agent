"""The maintenance lane must be fail-closed in production and real in CI.

Two contracts are tested here:

* **fail closed** — a SQLite URL in ``NEXUS_DATABASE_URL`` is rejected (the
  SQLite lane is selected by ``NEXUS_DB_PATH``), and an empty URL with
  ``NEXUS_REQUIRE_POSTGRES=true`` refuses the fallback. A maintenance CLI that
  silently backed up the wrong target would be worse than a red job.
* **real in CI** — with no cloud secrets the self-test creates a genuine
  SQLite WAL database with the application schema and runs backup ->
  restore-drill -> reconciliation end to end, writing verifiable evidence.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from nexus_ai_agent.config.settings import Settings, get_settings
from nexus_ai_agent.maintenance import ci_selftest
from nexus_ai_agent.maintenance.backup import build_blob_provider, local_maintenance_tier
from nexus_ai_agent.storage.db import BackendPolicyError, enforce_postgres_policy


def _fresh_settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    for name in (
        "NEXUS_DATABASE_URL",
        "DATABASE_URL",
        "NEXUS_REQUIRE_POSTGRES",
        "NEXUS_MAINTENANCE_LOCAL_TIER",
        "NEXUS_B2_KEY_ID",
        "NEXUS_B2_APPLICATION_KEY",
        "NEXUS_B2_BUCKET",
        "NEXUS_B2_ENDPOINT",
        "NEXUS_B2_REGION",
        "R2_ACCOUNT_ID",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "R2_BUCKET",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


def test_sqlite_url_in_database_url_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SQLite lane is NEXUS_DB_PATH; a sqlite URL must never mean "back it up"."""
    _fresh_settings(monkeypatch, NEXUS_DATABASE_URL="sqlite:///tmp/x.sqlite3")
    with pytest.raises(ValueError, match="only postgresql"):
        enforce_postgres_policy()
    get_settings.cache_clear()


def test_empty_url_with_postgres_required_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _fresh_settings(monkeypatch, NEXUS_REQUIRE_POSTGRES="true")
    with pytest.raises(BackendPolicyError):
        enforce_postgres_policy()
    get_settings.cache_clear()


def test_self_test_refuses_a_configured_production_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://user:pw@host/db")
    with pytest.raises(ci_selftest.SelfTestError, match="never runs against a configured"):
        ci_selftest.run(
            only="backup",
            database=tmp_path / "db.sqlite3",
            tier=tmp_path / "tier",
            workspace_root=tmp_path,
        )


def test_prepare_database_creates_a_wal_schema(tmp_path: Path) -> None:
    evidence = ci_selftest.prepare_database(tmp_path / "app.sqlite3")
    assert evidence["journal_mode"] == "wal"
    assert evidence["tables"] >= 10
    with sqlite3.connect(evidence["path"]) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_local_tier_is_opt_in_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _fresh_settings(monkeypatch)
    assert local_maintenance_tier(Settings(_env_file=None)) is None
    settings = Settings(_env_file=None).model_copy(
        update={"maintenance_local_tier": str(tmp_path / "tier")}
    )
    assert local_maintenance_tier(settings) == tmp_path / "tier"


def test_blob_provider_uses_the_local_tier_only_without_a_cloud_tier(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fresh_settings(monkeypatch)
    settings = Settings(_env_file=None).model_copy(
        update={"maintenance_local_tier": str(tmp_path / "tier")}
    )
    provider = build_blob_provider(settings)
    assert provider.name == "local_cache"
    assert provider.is_configured() is True


def test_blob_provider_still_prefers_a_configured_r2_tier(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fresh_settings(monkeypatch)
    settings = Settings(_env_file=None).model_copy(
        update={
            "maintenance_local_tier": str(tmp_path / "tier"),
            "r2_account_id": "acct",
            "r2_access_key_id": "key",
            "r2_secret_access_key": "secret",
            "r2_bucket": "bucket",
        }
    )
    provider = build_blob_provider(settings)
    assert provider.name != "local_cache", "a real tier must never be replaced by the CI tier"


def test_full_self_test_runs_backup_drill_and_reconcile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fresh_settings(monkeypatch)
    database = tmp_path / "nexus_maintenance_ci.sqlite3"
    tier = tmp_path / "tier"
    evidence = ci_selftest.run(only="all", database=database, tier=tier, workspace_root=tmp_path)
    assert evidence["ok"] is True
    assert evidence["database"]["journal_mode"] == "wal"
    assert evidence["backup"]["verified"] is True
    artifact = Path(evidence["backup"]["artifact_path"])
    assert artifact.is_file(), "the drill must consume the uploaded artifact"
    assert evidence["drill"]["ok"] is True
    assert "violations" in evidence["reconcile"]
    get_settings.cache_clear()


def test_cli_writes_the_evidence_document(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _fresh_settings(monkeypatch)
    out = tmp_path / "evidence.json"
    code = ci_selftest.main(
        [
            "--only",
            "backup",
            "--db",
            str(tmp_path / "db.sqlite3"),
            "--tier",
            str(tmp_path / "tier"),
            "--workspace-root",
            str(tmp_path),
            "--json-out",
            str(out),
        ]
    )
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["mode"] == "ci-selftest"
    assert payload["backup"]["verified"] is True
    assert "✅ maintenance self-test" in capsys.readouterr().out
    get_settings.cache_clear()


def test_cli_reports_a_missing_artifact(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _fresh_settings(monkeypatch)
    code = ci_selftest.main(
        [
            "--only",
            "drill",
            "--db",
            str(tmp_path / "db.sqlite3"),
            "--tier",
            str(tmp_path / "tier"),
            "--workspace-root",
            str(tmp_path),
        ]
    )
    assert code == 1
    get_settings.cache_clear()


def test_workflow_wires_the_self_test_when_the_secret_is_absent() -> None:
    text = (Path(__file__).resolve().parents[2] / ".github/workflows/maintenance.yml").read_text(
        encoding="utf-8"
    )
    assert "nexus_ai_agent.maintenance.ci_selftest" in text
    assert "--only all" in text
    assert "--only reconcile" in text
    assert "steps.mode.outputs.mode == 'ci'" in text
    assert "maintenance_ci_selftest.json" in text
