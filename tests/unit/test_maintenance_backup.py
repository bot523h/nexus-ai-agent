"""P0-C: a backup is verified, never asserted (owner directive §10).

The failure classes the directive names are each driven to a red result here:
missing R2 configuration, corrupt source, empty database, a semantically wrong
artifact, a truncated upload, remote corruption, and a restore that cannot
reproduce the source — every one of them must raise, never return a summary.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.maintenance import backup as backup_module
from nexus_ai_agent.maintenance.backup import (
    BackupVerificationError,
    _verify_sqlite_artifact,
    create_backup,
)
from nexus_ai_agent.storage.providers.base import ProviderUnavailable, StorageError


class _FakeR2:
    """An in-memory blob tier with knobs for the failure modes we must catch."""

    def __init__(self, *, configured: bool = True) -> None:
        self.objects: dict[str, bytes] = {}
        self.configured = configured
        self.corrupt_on_download = False
        self.truncate_on_download = False
        self.fail_upload = False
        self.uploads: list[str] = []

    def is_configured(self) -> bool:
        return self.configured

    async def upload(self, *, local_path: Path, remote_key: str) -> None:
        if self.fail_upload:
            raise RuntimeError("network down")
        self.uploads.append(remote_key)
        self.objects[remote_key] = Path(local_path).read_bytes()

    async def download(self, *, remote_key: str, local_path: Path) -> None:
        data = self.objects[remote_key]
        if self.truncate_on_download:
            data = data[: max(1, len(data) // 2)]
        if self.corrupt_on_download:
            data = data[:-1] + bytes([data[-1] ^ 0xFF])
        Path(local_path).write_bytes(data)


def _make_source_db(
    path: Path, *, rows: int = 3, tables: tuple[str, ...] = ("users", "chats")
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        for table in tables:
            connection.execute(
                f"CREATE TABLE IF NOT EXISTS {table} (id INTEGER PRIMARY KEY, payload TEXT)"
            )
            for index in range(rows):
                connection.execute(
                    f"INSERT INTO {table} (payload) VALUES (?)", (f"{table}-{index}",)
                )
        connection.commit()
    return path


@pytest.fixture()
def settings_obj(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    db_path = tmp_path / "app.sqlite3"
    monkeypatch.setenv("NEXUS_DB_PATH", str(db_path))
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings_module.get_settings.cache_clear()
    settings = settings_module.get_settings()
    yield settings
    settings_module.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_verified_backup_round_trips_and_restores(
    settings_obj: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = _make_source_db(Path(settings_obj.db_path))
    provider = _FakeR2()
    monkeypatch.setattr(backup_module, "_build_provider", lambda settings: provider)

    summary = create_backup(settings=settings_obj)

    assert summary["uploaded"] is True
    assert summary["verified"] is True
    assert summary["roundtrip"] == "byte-identical"
    assert summary["restore"] == "sqlite_readonly_restore"
    assert summary["tables"] == ["chats", "users"]
    assert summary["rows"] == 6
    assert summary["sha256"] == hashlib.sha256(provider.objects[summary["key"]]).hexdigest()
    assert summary["size_bytes"] == len(provider.objects[summary["key"]])
    assert summary["verified_at"]
    # the artifact really is the source database
    assert source.read_bytes()[:16] == provider.objects[summary["key"]][:16]


def test_dry_run_touches_nothing(settings_obj: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_source_db(Path(settings_obj.db_path))
    provider = _FakeR2()
    monkeypatch.setattr(backup_module, "_build_provider", lambda settings: provider)

    summary = create_backup(settings=settings_obj, dry_run=True)

    assert summary["uploaded"] is False
    assert summary["verified"] is False
    assert provider.uploads == []


# ---------------------------------------------------------------------------
# failure classes — all fail closed
# ---------------------------------------------------------------------------


def test_missing_r2_configuration_fails_loudly(
    settings_obj: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_source_db(Path(settings_obj.db_path))
    provider = _FakeR2(configured=False)
    monkeypatch.setattr(backup_module, "_build_provider", lambda settings: provider)

    with pytest.raises(ProviderUnavailable):
        create_backup(settings=settings_obj)
    assert provider.uploads == []


def test_empty_source_database_is_refused(
    settings_obj: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    Path(settings_obj.db_path).parent.mkdir(parents=True, exist_ok=True)
    Path(settings_obj.db_path).write_bytes(b"")
    provider = _FakeR2()
    monkeypatch.setattr(backup_module, "_build_provider", lambda settings: provider)

    with pytest.raises(BackupVerificationError):
        create_backup(settings=settings_obj)
    assert provider.uploads == []


def test_corrupt_source_is_refused_before_any_upload(
    settings_obj: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    Path(settings_obj.db_path).parent.mkdir(parents=True, exist_ok=True)
    Path(settings_obj.db_path).write_bytes(b"\x00\x01\x02 this is not a database" * 400)
    provider = _FakeR2()
    monkeypatch.setattr(backup_module, "_build_provider", lambda settings: provider)

    with pytest.raises(BackupVerificationError):
        create_backup(settings=settings_obj)
    assert provider.uploads == []


def test_a_valid_but_schemaless_database_is_refused(
    settings_obj: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The silent-empty-dump masquerade: a readable SQLite file with no tables."""
    path = Path(settings_obj.db_path)
    _make_source_db(path, rows=0, tables=())
    provider = _FakeR2()
    monkeypatch.setattr(backup_module, "_build_provider", lambda settings: provider)

    with pytest.raises(BackupVerificationError):
        create_backup(settings=settings_obj)
    assert provider.uploads == []


