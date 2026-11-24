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


def test_drill_key_material_refuses_bad_capabilities(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus_ai_agent.storage import encrypted_backup_drill as drill

    key = drill.BackupKeyMaterial.generate().key_bytes
    with pytest.raises(ValueError, match="invalid encryption key"):
        drill._key("not bytes")
    with pytest.raises(ValueError, match="invalid key material"):
        drill.BackupKeyMaterial(
            key, "untrusted", drill._key_fingerprint(key), drill._KEY_PROVENANCE_SENTINEL
        )
    with pytest.raises(ValueError, match="fingerprint"):
        drill.BackupKeyMaterial(key, "csprng", "incorrect", drill._KEY_PROVENANCE_SENTINEL)
    with pytest.raises(ValueError, match="invalid backup key"):
        drill.BackupKeyMaterial.from_secret_env(env={})
    with pytest.raises(ValueError, match="boolean"):
        drill._material(key, require_trusted=1)
    monkeypatch.setattr(drill.secrets, "token_bytes", lambda _: b"\x00" * 32)
    with pytest.raises(RuntimeError, match="could not generate"):
        drill.generate_drill_encryption_key()


def test_drill_merkle_empty_and_multichunk() -> None:
    import hashlib

    from nexus_ai_agent.storage import encrypted_backup_drill as drill

    assert drill._merkle(b"") == hashlib.sha256(b"").hexdigest()
    data = b"a" * 65536 + b"b"
    left = hashlib.sha256(b"\x00" + data[:65536]).digest()
    right = hashlib.sha256(b"\x00" + data[65536:]).digest()
    assert drill._merkle(data) == hashlib.sha256(b"\x01" + left + right).hexdigest()


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b"[]", b'{"schema_version":3}'])
def test_drill_manifest_rejects_duplicate_nonobject_or_incomplete(raw: bytes) -> None:
    from nexus_ai_agent.storage import encrypted_backup_drill as drill

    with pytest.raises(ValueError):
        drill._parse_manifest(raw)


def test_drill_regular_file_check_and_limit(tmp_path: Path) -> None:
    from nexus_ai_agent.storage import encrypted_backup_drill as drill

    with pytest.raises(ValueError, match="regular"):
        drill._read_regular(tmp_path)
    path = tmp_path / "data"
    path.write_bytes(b"1234")
    with pytest.raises(ValueError, match="size"):
        drill._read_regular(path, limit=3)
    assert path.read_bytes() == b"1234"


def test_drill_rejects_failed_integrity_check() -> None:
    from nexus_ai_agent.storage import encrypted_backup_drill as drill

    connection = Mock()
    connection.execute.return_value.fetchall.return_value = [("corrupt",)]
    with pytest.raises(ValueError, match="integrity"):
        drill._inventory(connection)
    connection.execute.assert_called_once_with("PRAGMA integrity_check")


@pytest.mark.parametrize("pins,readers", [([], frozenset()), (frozenset({1}), frozenset())])
def test_retention_rejects_invalid_pin_sets_without_filesystem_changes(
    tmp_path: Path, pins: object, readers: object
) -> None:
    from nexus_ai_agent.storage import encrypted_backup_drill as drill

    key = drill.BackupKeyMaterial.generate()
    with pytest.raises(ValueError, match="pins and reader leases"):
        drill.enforce_backup_retention(
            tmp_path,
            keep_generations=1,
            key_material=key,
            active_pins=pins,
            active_reader_leases=readers,
        )
    assert list(tmp_path.iterdir()) == []


def test_source_identity_missing_is_rejected(tmp_path: Path) -> None:
    from nexus_ai_agent.storage import encrypted_backup_drill as drill

    source = tmp_path / "database"
    source.touch()
    info = source.stat()
    source.unlink()
    with pytest.raises(ValueError, match="inode changed"):
        drill._check_source_identity(source, info)


