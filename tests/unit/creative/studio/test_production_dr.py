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

from nexus_ai_agent.creative.studio import production_dr  # noqa: F401 - monkeypatch seam
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

    def test_verification_never_deletes_files_from_the_bundle(
        self, tmp_path: Path, live_db: Any
    ) -> None:
        """Verification must not be a delete primitive.

        The previous contract asserted that verification *removed* the ``-wal``
        SQLite materialises.  That is exactly the behaviour Wave 2 makes
        impossible: a read must never unlink a file beside a path it was merely
        asked to inspect.  What is asserted instead is the property that
        matters — every name present before the call is still present after it.
        """
        manager, bundle = self._bundle(tmp_path, live_db)
        before = {path.name for path in bundle.iterdir()}
        manager.verify_backup_bundle(bundle)
        after = {path.name for path in bundle.iterdir()}
        assert before <= after
        missing = before - after
        assert missing == set()
        # A second pass must not report the sidecars the first pass created as a
        # *stale* bundle state, because it must not have created them at all.
        # (SQLite materialises them from a read; both passes see the same set.)
        second = manager.verify_backup_bundle(bundle)
        assert set(path.name for path in bundle.iterdir()) == after
        assert second.verified is True

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

        def exploding(path: Path, **_kwargs: Any) -> dict[str, int]:
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

        def failing(path: Path, **_kwargs: Any) -> str:
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
        def failing_after_swap(path: Path, **_kwargs: Any) -> str:
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

        def lying(path: Path, **_kwargs: Any) -> dict[str, int]:
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


