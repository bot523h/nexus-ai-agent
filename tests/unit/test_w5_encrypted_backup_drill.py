"""Preflight regression tests; a configuration receipt is not backup evidence."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from nexus_ai_agent.storage.encrypted_backup_drill import (
    BackupPreflightError,
    verify_production_backup_preflight,
)


def b2_config() -> dict[str, str]:
    return {
        "NEXUS_DATABASE_URL": "postgresql://user:private-db-secret@db.example/app?sslmode=verify-full",
        "NEXUS_B2_KEY_ID": "key-id",
        "NEXUS_B2_APPLICATION_KEY": "private-cloud-secret",
        "NEXUS_B2_BUCKET": "bucket",
        "NEXUS_B2_ENDPOINT": "https://storage.example",
        "NEXUS_B2_REGION": "region",
    }


def r2_config() -> dict[str, str]:
    return {
        "NEXUS_DATABASE_URL": "postgres://user:private-db-secret@db.example/app?sslmode=verify-full",
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
    result = run_encrypted_sqlite_backup_restore_drill(
        source, work, hashlib.sha256(b"W5 independent fixed test key").digest()
    )
    assert result.status == "LOCAL_DRILL_VERIFIED_REMOTE_NOT_CHECKED"
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
    key = hashlib.sha256(b"W5 independent fixed test key").digest()
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
        key = hashlib.sha256(b"W5 wrong independent test key").digest()
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
            source,
            work,
            hashlib.sha256(b"W5 independent fixed test key").digest(),
            inject_tamper_byte=True,
        )
    assert not (work / "restored.sqlite").exists()
    assert not list(work.glob(".nexus-drill-*"))


@pytest.mark.parametrize(
    "key",
    [bytes(range(32)), bytes(reversed(range(32))), b"abcd" * 8, b"abcdefghijklmnop" * 2, b"a" * 33],
)
def test_structurally_predictable_or_wrong_size_keys_are_refused(key: bytes) -> None:
    from nexus_ai_agent.storage.encrypted_backup_drill import _key

    with pytest.raises(ValueError, match="^invalid encryption key$"):
        _key(key)


def test_memoryview_key_is_supported() -> None:
    from nexus_ai_agent.storage.encrypted_backup_drill import _key

    key = hashlib.sha256(b"test-only key material").digest()
    assert _key(memoryview(key)) == _key(key)


@pytest.mark.parametrize(
    "sslmode", ["disable", "allow", "prefer", "require", "", "verify-full&sslmode=disable"]
)
def test_preflight_fails_closed_for_insecure_tls_without_traceback_leaks(sslmode: str) -> None:
    import traceback

    config = b2_config()
    config["NEXUS_DATABASE_URL"] = "postgresql://user:db-secret@example.test/app?sslmode=" + sslmode
    with pytest.raises(BackupPreflightError) as exc:
        verify_production_backup_preflight(config)
    formatted = "".join(
        traceback.format_exception(type(exc.value), exc.value, exc.value.__traceback__)
    )
    assert "db-secret" not in formatted
    assert "private-cloud-secret" not in formatted


def test_inventory_does_not_drop_sqlite_prefix_without_underscore(tmp_path: Path) -> None:
    import sqlite3

    from nexus_ai_agent.storage.encrypted_backup_drill import _inventory

    with sqlite3.connect(tmp_path / "test.db") as db:
        db.execute('CREATE TABLE "sqliteCache" (item INTEGER)')
        db.execute('INSERT INTO "sqliteCache" VALUES (1)')
        assert _inventory(db) == {"sqliteCache": 1}


def test_size_cap_empty_database_and_immutable_receipt(tmp_path: Path) -> None:
    import sqlite3

    from nexus_ai_agent.storage.encrypted_backup_drill import (
        run_encrypted_sqlite_backup_restore_drill,
    )

    source = tmp_path / "source.sqlite"
    work = tmp_path / "work"
    work.mkdir()
    key = hashlib.sha256(b"test-only key material").digest()
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE data (value INTEGER)")
    with pytest.raises(ValueError, match="database size exceeds drill limit"):
        run_encrypted_sqlite_backup_restore_drill(source, work, key, max_database_bytes=1)
    evidence = run_encrypted_sqlite_backup_restore_drill(source, work, key)
    with pytest.raises(TypeError):
        evidence.row_counts["data"] = 10  # type: ignore[index]
    empty_source = tmp_path / "empty.sqlite"
    empty_work = tmp_path / "empty-work"
    empty_work.mkdir()
    with sqlite3.connect(empty_source):
        pass
    with pytest.raises(ValueError, match="source database has no user tables"):
        run_encrypted_sqlite_backup_restore_drill(empty_source, empty_work, key)
    evidence = run_encrypted_sqlite_backup_restore_drill(
        empty_source, empty_work, key, allow_empty_database=True
    )
    assert not evidence.row_counts


def test_explicit_remote_preflight_changes_evidence_only_after_validation(tmp_path: Path) -> None:
    from nexus_ai_agent.storage.encrypted_backup_drill import (
        run_encrypted_sqlite_backup_restore_drill,
    )

    source = tmp_path / "source.sqlite"
    work = tmp_path / "work"
    work.mkdir()
    _source(source)
    key = hashlib.sha256(b"test-only key material").digest()
    with pytest.raises(BackupPreflightError):
        run_encrypted_sqlite_backup_restore_drill(source, work, key, preflight_env={})
    assert not list(work.iterdir())
    evidence = run_encrypted_sqlite_backup_restore_drill(
        source, work, key, preflight_env=b2_config()
    )
    assert evidence.status == "LOCAL_DRILL_VERIFIED_REMOTE_PREFLIGHT_OK"


def test_preflight_accepts_aliases_and_rejects_conflicts() -> None:
    config = b2_config()
    aliases = {
        "DATABASE_URL": config["NEXUS_DATABASE_URL"],
        "B2_APPLICATION_KEY_ID": config["NEXUS_B2_KEY_ID"],
        "B2_APPLICATION_KEY": config["NEXUS_B2_APPLICATION_KEY"],
        "B2_BUCKET_NAME": config["NEXUS_B2_BUCKET"],
        "B2_ENDPOINT_URL": config["NEXUS_B2_ENDPOINT"],
        "B2_REGION": config["NEXUS_B2_REGION"],
    }
    assert verify_production_backup_preflight(aliases) == "REMOTE_CONFIGURATION_VALIDATED"
    aliases["NEXUS_B2_BUCKET"] = "other-bucket"
    with pytest.raises(BackupPreflightError):
        verify_production_backup_preflight(aliases)


def test_r2_aliases_and_optional_endpoint() -> None:
    config = r2_config()
    aliases = {
        "DATABASE_URL": config["NEXUS_DATABASE_URL"],
        "NEXUS_R2_ACCOUNT_ID": config["R2_ACCOUNT_ID"],
        "NEXUS_R2_ACCESS_KEY_ID": config["R2_ACCESS_KEY_ID"],
        "NEXUS_R2_SECRET_ACCESS_KEY": config["R2_SECRET_ACCESS_KEY"],
        "NEXUS_R2_BUCKET_NAME": config["R2_BUCKET"],
        "NEXUS_R2_ENDPOINT_URL": "https://r2.example",
    }
    assert verify_production_backup_preflight(aliases) == "REMOTE_CONFIGURATION_VALIDATED"
    aliases["NEXUS_R2_ENDPOINT_URL"] = "http://r2.example"
    with pytest.raises(BackupPreflightError):
        verify_production_backup_preflight(aliases)


@pytest.mark.parametrize("bucket", ["ab", "Bad", "a..b", "a/b", "-abc", "x" * 64])
def test_preflight_rejects_invalid_bucket(bucket: str) -> None:
    config = b2_config()
    config["NEXUS_B2_BUCKET"] = bucket
    with pytest.raises(BackupPreflightError):
        verify_production_backup_preflight(config)


def test_repeat_drills_publish_distinct_complete_generations(tmp_path: Path) -> None:
    from nexus_ai_agent.storage.encrypted_backup_drill import (
        run_encrypted_sqlite_backup_restore_drill,
    )

    source, work = tmp_path / "source.sqlite", tmp_path / "work"
    work.mkdir()
    _source(source)
    key = hashlib.sha256(b"repeatable test key").digest()
    first = run_encrypted_sqlite_backup_restore_drill(source, work, key)
    second = run_encrypted_sqlite_backup_restore_drill(source, work, key)
    assert first.archive_path.parent != second.archive_path.parent
    assert first.source_sha256 == second.source_sha256
    for receipt in (first, second):
        assert receipt.archive_path.exists() and receipt.manifest_path.exists()
        assert receipt.restored_path.exists()
        assert not (receipt.archive_path.parent / "snapshot.sqlite").exists()
    assert not list(work.glob(".nexus-drill-*"))


def test_authentication_failure_publishes_no_generation(tmp_path: Path) -> None:
    from nexus_ai_agent.storage.encrypted_backup_drill import (
        BackupIntegrityTamperError,
        run_encrypted_sqlite_backup_restore_drill,
    )

    source, work = tmp_path / "source.sqlite", tmp_path / "work"
    work.mkdir()
    _source(source)
    key = hashlib.sha256(b"repeatable test key").digest()
    with pytest.raises(BackupIntegrityTamperError):
        run_encrypted_sqlite_backup_restore_drill(source, work, key, inject_tamper_byte=True)
    assert not list(work.iterdir())
