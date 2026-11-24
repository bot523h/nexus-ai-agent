"""Restore safety and error-path tests for W5."""

from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.maintenance import restore
from nexus_ai_agent.maintenance.failures import MaintenanceOperationError
from nexus_ai_agent.storage.providers.base import ProviderUnavailable


def test_fail_scrubs_exception_and_records_failure() -> None:
    report = restore.DrillReport(mode="drill")
    error = ValueError("secret-canary")
    with pytest.raises(ValueError) as caught:
        restore._fail(report, "FETCH", error)
    assert "secret-canary" not in str(caught.value)
    assert report.chain[-1]["ok"] is False
    assert "secret-canary" not in str(report.as_dict())


def test_list_provider_unavailable_scrubs_credentials() -> None:
    error = ProviderUnavailable("secret-canary")
    provider = Mock(list_files=AsyncMock(side_effect=error))
    with pytest.raises(ProviderUnavailable) as caught:
        restore.resolve_latest_r2_key(provider)
    assert "secret-canary" not in str(caught.value)


@pytest.mark.parametrize(
    "header,engine",
    [(b"SQLite format 3 rest", "sqlite"), (b"-- PostgreSQL database dump", "postgres")],
)
def test_sniff_engine_from_artifact_bytes(tmp_path: Path, header: bytes, engine: str) -> None:
    path = tmp_path / "dump"
    path.write_bytes(header)
    assert restore._detect_engine(path, "unknown") == engine


def test_unknown_or_missing_artifact_refused(tmp_path: Path) -> None:
    path = tmp_path / "dump"
    with pytest.raises(restore.RestorePlanError, match="not readable"):
        restore._detect_engine(path, "unknown")
    path.write_bytes(b"not a database")
    with pytest.raises(restore.RestoreVerificationError, match="refusing to guess"):
        restore._detect_engine(path, "unknown")


def test_checksum_rejects_invalid_sidecar_without_untrusted_content(tmp_path: Path) -> None:
    artifact = tmp_path / "dump"
    artifact.write_bytes(b"data")
    sidecar = tmp_path / "sidecar"
    sidecar.write_text('{"secret-canary":')
    report = restore.DrillReport(mode="drill")
    with pytest.raises(restore.RestoreVerificationError) as caught:
        restore._verify_checksum(artifact, report, expected_sha256=None, sidecar=sidecar)
    assert "secret-canary" not in str(caught.value)
    assert not report.ok
    assert restore._sidecar_manifest_problem([]) == "not a JSON object (list)"


@pytest.mark.parametrize(
    "error", [TimeoutError("secret-canary"), ProviderUnavailable("secret-canary")]
)
def test_remote_fetch_failure_cleans_caller_owned_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    stage = tmp_path / "scratch"
    provider = Mock(is_configured=Mock(return_value=True), download=AsyncMock(side_effect=error))
    monkeypatch.setattr(restore, "enforce_postgres_policy", lambda: None)
    monkeypatch.setattr(restore, "verify_production_backup_preflight", Mock())
    monkeypatch.setattr(restore, "_build_provider", lambda _: provider)
    original = restore.tempfile.mkdtemp
    monkeypatch.setattr(
        restore.tempfile, "mkdtemp", lambda prefix: original(prefix=prefix, dir=tmp_path)
    )
    with pytest.raises((MaintenanceOperationError, ProviderUnavailable)) as caught:
        restore.restore_drill(settings=Settings(), from_r2_key="backups/db/file.sqlite3")
    assert "secret-canary" not in str(caught.value)
    assert list(tmp_path.iterdir()) == []
    assert not stage.exists()


def test_scratch_double_exit_is_safe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = restore.tempfile.mkdtemp
    monkeypatch.setattr(
        restore.tempfile, "mkdtemp", lambda prefix: original(prefix=prefix, dir=tmp_path)
    )
    scratch = restore._ScratchDir("test_")
    with scratch as path:
        (path / "content").write_text("content")
    scratch.__exit__(None, None, None)
    assert list(tmp_path.iterdir()) == []


def test_restore_validates_source_and_refuses_same_postgres_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus_ai_agent.storage import db

    with pytest.raises(restore.RestorePlanError, match="exactly one"):
        restore.restore_drill(settings=Settings())
    monkeypatch.setattr(db, "resolve_database_url", lambda: "postgresql://a:secret@host/db")
    with pytest.raises(restore.RestorePlanError, match="configured source"):
        restore._guard_target_identity("postgresql://other@host/db")
    restore._guard_target_identity("postgresql://other@host/other")


def test_sqlite_restore_failure_cleans_staging_and_preserves_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.write_bytes(b"new")
    target.write_bytes(b"old")
    monkeypatch.setattr(restore.os, "replace", Mock(side_effect=OSError("failure")))
    report = restore.DrillReport(mode="apply")
    with pytest.raises(OSError, match="failure"):
        restore._apply_sqlite_restore(
            source, target, report, force=True, stamp="stamp", expect_app_schema=False
        )
    assert target.read_bytes() == b"old"
    assert (tmp_path / "target.pre-restore-stamp").read_bytes() == b"old"
    assert not list(tmp_path.glob("target.restore-*"))