@pytest.fixture
def drill_artifact(tmp_path: Path):
    import sqlite3

    from nexus_ai_agent.storage import encrypted_backup_drill as drill

    source = tmp_path / "source.sqlite"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE t (n INTEGER)")
        conn.execute("INSERT INTO t VALUES (42)")
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    key = drill.BackupKeyMaterial.generate()
    evidence = drill.run_encrypted_sqlite_backup_restore_drill(source, work, key)
    return source, work, key, evidence


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", 7),
        ("key_source", "invalid"),
        ("schema_version", 1),
        ("nonce_hex", "00"),
        ("auth_tag_hex", "00"),
        ("merkle_root", "a" * 64),
    ],
)
def test_drill_manifest_validation_fails_closed_without_leaking_input(
    drill_artifact, field: str, value: object
) -> None:
    import json
    import traceback

    from nexus_ai_agent.storage import encrypted_backup_drill as drill

    _, _, key, evidence = drill_artifact
    manifest = json.loads(evidence.manifest_path.read_text())
    manifest[field] = value
    evidence.manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(drill.BackupIntegrityTamperError) as caught:
        drill.verify_encrypted_sqlite_backup(evidence.archive_path, evidence.manifest_path, key)
    assert caught.value.__context__ is None
    assert "invalid" not in "".join(traceback.format_exception(caught.value))
    assert evidence.restored_path.exists()


def test_authenticated_wrong_plaintext_digest_is_still_rejected(drill_artifact) -> None:
    import hashlib
    import json

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from nexus_ai_agent.storage import encrypted_backup_drill as drill

    _, _, key, evidence = drill_artifact
    manifest = json.loads(evidence.manifest_path.read_text())
    plaintext = evidence.restored_path.read_bytes()
    manifest["source_sha256"] = "f" * 64
    aad = {
        name: manifest[name]
        for name in (
            "schema_version",
            "source_sha256",
            "row_counts",
            "nonce_hex",
            "key_source",
            "key_fingerprint",
            "created_at_utc",
        )
    }
    sealed = AESGCM(drill._key(key.key_bytes)).encrypt(
        bytes.fromhex(manifest["nonce_hex"]), plaintext, drill._canonical(aad)
    )
    ciphertext, tag = sealed[:-16], sealed[-16:]
    manifest["auth_tag_hex"] = tag.hex()
    manifest["ciphertext_sha256"] = hashlib.sha256(ciphertext).hexdigest()
    manifest["merkle_root"] = drill._merkle(ciphertext)
    evidence.archive_path.write_bytes(ciphertext)
    evidence.manifest_path.write_bytes(drill._canonical(manifest))
    with pytest.raises(drill.BackupIntegrityTamperError):
        drill.verify_encrypted_sqlite_backup(evidence.archive_path, evidence.manifest_path, key)
    assert evidence.restored_path.read_bytes() == plaintext


def test_reader_lease_rejects_non_directory_and_hardlinked_lock(tmp_path: Path) -> None:
    import os

    from nexus_ai_agent.storage import encrypted_backup_drill as drill

    regular = tmp_path / "file"
    regular.touch()
    with pytest.raises(ValueError, match="not a directory"), drill.backup_reader_lease(regular):
        pytest.fail("regular file accepted as generation")
    generation = tmp_path / "generation"
    generation.mkdir()
    lock = generation / ".reader.lock"
    lock.touch()
    os.link(lock, tmp_path / "second-link")
    with pytest.raises(PermissionError, match="unsafe"), drill.backup_reader_lease(generation):
        pytest.fail("hardlinked lock accepted")
    assert lock.exists()


def test_reader_rejects_generation_replaced_before_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from nexus_ai_agent.storage import encrypted_backup_drill as drill

    root = tmp_path / "generation"
    root.mkdir()
    original_stat = os.stat

    def changed(path, *args, **kwargs):
        info = original_stat(path, *args, **kwargs)
        if path == root:
            fields = list(info)
            fields[1] += 1
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(drill.os, "stat", changed)
    with pytest.raises(FileNotFoundError, match="replaced"), drill.backup_reader_lease(root):
        pytest.fail("replaced generation accepted")


