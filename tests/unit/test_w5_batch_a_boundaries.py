"""W5 backup failure boundaries and redaction invariants."""

from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.maintenance import backup
from nexus_ai_agent.maintenance.failures import (
    ActionableFailure,
    MaintenanceOperationError,
)
from nexus_ai_agent.storage.providers.base import ProviderUnavailable


def test_missing_pg_dump_fails_without_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = Mock()
    monkeypatch.setattr(backup.shutil, "which", lambda _: None)
    monkeypatch.setattr(backup.subprocess, "run", runner)
    with pytest.raises(RuntimeError, match="not found"):
        backup._dump_postgres("postgresql://user:secret@host/db", tmp_path / "dump")
    runner.assert_not_called()


@pytest.mark.parametrize("failure", ["bad-url", "os-error", "no-password"])
def test_pg_dump_errors_do_not_leak_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.setattr(backup.shutil, "which", lambda _: "/fake/pg_dump")
    runner = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(backup.subprocess, "run", runner)
    if failure == "bad-url":
        monkeypatch.setattr(
            backup, "split_dsn_password", Mock(side_effect=ValueError("secret-token"))
        )
    elif failure == "os-error":
        runner.side_effect = OSError("secret-token")
    else:
        monkeypatch.setattr(
            backup, "split_dsn_password", Mock(return_value=("postgresql://host/db", None))
        )
        monkeypatch.delenv("PGPASSWORD", raising=False)
    if failure == "no-password":
        backup._dump_postgres("postgresql://host/db", tmp_path / "dump")
        assert "PGPASSWORD" not in runner.call_args.kwargs["env"]
    else:
        with pytest.raises(RuntimeError) as caught:
            backup._dump_postgres("postgresql://user:secret-token@host/db", tmp_path / "dump")
        assert "secret-token" not in str(caught.value)
        assert caught.value.__context__ is None
        if failure == "bad-url":
            runner.assert_not_called()


def test_missing_sqlite_source_is_not_created(tmp_path: Path) -> None:
    source = tmp_path / "missing.db"
    with pytest.raises(RuntimeError, match="not found"):
        backup._dump_sqlite(source, tmp_path / "copy.db")
    assert not source.exists() and not (tmp_path / "copy.db").exists()


def test_integrity_failure_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def execute(self, query: str):
            assert query == "PRAGMA integrity_check"
            return Mock(fetchall=Mock(return_value=[("corruption",)]))

    source = tmp_path / "backup"
    source.write_bytes(b"not used by fake connection")
    monkeypatch.setattr(backup.sqlite3, "connect", lambda _: Connection())
    monkeypatch.setattr(backup, "_sqlite_inventory", lambda *_: {"jobs": 1})
    with pytest.raises(RuntimeError, match="integrity_check failed"):
        backup._verify_sqlite_dump(source)


def test_postgres_footer_must_be_last_meaningful_content(tmp_path: Path) -> None:
    path = tmp_path / "dump.sql"
    path.write_bytes(b"\n--\n\\unrestrict token\n")
    assert backup._last_meaningful_line_of_file(path) == b""
    with pytest.raises(RuntimeError, match="truncated"):
        backup._verify_postgres_dump(path)
    path.write_bytes(b"")
    with pytest.raises(RuntimeError, match="empty"):
        backup._verify_postgres_dump(path)


@pytest.mark.parametrize("category", ["unknown", "network", "secret-token-canary"])
def test_upload_actionable_rewrites_provider_error_safely(tmp_path: Path, category: str) -> None:
    secret = "secret-token-canary"
    failure = ActionableFailure(secret, secret, secret, category, True, secret)
    provider = Mock(upload=AsyncMock(side_effect=MaintenanceOperationError(failure)))
    with pytest.raises(MaintenanceOperationError) as caught:
        backup._upload_actionable(provider, local_path=tmp_path / "dump", key="object")
    assert secret not in str(caught.value)
    assert secret not in caught.value.failure.why
    assert secret not in caught.value.failure.operator_action
    assert caught.value.failure.what == "upload"
    assert caught.value.failure.where == "object"
    assert caught.value.failure.category == ("unknown" if category == secret else category)
    import traceback

    assert secret not in "".join(traceback.format_exception(caught.value))