def test_remote_corruption_is_caught_by_the_round_trip(
    settings_obj: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_source_db(Path(settings_obj.db_path))
    provider = _FakeR2()
    provider.corrupt_on_download = True
    monkeypatch.setattr(backup_module, "_build_provider", lambda settings: provider)

    with pytest.raises(BackupVerificationError) as excinfo:
        create_backup(settings=settings_obj)
    assert "content does not match" in str(excinfo.value)


def test_truncated_remote_artifact_is_caught(
    settings_obj: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_source_db(Path(settings_obj.db_path))
    provider = _FakeR2()
    provider.truncate_on_download = True
    monkeypatch.setattr(backup_module, "_build_provider", lambda settings: provider)

    with pytest.raises(BackupVerificationError) as excinfo:
        create_backup(settings=settings_obj)
    assert "truncated" in str(excinfo.value)


def test_upload_failure_is_not_a_successful_backup(
    settings_obj: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_source_db(Path(settings_obj.db_path))
    provider = _FakeR2()
    provider.fail_upload = True
    monkeypatch.setattr(backup_module, "_build_provider", lambda settings: provider)

    with pytest.raises(StorageError):
        create_backup(settings=settings_obj)


# ---------------------------------------------------------------------------
# restore verification catches semantically wrong artifacts
# ---------------------------------------------------------------------------


def test_restore_verification_detects_a_lost_table(tmp_path: Path) -> None:
    source = _make_source_db(tmp_path / "source.sqlite3", tables=("users", "chats"))
    artifact = tmp_path / "artifact.sqlite3"
    with sqlite3.connect(artifact) as connection:
        connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, payload TEXT)")
        connection.commit()

    with pytest.raises(BackupVerificationError) as excinfo:
        _verify_sqlite_artifact(source, artifact, "restore")
    assert "lost tables" in str(excinfo.value)


def test_restore_verification_detects_row_drift(tmp_path: Path) -> None:
    """The exact 'looks valid, is wrong' case: a readable DB missing rows."""
    source = _make_source_db(tmp_path / "source.sqlite3", rows=5)
    artifact = _make_source_db(tmp_path / "artifact.sqlite3", rows=2)

    with pytest.raises(BackupVerificationError) as excinfo:
        _verify_sqlite_artifact(source, artifact, "restore")
    assert "row counts drifted" in str(excinfo.value)


def test_restore_verification_rejects_an_unreadable_artifact(tmp_path: Path) -> None:
    source = _make_source_db(tmp_path / "source.sqlite3")
    artifact = tmp_path / "artifact.sqlite3"
    artifact.write_bytes(b"definitely not sqlite")

    with pytest.raises(BackupVerificationError):
        _verify_sqlite_artifact(source, artifact, "restore")


def test_restore_verification_rejects_an_empty_artifact(tmp_path: Path) -> None:
    source = _make_source_db(tmp_path / "source.sqlite3")
    artifact = tmp_path / "artifact.sqlite3"
    artifact.write_bytes(b"")

    with pytest.raises(BackupVerificationError) as excinfo:
        _verify_sqlite_artifact(source, artifact, "restore")
    assert "empty" in str(excinfo.value)