def test_generation_rejects_symlink_entries_without_following(tmp_path: Path) -> None:
    import os

    from nexus_ai_agent.storage import encrypted_backup_drill as drill

    (tmp_path / "backup.enc").symlink_to("missing")
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(ValueError, match="unsafe generation"):
            drill._validate_generation_entries(fd)
    finally:
        os.close(fd)
    assert (tmp_path / "backup.enc").is_symlink()


@pytest.mark.parametrize(
    "setting,value,expected",
    [
        ("retain_generations", 0, "keep_generations"),
        ("backup_timeout_seconds", float("nan"), "timeout"),
        ("inject_tamper_byte", 1, "boolean"),
    ],
)
def test_drill_rejects_invalid_options_before_publication(
    drill_artifact, setting, value, expected
) -> None:
    from nexus_ai_agent.storage import encrypted_backup_drill as drill

    source, work, key, _ = drill_artifact
    before = set(work.iterdir())
    with pytest.raises(ValueError, match=expected):
        drill.run_encrypted_sqlite_backup_restore_drill(source, work, key, **{setting: value})
    assert set(work.iterdir()) == before


def test_drill_rejects_non_regular_source_before_publication(drill_artifact) -> None:
    from nexus_ai_agent.storage import encrypted_backup_drill as drill

    source, work, key, _ = drill_artifact
    before = set(work.iterdir())
    with pytest.raises(ValueError, match="source is not a regular file"):
        drill.run_encrypted_sqlite_backup_restore_drill(source.parent, work, key)
    assert set(work.iterdir()) == before


def test_drill_refuses_retention_if_secure_rmtree_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.storage import encrypted_backup_drill as drill

    key = drill.BackupKeyMaterial.generate()
    monkeypatch.setattr(drill.shutil.rmtree, "avoids_symlink_attacks", False)
    with pytest.raises(RuntimeError, match="descriptor-relative"):
        drill.enforce_backup_retention(tmp_path, keep_generations=1, key_material=key)
    assert list(tmp_path.iterdir()) == []


def test_drill_retention_rejects_hardlinked_lock(tmp_path: Path) -> None:
    import os

    from nexus_ai_agent.storage import encrypted_backup_drill as drill

    lock = tmp_path / ".backup-retention.lock"
    lock.touch()
    os.link(lock, tmp_path / "extra")
    with pytest.raises(PermissionError, match="unsafe backup retention lock"):
        drill.enforce_backup_retention(
            tmp_path, keep_generations=1, key_material=drill.BackupKeyMaterial.generate()
        )
    assert lock.exists()