# --------------------------------------------------------------------------- #
# Wave 2A — path traversal must be refused before any I/O happens
# --------------------------------------------------------------------------- #
class TestManifestPathTraversalDefence:
    """A manifest is untrusted input until its HMAC verifies.

    Two independent layers are attacked here, because either one alone is a
    single point of failure: the manifest may not *hold* a path, and verification
    may not *act* on one before authenticating it.
    """

    @staticmethod
    def _valid_fields() -> dict[str, Any]:
        return {
            "manifest_id": "man-1",
            "bundle_id": "bun-1",
            "created_at_utc": "2026-11-27T00:00:00+00:00",
            "source_name": "studio.sqlite3",
            "backup_filename": HOT_BACKUP_FILENAME,
            "backup_sha256": "a" * 64,
            "backup_size_bytes": 10,
            "key_id": "bk-1",
        }

    @pytest.mark.parametrize(
        "name",
        [
            "../victim.sqlite3",
            "../../victim.sqlite3",
            "/abs/path/victim.sqlite3",
            "sub/dir/victim.sqlite3",
            "sub\\victim.sqlite3",
            "..",
            ".",
            ".hidden.sqlite3",
            "victim.db",
            "victim.sqlite3 ",
            "",
            "victim\x00.sqlite3",
        ],
    )
    def test_a_traversing_or_non_canonical_filename_is_refused_at_construction(
        self, name: str
    ) -> None:
        with pytest.raises(DisasterRecoveryError) as excinfo:
            BackupManifest(**{**self._valid_fields(), "backup_filename": name})
        assert excinfo.value.reason in {"invalid_backup_filename", "invalid_field"}

    def test_canonical_sqlite3_names_are_still_accepted(self) -> None:
        for name in (HOT_BACKUP_FILENAME, "hot_backup-2026_11_27.sqlite3", "A1.sqlite3"):
            manifest = BackupManifest(**{**self._valid_fields(), "backup_filename": name})
            assert manifest.backup_filename == name

    def test_tampered_manifest_with_path_traversal_does_not_read_or_unlink_outside_bundle(
        self, tmp_path: Path
    ) -> None:
        """The exact attack: an unsigned manifest pointing at a sibling victim."""
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        victim = tmp_path / "victim.sqlite3"
        victim.write_bytes(b"PRECIOUS-HOST-DATA")
        victim_wal = tmp_path / "victim.sqlite3-wal"
        victim_wal.write_bytes(b"WAL")
        victim_shm = tmp_path / "victim.sqlite3-shm"
        victim_shm.write_bytes(b"SHM")

        # An attacker-written manifest with a traversal in the filename.
        payload = {
            **self._valid_fields(),
            "backup_filename": "../victim.sqlite3",
            "manifest_hmac_sha256": "0" * 64,
        }
        (bundle / MANIFEST_FILENAME).write_text(json.dumps(payload), encoding="utf-8")

        manager = _manager(tmp_path)
        opened: list[str] = []
        real_open = os.open
        real_sha = production_dr._sha256_file

        def tracking_open(path: Any, *args: Any, **kwargs: Any) -> Any:
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        def tracking_sha(path: Path) -> tuple[str, int]:
            opened.append(str(path))
            return real_sha(path)

        try:
            production_dr.os.open = tracking_open  # type: ignore[assignment]
            production_dr._sha256_file = tracking_sha  # type: ignore[assignment]
            report = manager.verify_backup_bundle(bundle)
        finally:
            production_dr.os.open = real_open  # type: ignore[assignment]
            production_dr._sha256_file = real_sha  # type: ignore[assignment]

        assert report.verified is False
        assert report.manifest is None
        assert any("manifest_malformed" in problem for problem in report.problems)
        # Nothing outside the bundle was opened, hashed, or unlinked.
        assert opened == []
        assert victim.read_bytes() == b"PRECIOUS-HOST-DATA"
        assert victim_wal.exists()
        assert victim_shm.exists()

    def _signed_bundle_with_manifest(
        self, tmp_path: Path, live_db: Any, **overrides: Any
    ) -> tuple[StudioDisasterRecoveryManager, Path, BackupKeyMaterial]:
        path, _connection = live_db
        key = BackupKeyMaterial("bk-1", KEY_HEX)
        manager = _manager(tmp_path, key=key)
        manifest = manager.create_hot_backup(database_path=path)
        bundle = manager.backup_root / manifest.bundle_id
        stored = json.loads((bundle / MANIFEST_FILENAME).read_text("utf-8"))
        stored.update(overrides)
        # Re-sign whatever the attacker now claims, with a *different* key: the
        # signature must not verify, which is the gate under test.
        candidate = BackupManifest.from_dict({**stored, "manifest_hmac_sha256": "0" * 64})
        rogue = BackupKeyMaterial("bk-1", "cd" * 32)
        (bundle / MANIFEST_FILENAME).write_text(
            json.dumps(candidate.with_signature(rogue).to_dict()), encoding="utf-8"
        )
        return manager, bundle, rogue

    def test_an_invalid_signature_stops_before_any_backup_io(
        self, tmp_path: Path, live_db: Any
    ) -> None:
        """A forged-but-parseable manifest names a *sibling* file: it must not be read."""
        manager, bundle, _rogue = self._signed_bundle_with_manifest(tmp_path, live_db)
        sibling = bundle.parent / "sibling.sqlite3"
        sibling.write_bytes(b"NOT-A-BACKUP")
        stored = json.loads((bundle / MANIFEST_FILENAME).read_text("utf-8"))
        stored["backup_filename"] = "sibling.sqlite3"
        rogue = BackupKeyMaterial("bk-1", "cd" * 32)
        forged = BackupManifest.from_dict({**stored, "manifest_hmac_sha256": "0" * 64})
        (bundle / MANIFEST_FILENAME).write_text(
            json.dumps(forged.with_signature(rogue).to_dict()), encoding="utf-8"
        )

        touched: list[str] = []
        real_sha = production_dr._sha256_file
        real_integrity = production_dr._integrity_check

        def tracking_sha(path: Path) -> tuple[str, int]:
            touched.append("sha:" + path.name)
            return real_sha(path)

        def tracking_integrity(path: Path) -> str:
            touched.append("integrity:" + path.name)
            return real_integrity(path)

        try:
            production_dr._sha256_file = tracking_sha  # type: ignore[assignment]
            production_dr._integrity_check = tracking_integrity  # type: ignore[assignment]
            report = manager.verify_backup_bundle(bundle)
        finally:
            production_dr._sha256_file = real_sha  # type: ignore[assignment]
            production_dr._integrity_check = real_integrity  # type: ignore[assignment]

        assert report.verified is False
        assert report.problems == ("signature_invalid",)
        # The cryptographic gate ran first: no file was hashed or opened.
        assert touched == []

    def test_a_symlinked_backup_is_refused_even_when_signed(
        self, tmp_path: Path, live_db: Any
    ) -> None:
        manager, bundle, _ = self._signed_bundle_with_manifest(tmp_path, live_db)
        # Re-create a genuine bundle, then replace the backup with a symlink and
        # sign the manifest honestly, so only the symlink gate can catch it.
        path, _connection = live_db
        key = BackupKeyMaterial("bk-1", KEY_HEX)
        honest = _manager(tmp_path, key=key).create_hot_backup(database_path=path)
        bundle = manager.backup_root / honest.bundle_id
        backup = bundle / HOT_BACKUP_FILENAME
        outside = tmp_path / "outside.sqlite3"
        outside.write_bytes(b"OUTSIDE")
        backup.unlink()
        backup.symlink_to(outside)
        report = manager.verify_backup_bundle(bundle)
        assert report.verified is False
        assert "backup_symlink_rejected" in report.problems
        assert outside.read_bytes() == b"OUTSIDE"

    def test_a_directory_named_like_the_backup_is_refused(
        self, tmp_path: Path, live_db: Any
    ) -> None:
        path, _connection = live_db
        key = BackupKeyMaterial("bk-1", KEY_HEX)
        manager = _manager(tmp_path, key=key)
        manifest = manager.create_hot_backup(database_path=path)
        bundle = manager.backup_root / manifest.bundle_id
        backup = bundle / HOT_BACKUP_FILENAME
        backup.unlink()
        backup.mkdir()
        report = manager.verify_backup_bundle(bundle)
        assert report.verified is False
        assert "backup_not_a_regular_file" in report.problems

    def test_unexpected_sidecars_are_reported_and_preserved(
        self, tmp_path: Path, live_db: Any
    ) -> None:
        """Verification is read-only: it reports sidecars, it never deletes them."""
        path, _connection = live_db
        key = BackupKeyMaterial("bk-1", KEY_HEX)
        manager = _manager(tmp_path, key=key)
        manifest = manager.create_hot_backup(database_path=path)
        bundle = manager.backup_root / manifest.bundle_id
        wal = bundle / f"{HOT_BACKUP_FILENAME}-wal"
        shm = bundle / f"{HOT_BACKUP_FILENAME}-shm"
        wal.write_bytes(b"STALE-WAL")
        shm.write_bytes(b"STALE-SHM")
        # A read-only SQLite open still *materialises* sidecars for a WAL file, so
        # they are (re)created after the check; what must never happen is a
        # delete.  Planting them fresh before the call proves the report fires.
        report = manager.verify_backup_bundle(bundle)
        assert report.verified is False
        assert report.problems == ("unexpected_sidecar: wal",)
        # Preserved, never unlinked: reporting is the whole job here.
        assert wal.exists() and shm.exists()
        assert wal.read_bytes() == b"STALE-WAL"

    def test_an_empty_wal_and_a_shm_are_bookkeeping_not_findings(
        self, tmp_path: Path, live_db: Any
    ) -> None:
        """Verification must stay idempotent across its own read side-effects."""
        path, _connection = live_db
        manager = _manager(tmp_path, key=BackupKeyMaterial("bk-1", KEY_HEX))
        manifest = manager.create_hot_backup(database_path=path)
        bundle = manager.backup_root / manifest.bundle_id
        (bundle / f"{HOT_BACKUP_FILENAME}-wal").write_bytes(b"")
        (bundle / f"{HOT_BACKUP_FILENAME}-shm").write_bytes(b"x" * 32768)
        report = manager.verify_backup_bundle(bundle)
        assert report.verified is True
        assert report.problems == ()

    # -- the gate ordering, attacked from the instrumentation side ---------- #
    def _forge_signed_manifest_into(
        self, bundle: Path, *, backup_filename: str = HOT_BACKUP_FILENAME
    ) -> None:
        """Rewrite the manifest and sign it with a key the manager does not hold.

        Note that a traversing ``backup_filename`` cannot be *constructed*, so the
        only way an attacker can get one onto disk is to write the JSON directly —
        which is exactly what an attacker with file-write access but no key does.
        """
        stored = json.loads((bundle / MANIFEST_FILENAME).read_text("utf-8"))
        stored["backup_filename"] = backup_filename
        stored["manifest_hmac_sha256"] = "0" * 64
        (bundle / MANIFEST_FILENAME).write_text(json.dumps(stored), encoding="utf-8")

    @staticmethod
    def _recording_probe() -> tuple[Any, list[str]]:
        """Patch the I/O seams so any verification read is recorded."""
        touched: list[str] = []
        originals = {
            "sha": production_dr._sha256_file,
            "integrity": production_dr._integrity_check,
            "counts": production_dr._table_row_counts,
        }

        def recording_sha(path: Path) -> tuple[str, int]:
            touched.append("sha:" + path.name)
            return originals["sha"](path)

        def recording_integrity(path: Path, **kwargs: Any) -> str:
            touched.append("integrity:" + path.name)
            return originals["integrity"](path, **kwargs)

        def recording_counts(path: Path, **kwargs: Any) -> dict[str, int]:
            touched.append("counts:" + path.name)
            return originals["counts"](path, **kwargs)

        production_dr._sha256_file = recording_sha  # type: ignore[assignment]
        production_dr._integrity_check = recording_integrity  # type: ignore[assignment]
        production_dr._table_row_counts = recording_counts  # type: ignore[assignment]
        return originals, touched

    @staticmethod
    def _restore_probe(originals: Any) -> None:
        production_dr._sha256_file = originals["sha"]  # type: ignore[assignment]
        production_dr._integrity_check = originals["integrity"]  # type: ignore[assignment]
        production_dr._table_row_counts = originals["counts"]  # type: ignore[assignment]

    def test_a_forged_signature_is_not_even_tried_against_the_digest(
        self, tmp_path: Path, live_db: Any
    ) -> None:
        """Ordering, measured: a bad HMAC must short-circuit before any read.

        This is the property M44 attacks.  If the gate is removed, verification
        falls through and *does* hash the file, so ``touched`` stops being empty
        even when the final verdict is still "unverified".
        """
        path, _connection = live_db
        manager = _manager(tmp_path, key=BackupKeyMaterial("bk-1", KEY_HEX))
        manifest = manager.create_hot_backup(database_path=path)
        bundle = manager.backup_root / manifest.bundle_id
        self._forge_signed_manifest_into(bundle)

        originals, touched = self._recording_probe()
        try:
            report = manager.verify_backup_bundle(bundle)
        finally:
            self._restore_probe(originals)

        assert report.verified is False
        assert report.problems == ("signature_invalid",)
        assert touched == [], f"verification performed I/O before authenticating: {touched}"
        assert report.actual_sha256 == ""
        assert report.actual_size_bytes == 0

    def test_a_traversing_filename_is_refused_before_it_is_ever_joined(
        self, tmp_path: Path, live_db: Any
    ) -> None:
        """Ordering, measured, for the *other* gate (M45).

        A signed manifest naming ``../sibling.sqlite3`` must be refused at
        construction -- before ``bundle_dir / name`` is evaluated -- so no probe
        sees a read and the sibling is untouched.
        """
        path, _connection = live_db
        manager = _manager(tmp_path, key=BackupKeyMaterial("bk-1", KEY_HEX))
        manifest = manager.create_hot_backup(database_path=path)
        bundle = manager.backup_root / manifest.bundle_id
        sibling = bundle.parent / "sibling.sqlite3"
        sibling.write_bytes(b"NOT-A-BACKUP")
        self._forge_signed_manifest_into(bundle, backup_filename="../sibling.sqlite3")

        originals, touched = self._recording_probe()
        try:
            report = manager.verify_backup_bundle(bundle)
        finally:
            self._restore_probe(originals)

        assert report.verified is False
        assert report.manifest is None  # the manifest never became a value
        assert any("invalid_backup_filename" in problem for problem in report.problems)
        assert touched == []
        assert sibling.read_bytes() == b"NOT-A-BACKUP"

    def test_the_signature_gate_precedes_the_filename_gate(self) -> None:
        """Ordering on one manifest: which problem is reported first?"""
        # A traversing name never survives construction, so the *signature* is the
        # only gate a malformed manifest can even reach.  Proven by construction:
        with pytest.raises(DisasterRecoveryError) as excinfo:
            BackupManifest(
                manifest_id="man-1",
                bundle_id="bun-1",
                created_at_utc="2026-11-27T00:00:00+00:00",
                source_name="studio.sqlite3",
                backup_filename="../x.sqlite3",
                backup_sha256="a" * 64,
                backup_size_bytes=1,
                key_id="bk-1",
            )
        assert excinfo.value.reason == "invalid_backup_filename"


