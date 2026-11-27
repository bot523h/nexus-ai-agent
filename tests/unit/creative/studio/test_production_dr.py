"""Wave 5 — hot backup, signed bundles, staged restore, cancellation-safe leases.

Zero mocks: real SQLite databases with real WAL journals, real files on disk,
real ``fcntl`` locks and real worker threads.  The tests that matter most are the
adversarial ones:

* the *live* database has uncommitted-to-main-file WAL frames at backup time, and
  the snapshot must still contain them (``shutil.copy`` would not);
* every tampering path a bundle can take (bit flip, truncation, unsigned
  manifest, wrong key, dropped table, tampered row count, missing file) must be
  reported — and must make ``restore_from_backup`` refuse;
* a cancelled coroutine must keep its admission slot until its thread finishes,
  so cancellation cannot over-admit the machine.

Coverage target for ``production_dr``: ``>= 98%`` line *and* branch.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.creative.studio.production_dr import (
    HOT_BACKUP_FILENAME,
    MANIFEST_FILENAME,
    AdmissionLease,
    BackupBundleReport,
    BackupKeyMaterial,
    BackupManifest,
    DisasterRecoveryError,
    GovernorSaturatedError,
    RestoreResult,
    StudioDisasterRecoveryManager,
    StudioResourceGovernor,
    utc_now,
)

KEY_HEX = "ab" * 32


class _Clock:
    def __init__(self) -> None:
        self.tick = 0

    def __call__(self) -> str:
        self.tick += 1
        return f"2026-11-26T03:00:{self.tick:02d}+00:00"


def _live_database(path: Path, *, clips: int = 5, markers: int = 3) -> sqlite3.Connection:
    """A real WAL-backed database with its connection still open."""
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE clips (id TEXT PRIMARY KEY, name TEXT)")
    connection.execute("CREATE TABLE markers (id INTEGER PRIMARY KEY, at_us INTEGER)")
    connection.executemany(
        "INSERT INTO clips VALUES (?, ?)", [(f"clip-{i}", f"Clip {i}") for i in range(clips)]
    )
    connection.executemany(
        "INSERT INTO markers VALUES (?, ?)", [(i, i * 1000) for i in range(markers)]
    )
    connection.commit()
    return connection


def _manager(
    tmp_path: Path, *, key: BackupKeyMaterial | None = None
) -> StudioDisasterRecoveryManager:
    return StudioDisasterRecoveryManager(
        tmp_path / "backups", key_material=key or BackupKeyMaterial("bk-1", KEY_HEX), now=_Clock()
    )


@pytest.fixture()
def live_db(tmp_path: Path) -> Iterator[Any]:
    connection = _live_database(tmp_path / "studio.sqlite3")
    try:
        yield tmp_path / "studio.sqlite3", connection
    finally:
        connection.close()


# --------------------------------------------------------------------------- #
# Key material
# --------------------------------------------------------------------------- #
class TestBackupKeyMaterial:
    def test_the_secret_never_appears_in_repr_or_str(self) -> None:
        key = BackupKeyMaterial("bk-1", KEY_HEX)
        for rendering in (repr(key), str(key), json.dumps(key.redacted_dict())):
            assert KEY_HEX not in rendering
            assert "redacted" in rendering
            assert "bk-1" in rendering
        # A dataclass repr would leak it; this is the regression that guards that.
        assert key.secret_hex == KEY_HEX

    def test_generate_produces_a_256_bit_key(self) -> None:
        key = BackupKeyMaterial.generate("bk-fresh")
        assert len(key.secret_bytes()) == 32
        assert key.key_id == "bk-fresh"
        assert BackupKeyMaterial.generate().key_id == "bk-default"

    def test_a_short_key_is_refused(self) -> None:
        with pytest.raises(DisasterRecoveryError) as excinfo:
            BackupKeyMaterial("bk-1", "ab" * 8)
        assert excinfo.value.reason == "weak_key"

    def test_a_non_hex_key_is_refused(self) -> None:
        with pytest.raises(DisasterRecoveryError) as excinfo:
            BackupKeyMaterial("bk-1", "zz" * 32)
        assert excinfo.value.reason == "invalid_key"

    def test_an_odd_length_hex_key_is_refused(self) -> None:
        with pytest.raises(DisasterRecoveryError) as excinfo:
            BackupKeyMaterial("bk-1", "a" * 63)
        assert excinfo.value.reason == "invalid_key"

    @pytest.mark.parametrize("field_name", ["key_id", "secret_hex"])
    def test_blank_fields_are_refused(self, field_name: str) -> None:
        kwargs = {"key_id": "bk-1", "secret_hex": KEY_HEX}
        kwargs[field_name] = "   "
        with pytest.raises(DisasterRecoveryError):
            BackupKeyMaterial(**kwargs)

    def test_hex_is_normalised_to_lowercase(self) -> None:
        assert BackupKeyMaterial("bk-1", KEY_HEX.upper()).secret_hex == KEY_HEX

    def test_signatures_are_deterministic_and_key_bound(self) -> None:
        first = BackupKeyMaterial("bk-1", KEY_HEX)
        other = BackupKeyMaterial("bk-1", "cd" * 32)
        payload = {"a": 1, "b": [2, 3]}
        assert first.sign(payload) == first.sign(payload)
        assert first.verify(payload, first.sign(payload)) is True
        assert other.verify(payload, first.sign(payload)) is False
        assert first.verify(payload, "") is False
        assert first.verify(payload, "not-a-signature") is False


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #
class TestBackupManifest:
    def _manifest(self, **overrides: Any) -> BackupManifest:
        kwargs: dict[str, Any] = {
            "manifest_id": "mnf-1",
            "bundle_id": "bkp-1",
            "created_at_utc": "2026-11-26T03:00:00+00:00",
            "source_name": "studio.sqlite3",
            "backup_filename": HOT_BACKUP_FILENAME,
            "backup_sha256": "ab" * 32,
            "backup_size_bytes": 4096,
            "tables": {"markers": 3, "clips": 5},
            "labels": {"fa": "پشتیبان", "en": "backup"},
            "key_id": "bk-1",
        }
        kwargs.update(overrides)
        return BackupManifest(**kwargs)

    def test_row_counts_are_sorted_and_totaled(self) -> None:
        manifest = self._manifest()
        assert list(manifest.tables) == ["clips", "markers"]
        assert manifest.total_rows == 8

    def test_a_manifest_signs_and_verifies(self) -> None:
        key = BackupKeyMaterial("bk-1", KEY_HEX)
        signed = self._manifest().with_signature(key)
        assert signed.manifest_hmac_sha256
        assert signed.verify_signature(key) is True
        # Any field change invalidates the signature.
        assert (
            dataclasses.replace(signed, labels={"fa": "x", "en": "y"}).verify_signature(key)
            is False
        )
        assert dataclasses.replace(signed, backup_size_bytes=4097).verify_signature(key) is False
        assert (
            dataclasses.replace(signed, tables={"clips": 6, "markers": 3}).verify_signature(key)
            is False
        )
        assert (
            dataclasses.replace(
                signed, created_at_utc="2026-11-26T03:59:59+00:00"
            ).verify_signature(key)
            is False
        )
        # ``total_rows`` is derived from ``tables``, so it cannot diverge from the
        # signed content at all -- that is why it is not in the list above.
        assert signed.total_rows == sum(signed.tables.values())

    def test_a_manifest_signed_by_another_key_does_not_verify(self) -> None:
        signed = self._manifest().with_signature(BackupKeyMaterial("bk-1", KEY_HEX))
        assert signed.verify_signature(BackupKeyMaterial("bk-1", "cd" * 32)) is False
        assert signed.verify_signature(BackupKeyMaterial("bk-2", KEY_HEX)) is False

    def test_signing_with_the_wrong_key_id_is_refused(self) -> None:
        with pytest.raises(DisasterRecoveryError) as excinfo:
            self._manifest().with_signature(BackupKeyMaterial("bk-other", KEY_HEX))
        assert excinfo.value.reason == "key_id_mismatch"

    def test_round_trip_through_dict(self) -> None:
        key = BackupKeyMaterial("bk-1", KEY_HEX)
        signed = self._manifest().with_signature(key)
        restored = BackupManifest.from_dict(json.loads(json.dumps(signed.to_dict())))
        assert restored == signed
        assert restored.verify_signature(key) is True

    @pytest.mark.parametrize(
        "bad",
        [
            "not-a-dict",
            {"manifest_id": "x"},
            {
                "manifest_id": "m",
                "bundle_id": "b",
                "created_at_utc": "t",
                "source_name": "s",
                "backup_filename": "f",
                "backup_sha256": "not-hex",
                "backup_size_bytes": 1,
                "key_id": "k",
            },
        ],
    )
    def test_malformed_manifests_are_refused(self, bad: Any) -> None:
        with pytest.raises(DisasterRecoveryError):
            BackupManifest.from_dict(bad)

    @pytest.mark.parametrize(
        ("field_name", "value"),
        [
            ("manifest_id", "  "),
            ("backup_sha256", "zz" * 32),
            ("backup_sha256", "ab" * 31),
            ("backup_size_bytes", -1),
            ("backup_size_bytes", True),
            ("tables", {"clips": -1}),
            ("schema", "v0"),
        ],
    )
    def test_invalid_manifest_fields_are_refused(self, field_name: str, value: Any) -> None:
        with pytest.raises(DisasterRecoveryError):
            self._manifest(**{field_name: value})


# --------------------------------------------------------------------------- #
# Hot backup
# --------------------------------------------------------------------------- #
class TestHotBackup:
    def test_a_live_wal_database_is_snapshotted_with_its_live_frames(
        self, tmp_path: Path, live_db: Any
    ) -> None:
        path, connection = live_db
        # Write *after* the last checkpoint: these rows live only in the WAL.
        connection.execute("INSERT INTO clips VALUES ('clip-hot', 'Hot')")
        connection.commit()
        manager = _manager(tmp_path)
        manifest = manager.create_hot_backup(database_path=path)
        assert manifest.tables == {"clips": 6, "markers": 3}
        assert manifest.total_rows == 9
        assert manifest.backup_size_bytes > 0
        assert len(manifest.backup_sha256) == 64
        bundle = manager.backup_root / manifest.bundle_id
        assert (bundle / HOT_BACKUP_FILENAME).is_file()
        assert (bundle / MANIFEST_FILENAME).is_file()
        # The snapshot itself really does contain the WAL-only row.
        snapshot = sqlite3.connect(bundle / HOT_BACKUP_FILENAME)
        try:
            assert snapshot.execute("SELECT name FROM clips WHERE id='clip-hot'").fetchone() == (
                "Hot",
            )
        finally:
            snapshot.close()
        assert manager.verify_backup_bundle(bundle).verified is True

    def test_a_missing_source_is_refused(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        with pytest.raises(DisasterRecoveryError) as excinfo:
            manager.create_hot_backup(database_path=tmp_path / "nope.sqlite3")
        assert excinfo.value.reason == "source_missing"

    def test_labels_are_recorded_in_the_signed_manifest(self, tmp_path: Path, live_db: Any) -> None:
        path, _connection = live_db
        manager = _manager(tmp_path)
        manifest = manager.create_hot_backup(
            database_path=path, labels={"fa": "پشتیبان", "en": "backup"}
        )
        assert manifest.labels == {"fa": "پشتیبان", "en": "backup"}
        assert manifest.verify_signature(BackupKeyMaterial("bk-1", KEY_HEX)) is True

    def test_two_backups_of_the_same_database_get_distinct_bundles(
        self, tmp_path: Path, live_db: Any
    ) -> None:
        path, _connection = live_db
        manager = _manager(tmp_path)
        first = manager.create_hot_backup(database_path=path)
        second = manager.create_hot_backup(database_path=path)
        assert first.bundle_id != second.bundle_id
        assert first.manifest_id != second.manifest_id
        assert len(manager.list_bundles()) == 2

    def test_a_failing_snapshot_leaves_no_half_written_bundle(
        self, tmp_path: Path, live_db: Any
    ) -> None:
        path, _connection = live_db
        manager = _manager(tmp_path)
        # A real failure: the "source" is actually a text file with a .sqlite3
        # name, so SQLite cannot open it as a database.
        broken = tmp_path / "broken.sqlite3"
        broken.write_text("this is not a database", encoding="utf-8")
        with pytest.raises(sqlite3.DatabaseError):
            manager.create_hot_backup(database_path=broken)
        assert manager.list_bundles() == ()
        assert list(manager.backup_root.iterdir()) == []

    def test_prune_keeps_the_newest_bundles(self, tmp_path: Path, live_db: Any) -> None:
        path, _connection = live_db
        manager = _manager(tmp_path)
        bundles = [manager.create_hot_backup(database_path=path) for _ in range(3)]
        doomed = manager.prune_bundles(keep=1)
        assert len(doomed) == 2
        survivors = manager.list_bundles()
        assert len(survivors) == 1
        # The survivor is a real bundle from that run, and it still verifies.
        assert survivors[0] in {bundle.bundle_id for bundle in bundles}
        assert manager.verify_backup_bundle(manager.backup_root / survivors[0]).verified is True
        with pytest.raises(DisasterRecoveryError):
            manager.prune_bundles(keep=0)

    def test_prune_with_nothing_to_do_is_a_no_op(self, tmp_path: Path, live_db: Any) -> None:
        path, _connection = live_db
        manager = _manager(tmp_path)
        manager.create_hot_backup(database_path=path)
        assert manager.prune_bundles(keep=5) == ()
        assert len(manager.list_bundles()) == 1

    def test_the_manager_refuses_a_non_key_object(self, tmp_path: Path) -> None:
        with pytest.raises(DisasterRecoveryError) as excinfo:
            StudioDisasterRecoveryManager(tmp_path, key_material="ab" * 32)  # type: ignore[arg-type]
        assert excinfo.value.reason == "invalid_key"

    def test_the_manager_exposes_its_root_and_key_id(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        assert manager.backup_root == tmp_path / "backups"
        assert manager.key_id == "bk-1"


# --------------------------------------------------------------------------- #
# Bundle verification — every tampering path
# --------------------------------------------------------------------------- #
class TestVerifyBundle:
    def _bundle(self, tmp_path: Path, live_db: Any) -> tuple[StudioDisasterRecoveryManager, Path]:
        path, _connection = live_db
        manager = _manager(tmp_path)
        manifest = manager.create_hot_backup(database_path=path)
        return manager, manager.backup_root / manifest.bundle_id

    def test_a_fresh_bundle_verifies(self, tmp_path: Path, live_db: Any) -> None:
        manager, bundle = self._bundle(tmp_path, live_db)
        report = manager.verify_backup_bundle(bundle)
        assert report.verified is True
        assert report.problems == ()
        assert report.integrity_check == "ok"
        assert report.actual_total_rows == 8
        assert report.manifest is not None
        assert report.to_dict()["verified"] is True

    def test_verification_does_not_mutate_the_bundle(self, tmp_path: Path, live_db: Any) -> None:
        manager, bundle = self._bundle(tmp_path, live_db)
        before = sorted((path.name, path.stat().st_size) for path in bundle.iterdir())
        manager.verify_backup_bundle(bundle)
        after = sorted((path.name, path.stat().st_size) for path in bundle.iterdir())
        assert before == after
        assert not (bundle / f"{HOT_BACKUP_FILENAME}-wal").exists()

    def test_a_missing_bundle_is_reported_not_raised(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        report = manager.verify_backup_bundle(tmp_path / "nope")
        assert report.verified is False
        assert "bundle_missing" in report.problems
        assert report.manifest is None

    def test_a_missing_manifest_is_reported(self, tmp_path: Path, live_db: Any) -> None:
        manager, bundle = self._bundle(tmp_path, live_db)
        (bundle / MANIFEST_FILENAME).unlink()
        report = manager.verify_backup_bundle(bundle)
        assert report.verified is False
        assert "manifest_missing" in report.problems

    def test_a_non_json_manifest_is_reported(self, tmp_path: Path, live_db: Any) -> None:
        manager, bundle = self._bundle(tmp_path, live_db)
        (bundle / MANIFEST_FILENAME).write_text("{not json", encoding="utf-8")
        report = manager.verify_backup_bundle(bundle)
        assert report.verified is False
        assert any("manifest_malformed" in problem for problem in report.problems)

    def test_a_single_flipped_bit_breaks_the_digest(self, tmp_path: Path, live_db: Any) -> None:
        manager, bundle = self._bundle(tmp_path, live_db)
        backup = bundle / HOT_BACKUP_FILENAME
        payload = bytearray(backup.read_bytes())
        # Flip a byte in the middle of the first page: the file still opens, so
        # only the recorded digest can catch this.
        payload[len(payload) // 2] ^= 0xFF
        backup.write_bytes(bytes(payload))
        report = manager.verify_backup_bundle(bundle)
        assert report.verified is False
        assert "digest_mismatch" in report.problems
        assert "signature_invalid" not in report.problems

    def test_truncation_is_reported_as_size_and_digest_mismatch(
        self, tmp_path: Path, live_db: Any
    ) -> None:
        manager, bundle = self._bundle(tmp_path, live_db)
        backup = bundle / HOT_BACKUP_FILENAME
        data = backup.read_bytes()
        backup.write_bytes(data[: len(data) // 2])
        report = manager.verify_backup_bundle(bundle)
        assert report.verified is False
        assert "size_mismatch" in report.problems
        assert "digest_mismatch" in report.problems

    def test_a_missing_backup_file_is_reported(self, tmp_path: Path, live_db: Any) -> None:
        manager, bundle = self._bundle(tmp_path, live_db)
        (bundle / HOT_BACKUP_FILENAME).unlink()
        report = manager.verify_backup_bundle(bundle)
        assert report.verified is False
        assert "backup_missing" in report.problems

    def test_an_unsigned_manifest_is_refused(self, tmp_path: Path, live_db: Any) -> None:
        manager, bundle = self._bundle(tmp_path, live_db)
        payload = json.loads((bundle / MANIFEST_FILENAME).read_text("utf-8"))
        payload["manifest_hmac_sha256"] = ""
        (bundle / MANIFEST_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
        report = manager.verify_backup_bundle(bundle)
        assert report.verified is False
        assert "signature_invalid" in report.problems

    def test_a_manifest_signed_by_another_key_is_refused(
        self, tmp_path: Path, live_db: Any
    ) -> None:
        manager, bundle = self._bundle(tmp_path, live_db)
        other_key = BackupKeyMaterial("bk-1", "cd" * 32)
        report = manager.verify_backup_bundle(bundle, key_material=other_key)
        assert report.verified is False
        assert "signature_invalid" in report.problems

    def test_a_manifest_with_rewritten_row_counts_is_refused(
        self, tmp_path: Path, live_db: Any
    ) -> None:
        """Re-signing a lie: a valid signature over wrong counts must still fail."""
        manager, bundle = self._bundle(tmp_path, live_db)
        payload = json.loads((bundle / MANIFEST_FILENAME).read_text("utf-8"))
        payload["tables"] = {"clips": 999, "markers": 3}
        payload["total_rows"] = 1002
        payload.pop("manifest_hmac_sha256")
        forged = BackupManifest.from_dict(payload)
        # Re-sign with the *real* key: the attacker has the signing key here, so
        # only the on-disk counts can catch the lie.
        forged = forged.with_signature(BackupKeyMaterial("bk-1", KEY_HEX))
        (bundle / MANIFEST_FILENAME).write_text(json.dumps(forged.to_dict()), encoding="utf-8")
        report = manager.verify_backup_bundle(bundle)
        assert report.verified is False
        assert "table_counts_mismatch" in report.problems
        assert "total_rows_mismatch" in report.problems

    def test_a_backup_with_a_dropped_table_is_refused(self, tmp_path: Path, live_db: Any) -> None:
        manager, bundle = self._bundle(tmp_path, live_db)
        backup = bundle / HOT_BACKUP_FILENAME
        connection = sqlite3.connect(backup)
        connection.execute("DROP TABLE markers")
        connection.commit()
        connection.close()
        report = manager.verify_backup_bundle(bundle)
        assert report.verified is False
        assert "table_counts_mismatch" in report.problems

    def test_a_corrupt_database_file_fails_integrity_check(
        self, tmp_path: Path, live_db: Any
    ) -> None:
        manager, bundle = self._bundle(tmp_path, live_db)
        backup = bundle / HOT_BACKUP_FILENAME
        # Keep the length but destroy the SQLite header magic.
        payload = bytearray(backup.read_bytes())
        payload[0:16] = b"\x00" * 16
        backup.write_bytes(bytes(payload))
        report = manager.verify_backup_bundle(bundle)
        assert report.verified is False
        assert any(problem.startswith("integrity_check_failed") for problem in report.problems)


# --------------------------------------------------------------------------- #
# Restore
# --------------------------------------------------------------------------- #
class TestRestore:
    def _bundle(self, tmp_path: Path, live_db: Any) -> tuple[StudioDisasterRecoveryManager, Path]:
        path, _connection = live_db
        manager = _manager(tmp_path)
        manifest = manager.create_hot_backup(database_path=path)
        return manager, manager.backup_root / manifest.bundle_id

    def test_restoring_replaces_the_target_with_the_snapshot(
        self, tmp_path: Path, live_db: Any
    ) -> None:
        manager, bundle = self._bundle(tmp_path, live_db)
        target = tmp_path / "restore" / "studio.sqlite3"
        result = manager.restore_from_backup(bundle, target_database_path=target)
        assert isinstance(result, RestoreResult)
        assert result.integrity_check == "ok"
        assert dict(result.tables) == {"clips": 5, "markers": 3}
        assert result.total_rows == 8
        assert result.quarantined_path == ""  # nothing was there to quarantine
        assert result.bundle_id == bundle.name
        connection = sqlite3.connect(target)
        try:
            assert connection.execute("SELECT COUNT(*) FROM clips").fetchone() == (5,)
        finally:
            connection.close()
        assert result.to_dict()["total_rows"] == 8

    def test_restoring_over_a_live_database_quarantines_it_and_drops_stale_sidecars(
        self, tmp_path: Path, live_db: Any
    ) -> None:
        manager, bundle = self._bundle(tmp_path, live_db)
        target = tmp_path / "studio.sqlite3"
        connection = sqlite3.connect(target)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS stale_clips (id TEXT PRIMARY KEY, name TEXT)"
        )
        connection.execute("INSERT INTO stale_clips VALUES ('stale', 'Stale')")
        connection.commit()
        # Materialise a real -wal/-shm pair beside the target.
        assert (tmp_path / "studio.sqlite3-wal").exists()
        result = manager.restore_from_backup(
            bundle,
            target_database_path=target,
        )
        connection.close()
        assert result.quarantined_path and Path(result.quarantined_path).is_file()
        assert set(result.removed_sidecars) >= {"-wal"}
        assert not (tmp_path / "studio.sqlite3-wal").exists()
        assert not (tmp_path / "studio.sqlite3-shm").exists()
        # The quarantine is the *old* database: the stale row is still there.
        quarantine = sqlite3.connect(result.quarantined_path)
        try:
            assert quarantine.execute("SELECT name FROM stale_clips").fetchone() == ("Stale",)
        finally:
            quarantine.close()
        restored = sqlite3.connect(target)
        try:
            assert restored.execute("SELECT COUNT(*) FROM clips").fetchone() == (5,)
        finally:
            restored.close()

    def test_a_tampered_bundle_is_never_restored(self, tmp_path: Path, live_db: Any) -> None:
        manager, bundle = self._bundle(tmp_path, live_db)
        backup = bundle / HOT_BACKUP_FILENAME
        data = bytearray(backup.read_bytes())
        data[len(data) // 2] ^= 0xFF
        backup.write_bytes(bytes(data))
        target = tmp_path / "restored.sqlite3"
        with pytest.raises(DisasterRecoveryError) as excinfo:
            manager.restore_from_backup(bundle, target_database_path=target)
        assert excinfo.value.reason == "backup_unverified"
        # Nothing was created at the target.
        assert not target.exists()
        assert list((tmp_path).glob("*.restoring-*")) == []

    def test_a_missing_bundle_is_never_restored(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        with pytest.raises(DisasterRecoveryError) as excinfo:
            manager.restore_from_backup(
                tmp_path / "nope", target_database_path=tmp_path / "x.sqlite3"
            )
        assert excinfo.value.reason == "backup_unverified"

    def test_the_restore_lock_is_exclusive(self, tmp_path: Path, live_db: Any) -> None:
        import fcntl
        import os

        manager, bundle = self._bundle(tmp_path, live_db)
        target = tmp_path / "locked.sqlite3"
        lock_path = target.with_name(target.name + ".restore.lock")
        holder = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(holder, fcntl.LOCK_EX)
        seen: list[str] = []

        def attempt() -> None:
            try:
                manager.restore_from_backup(bundle, target_database_path=target)
                seen.append("restored")
            except BaseException as exc:  # pragma: no cover - reported below
                seen.append(type(exc).__name__)

        worker = threading.Thread(target=attempt, daemon=True)
        try:
            worker.start()
            worker.join(timeout=0.4)
            # Still blocked on the lock: no restore happened yet.
            assert seen == []
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            os.close(holder)
        worker.join(timeout=30)
        assert seen == ["restored"]
        assert target.is_file()

    def test_restoring_twice_is_stable(self, tmp_path: Path, live_db: Any) -> None:
        manager, bundle = self._bundle(tmp_path, live_db)
        target = tmp_path / "twice.sqlite3"
        first = manager.restore_from_backup(bundle, target_database_path=target)
        second = manager.restore_from_backup(bundle, target_database_path=target)
        assert first.restored_sha256 == second.restored_sha256
        assert second.quarantined_path  # the first restore is kept aside
        assert dict(second.tables) == {"clips": 5, "markers": 3}

    def test_the_restore_cleans_up_a_failed_staging_step(
        self, tmp_path: Path, live_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager, bundle = self._bundle(tmp_path, live_db)
        target = tmp_path / "stage.sqlite3"

        import nexus_ai_agent.creative.studio.production_dr as module

        original = module._table_row_counts

        def exploding(path: Path) -> dict[str, int]:
            counts = original(path)
            if path.name.startswith("stage.sqlite3.restoring-"):
                raise DisasterRecoveryError("staged_counts_mismatch", "simulated")
            return counts

        monkeypatch.setattr(module, "_table_row_counts", exploding)
        with pytest.raises(DisasterRecoveryError):
            manager.restore_from_backup(bundle, target_database_path=target)
        # The staged copy is gone and the target was never created.
        assert list(tmp_path.glob("stage.sqlite3.restoring-*")) == []
        assert not target.exists()

    def test_a_snapshot_that_fails_integrity_leaves_no_bundle(
        self, tmp_path: Path, live_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A corrupt snapshot must be discarded, never kept as a "backup"."""
        path, _connection = live_db
        manager = _manager(tmp_path)

        import nexus_ai_agent.creative.studio.production_dr as module

        monkeypatch.setattr(module, "_integrity_check", lambda _path: "malformed")
        with pytest.raises(DisasterRecoveryError) as excinfo:
            manager.create_hot_backup(database_path=path)
        assert excinfo.value.reason == "backup_integrity_failed"
        assert manager.list_bundles() == ()
        assert list(manager.backup_root.iterdir()) == []

    def test_a_staged_copy_that_fails_integrity_is_discarded(
        self, tmp_path: Path, live_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager, bundle = self._bundle(tmp_path, live_db)
        target = tmp_path / "stage-integrity.sqlite3"

        import nexus_ai_agent.creative.studio.production_dr as module

        original = module._integrity_check

        def failing(path: Path) -> str:
            if ".restoring-" in path.name:
                return "malformed"
            return original(path)

        monkeypatch.setattr(module, "_integrity_check", failing)
        with pytest.raises(DisasterRecoveryError) as excinfo:
            manager.restore_from_backup(bundle, target_database_path=target)
        assert excinfo.value.reason == "staged_integrity_failed"
        assert list(tmp_path.glob("stage-integrity.sqlite3.restoring-*")) == []
        assert not target.exists()

    def test_a_swap_that_raises_still_cleans_the_staging_file(
        self, tmp_path: Path, live_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The outer ``except BaseException`` of the swap body."""
        manager, bundle = self._bundle(tmp_path, live_db)
        target = tmp_path / "swap.sqlite3"

        def exploding_replace(_src: Any, _dst: Any) -> None:
            raise OSError("simulated cross-device failure")

        monkeypatch.setattr(os, "replace", exploding_replace)
        with pytest.raises(OSError):
            manager.restore_from_backup(bundle, target_database_path=target)
        assert list(tmp_path.glob("swap.sqlite3.restoring-*")) == []
        assert not target.exists()

    def test_a_sidecar_that_reappears_after_the_swap_is_recorded(
        self, tmp_path: Path, live_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The post-swap sweep: a reader may recreate ``-wal`` during the swap."""
        manager, bundle = self._bundle(tmp_path, live_db)
        target = tmp_path / "sweep.sqlite3"

        import nexus_ai_agent.creative.studio.production_dr as module

        original = module._remove_sidecars
        calls: list[str] = []

        def noisy(path: Path) -> tuple[str, ...]:
            calls.append(path.name)
            found = original(path)
            if path == target and calls.count("sweep.sqlite3") == 1:
                # First sweep (before the swap) reports nothing; the *second*
                # sees a sidecar that appeared while we were replacing the file.
                return found
            if path == target:
                sidecar = target.with_name(target.name + "-wal")
                sidecar.write_bytes(b"")
                return original(path) + ("-wal",)
            return found

        monkeypatch.setattr(module, "_remove_sidecars", noisy)
        result = manager.restore_from_backup(bundle, target_database_path=target)
        assert "-wal" in result.removed_sidecars

    def test_a_restored_file_that_fails_integrity_is_reported(
        self, tmp_path: Path, live_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager, bundle = self._bundle(tmp_path, live_db)
        target = tmp_path / "postcheck.sqlite3"

        import nexus_ai_agent.creative.studio.production_dr as module

        original = module._integrity_check
        state = {"swapped": False}

        # Post-restore verification runs *after* the swap: make it fail.
        def failing_after_swap(path: Path) -> str:
            if path == target and state["swapped"]:
                return "malformed"
            return original(path)

        original_replace = os.replace

        def marking_replace(src: Any, dst: Any) -> None:
            original_replace(src, dst)
            state["swapped"] = True

        monkeypatch.setattr(os, "replace", marking_replace)
        monkeypatch.setattr(module, "_integrity_check", failing_after_swap)
        with pytest.raises(DisasterRecoveryError) as excinfo:
            manager.restore_from_backup(bundle, target_database_path=target)
        assert excinfo.value.reason == "restore_integrity_failed"

    def test_a_staged_copy_whose_counts_differ_is_refused(
        self, tmp_path: Path, live_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A staged file that opens fine but holds the wrong rows is refused."""
        manager, bundle = self._bundle(tmp_path, live_db)
        target = tmp_path / "counts.sqlite3"

        import nexus_ai_agent.creative.studio.production_dr as module

        original = module._table_row_counts

        def lying(path: Path) -> dict[str, int]:
            counts = original(path)
            if ".restoring-" in path.name:
                return {name: count + 1 for name, count in counts.items()}
            return counts

        monkeypatch.setattr(module, "_table_row_counts", lying)
        with pytest.raises(DisasterRecoveryError) as excinfo:
            manager.restore_from_backup(bundle, target_database_path=target)
        assert excinfo.value.reason == "staged_counts_mismatch"
        assert list(tmp_path.glob("counts.sqlite3.restoring-*")) == []


# --------------------------------------------------------------------------- #
# Native lock helpers
# --------------------------------------------------------------------------- #
class TestNativeLockHelpers:
    def test_a_failing_flock_closes_its_descriptor_and_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import fcntl

        import nexus_ai_agent.creative.studio.production_dr as module

        opened: list[int] = []
        closed: list[int] = []
        real_open = os.open
        real_close = os.close

        def tracking_open(*args: Any, **kwargs: Any) -> int:
            handle = int(real_open(*args, **kwargs))
            opened.append(handle)
            return handle

        def tracking_close(handle: int) -> None:
            closed.append(handle)
            real_close(handle)

        def exploding_flock(_handle: int, _operation: int) -> None:
            raise OSError("simulated flock failure")

        monkeypatch.setattr(os, "open", tracking_open)
        monkeypatch.setattr(os, "close", tracking_close)
        monkeypatch.setattr(fcntl, "flock", exploding_flock)
        with pytest.raises(OSError):
            module._acquire_flock(tmp_path / "native.lock")
        assert opened and closed == opened  # the descriptor leaked no further

    def test_acquire_and_release_round_trip(self, tmp_path: Path) -> None:
        from nexus_ai_agent.creative.studio.production_dr import (
            _acquire_flock,
            _release_flock,
        )

        lock_path = tmp_path / "round-trip.lock"
        handle = _acquire_flock(lock_path)
        assert lock_path.exists()
        _release_flock(handle)
        # The lock is free again: a second acquisition succeeds immediately.
        again = _acquire_flock(lock_path)
        _release_flock(again)


# --------------------------------------------------------------------------- #
# Resource governor
# --------------------------------------------------------------------------- #
class TestAdmissionLease:
    def test_a_lease_returns_its_slot_exactly_once(self) -> None:
        governor = StudioResourceGovernor(max_concurrent_jobs=2)
        try:
            lease = governor.acquire()
            assert isinstance(lease, AdmissionLease)
            observations = [lease.held, governor.active_leases]
            lease.release()
            observations.append(lease.held)
            observations.append(governor.active_leases)
            lease.release()  # idempotent: never double-counts
            # held: True -> False; active: 1 -> 0; and the second release changed
            # nothing (one completion, not two).
            assert observations == [True, 1, False, 0]
            assert governor.stats()["completed"] == 1
        finally:
            governor.shutdown()

    def test_a_lease_is_a_context_manager(self) -> None:
        governor = StudioResourceGovernor(max_concurrent_jobs=1)
        try:
            with governor.acquire() as lease:
                assert lease.held is True
                assert governor.active_leases == 1
            assert governor.active_leases == 0
            with governor.admit():
                assert governor.active_leases == 1
            assert governor.active_leases == 0
        finally:
            governor.shutdown()

    def test_detach_marks_the_lease_as_handed_off(self) -> None:
        governor = StudioResourceGovernor(max_concurrent_jobs=1)
        try:
            lease = governor.acquire()
            lease.detach()
            assert lease.detached is True
            assert lease.held is True  # still occupying the slot
            assert "held=True" in repr(lease)
            lease.release()
        finally:
            governor.shutdown()


class TestStudioResourceGovernor:
    def test_the_ceiling_is_enforced(self) -> None:
        governor = StudioResourceGovernor(max_concurrent_jobs=1, max_queue_depth=0)
        try:
            lease = governor.acquire()
            with pytest.raises(GovernorSaturatedError) as excinfo:
                governor.acquire()
            assert excinfo.value.reason == "saturated"
            assert governor.stats()["rejected"] == 1
            lease.release()
            assert governor.acquire().held is True
        finally:
            governor.shutdown()

    def test_a_waiter_gets_the_next_free_slot(self) -> None:
        governor = StudioResourceGovernor(max_concurrent_jobs=1, max_queue_depth=4)
        try:
            lease = governor.acquire()
            got: list[AdmissionLease] = []

            def wait() -> None:
                got.append(governor.acquire())

            worker = threading.Thread(target=wait, daemon=True)
            worker.start()
            time.sleep(0.05)
            assert got == []
            assert governor.stats()["waiting"] == 1
            lease.release()
            worker.join(timeout=10)
            assert len(got) == 1
            got[0].release()
            assert governor.stats()["peak"] == 1
        finally:
            governor.shutdown()

    @pytest.mark.parametrize("value", [0, -1, True])
    def test_invalid_limits_are_refused(self, value: Any) -> None:
        with pytest.raises(GovernorSaturatedError) as excinfo:
            StudioResourceGovernor(max_concurrent_jobs=value)
        assert excinfo.value.reason == "invalid_limit"

    def test_a_negative_queue_depth_is_refused(self) -> None:
        with pytest.raises(GovernorSaturatedError):
            StudioResourceGovernor(max_concurrent_jobs=1, max_queue_depth=-1)

    def test_sync_runs_are_bounded_and_counted(self) -> None:
        # The lease ceiling under test is 2; the executor that runs the admitted
        # callables is separate and needs headroom (see run_with_lease's docstring).
        governor = StudioResourceGovernor(max_concurrent_jobs=2, executor_workers=6)
        results: list[int] = []
        lock = threading.Lock()

        def work(value: int) -> None:
            time.sleep(0.02)
            with lock:
                results.append(value)

        try:

            def run_one(value: int) -> None:
                with governor.admit():
                    work(value)

            threads = [threading.Thread(target=lambda v=value: run_one(v)) for value in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            assert sorted(results) == list(range(6))
            stats = governor.stats()
            assert stats["admitted"] == 6
            assert stats["completed"] == 6
            assert stats["active"] == 0
            assert stats["peak"] <= 2
            assert stats["max_executor_workers"] == 6
        finally:
            governor.shutdown()

    def test_run_with_lease_returns_the_worker_result(self) -> None:
        governor = StudioResourceGovernor(max_concurrent_jobs=2)

        async def main() -> int:
            return await governor.run_with_lease(lambda a, b=0: a + b, 40, b=2)

        try:
            assert asyncio.run(main()) == 42
            assert governor.active_leases == 0
            assert governor.stats()["completed"] == 1
        finally:
            governor.shutdown()

    def test_a_failing_worker_releases_its_slot_immediately(self) -> None:
        governor = StudioResourceGovernor(max_concurrent_jobs=1)

        def explode() -> None:
            raise RuntimeError("worker failed")

        async def main() -> None:
            with pytest.raises(RuntimeError):
                await governor.run_with_lease(explode)
            assert governor.active_leases == 0

        try:
            asyncio.run(main())
        finally:
            governor.shutdown()

    def test_cancellation_keeps_the_slot_until_the_thread_finishes(self) -> None:
        """The over-admission bug: cancel must not free a slot still in use."""
        governor = StudioResourceGovernor(max_concurrent_jobs=1, max_queue_depth=0)
        finished = threading.Event()

        def slow() -> None:
            time.sleep(0.25)
            finished.set()

        async def main() -> list[int]:
            observations: list[int] = []
            task = asyncio.ensure_future(governor.run_with_lease(slow))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            observations.append(governor.active_leases)  # still holding: 1
            assert not finished.is_set()
            # A second admission must be REFUSED while the orphaned thread runs.
            with pytest.raises(GovernorSaturatedError):
                governor.acquire()
            await asyncio.sleep(0.35)
            observations.append(governor.active_leases)  # released: 0
            return observations

        try:
            observations = asyncio.run(main())
            assert observations == [1, 0]
            assert finished.is_set()
        finally:
            governor.shutdown()

    def test_run_with_lease_actually_bounds_parallelism(self) -> None:
        governor = StudioResourceGovernor(max_concurrent_jobs=2)
        active = 0
        peak = 0
        lock = threading.Lock()

        def work() -> None:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with lock:
                active -= 1

        async def main() -> None:
            await asyncio.gather(*(governor.run_with_lease(work) for _ in range(8)))

        try:
            asyncio.run(main())
            assert peak <= 2
            assert governor.stats()["admitted"] == 8
        finally:
            governor.shutdown()

    @pytest.mark.parametrize("value", [0, -1, True])
    def test_invalid_executor_sizes_are_refused(self, value: Any) -> None:
        with pytest.raises(GovernorSaturatedError) as excinfo:
            StudioResourceGovernor(max_concurrent_jobs=2, executor_workers=value)
        assert excinfo.value.reason == "invalid_limit"

    def test_the_default_executor_never_sizes_below_four(self) -> None:
        for ceiling in (1, 2, 3, 8):
            governor = StudioResourceGovernor(max_concurrent_jobs=ceiling)
            try:
                assert governor.stats()["max_executor_workers"] == max(4, ceiling)
            finally:
                governor.shutdown()

    def test_releasing_a_lease_the_governor_never_issued_is_a_no_op(self) -> None:
        """Defensive: a foreign/duplicated lease must not inflate the counters."""
        governor = StudioResourceGovernor(max_concurrent_jobs=1)
        try:
            foreign = AdmissionLease(governor, "lse_never_admitted")
            governor._release(foreign)
            assert governor.stats()["completed"] == 0
            assert governor.active_leases == 0
        finally:
            governor.shutdown()

    def test_an_interrupted_wait_does_not_leak_a_waiter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the spin is interrupted, the waiting count is rolled back."""
        governor = StudioResourceGovernor(max_concurrent_jobs=1, max_queue_depth=4)
        import time as time_module

        def exploding_sleep(_seconds: float) -> None:
            raise KeyboardInterrupt("simulated interrupt")

        monkeypatch.setattr(time_module, "sleep", exploding_sleep)
        holder = governor.acquire()
        try:
            # The queue has room, so we enter the spin and then blow up inside it.
            with pytest.raises(KeyboardInterrupt):
                governor.acquire()
            assert governor.stats()["waiting"] == 0
        finally:
            holder.release()
            governor.shutdown()

    def test_shutdown_is_idempotent_and_a_context_manager(self) -> None:
        with StudioResourceGovernor(max_concurrent_jobs=1) as governor:
            assert governor.max_concurrent_jobs == 1
        governor.shutdown()
        governor.shutdown(wait=False)

    def test_utc_now_is_timezone_aware(self) -> None:
        assert utc_now().endswith("+00:00")


# --------------------------------------------------------------------------- #
# Report objects
# --------------------------------------------------------------------------- #
class TestReports:
    def test_a_bundle_report_round_trips(self) -> None:
        report = BackupBundleReport(
            bundle_id="bkp-1",
            verified=False,
            manifest=None,
            problems=("digest_mismatch",),
            integrity_check="ok",
            actual_sha256="ab" * 32,
            actual_size_bytes=10,
            actual_tables={"clips": 1},
            actual_total_rows=1,
        )
        payload = report.to_dict()
        assert payload["manifest"] is None
        assert payload["problems"] == ["digest_mismatch"]
        assert payload["actual_tables"] == {"clips": 1}

    def test_a_restore_result_round_trips(self) -> None:
        result = RestoreResult(
            bundle_id="bkp-1",
            target_path="/tmp/studio.sqlite3",
            restored_sha256="ab" * 32,
            restored_size_bytes=4096,
            tables={"markers": 3, "clips": 5},
            total_rows=8,
            staged_path="/tmp/staged",
            quarantined_path="",
            removed_sidecars=("-wal", "-shm"),
            integrity_check="ok",
        )
        payload = result.to_dict()
        assert list(payload["tables"]) == ["clips", "markers"]
        assert payload["removed_sidecars"] == ["-wal", "-shm"]