@pytest.mark.parametrize("result", ["oserror", "nonzero", "success"])
def test_postgres_row_count_subprocess_errors_are_safe(
    monkeypatch: pytest.MonkeyPatch, result: str
) -> None:
    runner = Mock()
    if result == "oserror":
        runner.side_effect = OSError("secret-canary")
    elif result == "nonzero":
        runner.return_value = Mock(returncode=1, stderr="secret-canary")
    else:
        runner.return_value = Mock(returncode=0, stdout="odd\t3\n\tignored\n")
    monkeypatch.setattr(restore.subprocess, "run", runner)
    if result == "success":
        assert restore._list_postgres_row_counts("psql", "postgresql://host/db", ["odd"]) == {
            "odd": 3
        }
    else:
        with pytest.raises(restore.RestoreVerificationError) as caught:
            restore._list_postgres_row_counts("psql", "postgresql://host/db", ["odd"])
        assert "secret-canary" not in str(caught.value)
        assert caught.value.__context__ is None


def test_postgres_passfile_rejects_newline_and_does_not_create_file(tmp_path: Path) -> None:
    with pytest.raises(restore.RestorePlanError, match="credential"):
        restore._pg_psql_env("postgresql://user:secret%0Avalue@host/db", tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("url,force", [(None, True), ("postgresql://host/db", False)])
def test_postgres_apply_requires_target_and_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str | None, force: bool
) -> None:
    monkeypatch.setattr(restore.shutil, "which", lambda _: "/mock/psql")
    runner = Mock()
    monkeypatch.setattr(restore.subprocess, "run", runner)
    with pytest.raises(restore.RestorePlanError):
        restore._apply_postgres_restore(
            tmp_path / "dump", url, restore.DrillReport(mode="apply"), force=force, scratch=tmp_path
        )
    runner.assert_not_called()


@pytest.mark.parametrize("reason", ["exception", "nonzero"])
def test_postgres_apply_failure_is_redacted_and_never_claims_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    artifact = tmp_path / "dump.sql"
    artifact.write_text("CREATE TABLE sample (id integer);")
    monkeypatch.setattr(restore.shutil, "which", lambda _: "/mock/psql")
    runner = (
        Mock(side_effect=OSError("secret-canary"))
        if reason == "exception"
        else Mock(return_value=Mock(returncode=1, stderr="secret-canary"))
    )
    monkeypatch.setattr(restore.subprocess, "run", runner)
    report = restore.DrillReport(mode="apply")
    with pytest.raises(restore.RestoreVerificationError) as caught:
        restore._apply_postgres_restore(
            artifact,
            "postgresql://user:secret-canary@host/db",
            report,
            force=True,
            scratch=tmp_path,
        )
    assert "secret-canary" not in str(caught.value)
    assert not report.ok
    assert "--single-transaction" in runner.call_args.args[0]
    assert "secret-canary" not in repr(runner.call_args.args[0])


def test_sqlite_restore_rejects_postcopy_integrity_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"unverified")
    target = tmp_path / "target"
    target.write_bytes(b"old")
    monkeypatch.setattr(
        restore, "_verify_sqlite_dump", Mock(side_effect=RuntimeError("secret-canary"))
    )
    report = restore.DrillReport(mode="apply")
    with pytest.raises(restore.RestoreVerificationError) as caught:
        restore._apply_sqlite_restore(
            source, target, report, force=True, stamp="stamp", expect_app_schema=False
        )
    assert "secret-canary" not in str(caught.value)
    assert (tmp_path / "target.pre-restore-stamp").read_bytes() == b"old"
    assert not report.ok


@pytest.mark.parametrize(
    "mode,safety,warning", [("drill", False, False), ("apply", False, True), ("apply", True, False)]
)
def test_restore_main_human_output_reflects_report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    mode: str,
    safety: bool,
    warning: bool,
) -> None:
    report = restore.DrillReport(mode=mode, engine="sqlite", ok=True)
    report.record("FETCHED", True)
    report.record("OPTIONAL", False)
    if warning:
        report.warnings.append("example warning")
    if safety:
        report.restored["safety_copy"] = "/tmp/safety"
    report.restored["target"] = "target"
    monkeypatch.setattr(restore, "restore_drill", Mock(return_value=report))
    assert restore.main(["--from-local", "unused"]) == 0
    output = capsys.readouterr().out
    assert "FETCHED✓" in output and "OPTIONAL✗" in output
    assert ("warning:" in output) == warning
    assert ("target:" in output) == (mode == "apply")
    assert ("safety:" in output) == safety


def test_restore_main_reports_runtime_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(restore, "restore_drill", Mock(side_effect=RuntimeError("failure")))
    assert restore.main(["--from-local", "unused"]) == 1
    assert "failure" in capsys.readouterr().err


