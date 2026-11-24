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
        "NEXUS_B2_ENDPOINT": "https://s3.us-west-004.backblazeb2.com",
        "NEXUS_B2_REGION": "us-west-004",
    }


def r2_config() -> dict[str, str]:
    return {
        "NEXUS_DATABASE_URL": "postgres://user:private-db-secret@db.example/app?sslmode=verify-full",
        "R2_ACCOUNT_ID": "0123456789abcdef0123456789abcdef",
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
        source,
        work,
        hashlib.sha256(b"W5 independent fixed test key").digest(),
        require_trusted_key_source=False,
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
        run_encrypted_sqlite_backup_restore_drill(
            tmp_path / "missing", tmp_path, key, require_trusted_key_source=False
        )
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
    evidence = run_encrypted_sqlite_backup_restore_drill(
        source, work, key, require_trusted_key_source=False
    )
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
        verify_encrypted_sqlite_backup(
            evidence.archive_path, evidence.manifest_path, key, require_trusted_key_source=False
        )


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
            require_trusted_key_source=False,
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
        run_encrypted_sqlite_backup_restore_drill(
            source, work, key, max_database_bytes=1, require_trusted_key_source=False
        )
    evidence = run_encrypted_sqlite_backup_restore_drill(
        source, work, key, require_trusted_key_source=False
    )
    with pytest.raises(TypeError):
        evidence.row_counts["data"] = 10  # type: ignore[index]
    empty_source = tmp_path / "empty.sqlite"
    empty_work = tmp_path / "empty-work"
    empty_work.mkdir()
    with sqlite3.connect(empty_source):
        pass
    with pytest.raises(ValueError, match="source database has no user tables"):
        run_encrypted_sqlite_backup_restore_drill(
            empty_source, empty_work, key, require_trusted_key_source=False
        )
    evidence = run_encrypted_sqlite_backup_restore_drill(
        empty_source, empty_work, key, allow_empty_database=True, require_trusted_key_source=False
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
        run_encrypted_sqlite_backup_restore_drill(
            source, work, key, preflight_env={}, require_trusted_key_source=False
        )
    assert not list(work.iterdir())
    evidence = run_encrypted_sqlite_backup_restore_drill(
        source, work, key, preflight_env=b2_config(), require_trusted_key_source=False
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
        "NEXUS_R2_ENDPOINT_URL": "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com",
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
    first = run_encrypted_sqlite_backup_restore_drill(
        source, work, key, require_trusted_key_source=False
    )
    second = run_encrypted_sqlite_backup_restore_drill(
        source, work, key, require_trusted_key_source=False
    )
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
        run_encrypted_sqlite_backup_restore_drill(
            source, work, key, inject_tamper_byte=True, require_trusted_key_source=False
        )
    assert not list(work.iterdir())


def test_generated_key_and_hard_limit_before_io(tmp_path: Path) -> None:
    from nexus_ai_agent.storage.encrypted_backup_drill import (
        ABSOLUTE_MAX_DRILL_DATABASE_BYTES,
        _key,
        generate_drill_encryption_key,
        run_encrypted_sqlite_backup_restore_drill,
        verify_encrypted_sqlite_backup,
    )

    key = generate_drill_encryption_key()
    assert len(key) == 32 and len(_key(key)) == 32
    for limit in (0, -1, ABSOLUTE_MAX_DRILL_DATABASE_BYTES + 1, True):
        with pytest.raises(ValueError, match="max_database_bytes must be within"):
            run_encrypted_sqlite_backup_restore_drill(
                tmp_path / "missing",
                tmp_path,
                key,
                max_database_bytes=limit,
                require_trusted_key_source=False,
            )
        with pytest.raises(ValueError, match="max_database_bytes must be within"):
            verify_encrypted_sqlite_backup(
                tmp_path / "missing",
                tmp_path / "missing2",
                key,
                max_database_bytes=limit,
                require_trusted_key_source=False,
            )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "name,value",
    [
        ("NEXUS_B2_BUCKET", "short"),
        ("NEXUS_B2_BUCKET", "bad.name"),
        ("NEXUS_B2_ENDPOINT", "https://s3.us-east-005.backblazeb2.com"),
        ("NEXUS_B2_ENDPOINT", "https://s3.us-west-004.backblazeb2.com?foo=1"),
        ("NEXUS_B2_KEY_ID", "your-key"),
        ("NEXUS_B2_APPLICATION_KEY", "changeme"),
    ],
)
def test_b2_provider_strict_configuration(name: str, value: str) -> None:
    config = b2_config()
    config[name] = value
    with pytest.raises(BackupPreflightError):
        verify_production_backup_preflight(config)


def test_r2_endpoint_account_mismatch_is_rejected() -> None:
    config = r2_config()
    config["R2_ENDPOINT_URL"] = "https://ffffffffffffffffffffffffffffffff.r2.cloudflarestorage.com"
    with pytest.raises(BackupPreflightError):
        verify_production_backup_preflight(config)


def test_real_sqlite_busy_times_out_without_publication(tmp_path: Path) -> None:
    import sqlite3
    import time

    from nexus_ai_agent.storage.encrypted_backup_drill import (
        run_encrypted_sqlite_backup_restore_drill,
    )

    source, work = tmp_path / "source.sqlite", tmp_path / "work"
    work.mkdir()
    _source(source)
    key = hashlib.sha256(b"busy test secret").digest()
    with sqlite3.connect(source) as locker:
        locker.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="sqlite backup timed out"):
            run_encrypted_sqlite_backup_restore_drill(
                source, work, key, backup_timeout_seconds=0.05, require_trusted_key_source=False
            )
        assert time.monotonic() - started < 2.0
        locker.rollback()
    assert not list(work.iterdir())


def test_work_directory_rejects_group_writable_permissions(tmp_path: Path) -> None:
    from nexus_ai_agent.storage.encrypted_backup_drill import (
        run_encrypted_sqlite_backup_restore_drill,
    )

    source, work = tmp_path / "source.sqlite", tmp_path / "work"
    work.mkdir()
    work.chmod(0o770)
    _source(source)
    with pytest.raises(PermissionError, match="untrusted drill directory"):
        run_encrypted_sqlite_backup_restore_drill(
            source, work, hashlib.sha256(b"key").digest(), require_trusted_key_source=False
        )


def test_source_inode_replacement_during_backup_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    from nexus_ai_agent.storage import encrypted_backup_drill as drill

    source, work, replacement = (
        tmp_path / "source.sqlite",
        tmp_path / "work",
        tmp_path / "other.sqlite",
    )
    work.mkdir()
    _source(source)
    _source(replacement)
    original = time.monotonic
    calls = 0

    def swap_during_progress() -> float:
        nonlocal calls
        calls += 1
        if calls == 2:
            replacement.replace(source)
        return original()

    monkeypatch.setattr(time, "monotonic", swap_during_progress)
    with pytest.raises(ValueError, match="source database inode changed during drill"):
        drill.run_encrypted_sqlite_backup_restore_drill(
            source, work, hashlib.sha256(b"key").digest(), require_trusted_key_source=False
        )
    assert not list(work.iterdir())


@pytest.mark.parametrize("tier,minimum", [("b2", 6), ("r2", 3)])
@pytest.mark.parametrize("length", [2, 3, 5, 6, 63, 64])
def test_provider_bucket_length_boundaries(tier: str, minimum: int, length: int) -> None:
    config = b2_config() if tier == "b2" else r2_config()
    config["NEXUS_B2_BUCKET" if tier == "b2" else "R2_BUCKET"] = "a" * length
    if minimum <= length <= 63:
        assert verify_production_backup_preflight(config) == "REMOTE_CONFIGURATION_VALIDATED"
    else:
        with pytest.raises(BackupPreflightError):
            verify_production_backup_preflight(config)


@pytest.mark.parametrize("tier", ["b2", "r2"])
@pytest.mark.parametrize("bucket", ["abc.def", "-bucket", "bucket-", "Bucket", "abc_def"])
def test_provider_bucket_rejects_invalid_characters(tier: str, bucket: str) -> None:
    config = b2_config() if tier == "b2" else r2_config()
    config["NEXUS_B2_BUCKET" if tier == "b2" else "R2_BUCKET"] = bucket
    with pytest.raises(BackupPreflightError):
        verify_production_backup_preflight(config)


def test_preflight_settings_and_b2_root_endpoint() -> None:
    from nexus_ai_agent.config.settings import Settings

    config = b2_config()
    settings = Settings.model_construct(
        database_url=config["NEXUS_DATABASE_URL"],
        b2_key_id=config["NEXUS_B2_KEY_ID"],
        b2_application_key=config["NEXUS_B2_APPLICATION_KEY"],
        b2_bucket=config["NEXUS_B2_BUCKET"],
        b2_endpoint=config["NEXUS_B2_ENDPOINT"] + "/",
        b2_region=config["NEXUS_B2_REGION"],
    )
    assert verify_production_backup_preflight(settings) == "REMOTE_CONFIGURATION_VALIDATED"
    settings.b2_region = "us-east-005"
    with pytest.raises(BackupPreflightError):
        verify_production_backup_preflight(settings)


def test_drill_archive_decrypts_with_independently_constructed_aad(tmp_path: Path) -> None:
    """Decrypt an actual drill artifact without using its verification helpers."""
    import json

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from nexus_ai_agent.storage.encrypted_backup_drill import (
        run_encrypted_sqlite_backup_restore_drill,
    )

    source, work = tmp_path / "source.sqlite", tmp_path / "work"
    work.mkdir()
    _source(source)
    raw_key = hashlib.sha256(b"independent drill test key").digest()
    evidence = run_encrypted_sqlite_backup_restore_drill(
        source, work, raw_key, require_trusted_key_source=False
    )
    manifest_bytes = evidence.manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    aad = {
        name: manifest[name]
        for name in (
            "schema_version",
            "source_sha256",
            "row_counts",
            "nonce_hex",
            "key_source",
            "key_fingerprint",
        )
    }
    serialized = json.dumps(aad, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )
    key = hashlib.sha256(b"nexus-local-drill-aesgcm-v1\x00" + raw_key).digest()
    plaintext = AESGCM(key).decrypt(
        bytes.fromhex(manifest["nonce_hex"]),
        evidence.archive_path.read_bytes() + bytes.fromhex(manifest["auth_tag_hex"]),
        serialized,
    )
    assert plaintext == evidence.restored_path.read_bytes()
    assert hashlib.sha256(plaintext).hexdigest() == manifest["source_sha256"]
    assert raw_key not in manifest_bytes
    assert raw_key.hex().encode() not in manifest_bytes


@pytest.mark.parametrize("field", ["nonce_hex", "source_sha256", "row_counts"])
def test_authentication_rejects_every_mutated_aad_field(tmp_path: Path, field: str) -> None:
    import json

    from nexus_ai_agent.storage.encrypted_backup_drill import (
        BackupIntegrityTamperError,
        run_encrypted_sqlite_backup_restore_drill,
        verify_encrypted_sqlite_backup,
    )

    source, work = tmp_path / "source.sqlite", tmp_path / "work"
    work.mkdir()
    _source(source)
    key = hashlib.sha256(b"distinct aad regression test key").digest()
    evidence = run_encrypted_sqlite_backup_restore_drill(
        source, work, key, require_trusted_key_source=False
    )
    manifest = json.loads(evidence.manifest_path.read_bytes())
    if field == "nonce_hex":
        nonce = bytearray.fromhex(manifest[field])
        nonce[0] ^= 1
        manifest[field] = nonce.hex()
    elif field == "source_sha256":
        manifest[field] = "0" * 64
    else:
        manifest[field] = {"forged": 10}
    evidence.manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(BackupIntegrityTamperError):
        verify_encrypted_sqlite_backup(
            evidence.archive_path, evidence.manifest_path, key, require_trusted_key_source=False
        )


def test_key_capabilities_strict_drill_and_redacted_receipts(tmp_path: Path) -> None:
    import base64
    import json
    import secrets

    from nexus_ai_agent.storage.encrypted_backup_drill import (
        BackupKeyMaterial,
        run_encrypted_sqlite_backup_restore_drill,
        verify_encrypted_sqlite_backup,
    )

    source, work = tmp_path / "source.sqlite", tmp_path / "work"
    work.mkdir()
    _source(source)
    random_key = secrets.token_bytes(32)
    for key in (
        BackupKeyMaterial.generate(),
        BackupKeyMaterial.from_secret_env(env={"NEXUS_BACKUP_ENCRYPTION_KEY": random_key.hex()}),
        BackupKeyMaterial.from_secret_env(
            env={"NEXUS_BACKUP_ENCRYPTION_KEY": base64.b64encode(random_key).decode()}
        ),
    ):
        receipt = run_encrypted_sqlite_backup_restore_drill(
            source, work, key, require_trusted_key_source=True
        )
        assert receipt.key_source == key.source
        assert receipt.key_fingerprint == key.fingerprint_sha256
        manifest = json.loads(receipt.manifest_path.read_text())
        assert manifest["key_source"] == key.source
        assert manifest["key_fingerprint"] == key.fingerprint_sha256
        assert key.key_bytes.hex() not in repr(key)
        assert (
            verify_encrypted_sqlite_backup(
                receipt.archive_path, receipt.manifest_path, key, require_trusted_key_source=True
            )
            == receipt.restored_path.read_bytes()
        )
        manifest["key_source"] = "raw_unattested"
        receipt.manifest_path.write_text(json.dumps(manifest))
        from nexus_ai_agent.storage.encrypted_backup_drill import BackupIntegrityTamperError

        with pytest.raises(BackupIntegrityTamperError):
            verify_encrypted_sqlite_backup(
                receipt.archive_path, receipt.manifest_path, key, require_trusted_key_source=False
            )

    for rejected in (random_key, BackupKeyMaterial.from_raw_bytes(random_key)):
        with pytest.raises(ValueError, match="trusted key material required"):
            run_encrypted_sqlite_backup_restore_drill(
                source, work, rejected, require_trusted_key_source=True
            )
    with pytest.raises(ValueError, match="provenance"):
        BackupKeyMaterial(random_key, "csprng", "fake", object())
    with pytest.raises(ValueError):
        BackupKeyMaterial.from_raw_bytes(random_key, source="csprng")
    with pytest.raises(ValueError):
        BackupKeyMaterial.from_secret_env(env={"NEXUS_BACKUP_ENCRYPTION_KEY": "not-a-key"})


def test_verify_legacy_v1_drill_artifact(tmp_path: Path) -> None:
    import json

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from nexus_ai_agent.storage.encrypted_backup_drill import (
        run_encrypted_sqlite_backup_restore_drill,
        verify_encrypted_sqlite_backup,
    )

    source, work = tmp_path / "source.sqlite", tmp_path / "work"
    work.mkdir()
    _source(source)
    key = hashlib.sha256(b"legacy compatibility test").digest()
    receipt = run_encrypted_sqlite_backup_restore_drill(
        source, work, key, require_trusted_key_source=False
    )
    manifest = json.loads(receipt.manifest_path.read_text())
    manifest.pop("key_source")
    manifest.pop("key_fingerprint")
    manifest["schema_version"] = 1
    aad = {
        name: manifest[name]
        for name in ("schema_version", "source_sha256", "row_counts", "nonce_hex")
    }
    serialized = json.dumps(aad, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )
    derived = hashlib.sha256(b"nexus-local-drill-aesgcm-v1\x00" + key).digest()
    encrypted = AESGCM(derived).encrypt(
        bytes.fromhex(manifest["nonce_hex"]), receipt.restored_path.read_bytes(), serialized
    )
    # GCM ciphertext is independent of AAD, so only the authentication tag changes.
    assert encrypted[:-16] == receipt.archive_path.read_bytes()
    manifest["auth_tag_hex"] = encrypted[-16:].hex()
    receipt.manifest_path.write_text(json.dumps(manifest))
    assert (
        verify_encrypted_sqlite_backup(
            receipt.archive_path, receipt.manifest_path, key, require_trusted_key_source=False
        )
        == receipt.restored_path.read_bytes()
    )


def test_unattested_capability_cannot_be_upgraded_by_dataclass_replace(tmp_path: Path) -> None:
    import dataclasses

    from nexus_ai_agent.storage.encrypted_backup_drill import (
        BackupKeyMaterial,
        run_encrypted_sqlite_backup_restore_drill,
    )

    raw = BackupKeyMaterial.from_raw_bytes(hashlib.sha256(b"predictable key").digest())
    with pytest.raises(ValueError, match="provenance"):
        dataclasses.replace(raw, source="csprng")
    trusted = BackupKeyMaterial.generate()
    with pytest.raises(ValueError, match="provenance"):
        dataclasses.replace(trusted, source="secret_env")

    class PretendTrusted(BackupKeyMaterial):
        def __post_init__(self) -> None:
            pass

    forged = PretendTrusted(raw.key_bytes, "csprng", raw.fingerprint_sha256, object())
    source, work = tmp_path / "source.sqlite", tmp_path / "work"
    work.mkdir()
    _source(source)
    with pytest.raises(ValueError, match="trusted key material required"):
        run_encrypted_sqlite_backup_restore_drill(
            source, work, forged, require_trusted_key_source=True
        )


def test_preflight_requires_hostname_verification_unless_explicitly_opted_out() -> None:
    config = b2_config()
    config["NEXUS_DATABASE_URL"] = config["NEXUS_DATABASE_URL"].replace("verify-full", "verify-ca")
    with pytest.raises(BackupPreflightError):
        verify_production_backup_preflight(config)
    assert verify_production_backup_preflight(config, allow_verify_ca=True) == (
        "REMOTE_CONFIGURATION_VALIDATED"
    )
    with pytest.raises(BackupPreflightError):
        verify_production_backup_preflight(config, allow_verify_ca=1)  # type: ignore[arg-type]
    config["NEXUS_DATABASE_URL"] += "&sslmode=verify-full"
    with pytest.raises(BackupPreflightError):
        verify_production_backup_preflight(config, allow_verify_ca=True)


def test_raw_key_requires_explicit_legacy_opt_in(tmp_path: Path) -> None:
    import secrets

    from nexus_ai_agent.storage.encrypted_backup_drill import (
        run_encrypted_sqlite_backup_restore_drill,
        verify_encrypted_sqlite_backup,
    )

    source, work = tmp_path / "source.sqlite", tmp_path / "work"
    work.mkdir()
    _source(source)
    key = secrets.token_bytes(32)
    with pytest.raises(ValueError, match="trusted key material required"):
        run_encrypted_sqlite_backup_restore_drill(source, work, key)
    assert not list(work.iterdir())
    receipt = run_encrypted_sqlite_backup_restore_drill(
        source, work, key, require_trusted_key_source=False
    )
    with pytest.raises(ValueError, match="trusted key material required"):
        verify_encrypted_sqlite_backup(receipt.archive_path, receipt.manifest_path, key)
    assert (
        verify_encrypted_sqlite_backup(
            receipt.archive_path, receipt.manifest_path, key, require_trusted_key_source=False
        )
        == receipt.restored_path.read_bytes()
    )


def test_encryption_canaries_nondeterminism_and_authenticated_tamper(tmp_path: Path) -> None:
    import json
    import sqlite3
    import traceback

    from nexus_ai_agent.storage.encrypted_backup_drill import (
        BackupIntegrityTamperError,
        BackupKeyMaterial,
        run_encrypted_sqlite_backup_restore_drill,
        verify_encrypted_sqlite_backup,
    )

    source, work = tmp_path / "source.sqlite", tmp_path / "work"
    work.mkdir()
    canary = "کاناری-محرمانه-多字节-🔒"
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE "confidential_canary_table" ("private_canary_column" TEXT)')
        db.execute('INSERT INTO "confidential_canary_table" VALUES (?)', (canary,))
    keys = (BackupKeyMaterial.generate(), BackupKeyMaterial.generate())
    receipts = [run_encrypted_sqlite_backup_restore_drill(source, work, key) for key in keys]
    archives = [receipt.archive_path.read_bytes() for receipt in receipts]
    manifests = [json.loads(receipt.manifest_path.read_text()) for receipt in receipts]
    for archive in archives:
        for plaintext in (canary.encode(), b"confidential_canary_table", b"private_canary_column"):
            assert plaintext not in archive
    assert manifests[0]["nonce_hex"] != manifests[1]["nonce_hex"]
    assert archives[0] != archives[1]
    first = receipts[0]
    assert verify_encrypted_sqlite_backup(first.archive_path, first.manifest_path, keys[0]) == (
        first.restored_path.read_bytes()
    )
    first.archive_path.write_bytes(bytes([archives[0][0] ^ 1]) + archives[0][1:])
    with pytest.raises(BackupIntegrityTamperError):
        verify_encrypted_sqlite_backup(first.archive_path, first.manifest_path, keys[0])
    first.archive_path.write_bytes(archives[0])
    manifests[0]["source_sha256"] = "a" * 64
    first.manifest_path.write_text(json.dumps(manifests[0]))
    with pytest.raises(BackupIntegrityTamperError) as error:
        verify_encrypted_sqlite_backup(first.archive_path, first.manifest_path, keys[0])
    assert error.value.__cause__ is None and error.value.__context__ is None
    assert canary not in "".join(traceback.format_exception(error.value))


def test_decrypt_exception_chain_cannot_expose_sensitive_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import traceback

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from nexus_ai_agent.storage.encrypted_backup_drill import (
        BackupIntegrityTamperError,
        BackupKeyMaterial,
        run_encrypted_sqlite_backup_restore_drill,
        verify_encrypted_sqlite_backup,
    )

    source, work = tmp_path / "source.sqlite", tmp_path / "work"
    work.mkdir()
    _source(source)
    key = BackupKeyMaterial.generate()
    receipt = run_encrypted_sqlite_backup_restore_drill(source, work, key)
    sensitive = "do-not-leak-decrypt-plaintext-or-secret"

    def fail(*_args: object, **_kwargs: object) -> bytes:
        raise ValueError(sensitive)

    monkeypatch.setattr(AESGCM, "decrypt", fail)
    with pytest.raises(BackupIntegrityTamperError) as error:
        verify_encrypted_sqlite_backup(receipt.archive_path, receipt.manifest_path, key)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert sensitive not in "".join(traceback.format_exception(error.value))


def test_invalid_secret_env_decoder_does_not_attach_context() -> None:
    from nexus_ai_agent.storage.encrypted_backup_drill import BackupKeyMaterial

    secret = "super-secret-key-value!"
    with pytest.raises(ValueError) as caught:
        BackupKeyMaterial.from_secret_env(env={"NEXUS_BACKUP_ENCRYPTION_KEY": secret})
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert secret not in str(caught.value.args)