def test_retention_checks_archive_regular_after_verification(
    drill_artifact, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import stat

    from nexus_ai_agent.storage import encrypted_backup_drill as drill

    _, work, key, evidence = drill_artifact
    real_stat = os.stat

    def changed(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        if path == "backup.enc" and kwargs.get("dir_fd") is not None:
            fields = list(info)
            fields[0] = stat.S_IFDIR | 0o700
            return os.stat_result(fields)
        return info

    # Generation listing must see a regular archive; change only the later stat.
    real_validate = drill._validate_generation_entries
    monkeypatch.setattr(drill, "_validate_generation_entries", lambda fd: real_validate(fd))
    calls = 0

    def late_changed(path, *args, **kwargs):
        nonlocal calls
        if path == "backup.enc" and kwargs.get("dir_fd") is not None:
            calls += 1
            if calls == 2:
                return changed(path, *args, **kwargs)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(drill.os, "stat", late_changed)
    receipt = drill.enforce_backup_retention(work, keep_generations=1, key_material=key)
    assert receipt.skipped_corrupt_ids == (evidence.archive_path.parent.name,)
    assert evidence.archive_path.exists()


@pytest.fixture
def two_drill_generations(drill_artifact):
    from nexus_ai_agent.storage import encrypted_backup_drill as drill

    source, work, key, old = drill_artifact
    new = drill.run_encrypted_sqlite_backup_restore_drill(source, work, key)
    return work, key, old, new


def test_retention_disk_pin_protects_old_generation(two_drill_generations) -> None:
    from nexus_ai_agent.storage import encrypted_backup_drill as drill

    work, key, old, new = two_drill_generations
    (old.archive_path.parent / ".pin").touch()
    receipt = drill.enforce_backup_retention(work, keep_generations=1, key_material=key)
    assert receipt.retained_ids == (new.archive_path.parent.name,)
    assert receipt.skipped_pinned_ids == (old.archive_path.parent.name,)
    assert old.archive_path.exists()


@pytest.mark.parametrize("after_move", [False, True])
def test_retention_failure_never_silently_claims_pruning(
    two_drill_generations, monkeypatch: pytest.MonkeyPatch, after_move: bool
) -> None:
    from nexus_ai_agent.storage import encrypted_backup_drill as drill

    work, key, old, new = two_drill_generations
    if after_move:
        monkeypatch.setattr(drill.os, "fsync", Mock(side_effect=OSError("sync failed")))
        with pytest.raises(OSError, match="sync failed"):
            drill.enforce_backup_retention(work, keep_generations=1, key_material=key)
        assert not old.archive_path.parent.exists()
        assert len(list(work.glob(".pruned-*"))) == 1
    else:
        monkeypatch.setattr(drill.os, "rename", Mock(side_effect=OSError("rename failed")))
        receipt = drill.enforce_backup_retention(work, keep_generations=1, key_material=key)
        assert receipt.skipped_corrupt_ids == (old.archive_path.parent.name,)
        assert not receipt.pruned_ids and old.archive_path.exists()
    assert new.archive_path.exists()


def test_drill_refuses_changed_restore_inventory_without_publishing(
    drill_artifact, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.storage import encrypted_backup_drill as drill

    source, work, key, _ = drill_artifact
    previous = set(work.iterdir())
    original = drill._inventory
    calls = 0

    def changed(conn):
        nonlocal calls
        calls += 1
        result = original(conn)
        return result if calls == 1 else {**result, "unexpected": 1}

    monkeypatch.setattr(drill, "_inventory", changed)
    with pytest.raises(drill.BackupIntegrityTamperError, match="inventory mismatch"):
        drill.run_encrypted_sqlite_backup_restore_drill(source, work, key)
    assert calls == 2
    assert set(work.iterdir()) == previous


def test_drill_size_check_handles_file_growth_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from nexus_ai_agent.storage import encrypted_backup_drill as drill

    path = tmp_path / "growing"
    path.write_bytes(b"abcd")
    original = os.fstat

    def stale_stat(fd: int):
        result = original(fd)
        if os.readlink(f"/proc/self/fd/{fd}") == str(path):
            fields = list(result)
            fields[6] = 2
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(drill.os, "fstat", stale_stat)
    with pytest.raises(ValueError, match="size exceeds"):
        drill._read_regular(path, limit=3)
    assert path.read_bytes() == b"abcd"


def test_retention_preserves_generation_if_reader_arrives_after_validation(
    two_drill_generations, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reader racing the prune lock leaves its authenticated generation intact."""
    import fcntl

    from nexus_ai_agent.storage import encrypted_backup_drill as drill

    work, key, old, new = two_drill_generations
    original = fcntl.flock
    observed: list[int] = []

    def reader_wins(fd: int, operation: int) -> None:
        if operation == fcntl.LOCK_EX | fcntl.LOCK_NB:
            observed.append(fd)
            raise BlockingIOError("reader acquired lease after scan")
        original(fd, operation)

    monkeypatch.setattr(drill.fcntl, "flock", reader_wins)
    receipt = drill.enforce_backup_retention(work, keep_generations=1, key_material=key)
    assert observed
    assert receipt.skipped_leased_ids == (old.archive_path.parent.name,)
    assert not receipt.pruned_ids
    assert old.archive_path.exists() and new.archive_path.exists()
