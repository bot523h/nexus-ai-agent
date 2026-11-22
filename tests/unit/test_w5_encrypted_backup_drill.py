"""Preflight regression tests; a configuration receipt is not backup evidence."""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus_ai_agent.storage.encrypted_backup_drill import (
    BackupPreflightError,
    verify_production_backup_preflight,
)


def b2_config() -> dict[str, str]:
    return {
        "NEXUS_DATABASE_URL": "postgresql://user:private-db-secret@db.example/app",
        "NEXUS_B2_KEY_ID": "key-id",
        "NEXUS_B2_APPLICATION_KEY": "private-cloud-secret",
        "NEXUS_B2_BUCKET": "bucket",
        "NEXUS_B2_ENDPOINT": "https://storage.example",
        "NEXUS_B2_REGION": "region",
    }


def r2_config() -> dict[str, str]:
    return {
        "NEXUS_DATABASE_URL": "postgres://user:private-db-secret@db.example/app",
        "R2_ACCOUNT_ID": "account",
        "R2_ACCESS_KEY_ID": "access",
        "R2_SECRET_ACCESS_KEY": "private-cloud-secret",
        "R2_BUCKET": "bucket",
    }


def test_explicit_local_mode_does_not_claim_backup_success() -> None:
    assert (
        verify_production_backup_preflight({}, require_remote_tier=False) == "REMOTE_UNCONFIGURED"
    )
    with pytest.raises(BackupPreflightError, match="^FAILED_PREFLIGHT_REMOTE_MISSING$"):
        verify_production_backup_preflight({})


@pytest.mark.parametrize("config", [b2_config(), r2_config()])
def test_complete_tier_is_only_a_configuration_receipt(config: dict[str, str]) -> None:
    assert verify_production_backup_preflight(config) == "REMOTE_CONFIGURATION_VALIDATED"
    assert (
        verify_production_backup_preflight(config, require_remote_tier=False)
        == "REMOTE_CONFIGURATION_VALIDATED"
    )


@pytest.mark.parametrize("name", list(b2_config()))
def test_each_missing_b2_field_fails_closed(name: str) -> None:
    config = b2_config()
    del config[name]
    for required in (True, False):
        with pytest.raises(BackupPreflightError, match="^FAILED_PREFLIGHT_REMOTE_MISSING$"):
            verify_production_backup_preflight(config, require_remote_tier=required)


@pytest.mark.parametrize("name", list(r2_config()))
def test_each_missing_r2_field_fails_closed(name: str) -> None:
    config = r2_config()
    config[name] = ""
    with pytest.raises(BackupPreflightError):
        verify_production_backup_preflight(config)


def test_complete_tier_does_not_hide_a_partial_second_tier() -> None:
    config = b2_config()
    config["R2_BUCKET"] = "bucket"
    with pytest.raises(BackupPreflightError):
        verify_production_backup_preflight(config)


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///app.db",
        "postgresql:///app",
        "postgresql://host",
        "postgresql://host:invalid/app",
        "postgresql://host:0/app",
        "postgresql://host:65536/app",
        "postgresql://host/app#fragment",
        "postgresql://host/app bad",
        "postgresql://[invalid/app",
    ],
)
def test_invalid_database_url_is_redacted(url: str) -> None:
    config = b2_config()
    config["NEXUS_DATABASE_URL"] = url
    with pytest.raises(BackupPreflightError) as error:
        verify_production_backup_preflight(config)
    assert str(error.value) == "FAILED_PREFLIGHT_REMOTE_MISSING"
    assert "private-cloud-secret" not in str(error.value)


@pytest.mark.parametrize(
    "endpoint", ["http://host", "https://user:secret@host", "https:///missing"]
)
def test_invalid_endpoint_is_redacted(endpoint: str) -> None:
    config = b2_config()
    config["NEXUS_B2_ENDPOINT"] = endpoint
    with pytest.raises(BackupPreflightError) as error:
        verify_production_backup_preflight(config)
    assert str(error.value) == "FAILED_PREFLIGHT_REMOTE_MISSING"


@pytest.mark.parametrize("value", [" secret", "secret\n", "sec\x00ret", "sec\x7fret"])
def test_invalid_secret_value_never_leaks(value: str) -> None:
    config = b2_config()
    config["NEXUS_B2_APPLICATION_KEY"] = value
    with pytest.raises(BackupPreflightError) as error:
        verify_production_backup_preflight(config)
    assert str(error.value) == "FAILED_PREFLIGHT_REMOTE_MISSING"