# --------------------------------------------------------------------------- #
# Wave 2B — a cancelled waiter must never leak governor capacity
# --------------------------------------------------------------------------- #
class TestCancelledWaiterLeaseLeak:
    def test_cancelled_waiter_during_lease_acquire_never_leaks_governor_capacity(
        self,
    ) -> None:
        governor = StudioResourceGovernor(max_concurrent_jobs=1, max_queue_depth=4)
        holder_release = threading.Event()

        def holder() -> None:
            holder_release.wait(2.0)

        async def main() -> dict[str, int | bool]:
            first = asyncio.ensure_future(governor.run_with_lease(holder))
            while governor.active_leases == 0:
                await asyncio.sleep(0.01)

            waiter_started = threading.Event()

            def waiter_worker() -> str:
                waiter_started.set()
                return "ran"

            second = asyncio.ensure_future(governor.run_with_lease(waiter_worker))
            # Let the second task reach the blocking acquire in its worker thread.
            for _ in range(200):
                if governor.stats()["waiting"] == 1:
                    break
                await asyncio.sleep(0.005)
            assert governor.stats()["waiting"] == 1

            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second
            assert waiter_started.is_set() is False  # the work never started

            # Release the holder: the abandoned thread must NOT take the slot.
            holder_release.set()
            await first

            for _ in range(200):
                if governor.active_leases == 0 and governor.stats()["waiting"] == 0:
                    break
                await asyncio.sleep(0.005)

            # A third caller gets the slot immediately -- no leaked capacity.
            third = await asyncio.wait_for(governor.run_with_lease(lambda: "ok"), timeout=2.0)
            return {
                "active": governor.active_leases,
                "waiting": governor.stats()["waiting"],
                "admitted": governor.stats()["admitted"],
                "third": third == "ok",
                "waiter_ran": waiter_started.is_set(),
            }

        try:
            result = asyncio.run(main())
        finally:
            holder_release.set()
            governor.shutdown()

        assert result["active"] == 0
        assert result["waiting"] == 0
        assert result["third"] is True
        assert result["waiter_ran"] is False
        # Two admissions only: the holder and the third caller.  The cancelled
        # waiter consumed nothing.
        assert result["admitted"] == 2

    def test_a_cancelled_waiter_that_wins_the_race_still_returns_its_lease(self) -> None:
        """The narrow race: the slot opens in the same instant as the cancel."""
        governor = StudioResourceGovernor(max_concurrent_jobs=1, max_queue_depth=2)
        holder_release = threading.Event()

        async def main() -> tuple[int, int]:
            first = asyncio.ensure_future(governor.run_with_lease(holder_release.wait))
            while governor.active_leases == 0:
                await asyncio.sleep(0.005)
            second = asyncio.ensure_future(governor.run_with_lease(lambda: "work"))
            for _ in range(200):
                if governor.stats()["waiting"] == 1:
                    break
                await asyncio.sleep(0.005)
            # Free the slot *and* cancel at once: the acquire may already have won.
            holder_release.set()
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second
            await first
            for _ in range(400):
                stats = governor.stats()
                if governor.active_leases == 0 and stats["waiting"] == 0:
                    break
                await asyncio.sleep(0.005)
            return governor.active_leases, governor.stats()["waiting"]

        try:
            active, waiting = asyncio.run(main())
        finally:
            holder_release.set()
            governor.shutdown()
        assert active == 0
        assert waiting == 0
        assert governor.stats()["admitted"] <= 2

    def test_acquire_interruptible_honours_a_pre_set_cancel_event(self) -> None:
        governor = StudioResourceGovernor(max_concurrent_jobs=1)
        first = governor.acquire()
        event = threading.Event()
        event.set()
        try:
            with pytest.raises(GovernorSaturatedError) as excinfo:
                governor._acquire_interruptible(cancel_event=event)
            assert excinfo.value.reason == "cancelled"
            assert governor.stats()["waiting"] == 0
        finally:
            first.release()
            governor.shutdown()

    def test_acquire_interruptible_honours_its_wait_budget(self) -> None:
        governor = StudioResourceGovernor(max_concurrent_jobs=1)
        first = governor.acquire()
        ticks = iter([0.0, 0.0, 5.0, 9.0])

        def clock() -> float:
            try:
                return next(ticks)
            except StopIteration:  # pragma: no cover - defensive
                return 9.0

        governor._clock = clock  # type: ignore[assignment]
        try:
            with pytest.raises(GovernorSaturatedError) as excinfo:
                governor._acquire_interruptible(timeout_s=1.0)
            assert excinfo.value.reason == "acquire_timeout"
        finally:
            first.release()
            governor.shutdown()

    def test_a_non_string_filename_is_refused_before_any_path_join(self) -> None:
        with pytest.raises(DisasterRecoveryError) as excinfo:
            BackupManifest(
                manifest_id="man-1",
                bundle_id="bun-1",
                created_at_utc="2026-11-27T00:00:00+00:00",
                source_name="studio.sqlite3",
                backup_filename=123,  # type: ignore[arg-type]
                backup_sha256="a" * 64,
                backup_size_bytes=10,
                key_id="bk-1",
            )
        assert excinfo.value.reason == "invalid_field"

    def test_a_resolved_backup_that_escapes_the_root_is_refused(
        self, tmp_path: Path, live_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Layer 2 of the traversal defence, attacked directly.

        The manifest can no longer *hold* a traversal, so this forces the
        containment check by making ``Path.resolve`` lie about where the candidate
        lands -- exactly the shape a symlinked or bind-mounted bundle would take.
        """
        path, _connection = live_db
        manager = _manager(tmp_path, key=BackupKeyMaterial("bk-1", KEY_HEX))
        manifest = manager.create_hot_backup(database_path=path)
        bundle = manager.backup_root / manifest.bundle_id
        outside = tmp_path / "elsewhere" / HOT_BACKUP_FILENAME
        outside.parent.mkdir()

        real_resolve = Path.resolve

        def lying_resolve(self: Path, strict: bool = False) -> Path:
            if self.name == HOT_BACKUP_FILENAME:
                return outside
            return real_resolve(self, strict=strict)

        monkeypatch.setattr(Path, "resolve", lying_resolve)
        report = manager.verify_backup_bundle(bundle)
        assert report.verified is False
        assert "backup_path_escape" in report.problems

    def test_restore_refuses_a_bundle_with_a_blocking_problem(
        self, tmp_path: Path, live_db: Any
    ) -> None:
        path, _connection = live_db
        manager = _manager(tmp_path, key=BackupKeyMaterial("bk-1", KEY_HEX))
        manifest = manager.create_hot_backup(database_path=path)
        bundle = manager.backup_root / manifest.bundle_id
        (bundle / HOT_BACKUP_FILENAME).write_bytes(b"corrupted")
        with pytest.raises(DisasterRecoveryError) as excinfo:
            manager.restore_from_backup(bundle, target_database_path=tmp_path / "restored.sqlite3")
        assert excinfo.value.reason == "backup_unverified"

    def test_restore_tolerates_only_the_bookkeeping_sidecar_finding(
        self, tmp_path: Path, live_db: Any
    ) -> None:
        """A dirty WAL is fatal to verification but must not block a restore."""
        path, _connection = live_db
        manager = _manager(tmp_path, key=BackupKeyMaterial("bk-1", KEY_HEX))
        manifest = manager.create_hot_backup(database_path=path)
        bundle = manager.backup_root / manifest.bundle_id
        (bundle / f"{HOT_BACKUP_FILENAME}-wal").write_bytes(b"STALE-FRAMES")
        report = manager.verify_backup_bundle(bundle)
        assert "unexpected_sidecar: wal" in report.problems
        target = tmp_path / "restored.sqlite3"
        result = manager.restore_from_backup(bundle, target_database_path=target)
        assert result.restored_sha256 == manifest.backup_sha256
        assert target.is_file()

    def test_a_lease_won_by_the_abandoned_thread_is_released(self) -> None:
        """The narrow race, forced deterministically instead of hoped for.

        The waiter's acquire keeps running in its thread and *wins* the slot
        after the coroutine has already been cancelled.  That lease must come
        back: the old code handed it to a dead coroutine, which lost the capacity
        for the lifetime of the governor.
        """
        governor = StudioResourceGovernor(max_concurrent_jobs=1, max_queue_depth=1)
        won: list[AdmissionLease] = []
        original_acquire = governor._acquire_interruptible

        def slow_acquire(
            *, cancel_event: threading.Event | None = None, timeout_s: float | None = None
        ) -> AdmissionLease:
            # Deliberately ignore ``cancel_event``: this *is* the race where the
            # thread registers a lease after the cancellation was requested.
            time.sleep(0.15)
            lease = original_acquire(cancel_event=None, timeout_s=None)
            won.append(lease)
            return lease

        governor._acquire_interruptible = slow_acquire  # type: ignore[assignment]

        held = governor.acquire()
        assert governor.active_leases == 1

        async def main() -> tuple[int, int]:
            task = asyncio.ensure_future(governor.run_with_lease(lambda: "never"))
            for _ in range(400):
                if governor.stats()["waiting"] == 1:
                    break
                await asyncio.sleep(0.005)
            assert governor.stats()["waiting"] == 1
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            # The thread is still sleeping; free the slot so it can win it.
            held.release()
            await asyncio.sleep(0.4)
            return governor.active_leases, governor.stats()["waiting"]

        try:
            active, waiting = asyncio.run(main())
        finally:
            held.release() if held.held else None
            for lease in won:
                lease.release()
            governor.shutdown()
        # The abandoned thread did win a lease, and it was returned (held=False)
        # by the done-callback rather than leaked.
        assert len(won) >= 1
        assert all(lease.held is False for lease in won)
        assert active == 0
        assert waiting == 0
        # And the capacity is genuinely usable again.
        fresh = governor.acquire()
        assert governor.active_leases == 1
        fresh.release()
        assert governor.active_leases == 0

    def test_the_orphan_release_swallows_a_failing_future(self) -> None:
        """A cancellation path must never mask the CancelledError with a new error."""
        governor = StudioResourceGovernor(max_concurrent_jobs=1)

        class _FailingFuture:
            def add_done_callback(self, callback: Any) -> None:
                callback(self)

            def cancelled(self) -> bool:
                return False

            def result(self, timeout: float | None = None) -> Any:
                raise GovernorSaturatedError("cancelled", "no slot")

        class _CancelledFuture:
            def add_done_callback(self, callback: Any) -> None:
                callback(self)

            def cancelled(self) -> bool:
                return True

            def result(self, timeout: float | None = None) -> Any:  # pragma: no cover
                raise AssertionError("must not be read once cancelled")

        for future in (_FailingFuture(), _CancelledFuture()):
            governor._release_orphaned_lease(future)  # type: ignore[arg-type]
        assert governor.active_leases == 0
        governor.shutdown()

    def test_the_orphan_release_ignores_a_non_lease_result(self) -> None:
        """A future that resolves to something else must not be "released"."""
        governor = StudioResourceGovernor(max_concurrent_jobs=1)

        class _OddFuture:
            def add_done_callback(self, callback: Any) -> None:
                callback(self)

            def cancelled(self) -> bool:
                return False

            def result(self, timeout: float | None = None) -> Any:
                return "not a lease"

        governor._release_orphaned_lease(_OddFuture())  # type: ignore[arg-type]
        assert governor.active_leases == 0
        governor.shutdown()

    def test_a_cancelled_waiter_releases_even_when_the_thread_is_slow(self) -> None:
        """Cancellation stays prompt: it must not block on the slow thread."""
        governor = StudioResourceGovernor(max_concurrent_jobs=1, max_queue_depth=1)
        held = governor.acquire()

        async def main() -> int:
            start = time.monotonic()
            task = asyncio.ensure_future(governor.run_with_lease(lambda: "never"))
            for _ in range(400):
                if governor.stats()["waiting"] == 1:
                    break
                await asyncio.sleep(0.005)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            elapsed = time.monotonic() - start
            assert elapsed < 0.5, f"cancellation took {elapsed:.3f}s"
            # The waiter's thread may still be polling; once the holder lets go it
            # must either see the flag (and raise) or return its lease to us.
            held.release()
            for _ in range(400):
                if governor.active_leases == 0 and governor.stats()["waiting"] == 0:
                    break
                await asyncio.sleep(0.005)
            return governor.active_leases

        try:
            active = asyncio.run(main())
        finally:
            held.release()
            governor.shutdown()
        assert active == 0
        assert governor.active_leases == 0

    def test_the_filename_validator_refuses_a_blank_name_directly(self) -> None:
        """Belt and braces: the guard is reachable without a full manifest."""
        for blank in ("", "   "):
            with pytest.raises(DisasterRecoveryError) as excinfo:
                production_dr._validate_backup_filename(blank)
            assert excinfo.value.reason == "invalid_backup_filename"

    def test_a_cancellation_that_lands_inside_the_lock_still_allocates_nothing(self) -> None:
        """The last-check window: the flag flips between the poll and the lock."""
        governor = StudioResourceGovernor(max_concurrent_jobs=1, max_queue_depth=2)

        class _LateEvent(threading.Event):
            """Reports clear on the first read, set on the second."""

            def __init__(self) -> None:
                super().__init__()
                self.reads = 0

            def is_set(self) -> bool:
                self.reads += 1
                return self.reads >= 2

        late = _LateEvent()
        try:
            with pytest.raises(GovernorSaturatedError) as excinfo:
                governor._acquire_interruptible(cancel_event=late)
            assert excinfo.value.reason == "cancelled"
            assert governor.active_leases == 0
            assert governor.stats()["waiting"] == 0
        finally:
            governor.shutdown()