def test_postgres_row_count_empty_list_never_starts_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = Mock()
    monkeypatch.setattr(restore.subprocess, "run", runner)
    assert restore._list_postgres_row_counts("psql", "postgresql://host/db", []) == {}
    runner.assert_not_called()


def test_passwordless_target_creates_no_passfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PGPASSFILE", raising=False)
    url, env = restore._pg_psql_env("postgresql://host/db", tmp_path)
    assert url == "postgresql://host/db" and "PGPASSFILE" not in env
    assert list(tmp_path.iterdir()) == []


def test_restore_with_no_app_tables_does_not_fabricate_schema_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    source = tmp_path / "source"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE example (id INTEGER)")
    target = tmp_path / "target"
    monkeypatch.setattr(restore, "_expected_app_tables", lambda: set())
    report = restore.DrillReport(mode="apply")
    restore._apply_sqlite_restore(
        source, target, report, force=False, stamp="stamp", expect_app_schema=True
    )
    assert target.exists()
    assert not any(entry["state"] == "APP_SCHEMA_READABLE" for entry in report.chain)


def test_postgres_apply_records_app_schema_only_after_successful_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "dump.sql"
    artifact.write_text("CREATE TABLE example (id integer);")
    monkeypatch.setattr(restore.shutil, "which", lambda _: "/mock/psql")
    monkeypatch.setattr(restore.subprocess, "run", Mock(return_value=Mock(returncode=0)))
    monkeypatch.setattr(restore, "_list_postgres_tables", lambda *_: {"example"})
    monkeypatch.setattr(restore, "_expected_app_tables", lambda: {"example"})
    report = restore.DrillReport(mode="apply")
    restore._apply_postgres_restore(
        artifact,
        "postgresql://host/db",
        report,
        force=True,
        scratch=tmp_path,
        expect_app_schema=True,
    )
    assert any(step["state"] == "APP_SCHEMA_READABLE" and step["ok"] for step in report.chain)
    assert any(step["state"] == "POST_RESTORE_DATA" and not step["ok"] for step in report.chain)


def test_remote_restore_preflight_checks_selected_postgres_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "postgresql://host/db?sslmode=verify-full"
    monkeypatch.setattr(restore, "enforce_postgres_policy", lambda: url)
    preflight = Mock()
    monkeypatch.setattr(restore, "verify_production_backup_preflight", preflight)
    monkeypatch.setattr(
        restore, "_build_provider", lambda _: Mock(is_configured=Mock(return_value=False))
    )
    with pytest.raises(ProviderUnavailable):
        restore.restore_drill(settings=Settings(), from_r2_key="backup")
    assert preflight.call_count == 2
    assert preflight.call_args_list[1].args[0].database_url == url


def test_sqlite_apply_cannot_use_only_postgres_target_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    artifact = tmp_path / "source.sqlite3"
    with sqlite3.connect(artifact) as conn:
        conn.execute("CREATE TABLE example (id INTEGER)")
    monkeypatch.setattr(restore, "_guard_target_identity", Mock())
    with pytest.raises(restore.RestorePlanError, match="sqlite --apply needs"):
        restore.restore_drill(
            settings=Settings(), from_local=artifact, apply=True, target_url="postgresql://other/db"
        )


def test_restore_proven_label_requires_successful_data_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    artifact = tmp_path / "source.sqlite3"
    with sqlite3.connect(artifact) as conn:
        conn.execute("CREATE TABLE example (id INTEGER)")

    def apply(_artifact, _target, report, **_kwargs):
        report.record("POST_RESTORE_DATA", True, "data verified")

    monkeypatch.setattr(restore, "_apply_sqlite_restore", apply)
    report = restore.restore_drill(
        settings=Settings(), from_local=artifact, target=tmp_path / "target", apply=True
    )
    assert report.ok
    assert "data row counts match" in report.chain[-1]["detail"]


def test_remote_restore_success_cleans_download_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    from nexus_ai_agent.storage.providers.base import StorageError

    original_temp = restore.tempfile.mkdtemp
    monkeypatch.setattr(
        restore.tempfile,
        "mkdtemp",
        lambda suffix=None, prefix=None, dir=None: original_temp(
            suffix=suffix, prefix=prefix, dir=tmp_path
        ),
    )
    monkeypatch.setattr(restore, "enforce_postgres_policy", lambda: None)
    monkeypatch.setattr(restore, "verify_production_backup_preflight", Mock())
    provider = Mock(is_configured=Mock(return_value=True))

    async def download(*, remote_key: str, local_path: Path) -> None:
        if remote_key.endswith(".meta.json"):
            raise StorageError("missing sidecar")
        with sqlite3.connect(local_path) as conn:
            conn.execute("CREATE TABLE t (id INTEGER)")

    provider.download = download
    monkeypatch.setattr(restore, "_build_provider", lambda _: provider)
    report = restore.restore_drill(settings=Settings(), from_r2_key="backups/db/file.sqlite3")
    assert report.ok and report.warnings
    assert list(tmp_path.iterdir()) == []