def test_boolean_flag_must_really_be_bool() -> None:
    with pytest.raises(BackupPreflightError):
        verify_production_backup_preflight({}, require_remote_tier=1)  # type: ignore[arg-type]


def _source(path: Path) -> None:
    import sqlite3

    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE "strange""name" (id INTEGER, payload BLOB)')
        db.executemany('INSERT INTO "strange""name" VALUES (?, ?)', [(1, b"abc"), (2, b"def")])


def test_real_disk_encrypted_round_trip(tmp_path: Path) -> None:
    import hashlib
    import sqlite3

    from nexus_ai_agent.storage.encrypted_backup_drill import (
        run_encrypted_sqlite_backup_restore_drill,
    )

    source = tmp_path / "source.sqlite"
    work = tmp_path / "work"
    work.mkdir()
    _source(source)
    result = run_encrypted_sqlite_backup_restore_drill(source, work, bytes(range(32)))
    assert result.status == "LOCAL_DRILL_VERIFIED_REMOTE_UNCONFIGURED"
    assert result.row_counts == {'strange"name': 2}
    assert hashlib.sha256(result.restored_path.read_bytes()).hexdigest() == result.source_sha256
    assert b"abc" not in result.archive_path.read_bytes()
    assert result.archive_path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(result.restored_path) as restored:
        assert restored.execute('SELECT * FROM "strange""name" ORDER BY id').fetchall() == [
            (1, b"abc"),
            (2, b"def"),
        ]
    assert not list(work.glob(".nexus-drill-*"))
    with pytest.raises(BackupPreflightError):
        verify_production_backup_preflight({})


@pytest.mark.parametrize("key", [b"", b"short", b"x" * 32, b"ab" * 16])
def test_drill_rejects_weak_key_without_side_effects(tmp_path: Path, key: bytes) -> None:
    from nexus_ai_agent.storage.encrypted_backup_drill import (
        run_encrypted_sqlite_backup_restore_drill,
    )

    with pytest.raises(ValueError, match="invalid encryption key"):
        run_encrypted_sqlite_backup_restore_drill(tmp_path / "missing", tmp_path, key)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kind", ["ciphertext", "tag", "manifest", "key", "truncated"])
def test_authentication_rejects_corruption(tmp_path: Path, kind: str) -> None:
    import json

    from nexus_ai_agent.storage.encrypted_backup_drill import (
        BackupIntegrityTamperError,
        run_encrypted_sqlite_backup_restore_drill,
        verify_encrypted_sqlite_backup,
    )

    source = tmp_path / "source.sqlite"
    work = tmp_path / "work"
    work.mkdir()
    _source(source)
    key = bytes(range(32))
    evidence = run_encrypted_sqlite_backup_restore_drill(source, work, key)
    ciphertext = evidence.archive_path.read_bytes()
    if kind == "ciphertext":
        evidence.archive_path.write_bytes(bytes([ciphertext[0] ^ 1]) + ciphertext[1:])
    elif kind == "truncated":
        evidence.archive_path.write_bytes(ciphertext[:-1])
    elif kind in ("tag", "manifest"):
        manifest = json.loads(evidence.manifest_path.read_bytes())
        if kind == "tag":
            manifest["auth_tag_hex"] = "00" * 16
        else:
            manifest["row_counts"] = {"forged": 1}
        evidence.manifest_path.write_text(json.dumps(manifest))
    elif kind == "key":
        key = bytes(range(1, 33))
    with pytest.raises(BackupIntegrityTamperError):
        verify_encrypted_sqlite_backup(evidence.archive_path, evidence.manifest_path, key)


def test_injected_tamper_never_creates_restore(tmp_path: Path) -> None:
    from nexus_ai_agent.storage.encrypted_backup_drill import (
        BackupIntegrityTamperError,
        run_encrypted_sqlite_backup_restore_drill,
    )

    source = tmp_path / "source.sqlite"
    work = tmp_path / "work"
    work.mkdir()
    _source(source)
    with pytest.raises(BackupIntegrityTamperError):
        run_encrypted_sqlite_backup_restore_drill(
            source, work, bytes(range(32)), inject_tamper_byte=True
        )
    assert not (work / "restored.sqlite").exists()
    assert not list(work.glob(".nexus-drill-*"))
