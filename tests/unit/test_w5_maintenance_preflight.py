"""The real maintenance entry points fail before remote or file effects."""

from pathlib import Path

import pytest

from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.maintenance import backup, restore
from nexus_ai_agent.storage.encrypted_backup_drill import BackupPreflightError


def test_backup_invalid_preflight_precedes_provider_and_dump(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backup, "enforce_postgres_policy", lambda: None)
    monkeypatch.setattr(
        backup, "_build_provider", lambda _: pytest.fail("provider was constructed")
    )
    monkeypatch.setattr(backup, "_dump_sqlite", lambda *_: pytest.fail("database was dumped"))
    with pytest.raises(BackupPreflightError):
        backup.create_backup(settings=Settings.model_construct())


def test_remote_restore_invalid_preflight_precedes_provider_and_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(restore, "enforce_postgres_policy", lambda: None)
    monkeypatch.setattr(
        restore, "_build_provider", lambda _: pytest.fail("provider was constructed")
    )
    monkeypatch.setattr(restore, "_ScratchDir", lambda *_: pytest.fail("scratch was created"))
    target = tmp_path / "target.sqlite"
    with pytest.raises(BackupPreflightError):
        restore.restore_drill(
            settings=Settings.model_construct(), latest=True, target=target, apply=True
        )
    assert not target.exists()


def test_local_restore_does_not_require_cloud_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    monkeypatch.setattr(
        restore,
        "verify_production_backup_preflight",
        lambda *_a, **_k: pytest.fail("cloud preflight called"),
    )
    artifact = tmp_path / "local.sqlite"
    with sqlite3.connect(artifact) as db:
        db.execute("CREATE TABLE local_data (value INTEGER)")
        db.execute("INSERT INTO local_data VALUES (1)")
    report = restore.restore_drill(settings=Settings.model_construct(), from_local=artifact)
    assert report.ok


@pytest.mark.parametrize("invalid", ["tls", "bucket"])
def test_invalid_postgres_or_bucket_rejected_before_backup_effects(
    invalid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "postgresql://u:p@db.example/app?sslmode=" + (
        "require" if invalid == "tls" else "verify-full"
    )
    settings = Settings.model_construct(
        database_url=url,
        r2_account_id="0123456789abcdef0123456789abcdef",
        r2_access_key_id="access-key",
        r2_secret_access_key="secret-key",
        r2_bucket="bad.bucket" if invalid == "bucket" else "valid-bucket",
    )
    monkeypatch.setattr(backup, "enforce_postgres_policy", lambda: url)
    monkeypatch.setattr(
        backup, "_build_provider", lambda _: pytest.fail("provider was constructed")
    )
    monkeypatch.setattr(backup, "_dump_postgres", lambda *_: pytest.fail("database was dumped"))
    with pytest.raises(BackupPreflightError):
        backup.create_backup(settings=settings)


def test_maintenance_import_and_preflight_without_crypto() -> None:
    """A fresh process must not need cryptography for structural preflight."""
    import subprocess
    import sys

    script = """
import builtins
original = builtins.__import__
def blocked(name, *args, **kwargs):
    if name == 'cryptography' or name.startswith('cryptography.'):
        raise ModuleNotFoundError('cryptography deliberately unavailable')
    return original(name, *args, **kwargs)
builtins.__import__ = blocked
from nexus_ai_agent.maintenance import backup, restore
from nexus_ai_agent.storage.encrypted_backup_drill import verify_production_backup_preflight
assert verify_production_backup_preflight({}, require_remote_tier=False) == 'REMOTE_UNCONFIGURED'
assert backup.create_backup and restore.restore_drill
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