def test_dry_run_has_no_provider_or_dump_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backup, "enforce_postgres_policy", lambda: None)
    provider = Mock(side_effect=AssertionError("provider must not be constructed"))
    monkeypatch.setattr(backup, "_build_provider", provider)
    summary = backup.create_backup(
        settings=Settings(db_path=str(tmp_path / "missing")), dry_run=True
    )
    assert not summary["uploaded"] and not summary["verified"]
    assert not (tmp_path / "missing").exists()
    provider.assert_not_called()


@pytest.mark.parametrize("failed_read", [1, 2])
def test_backup_readback_failure_never_reports_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_read: int
) -> None:
    import sqlite3

    from nexus_ai_agent.storage.providers.base import StorageError

    db = tmp_path / "db.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE data (id INTEGER)")
        conn.execute("INSERT INTO data VALUES (1)")
    monkeypatch.setattr(backup, "enforce_postgres_policy", lambda: None)
    monkeypatch.setattr(backup, "verify_production_backup_preflight", Mock())
    provider = Mock(is_configured=Mock(return_value=True))
    provider._bucket = "bucket"
    uploads: list[str] = []
    monkeypatch.setattr(backup, "_build_provider", lambda _: provider)
    monkeypatch.setattr(
        backup, "_upload_actionable", lambda *args, **kwargs: uploads.append(kwargs["key"])
    )
    calls = 0

    def readback(*args: object, **kwargs: object) -> dict[str, str]:
        nonlocal calls
        calls += 1
        if calls == failed_read:
            raise StorageError("secret-canary")
        return {"roundtrip": "byte-identical"}

    monkeypatch.setattr(backup, "_verify_round_trip", readback)
    with pytest.raises(MaintenanceOperationError) as caught:
        backup.create_backup(settings=Settings(db_path=str(db)))
    assert calls == failed_read and len(uploads) == failed_read
    assert caught.value.failure.what == ("readback" if failed_read == 1 else "sidecar readback")
    assert "secret-canary" not in str(caught.value)
    assert caught.value.__cause__ is not None and "secret-canary" not in str(caught.value.__cause__)


def test_empty_dump_is_never_uploaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "db"
    db.touch()
    monkeypatch.setattr(backup, "enforce_postgres_policy", lambda: None)
    monkeypatch.setattr(backup, "verify_production_backup_preflight", Mock())
    provider = Mock(is_configured=Mock(return_value=True))
    monkeypatch.setattr(backup, "_build_provider", lambda _: provider)
    monkeypatch.setattr(backup, "_dump_sqlite", lambda src, dst: dst.write_bytes(b""))
    monkeypatch.setattr(backup, "_verify_sqlite_dump", lambda _: {})
    upload = Mock()
    monkeypatch.setattr(backup, "_upload_actionable", upload)
    with pytest.raises(RuntimeError, match="empty"):
        backup.create_backup(settings=Settings(db_path=str(db)))
    upload.assert_not_called()


def test_footer_tail_window_does_not_read_entire_dump(tmp_path: Path) -> None:
    path = tmp_path / "dump.sql"
    path.write_bytes(b"prefix" * 100 + b"\n-- PostgreSQL database dump complete\n")
    assert backup._last_meaningful_line_of_file(path, window=64) == backup._PG_DUMP_FOOTER.encode()


@pytest.mark.parametrize(
    "r2_configured,error", [(True, "problem"), (False, "problem"), (False, "")]
)
def test_partial_b2_fallback_requires_configured_r2(
    monkeypatch: pytest.MonkeyPatch, r2_configured: bool, error: str
) -> None:
    b2 = Mock(is_configured=Mock(return_value=False), configuration_error=Mock(return_value=error))
    r2 = Mock(is_configured=Mock(return_value=r2_configured))
    monkeypatch.setattr(backup.B2Provider, "from_settings", Mock(return_value=b2))
    monkeypatch.setattr(backup.R2Provider, "from_settings", Mock(return_value=r2))
    settings = Settings(b2_bucket="partially-configured")
    if r2_configured:
        assert backup.build_blob_provider(settings) is r2
    else:
        with pytest.raises(ProviderUnavailable, match=error or "B2 configuration is incomplete"):
            backup.build_blob_provider(settings)


def test_provider_unavailable_is_scrubbed_before_propagation(tmp_path: Path) -> None:
    import traceback

    error = ProviderUnavailable("secret-canary")
    provider = Mock(upload=AsyncMock(side_effect=error))
    with pytest.raises(ProviderUnavailable) as caught:
        backup._upload_actionable(provider, local_path=tmp_path / "dump", key="object")
    assert caught.value is error
    assert "secret-canary" not in "".join(traceback.format_exception(error))
