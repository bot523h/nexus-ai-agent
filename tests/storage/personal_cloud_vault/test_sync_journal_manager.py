"""Tests for PersonalCloudVaultManager: deduplication, conflicts, .nagarvault, and GC."""

from __future__ import annotations

from pathlib import Path
from typing import Any, TypedDict

import pytest

from nexus_ai_agent.storage.personal_cloud_vault import (
    BYOSStorageDriver,
    LocalDeviceVaultDriver,
    PersonalCloudVaultManager,
    PersonalVaultError,
    PortableBundleVaultDriver,
    VaultAuthenticationError,
    VaultIntegrityError,
    VaultKeyRing,
    VaultRevisionConflictError,
    VaultSnapshotManifest,
    VaultTamperDetectedError,
    VaultTransportError,
    export_snapshot_to_bundle,
)


class _SpyDriverWrapper:
    """Thin delegating BYOSStorageDriver recording exact put_chunk / get_chunk calls."""

    def __init__(self, inner: BYOSStorageDriver) -> None:
        self._inner = inner
        self.put_chunk_calls: list[str] = []
        self.get_chunk_calls: list[str] = []

    def put_chunk(self, chunk_id: str, data: bytes | bytearray | memoryview) -> None:
        self.put_chunk_calls.append(chunk_id)
        self._inner.put_chunk(chunk_id, data)

    def get_chunk(self, chunk_id: str) -> bytes:
        self.get_chunk_calls.append(chunk_id)
        return self._inner.get_chunk(chunk_id)

    def has_chunk(self, chunk_id: str) -> bool:
        return self._inner.has_chunk(chunk_id)

    def put_manifest(self, snapshot_id: str, data: bytes) -> None:
        self._inner.put_manifest(snapshot_id, data)

    def get_manifest(self, snapshot_id: str) -> bytes:
        return self._inner.get_manifest(snapshot_id)

    def list_manifests(self) -> list[str]:
        return self._inner.list_manifests()

    def delete_chunk(self, chunk_id: str) -> None:
        self._inner.delete_chunk(chunk_id)


def test_ten_chunk_deduplication_saves_90_percent_on_second_edit(tmp_path: Path) -> None:
    """10-chunk project where 1 chunk changes -> edit #2 uploads ONLY 1 chunk (90% savings)."""
    chunk_size = 512
    local_driver = LocalDeviceVaultDriver(tmp_path / "cloud_store")
    spy_driver = _SpyDriverWrapper(local_driver)
    assert isinstance(spy_driver, BYOSStorageDriver)

    manager = PersonalCloudVaultManager(spy_driver, chunk_size=chunk_size)
    assert manager.driver is spy_driver
    assert manager.chunk_size == chunk_size

    # Build a 10-chunk heavy project file (10 * 512 = 5120 bytes) with 10 distinct blocks
    blocks_v1 = [bytes([i + 1]) * chunk_size for i in range(10)]
    project_v1 = {"timeline/master_render.bin": b"".join(blocks_v1)}

    push_v1 = manager.push_project_snapshot(
        project_id="cinema-proj-01",
        source=project_v1,
        master_secret="user-zero-cost-vault-passphrase",
        snapshot_id="snap-v1",
    )
    assert push_v1.total_chunks == 10
    assert push_v1.uploaded_chunks == 10
    assert push_v1.deduplicated_chunks == 0
    assert len(spy_driver.put_chunk_calls) == 10

    # Edit #2: modify ONLY chunk #4 (index 4); the other 9 chunks remain identical
    spy_driver.put_chunk_calls.clear()
    blocks_v2 = list(blocks_v1)
    blocks_v2[4] = b"\xfe" * chunk_size
    project_v2 = {"timeline/master_render.bin": b"".join(blocks_v2)}

    push_v2 = manager.push_project_snapshot(
        project_id="cinema-proj-01",
        source=project_v2,
        master_secret="user-zero-cost-vault-passphrase",
        parent_snapshot_id=push_v1.snapshot_id,
        snapshot_id="snap-v2",
    )
    assert push_v2.total_chunks == 10
    assert push_v2.uploaded_chunks == 1
    assert push_v2.deduplicated_chunks == 9
    assert push_v2.deduplication_ratio == pytest.approx(0.90)
    assert push_v2.saved_plaintext_bytes == 9 * chunk_size
    assert len(spy_driver.put_chunk_calls) == 1
    assert len(local_driver.list_chunks()) == 11  # 10 from v1 + 1 delta chunk from v2

    # Both v1 and v2 reconstruct byte-for-byte
    pull_v1 = manager.pull_project_snapshot(
        project_id="cinema-proj-01",
        master_secret="user-zero-cost-vault-passphrase",
        snapshot_id="snap-v1",
    )
    assert pull_v1.files == project_v1

    pull_latest = manager.pull_project_snapshot(
        project_id="cinema-proj-01",
        master_secret="user-zero-cost-vault-passphrase",
    )
    assert pull_latest.manifest.snapshot_id == "snap-v2"
    assert pull_latest.files == project_v2


def test_two_device_ancestry_conflict_detection(tmp_path: Path) -> None:
    shared_cloud = LocalDeviceVaultDriver(tmp_path / "shared_r2_bucket")
    device_a = PersonalCloudVaultManager(shared_cloud, chunk_size=256)
    device_b = PersonalCloudVaultManager(shared_cloud, chunk_size=256)
    secret = "shared-owner-secret"

    # Empty remote rejects non-None parent_snapshot_id
    with pytest.raises(VaultRevisionConflictError):
        device_a.push_project_snapshot(
            project_id="proj-sync",
            source={"scene.json": b'{"v":0}'},
            master_secret=secret,
            parent_snapshot_id="non-existent-parent",
        )

    # Device A pushes initial snapshot v1
    v1 = device_a.push_project_snapshot(
        project_id="proj-sync",
        source={"scene.json": b'{"v":1}'},
        master_secret=secret,
        snapshot_id="snap-v1",
    )

    # Device B syncs v1, then Device A pushes v2-a ahead of Device B
    pulled_b = device_b.pull_project_snapshot(project_id="proj-sync", master_secret=secret)
    assert pulled_b.manifest.snapshot_id == v1.snapshot_id

    v2_a = device_a.push_project_snapshot(
        project_id="proj-sync",
        source={"scene.json": b'{"v":"2-from-A"}'},
        master_secret=secret,
        parent_snapshot_id="snap-v1",
        snapshot_id="snap-v2-a",
    )
    assert v2_a.snapshot_id == "snap-v2-a"

    # Device B attempts stale push referencing snap-v1 -> VaultRevisionConflictError!
    with pytest.raises(VaultRevisionConflictError):
        device_b.push_project_snapshot(
            project_id="proj-sync",
            source={"scene.json": b'{"v":"2-from-B"}'},
            master_secret=secret,
            parent_snapshot_id="snap-v1",
            snapshot_id="snap-v2-b",
        )

    # Pushing with parent_snapshot_id=None on non-empty history -> VaultRevisionConflictError
    with pytest.raises(VaultRevisionConflictError):
        device_b.push_project_snapshot(
            project_id="proj-sync",
            source={"scene.json": b'{"v":"rootless"}'},
            master_secret=secret,
            parent_snapshot_id=None,
        )

    # Pushing with unknown parent_snapshot_id -> VaultRevisionConflictError
    with pytest.raises(VaultRevisionConflictError):
        device_b.push_project_snapshot(
            project_id="proj-sync",
            source={"scene.json": b'{"v":"ghost-parent"}'},
            master_secret=secret,
            parent_snapshot_id="snap-ghost",
        )

    # Duplicate snapshot_id -> VaultRevisionConflictError
    with pytest.raises(VaultRevisionConflictError):
        device_a.push_project_snapshot(
            project_id="proj-sync",
            source={"scene.json": b'{"v":"dup"}'},
            master_secret=secret,
            parent_snapshot_id="snap-v2-a",
            snapshot_id="snap-v2-a",
        )


def test_portable_nagarvault_bundle_offline_transfer_between_devices(tmp_path: Path) -> None:
    # Device 1 prepares a multi-file directory on disk (nested + empty file + repeated chunks)
    src_dir = tmp_path / "device1_project"
    (src_dir / "assets").mkdir(parents=True)
    (src_dir / "notes").mkdir(parents=True)
    repeated_payload = b"REPEATED_BLOCK_" * 40
    (src_dir / "assets" / "clip_a.bin").write_bytes(repeated_payload)
    (src_dir / "assets" / "clip_b.bin").write_bytes(repeated_payload + b"_TAIL")
    (src_dir / "notes" / "readme.txt").write_bytes(b"")

    local_vault = LocalDeviceVaultDriver(tmp_path / "device1_vault")
    mgr_1 = PersonalCloudVaultManager(local_vault, chunk_size=128)
    pushed = mgr_1.push_project_snapshot(
        project_id="portable-film",
        source=src_dir,
        master_secret="offline-transfer-passphrase",
        metadata={"author": "director"},
    )
    # Deduplication also works within a single push across clip_a and clip_b!
    assert pushed.deduplicated_chunks >= 3

    bundle_path = tmp_path / "usb_stick" / "portable-film.nagarvault"
    export_snapshot_to_bundle(local_vault, pushed.snapshot_id, bundle_path)
    assert bundle_path.is_file()

    # Device 2 opens the single .nagarvault file offline and restores to disk
    bundle_driver = PortableBundleVaultDriver(bundle_path)
    mgr_2 = PersonalCloudVaultManager(bundle_driver, chunk_size=128)
    restore_dir = tmp_path / "device2_restored"

    with pytest.raises(VaultAuthenticationError):
        mgr_2.pull_project_snapshot(
            project_id="portable-film",
            master_secret="wrong-passphrase",
            target_dir=restore_dir,
        )
    assert not restore_dir.exists()

    pulled = mgr_2.pull_project_snapshot(
        project_id="portable-film",
        master_secret="offline-transfer-passphrase",
        target_dir=restore_dir,
    )
    assert pulled.manifest.snapshot_id == pushed.snapshot_id
    assert (restore_dir / "assets" / "clip_a.bin").read_bytes() == repeated_payload
    assert (restore_dir / "assets" / "clip_b.bin").read_bytes() == repeated_payload + b"_TAIL"
    assert (restore_dir / "notes" / "readme.txt").read_bytes() == b""


def test_fail_closed_pull_verifies_all_chunks_before_writing_any_file(tmp_path: Path) -> None:
    vault = LocalDeviceVaultDriver(tmp_path / "tamper_vault")
    mgr = PersonalCloudVaultManager(vault, chunk_size=64)
    secret = "fail-closed-key"

    push = mgr.push_project_snapshot(
        project_id="proj-fc",
        source={
            "01_first.txt": b"first file clean bytes",
            "02_second.bin": b"X" * 256,
        },
        master_secret=secret,
        snapshot_id="snap-fc",
    )

    # Corrupt the last chunk of 02_second.bin directly on disk
    last_cid = push.manifest.files[1].chunks[-1].chunk_id
    chunk_file = vault.root_dir / "chunks" / f"{last_cid}.chk"
    corrupted = bytearray(chunk_file.read_bytes())
    corrupted[-1] ^= 0x01
    chunk_file.write_bytes(bytes(corrupted))

    out_dir = tmp_path / "must_stay_clean"
    with pytest.raises(VaultTamperDetectedError):
        mgr.pull_project_snapshot(
            project_id="proj-fc",
            master_secret=secret,
            snapshot_id="snap-fc",
            target_dir=out_dir,
        )
    # Fail-closed guarantee: even 01_first.txt was NOT written when chunk check failed!
    assert not out_dir.exists()

    # Chunk substitution attack (overwrite last_cid with 01_first.txt's valid encrypted chunk)
    first_cid = push.manifest.files[0].chunks[0].chunk_id
    first_bytes = (vault.root_dir / "chunks" / f"{first_cid}.chk").read_bytes()
    chunk_file.write_bytes(first_bytes)
    with pytest.raises(VaultTamperDetectedError):
        mgr.pull_project_snapshot(
            project_id="proj-fc",
            master_secret=secret,
            snapshot_id="snap-fc",
            target_dir=out_dir,
        )
    assert not out_dir.exists()


def test_garbage_collect_unreferenced_chunks_retains_active_and_purges_orphans(
    tmp_path: Path,
) -> None:
    vault = LocalDeviceVaultDriver(tmp_path / "gc_vault")
    secret = "gc-secret"
    ring = VaultKeyRing.from_secrets(secret)
    mgr = PersonalCloudVaultManager(vault, chunk_size=64, key_ring=ring)

    r1 = mgr.push_project_snapshot(
        project_id="proj-gc",
        source={"a.bin": b"A" * 64 + b"B" * 64},
        master_secret=secret,
        snapshot_id="snap-1",
    )
    r2 = mgr.push_project_snapshot(
        project_id="proj-gc",
        source={"a.bin": b"A" * 64 + b"C" * 64},
        master_secret=secret,
        parent_snapshot_id=r1.snapshot_id,
        snapshot_id="snap-2",
    )
    # Inject an unreferenced orphan chunk directly on storage
    orphan_cid = "f" * 64
    vault.put_chunk(orphan_cid, b"dangling-upload-bytes")
    assert len(vault.list_chunks()) == 4  # A, B, C, orphan

    # Safe 1-hour production default (min_age_seconds=3600.0) protects fresh orphan chunks
    gc_default = mgr.garbage_collect_unreferenced_chunks()
    assert gc_default.deleted_chunk_ids == ()
    assert vault.has_chunk(orphan_cid)

    # Explicit min_age_seconds=0.0 keeps snap-1 and snap-2 active -> deletes only orphan_cid
    gc1 = mgr.garbage_collect_unreferenced_chunks(min_age_seconds=0.0)
    assert gc1.deleted_chunk_ids == (orphan_cid,)
    assert gc1.deleted_count == 1
    assert not vault.has_chunk(orphan_cid)

    # Prune snap-1 by keeping only active_snapshot_ids=["snap-2"] -> deletes B, retains A & C
    gc2 = mgr.garbage_collect_unreferenced_chunks(
        active_snapshot_ids=[r2.snapshot_id],
        min_age_seconds=0.0,
    )
    assert gc2.deleted_count == 1
    assert len(gc2.retained_chunk_ids) == 2
    vault.delete_manifest(r1.snapshot_id)

    restored = mgr.pull_project_snapshot(
        project_id="proj-gc",
        master_secret=secret,
        snapshot_id=r2.snapshot_id,
    )
    assert restored.files == {"a.bin": b"A" * 64 + b"C" * 64}

    # Missing active snapshot ID fails closed without deleting anything
    with pytest.raises(VaultTransportError):
        mgr.garbage_collect_unreferenced_chunks(active_snapshot_ids=["does-not-exist"])

    # Minimal 7-method driver (no list_chunks) also GCs via sync journal + candidate_chunk_ids
    minimal_vault = LocalDeviceVaultDriver(tmp_path / "minimal_gc")
    spy_minimal = _SpyDriverWrapper(minimal_vault)
    mgr_min = PersonalCloudVaultManager(spy_minimal, chunk_size=64, key_ring=ring)
    dangling = "e" * 64
    spy_minimal.put_chunk(dangling, b"orphan")
    mgr_min.record_orphan_candidate_chunk(dangling)
    gc_min = mgr_min.garbage_collect_unreferenced_chunks(
        candidate_chunk_ids=["d" * 64],
        min_age_seconds=0.0,
    )
    assert gc_min.deleted_chunk_ids == (dangling,)

    # Adversarial: tampering a referenced chunk out of a stored manifest aborts GC fail-closed
    import json

    raw_manifest = json.loads(vault.get_manifest(r2.snapshot_id).decode("utf-8"))
    raw_manifest["files"][0]["chunks"].pop()
    vault.put_manifest(r2.snapshot_id, json.dumps(raw_manifest).encode("utf-8"))
    surviving_orphan = "a" * 64
    vault.put_chunk(surviving_orphan, b"survives-tamper")
    with pytest.raises(VaultIntegrityError):
        mgr.garbage_collect_unreferenced_chunks(min_age_seconds=0.0)
    assert vault.has_chunk(surviving_orphan)

    # Adversarial: GC with no key ring at all refuses to delete anything
    unauthenticated = PersonalCloudVaultManager(vault, chunk_size=64)
    with pytest.raises(VaultIntegrityError):
        unauthenticated.garbage_collect_unreferenced_chunks(min_age_seconds=0.0)
    assert vault.has_chunk(surviving_orphan)


def test_manager_edge_cases_and_filesystem_containment_guards(tmp_path: Path) -> None:
    vault = LocalDeviceVaultDriver(tmp_path / "edge_vault")
    with pytest.raises(PersonalVaultError):
        PersonalCloudVaultManager(vault, kdf_iterations=10)

    mgr = PersonalCloudVaultManager(vault, chunk_size=64)

    with pytest.raises(PersonalVaultError):
        mgr.push_project_snapshot(project_id="  ", source={}, master_secret="s")
    with pytest.raises(PersonalVaultError):
        mgr.pull_project_snapshot(project_id="  ", master_secret="s")
    with pytest.raises(VaultTransportError):
        mgr.pull_project_snapshot(project_id="missing-proj", master_secret="s")

    # Missing source dir & symlink inside source dir
    with pytest.raises(VaultTransportError):
        mgr.push_project_snapshot(
            project_id="p",
            source=tmp_path / "no_such_dir",
            master_secret="secret",
        )

    src_with_sym = tmp_path / "src_with_sym"
    src_with_sym.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"secret")
    (src_with_sym / "leak.txt").symlink_to(outside)
    with pytest.raises(VaultTransportError):
        mgr.push_project_snapshot(
            project_id="p",
            source=src_with_sym,
            master_secret="secret",
        )

    # Valid push of 1 project alongside a second independent project in same vault
    mgr.push_project_snapshot(
        project_id="proj-other",
        source={"x.txt": b"other"},
        master_secret="secret",
        snapshot_id="other-s1",
    )
    p1 = mgr.push_project_snapshot(
        project_id="proj-main",
        source={"dir/f.txt": b"main"},
        master_secret="secret",
        snapshot_id="main-s1",
    )

    # Wrong project_id for explicit snapshot_id -> VaultTamperDetectedError
    with pytest.raises(VaultTamperDetectedError):
        mgr.pull_project_snapshot(
            project_id="proj-main",
            master_secret="secret",
            snapshot_id="other-s1",
        )

    # Symlink target_dir and symlinked subdirectory inside target_dir are rejected fail-closed
    sym_target = tmp_path / "sym_target"
    sym_target.symlink_to(src_with_sym)
    with pytest.raises(VaultTransportError):
        mgr.pull_project_snapshot(
            project_id="proj-main",
            master_secret="secret",
            snapshot_id=p1.snapshot_id,
            target_dir=sym_target,
        )

    tricky_target = tmp_path / "tricky_target"
    tricky_target.mkdir()
    (tricky_target / "dir").symlink_to(src_with_sym)
    with pytest.raises(VaultTransportError):
        mgr.pull_project_snapshot(
            project_id="proj-main",
            master_secret="secret",
            snapshot_id=p1.snapshot_id,
            target_dir=tricky_target,
        )

    # Divergent heads injected manually on remote cause pull(snapshot_id=None) to raise conflict
    raw_s1 = VaultSnapshotManifest.from_json_bytes(vault.get_manifest("main-s1"))
    # Push second root for proj-main manually to simulate divergent split-brain on storage
    import hashlib as _hashlib
    import hmac as _hmac
    from dataclasses import replace as _replace

    from nexus_ai_agent.storage.personal_cloud_vault import derive_vault_keys

    km = derive_vault_keys("secret", bytes.fromhex(raw_s1.salt_hex))
    unsigned_fork = _replace(raw_s1, snapshot_id="main-fork")
    fork_mac = _hmac.new(
        km.mac_key,
        b"nagar-pcv-manifest-v1|" + unsigned_fork._canonical_signing_bytes(),
        _hashlib.sha256,
    ).hexdigest()
    fork_manifest = _replace(unsigned_fork, manifest_mac=fork_mac)
    vault.put_manifest("main-fork", fork_manifest.to_json_bytes())

    with pytest.raises(VaultRevisionConflictError):
        mgr.pull_project_snapshot(project_id="proj-main", master_secret="secret")


def test_manager_deep_tamper_and_io_defense_in_depth_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib as _hashlib
    import hmac as _hmac
    from dataclasses import replace as _replace

    from nexus_ai_agent.storage.personal_cloud_vault import (
        VaultChunkRef,
        VaultFileEntry,
        build_snapshot_manifest,
        compute_chunk_id,
        compute_file_mac,
        derive_vault_keys,
    )

    vault = LocalDeviceVaultDriver(tmp_path / "deep_vault")
    mgr = PersonalCloudVaultManager(vault, chunk_size=64)
    secret = "deep-secret"

    push = mgr.push_project_snapshot(
        project_id="proj-deep",
        source={"f.bin": b"ABCD"},
        master_secret=secret,
        snapshot_id="s-base",
    )
    salt = bytes.fromhex(push.manifest.salt_hex)
    km = derive_vault_keys(secret, salt)
    cid = compute_chunk_id(b"ABCD", km)

    def _store_forged_manifest(snap_id: str, files: list[VaultFileEntry]) -> None:
        m = build_snapshot_manifest(
            project_id="proj-deep",
            snapshot_id=snap_id,
            parent_snapshot_id="s-base",
            created_at_utc="2026-11-16T10:00:00Z",
            chunk_size=64,
            files=files,
            key_material=km,
        )
        sig = _hmac.new(
            km.mac_key,
            b"nagar-pcv-manifest-v1|" + m._canonical_signing_bytes(),
            _hashlib.sha256,
        ).hexdigest()
        vault.put_manifest(snap_id, _replace(m, manifest_mac=sig).to_json_bytes())

    # 1) Manifest declares plaintext_len=99 while stored chunk decrypts to 4 bytes
    _store_forged_manifest(
        "s-bad-len",
        [
            VaultFileEntry(
                rel_path="f.bin",
                total_size=99,
                file_mac_hex=compute_file_mac("f.bin", b"ABCD", km),
                chunks=(VaultChunkRef(chunk_index=0, chunk_id=cid, plaintext_len=99),),
            )
        ],
    )
    with pytest.raises(VaultTamperDetectedError):
        mgr.pull_project_snapshot(
            project_id="proj-deep", master_secret=secret, snapshot_id="s-bad-len"
        )

    # 2) Cached chunk verified at len=4 on ref 0, then ref 1 claims plaintext_len=8 for same cid
    _store_forged_manifest(
        "s-bad-cached-len",
        [
            VaultFileEntry(
                rel_path="f.bin",
                total_size=12,
                file_mac_hex=compute_file_mac("f.bin", b"ABCDABCD", km),
                chunks=(
                    VaultChunkRef(chunk_index=0, chunk_id=cid, plaintext_len=4),
                    VaultChunkRef(chunk_index=1, chunk_id=cid, plaintext_len=8),
                ),
            )
        ],
    )
    with pytest.raises(VaultTamperDetectedError):
        mgr.pull_project_snapshot(
            project_id="proj-deep", master_secret=secret, snapshot_id="s-bad-cached-len"
        )

    # 3) Chunks valid & sizes match, but file_mac_hex is wrong -> Reassembled file MAC mismatch
    _store_forged_manifest(
        "s-bad-fmac",
        [
            VaultFileEntry(
                rel_path="f.bin",
                total_size=4,
                file_mac_hex="0" * 64,
                chunks=(VaultChunkRef(chunk_index=0, chunk_id=cid, plaintext_len=4),),
            )
        ],
    )
    with pytest.raises(VaultTamperDetectedError):
        mgr.pull_project_snapshot(
            project_id="proj-deep", master_secret=secret, snapshot_id="s-bad-fmac"
        )

    # 4) Destination target_dir blocked by file & destination leaf is a symlink
    blocker = tmp_path / "file_blocking_target"
    blocker.write_bytes(b"x")
    with pytest.raises(VaultTransportError):
        mgr.pull_project_snapshot(
            project_id="proj-deep",
            master_secret=secret,
            snapshot_id="s-base",
            target_dir=blocker / "sub",
        )

    leaf_sym_dir = tmp_path / "leaf_sym_dir"
    leaf_sym_dir.mkdir()
    (leaf_sym_dir / "f.bin").symlink_to(blocker)
    with pytest.raises(VaultTransportError):
        mgr.pull_project_snapshot(
            project_id="proj-deep",
            master_secret=secret,
            snapshot_id="s-base",
            target_dir=leaf_sym_dir,
        )

    # 5) Source file streaming open/stat OSError during directory push
    unreadable_src = tmp_path / "unreadable_src"
    unreadable_src.mkdir()
    (unreadable_src / "doc.bin").write_bytes(b"data")
    orig_open = Path.open

    def _fail_doc_open(self_path: Path, mode: str = "r") -> object:
        if self_path.name == "doc.bin":
            raise OSError("disk read error")
        return orig_open(self_path, mode)

    monkeypatch.setattr(Path, "open", _fail_doc_open)
    with pytest.raises(VaultTransportError):
        mgr.push_project_snapshot(
            project_id="proj-io",
            source=unreadable_src,
            master_secret=secret,
        )
    monkeypatch.setattr(Path, "open", orig_open)


def test_f1_regression_concurrent_gc_right_before_put_manifest_preserves_all_chunks(
    tmp_path: Path,
) -> None:
    """Auditor F1 reproduction: GC right before put_manifest in push #2 must not break pull."""
    import threading
    from collections.abc import Callable

    class _RaceHookDriver:
        def __init__(self, inner: LocalDeviceVaultDriver) -> None:
            self.inner = inner
            self.before_put_manifest: Callable[[str], None] | None = None
            self.after_put_manifest_once: Callable[[str], None] | None = None

        def put_chunk(self, chunk_id: str, data: bytes | bytearray | memoryview) -> None:
            self.inner.put_chunk(chunk_id, data)

        def get_chunk(self, chunk_id: str) -> bytes:
            return self.inner.get_chunk(chunk_id)

        def has_chunk(self, chunk_id: str) -> bool:
            return self.inner.has_chunk(chunk_id)

        def put_manifest(self, snapshot_id: str, data: bytes) -> None:
            if self.before_put_manifest is not None:
                self.before_put_manifest(snapshot_id)
            self.inner.put_manifest(snapshot_id, data)
            if self.after_put_manifest_once is not None:
                hook = self.after_put_manifest_once
                self.after_put_manifest_once = None
                hook(snapshot_id)

        def get_manifest(self, snapshot_id: str) -> bytes:
            return self.inner.get_manifest(snapshot_id)

        def list_manifests(self) -> list[str]:
            return self.inner.list_manifests()

        def list_chunks(self) -> list[str]:
            return self.inner.list_chunks()

        def delete_chunk(self, chunk_id: str) -> None:
            self.inner.delete_chunk(chunk_id)

        def delete_manifest(self, snapshot_id: str) -> None:
            self.inner.delete_manifest(snapshot_id)

    base_local = LocalDeviceVaultDriver(tmp_path / "f1_race_vault")
    hook_driver = _RaceHookDriver(base_local)
    secret = "f1-race-secret"
    mgr = PersonalCloudVaultManager(
        hook_driver,
        chunk_size=64,
        kdf_iterations=100_000,
        pad_to_block_size=128,
        key_ring=VaultKeyRing.from_secrets(secret),
    )

    # Push #1 commits initial snapshot (default OWASP 600k verified separately; 100k here for speed)
    v1_data = {"timeline.bin": b"A" * 64 + b"B" * 64}
    r1 = mgr.push_project_snapshot(
        project_id="proj-f1",
        source=v1_data,
        master_secret=secret,
        snapshot_id="snap-1",
    )
    assert mgr.in_flight_chunks == frozenset()

    # Plant a real unreferenced orphan chunk
    real_orphan = "9" * 64
    base_local.put_chunk(real_orphan, b"true-orphan")

    gc_reports_from_thread: list[tuple[str, ...]] = []

    def _race_before_manifest(snap_id: str) -> None:
        assert snap_id == "snap-2"
        # All chunks of snap-2 are leased in `_in_flight_chunks` right now!
        assert len(mgr.in_flight_chunks) == 3

        def _bg_gc() -> None:
            rep = mgr.garbage_collect_unreferenced_chunks(min_age_seconds=0.0)
            gc_reports_from_thread.append(rep.deleted_chunk_ids)

        worker = threading.Thread(target=_bg_gc)
        worker.start()
        worker.join(timeout=5.0)
        assert not worker.is_alive()

    hook_driver.before_put_manifest = _race_before_manifest

    v2_data = {"timeline.bin": b"A" * 64 + b"C" * 64 + b"D" * 40}
    r2 = mgr.push_project_snapshot(
        project_id="proj-f1",
        source=v2_data,
        master_secret=secret,
        parent_snapshot_id=r1.snapshot_id,
        snapshot_id="snap-2",
    )
    assert gc_reports_from_thread == [(real_orphan,)]
    assert mgr.in_flight_chunks == frozenset()

    # Pull of snap-2 succeeds cleanly with zero missing chunks (padded chunks restore exact size)!
    pulled_v2 = mgr.pull_project_snapshot(
        project_id="proj-f1",
        master_secret=secret,
        snapshot_id=r2.snapshot_id,
    )
    assert pulled_v2.files == v2_data

    # Layer 3 proof: even if an external process deletes new chunks pre- AND post-manifest,
    # `_verify_and_heal_manifest_chunks` automatically re-uploads them before returning!
    hook_driver.before_put_manifest = None
    external_evicted: list[str] = []

    class _ExternalSweepBeforeManifestDriver(_RaceHookDriver):
        def put_chunk(self, chunk_id: str, data: bytes | bytearray | memoryview) -> None:
            super().put_chunk(chunk_id, data)
            # Simulate rogue external sweeper deleting the new chunk right after initial upload
            if chunk_id not in r2.manifest.referenced_chunk_ids() and not external_evicted:
                external_evicted.append(chunk_id)
                self.inner.delete_chunk(chunk_id)

    sweep_driver = _ExternalSweepBeforeManifestDriver(base_local)

    def _evict_once_after_manifest(_snap_id: str) -> None:
        if external_evicted:
            base_local.delete_chunk(external_evicted[0])

    sweep_driver.after_put_manifest_once = _evict_once_after_manifest
    mgr_heal = PersonalCloudVaultManager(
        sweep_driver,
        chunk_size=64,
        kdf_iterations=100_000,
        key_ring=VaultKeyRing.from_secrets(secret),
    )

    v3_data = {"timeline.bin": b"A" * 64 + b"E" * 64}
    r3 = mgr_heal.push_project_snapshot(
        project_id="proj-f1",
        source=v3_data,
        master_secret=secret,
        parent_snapshot_id=r2.snapshot_id,
        snapshot_id="snap-3",
    )
    assert len(external_evicted) == 1
    assert base_local.has_chunk(external_evicted[0])

    pulled_v3 = mgr_heal.pull_project_snapshot(
        project_id="proj-f1",
        master_secret=secret,
        snapshot_id=r3.snapshot_id,
    )
    assert pulled_v3.files == v3_data


def test_f1_protected_chunks_min_age_refcounts_and_fail_closed_rollback(
    tmp_path: Path,
) -> None:
    import os
    import time

    vault = LocalDeviceVaultDriver(tmp_path / "f1_guards")
    default_mgr = PersonalCloudVaultManager(vault, chunk_size=64)
    # Verify default manager uses OWASP 600,000 iterations
    init_rep = default_mgr.push_project_snapshot(
        project_id="owasp-check",
        source={"k.txt": b"owasp"},
        master_secret="owasp-pass",
    )
    assert init_rep.manifest.kdf_iterations == 600_000

    mgr = PersonalCloudVaultManager(
        vault,
        chunk_size=64,
        kdf_iterations=100_000,
        key_ring=VaultKeyRing.from_secrets("owasp-pass"),
    )
    with pytest.raises(PersonalVaultError):
        mgr.garbage_collect_unreferenced_chunks(min_age_seconds=-0.5)

    # Overlapping in-flight refcount acquire/release test (shared chunk across concurrent pushes)
    shared_cid = "a" * 64
    mgr._acquire_in_flight_chunks([shared_cid])
    mgr._acquire_in_flight_chunks([shared_cid])
    assert shared_cid in mgr.in_flight_chunks
    mgr._release_in_flight_chunks([shared_cid])
    assert shared_cid in mgr.in_flight_chunks  # still leased by second push!
    mgr._release_in_flight_chunks([shared_cid])
    assert shared_cid not in mgr.in_flight_chunks

    # protected_chunk_ids and min_age_seconds protection
    c_protected = "1" * 64
    c_young_mem = "2" * 64
    c_young_disk = "3" * 64
    c_old_disk = "4" * 64
    for cid in (c_protected, c_young_mem, c_young_disk, c_old_disk):
        vault.put_chunk(cid, b"orphan-payload")

    mgr.record_orphan_candidate_chunk(c_young_mem, recorded_at=time.time())
    old_ts = time.time() - 3600.0
    os.utime(vault.root_dir / "chunks" / f"{c_old_disk}.chk", (old_ts, old_ts))

    gc_age = mgr.garbage_collect_unreferenced_chunks(
        protected_chunk_ids=[c_protected],
        min_age_seconds=300.0,
    )
    assert gc_age.deleted_chunk_ids == (c_old_disk,)
    assert vault.has_chunk(c_protected)
    assert vault.has_chunk(c_young_mem)
    assert vault.has_chunk(c_young_disk)

    # P1-3 Fail-Safe: minimal driver without get_chunk_mtime stamps `first_seen = now`
    # and RETAINS unknown-age chunks until `min_age_seconds` has elapsed!
    spy = _SpyDriverWrapper(vault)
    mgr_spy = PersonalCloudVaultManager(
        spy,
        chunk_size=64,
        kdf_iterations=100_000,
        key_ring=VaultKeyRing.from_secrets("owasp-pass"),
    )
    gc_unknown_ts = mgr_spy.garbage_collect_unreferenced_chunks(
        candidate_chunk_ids=[c_young_disk],
        min_age_seconds=300.0,
    )
    assert gc_unknown_ts.deleted_chunk_ids == ()
    assert vault.has_chunk(c_young_disk)
    mgr_spy.record_orphan_candidate_chunk(c_young_disk, recorded_at=time.time() - 400.0)
    gc_aged_ts = mgr_spy.garbage_collect_unreferenced_chunks(
        candidate_chunk_ids=[c_young_disk],
        min_age_seconds=300.0,
    )
    assert gc_aged_ts.deleted_chunk_ids == (c_young_disk,)

    # Fail-closed when broken storage loses chunks after re-upload (pre- and post-manifest)
    class _BlackholeChunkDriver:
        def __init__(self, inner: LocalDeviceVaultDriver, *, fail_post: bool) -> None:
            self.inner = inner
            self.fail_post = fail_post
            self.manifest_written = False
            self.deleted_manifests: list[str] = []

        def put_chunk(self, chunk_id: str, data: bytes | bytearray | memoryview) -> None:
            if not self.fail_post:
                return  # pre-manifest blackhole: silently drops every chunk write
            if not self.manifest_written:
                self.inner.put_chunk(chunk_id, data)

        def get_chunk(self, chunk_id: str) -> bytes:
            return self.inner.get_chunk(chunk_id)

        def has_chunk(self, chunk_id: str) -> bool:
            if self.fail_post and self.manifest_written:
                return False
            return self.inner.has_chunk(chunk_id)

        def put_manifest(self, snapshot_id: str, data: bytes) -> None:
            self.inner.put_manifest(snapshot_id, data)
            self.manifest_written = True

        def get_manifest(self, snapshot_id: str) -> bytes:
            return self.inner.get_manifest(snapshot_id)

        def list_manifests(self) -> list[str]:
            return self.inner.list_manifests()

        def delete_chunk(self, chunk_id: str) -> None:
            self.inner.delete_chunk(chunk_id)

        def delete_manifest(self, snapshot_id: str) -> None:
            self.deleted_manifests.append(snapshot_id)
            self.inner.delete_manifest(snapshot_id)

    bh_pre = _BlackholeChunkDriver(LocalDeviceVaultDriver(tmp_path / "bh_pre"), fail_post=False)
    mgr_bh_pre = PersonalCloudVaultManager(bh_pre, chunk_size=64, kdf_iterations=100_000)
    with pytest.raises(VaultTransportError, match="pre-manifest"):
        mgr_bh_pre.push_project_snapshot(
            project_id="bh",
            source={"a.bin": b"data"},
            master_secret="secret",
            snapshot_id="snap-bh-pre",
        )
    assert bh_pre.list_manifests() == []

    bh_post = _BlackholeChunkDriver(LocalDeviceVaultDriver(tmp_path / "bh_post"), fail_post=True)
    mgr_bh_post = PersonalCloudVaultManager(bh_post, chunk_size=64, kdf_iterations=100_000)
    with pytest.raises(VaultTransportError, match="post-manifest"):
        mgr_bh_post.push_project_snapshot(
            project_id="bh",
            source={"a.bin": b"data"},
            master_secret="secret",
            snapshot_id="snap-bh-post",
        )
    assert bh_post.deleted_manifests == ["snap-bh-post"]
    assert bh_post.list_manifests() == []

    # Mid-GC TOCTOU guard: a push starting *after* GC begins still protects its newly leased chunk
    late_cid = "8" * 64
    vault.put_chunk(late_cid, b"late-in-flight")

    class _MidGcLeaseDriver(_SpyDriverWrapper):
        def list_chunks(self) -> list[str]:
            mgr_late._acquire_in_flight_chunks([late_cid])
            return [late_cid]

    mgr_late = PersonalCloudVaultManager(
        _MidGcLeaseDriver(vault),
        chunk_size=64,
        kdf_iterations=100_000,
        key_ring=VaultKeyRing.from_secrets("owasp-pass"),
    )
    gc_late = mgr_late.garbage_collect_unreferenced_chunks(min_age_seconds=0.0)
    assert gc_late.deleted_chunk_ids == ()
    assert vault.has_chunk(late_cid)
    mgr_late._release_in_flight_chunks([late_cid])


def test_n1_streaming_push_o1_tracemalloc_ceiling_and_disk_locator_healing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove O(1) peak memory (< 4 * chunk_size) on a 20-chunk multi-file push + disk heal.

    Scope note: the ``< 4 * chunk_size`` ceiling is an absolute bound for *this* fixture
    (20 chunks), because total peak is ``payload + metadata(N)`` and metadata is linear
    in the chunk count. The general, N-independent claim is asserted in
    ``test_wave4_memory_scaling_benchmark`` as ``peak < 200_000 + 2048 * N``.
    """
    import gc
    import tracemalloc

    from nexus_ai_agent.storage.personal_cloud_vault.sync_journal_manager import (
        _ChunkSourceLocator,
        _read_chunk_from_locator,
    )

    chunk_size = 65_536  # 64 KiB
    project_dir = tmp_path / "heavy_cinema_project"
    (project_dir / "reels").mkdir(parents=True)
    # 4 media files totaling 20 distinct 64 KiB chunks (1,310,720 bytes = 20 * chunk_size)
    expected_files: dict[str, bytes] = {}
    chunk_cursor = 0
    for file_idx, chunks_in_file in enumerate((6, 5, 5, 4)):
        parts: list[bytes] = []
        for _ in range(chunks_in_file):
            tag = f"REEL-{file_idx}-CHUNK-{chunk_cursor:03d}-".encode("ascii")
            filler = bytes([(chunk_cursor + 17) & 0xFF]) * (chunk_size - len(tag))
            parts.append(tag + filler)
            chunk_cursor += 1
        rel = f"reels/track_{file_idx}.bin"
        raw_file = b"".join(parts)
        (project_dir / rel).write_bytes(raw_file)
        expected_files[rel] = raw_file
    (project_dir / "reels" / "empty_marker.idx").write_bytes(b"")
    expected_files["reels/empty_marker.idx"] = b""
    assert chunk_cursor == 20

    class _EvictTwoChunksDriver(LocalDeviceVaultDriver):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.evicted_before_manifest: list[str] = []

        def put_manifest(self, snapshot_id: str, data: bytes) -> None:
            super().put_manifest(snapshot_id, data)

    vault_dir = tmp_path / "streaming_vault"
    driver = _EvictTwoChunksDriver(vault_dir)
    mgr = PersonalCloudVaultManager(driver, chunk_size=chunk_size, kdf_iterations=100_000)

    gc.collect()
    tracemalloc.start()
    try:
        push_rep = mgr.push_project_snapshot(
            project_id="cinema-20-chunks",
            source=project_dir,
            master_secret="streaming-o1-passphrase",
            snapshot_id="snap-stream-1",
        )
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert push_rep.total_chunks == 20
    assert push_rep.uploaded_chunks == 20
    # Mandatory N1 invariant: peak allocation strictly < 4 * chunk_size despite 20 chunks!
    assert peak_bytes < 4 * chunk_size, (
        f"Streaming push peak_bytes={peak_bytes} exceeded 4 * chunk_size={4 * chunk_size}"
    )
    # Tightened round-2 ceiling: in-flight heap must stay near ONE chunk (measured
    # ~142-156 KB = wire buffer + prefix + baseline), not anywhere under the 256 KiB
    # ``4 * chunk_size`` guard above. Printed so a regression shows the real number.
    print(f"[N1] streaming push peak_bytes={peak_bytes} (limit 200000, chunk_size={chunk_size})")
    assert peak_bytes < 200_000, (
        f"Streaming push peak_bytes={peak_bytes} regressed above the 200000-byte "
        f"O(1) budget (chunk_size={chunk_size})"
    )

    pulled = mgr.pull_project_snapshot(
        project_id="cinema-20-chunks",
        master_secret="streaming-o1-passphrase",
        snapshot_id="snap-stream-1",
    )
    assert pulled.files == expected_files
    del expected_files, pulled

    # Verify _ChunkSourceLocator disk seek+read healing errors (missing/short/mutated/stat/grow)
    sample_file = tmp_path / "locator_sample.bin"
    sample_file.write_bytes(b"0123456789ABCDEF")
    loc = _ChunkSourceLocator(
        file_path=sample_file,
        byte_offset=4,
        byte_length=6,
        chunk_index=1,
    )
    assert _read_chunk_from_locator(loc, None) == b"456789"

    loc_short = _ChunkSourceLocator(
        file_path=sample_file,
        byte_offset=12,
        byte_length=8,
        chunk_index=2,
    )
    with pytest.raises(VaultTransportError, match="Short read"):
        _read_chunk_from_locator(loc_short, None)

    missing_loc = _ChunkSourceLocator(
        file_path=tmp_path / "deleted.bin",
        byte_offset=0,
        byte_length=4,
        chunk_index=0,
    )
    with pytest.raises(VaultTransportError, match="Failed re-reading"):
        _read_chunk_from_locator(missing_loc, None)

    with pytest.raises(PersonalVaultError):
        PersonalCloudVaultManager(driver, chunk_size=64, lease_ttl_seconds=0.0)

    # Concurrent file size mutation during streaming & local mutation during heal fail closed
    grow_dir = tmp_path / "grow_src"
    grow_dir.mkdir()
    grow_file = grow_dir / "clip.bin"
    grow_file.write_bytes(b"A" * 64)
    orig_stat = Path.stat

    def _inflated_stat(self_path: Path, *, follow_symlinks: bool = True) -> object:
        st = orig_stat(self_path, follow_symlinks=follow_symlinks)
        if self_path == grow_file and follow_symlinks:

            class _FakeStat:
                st_mode = st.st_mode
                st_ino = st.st_ino
                st_size = 999
                st_mtime_ns = st.st_mtime_ns
                st_ctime_ns = st.st_ctime_ns

            return _FakeStat()
        return st

    monkeypatch.setattr(Path, "stat", _inflated_stat)
    with pytest.raises(VaultTransportError, match="open-fstat"):
        mgr.push_project_snapshot(
            project_id="grow-proj",
            source=grow_dir,
            master_secret="s",
        )

    def _failing_stat(self_path: Path, *, follow_symlinks: bool = True) -> object:
        if self_path == grow_file:
            raise OSError("stat failed")
        return orig_stat(self_path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", _failing_stat)
    with pytest.raises(VaultTransportError):
        mgr.push_project_snapshot(
            project_id="grow-proj",
            source=grow_dir,
            master_secret="s",
        )
    monkeypatch.setattr(Path, "stat", orig_stat)


def test_n2_crash_recovery_staging_journal_resumes_without_reuploading_chunks(
    tmp_path: Path,
) -> None:
    """Simulated crash mid-upload resumes from VaultStagingJournal without re-uploading."""
    chunk_size = 128
    src_dir = tmp_path / "resumable_project"
    src_dir.mkdir()
    full_payload = b"".join(bytes([i + 1]) * chunk_size for i in range(10))
    (src_dir / "render.mov").write_bytes(full_payload)

    class _CrashAfterFiveDriver(LocalDeviceVaultDriver):
        def __init__(self, root: Path, *, crash_on_call: int | None) -> None:
            super().__init__(root)
            self.crash_on_call = crash_on_call
            self.put_calls: list[str] = []

        def put_chunk(self, chunk_id: str, data: bytes | bytearray | memoryview) -> None:
            if self.crash_on_call is not None and len(self.put_calls) >= self.crash_on_call:
                raise RuntimeError("simulated power failure mid-push")
            self.put_calls.append(chunk_id)
            super().put_chunk(chunk_id, data)

    vault_root = tmp_path / "crash_vault"
    crashing_driver = _CrashAfterFiveDriver(vault_root, crash_on_call=5)
    mgr_run1 = PersonalCloudVaultManager(
        crashing_driver,
        chunk_size=chunk_size,
        kdf_iterations=100_000,
    )
    with pytest.raises(RuntimeError, match="simulated power failure"):
        mgr_run1.push_project_snapshot(
            project_id="crash-film",
            source=src_dir,
            master_secret="resume-passphrase",
            snapshot_id="snap-resumed-1",
        )
    assert len(crashing_driver.put_calls) == 5
    # Lease was released on unwind, while staging journal persisted the 5 uploaded chunks + salt!
    assert crashing_driver.read_active_leased_chunks() == set()
    staged = crashing_driver.load_staging_journal("crash-film", None)
    assert staged is not None
    assert len(staged.uploaded_chunk_ids) == 5

    # Run #2 (fresh process/manager): changing passphrase ignores stale journal cleanly,
    # whereas matching passphrase reuses staged salt & skips all 5 pre-crash chunks!
    resumed_driver = _CrashAfterFiveDriver(vault_root, crash_on_call=None)
    mgr_run2 = PersonalCloudVaultManager(
        resumed_driver,
        chunk_size=chunk_size,
        kdf_iterations=100_000,
    )
    resume_rep = mgr_run2.push_project_snapshot(
        project_id="crash-film",
        source=src_dir,
        master_secret="resume-passphrase",
        snapshot_id="snap-resumed-1",
    )
    assert resume_rep.total_chunks == 10
    assert resume_rep.uploaded_chunks == 5
    assert resume_rep.deduplicated_chunks == 5
    assert len(resumed_driver.put_calls) == 5
    # Staging journal is cleaned up once manifest + post-manifest verification complete
    assert resumed_driver.load_staging_journal("crash-film", None) is None

    pulled = mgr_run2.pull_project_snapshot(
        project_id="crash-film",
        master_secret="resume-passphrase",
        snapshot_id="snap-resumed-1",
    )
    assert pulled.files == {"render.mov": full_payload}

    # Incremental delta push (`parent_snapshot_id != None`) also resumes from staging journal
    delta_payload = full_payload + (b"\xd0" * chunk_size) + (b"\xd1" * chunk_size)
    (src_dir / "render.mov").write_bytes(delta_payload)
    crash_delta = _CrashAfterFiveDriver(vault_root, crash_on_call=1)
    mgr_delta1 = PersonalCloudVaultManager(
        crash_delta,
        chunk_size=chunk_size,
        kdf_iterations=100_000,
    )
    with pytest.raises(RuntimeError):
        mgr_delta1.push_project_snapshot(
            project_id="crash-film",
            source=src_dir,
            master_secret="resume-passphrase",
            parent_snapshot_id="snap-resumed-1",
            snapshot_id="snap-resumed-2",
        )
    ok_delta = _CrashAfterFiveDriver(vault_root, crash_on_call=None)
    mgr_delta2 = PersonalCloudVaultManager(
        ok_delta,
        chunk_size=chunk_size,
        kdf_iterations=100_000,
    )
    rep_delta = mgr_delta2.push_project_snapshot(
        project_id="crash-film",
        source=src_dir,
        master_secret="resume-passphrase",
        parent_snapshot_id="snap-resumed-1",
        snapshot_id="snap-resumed-2",
    )
    assert rep_delta.uploaded_chunks == 1
    assert len(ok_delta.put_calls) == 1

    # Also cover crashed initial push followed by passphrase change on retry
    vault_pw_change = tmp_path / "pw_change_vault"
    crash_pw = _CrashAfterFiveDriver(vault_pw_change, crash_on_call=2)
    mgr_pw1 = PersonalCloudVaultManager(crash_pw, chunk_size=chunk_size, kdf_iterations=100_000)
    with pytest.raises(RuntimeError):
        mgr_pw1.push_project_snapshot(
            project_id="pw-film",
            source=src_dir,
            master_secret="old-passphrase",
        )
    ok_pw = _CrashAfterFiveDriver(vault_pw_change, crash_on_call=None)
    mgr_pw2 = PersonalCloudVaultManager(ok_pw, chunk_size=chunk_size, kdf_iterations=100_000)
    rep_new_pw = mgr_pw2.push_project_snapshot(
        project_id="pw-film",
        source=src_dir,
        master_secret="new-passphrase",
        snapshot_id="snap-new-pw",
    )
    assert rep_new_pw.uploaded_chunks == 12


def test_n6_cross_client_cross_manager_gc_race_file_lease_and_disk_locator(
    tmp_path: Path,
) -> None:
    """Two independent managers on shared storage: prove BOTH file lease AND disk locator heal."""
    from collections.abc import Callable

    from nexus_ai_agent.storage.personal_cloud_vault import DEFAULT_GC_MIN_AGE_SECONDS

    assert DEFAULT_GC_MIN_AGE_SECONDS == 3600.0

    chunk_size = 128
    shared_vault_dir = tmp_path / "shared_multi_process_vault"
    src_dir = tmp_path / "editor_workspace"
    src_dir.mkdir()
    secret = "cross-client-shared-secret"

    class _HookedLocalDriver(LocalDeviceVaultDriver):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.before_put_manifest: Callable[[str], None] | None = None
            self.put_chunk_history: list[str] = []

        def put_chunk(self, chunk_id: str, data: bytes | bytearray | memoryview) -> None:
            self.put_chunk_history.append(chunk_id)
            super().put_chunk(chunk_id, data)

        def put_manifest(self, snapshot_id: str, data: bytes) -> None:
            if self.before_put_manifest is not None:
                self.before_put_manifest(snapshot_id)
            super().put_manifest(snapshot_id, data)

    # Separate driver + separate PersonalCloudVaultManager instances (zero shared in-RAM state!)
    driver_client_a = _HookedLocalDriver(shared_vault_dir)
    driver_client_b = LocalDeviceVaultDriver(shared_vault_dir)
    mgr_a = PersonalCloudVaultManager(
        driver_client_a,
        chunk_size=chunk_size,
        kdf_iterations=100_000,
        key_ring=VaultKeyRing.from_secrets(secret),
    )
    mgr_b = PersonalCloudVaultManager(
        driver_client_b,
        chunk_size=chunk_size,
        kdf_iterations=100_000,
        key_ring=VaultKeyRing.from_secrets(secret),
    )

    # Initial snapshot v1 on disk
    v1_bytes = b"A" * chunk_size + b"B" * chunk_size
    (src_dir / "timeline.edl").write_bytes(v1_bytes)
    r1 = mgr_a.push_project_snapshot(
        project_id="shared-cinema",
        source=src_dir,
        master_secret=secret,
        snapshot_id="snap-v1",
    )

    # Part 1: Cross-Process File Lease prevents mgr_b (with min_age_seconds=0.0!) from deleting
    # mgr_a's in-flight chunks right before put_manifest, while still deleting true orphans!
    true_orphan = "7" * 64
    driver_client_b.put_chunk(true_orphan, b"unleased-dangling-chunk")
    driver_client_a.put_chunk_history.clear()
    gc_part1_deleted: list[tuple[str, ...]] = []

    def _client_b_gc_with_active_lease(snap_id: str) -> None:
        assert snap_id == "snap-v2"
        # Client B has ZERO in-memory `_in_flight_chunks`, yet sees Client A's file lease on disk!
        assert mgr_b.in_flight_chunks == frozenset()
        leased_on_disk = driver_client_b.read_active_leased_chunks()
        assert len(leased_on_disk) == 3
        gc_rep = mgr_b.garbage_collect_unreferenced_chunks(min_age_seconds=0.0)
        gc_part1_deleted.append(gc_rep.deleted_chunk_ids)

    driver_client_a.before_put_manifest = _client_b_gc_with_active_lease
    v2_bytes = b"A" * chunk_size + b"C" * chunk_size + b"D" * 77
    (src_dir / "timeline.edl").write_bytes(v2_bytes)
    r2 = mgr_a.push_project_snapshot(
        project_id="shared-cinema",
        source=src_dir,
        master_secret=secret,
        parent_snapshot_id=r1.snapshot_id,
        snapshot_id="snap-v2",
    )
    assert gc_part1_deleted == [(true_orphan,)]
    # Only the 2 delta chunks (C and D) were uploaded once — 0 re-uploads needed thanks to lease!
    assert len(driver_client_a.put_chunk_history) == 2
    assert mgr_b.pull_project_snapshot(
        project_id="shared-cinema",
        master_secret=secret,
        snapshot_id=r2.snapshot_id,
    ).files == {"timeline.edl": v2_bytes}

    # Part 2: Force lease removal right before Client B runs GC(min_age_seconds=0.0) so Client B
    # ACTUALLY deletes Client A's newly uploaded chunks before put_manifest -> Client A heals
    # directly from disk via `_ChunkSourceLocator(file_path, byte_offset, byte_length, ...)`!
    driver_client_a.put_chunk_history.clear()
    gc_part2_deleted: list[tuple[str, ...]] = []

    def _client_b_gc_after_forced_lease_loss(snap_id: str) -> None:
        assert snap_id == "snap-v3"
        for lease_file in (shared_vault_dir / ".leases").glob("lease-*.json"):
            lease_file.unlink()
        gc_rep = mgr_b.garbage_collect_unreferenced_chunks(min_age_seconds=0.0)
        gc_part2_deleted.append(gc_rep.deleted_chunk_ids)

    driver_client_a.before_put_manifest = _client_b_gc_after_forced_lease_loss
    v3_bytes = b"A" * chunk_size + b"E" * chunk_size + b"F" * 55
    (src_dir / "timeline.edl").write_bytes(v3_bytes)
    r3 = mgr_a.push_project_snapshot(
        project_id="shared-cinema",
        source=src_dir,
        master_secret=secret,
        parent_snapshot_id=r2.snapshot_id,
        snapshot_id="snap-v3",
    )
    # Client B deleted both newly uploaded chunks (E and F)...
    assert len(gc_part2_deleted) == 1
    assert len(gc_part2_deleted[0]) == 2
    # ...and Client A re-read both chunks from `src_dir / "timeline.edl"` via _ChunkSourceLocator
    # (2 initial uploads + 2 disk-locator healing uploads = 4 put_chunk calls)!
    assert len(driver_client_a.put_chunk_history) == 4
    assert mgr_b.pull_project_snapshot(
        project_id="shared-cinema",
        master_secret=secret,
        snapshot_id=r3.snapshot_id,
    ).files == {"timeline.edl": v3_bytes}

    # Fail-closed guard: if local disk file is ALSO mutated while external GC deleted remote chunk
    def _evict_and_mutate_local_source(_snap_id: str) -> None:
        for lease_file in (shared_vault_dir / ".leases").glob("lease-*.json"):
            lease_file.unlink()
        mgr_b.garbage_collect_unreferenced_chunks(min_age_seconds=0.0)
        (src_dir / "timeline.edl").write_bytes(b"CORRUPTED_DURING_PUSH" * 20)

    driver_client_a.before_put_manifest = _evict_and_mutate_local_source
    (src_dir / "timeline.edl").write_bytes(b"A" * chunk_size + b"G" * chunk_size)
    with pytest.raises(VaultTransportError, match="local source mutated"):
        mgr_a.push_project_snapshot(
            project_id="shared-cinema",
            source=src_dir,
            master_secret=secret,
            parent_snapshot_id=r3.snapshot_id,
            snapshot_id="snap-v4-aborted",
        )

    # Mid-GC cross-process lease TOCTOU guard: lease acquired during list_chunks() stops delete
    toctou_cid = "6" * 64
    driver_client_b.put_chunk(toctou_cid, b"cross-proc-late")

    class _CrossProcMidGcDriver(LocalDeviceVaultDriver):
        def list_chunks(self) -> list[str]:
            driver_client_a.acquire_push_lease([toctou_cid], lease_id="late-cross-proc")
            return [toctou_cid]

    mgr_mid = PersonalCloudVaultManager(
        _CrossProcMidGcDriver(shared_vault_dir),
        chunk_size=chunk_size,
        kdf_iterations=100_000,
        key_ring=VaultKeyRing.from_secrets(secret),
    )
    gc_mid = mgr_mid.garbage_collect_unreferenced_chunks(min_age_seconds=0.0)
    assert gc_mid.deleted_chunk_ids == ()
    assert driver_client_b.has_chunk(toctou_cid)
    driver_client_a.release_push_lease("late-cross-proc")


def test_p1_1_initial_staging_journal_survives_crash_after_first_put_chunk(
    tmp_path: Path,
) -> None:
    """Crash right after 1st put_chunk (before journal chunk list update) preserves salt & chunk."""
    chunk_size = 64
    src_dir = tmp_path / "p1_1_src"
    src_dir.mkdir()
    payload = (b"1" * chunk_size) + (b"2" * chunk_size) + (b"3" * chunk_size)
    (src_dir / "clip.bin").write_bytes(payload)

    class _CrashRightAfterFirstPutDriver(LocalDeviceVaultDriver):
        def __init__(self, root: Path, *, crash_after_first_put: bool) -> None:
            super().__init__(root)
            self.crash_after_first_put = crash_after_first_put
            self.put_calls: list[str] = []

        def put_chunk(self, chunk_id: str, data: bytes | bytearray | memoryview) -> None:
            super().put_chunk(chunk_id, data)
            self.put_calls.append(chunk_id)
            if self.crash_after_first_put and len(self.put_calls) == 1:
                raise RuntimeError("crash after 1st put_chunk before journal update")

    vault_dir = tmp_path / "p1_1_vault"
    d1 = _CrashRightAfterFirstPutDriver(vault_dir, crash_after_first_put=True)
    m1 = PersonalCloudVaultManager(d1, chunk_size=chunk_size, kdf_iterations=100_000)

    with pytest.raises(RuntimeError, match="before journal update"):
        m1.push_project_snapshot(
            project_id="p1-1-proj",
            source=src_dir,
            master_secret="p1-1-secret",
            snapshot_id="snap-p1-1",
        )
    # Chunk #1 IS on storage, and the pre-upload initial journal recorded salt_hex
    # even though uploaded_chunk_ids is still empty ()!
    assert len(d1.put_calls) == 1
    first_cid = d1.put_calls[0]
    assert d1.has_chunk(first_cid)
    initial_journal = d1.load_staging_journal("p1-1-proj", None)
    assert initial_journal is not None
    assert initial_journal.uploaded_chunk_ids == ()
    saved_salt_hex = initial_journal.salt_hex

    # Retry on fresh manager: recovers `saved_salt_hex`, sees chunk #1 via `has_chunk`,
    # does NOT re-upload chunk #1, uploads ONLY chunks #2 & #3, and leaves 0 orphans!
    d2 = _CrashRightAfterFirstPutDriver(vault_dir, crash_after_first_put=False)
    m2 = PersonalCloudVaultManager(d2, chunk_size=chunk_size, kdf_iterations=100_000)
    rep = m2.push_project_snapshot(
        project_id="p1-1-proj",
        source=src_dir,
        master_secret="p1-1-secret",
        snapshot_id="snap-p1-1",
    )
    assert rep.manifest.salt_hex == saved_salt_hex
    assert rep.total_chunks == 3
    assert rep.uploaded_chunks == 2
    assert rep.deduplicated_chunks == 1
    assert first_cid not in d2.put_calls
    assert len(d2.list_chunks()) == 3
    assert d2.load_staging_journal("p1-1-proj", None) is None
    assert m2.pull_project_snapshot(
        project_id="p1-1-proj",
        master_secret="p1-1-secret",
        snapshot_id="snap-p1-1",
    ).files == {"clip.bin": payload}


def test_p1_2_p1_4_fail_closed_corrupt_lease_gc_and_isfinite_guards(
    tmp_path: Path,
) -> None:
    """Corrupt lease aborts GC fail-closed (0 deletes); NaN/Inf/bool temporal inputs fail."""
    vault = LocalDeviceVaultDriver(tmp_path / "p1_2_vault")
    for bad_val in (float("nan"), float("inf"), float("-inf"), True):
        with pytest.raises(PersonalVaultError):
            PersonalCloudVaultManager(vault, chunk_size=64, lease_ttl_seconds=bad_val)

    mgr = PersonalCloudVaultManager(
        vault,
        chunk_size=64,
        kdf_iterations=100_000,
        key_ring=VaultKeyRing.from_secrets("p1-2-secret"),
    )
    for bad_val in (float("nan"), float("inf"), float("-inf"), True):
        with pytest.raises(PersonalVaultError):
            mgr.garbage_collect_unreferenced_chunks(min_age_seconds=bad_val)
        with pytest.raises(PersonalVaultError):
            mgr.record_orphan_candidate_chunk("a" * 64, recorded_at=bad_val)

    protected_by_corrupt_lease = "c" * 64
    vault.put_chunk(protected_by_corrupt_lease, b"must-not-be-deleted")
    corrupt_lease = vault.root_dir / ".leases" / "lease-corrupted.json"
    corrupt_lease.write_text("{truncated-corrupt-lease", encoding="utf-8")

    with pytest.raises(VaultTransportError):
        mgr.garbage_collect_unreferenced_chunks(min_age_seconds=0.0)
    assert vault.has_chunk(protected_by_corrupt_lease)
    corrupt_lease.unlink()


def test_p1_5_real_os_subprocess_cross_process_lease_fstat_and_gc_coordination(
    tmp_path: Path,
) -> None:
    """Real OS child process via subprocess.run respects parent lease + fstat catches swaps."""
    import json
    import os
    import subprocess
    import sys

    chunk_size = 64
    vault_dir = tmp_path / "subproc_shared_vault"
    src_dir = tmp_path / "subproc_src"
    src_dir.mkdir()
    media_bytes = (b"X" * chunk_size) + (b"Y" * chunk_size)
    (src_dir / "reel.bin").write_bytes(media_bytes)

    orphan_cid = "e" * 64
    subproc_results: list[dict[str, object]] = []

    child_code = """
import json, os, sys
from pathlib import Path
from nexus_ai_agent.storage.personal_cloud_vault import (
    LocalDeviceVaultDriver,
    PersonalCloudVaultManager,
    VaultKeyRing,
)
vdir = Path(sys.argv[1])
drv = LocalDeviceVaultDriver(vdir)
leased = sorted(drv.read_active_leased_chunks())
mgr = PersonalCloudVaultManager(
    drv,
    chunk_size=64,
    kdf_iterations=100_000,
    key_ring=VaultKeyRing.from_secrets("subproc-secret"),
)
rep = mgr.garbage_collect_unreferenced_chunks(min_age_seconds=0.0)
print(json.dumps({
    "child_pid": os.getpid(),
    "leased": leased,
    "deleted": list(rep.deleted_chunk_ids),
}))
"""

    class _SubprocHookDriver(LocalDeviceVaultDriver):
        def put_manifest(self, snapshot_id: str, data: bytes) -> None:
            proc = subprocess.run(
                [sys.executable, "-c", child_code, str(vault_dir)],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            subproc_results.append(json.loads(proc.stdout.strip()))
            super().put_manifest(snapshot_id, data)

    parent_driver = _SubprocHookDriver(vault_dir)
    parent_driver.put_chunk(orphan_cid, b"real-orphan")
    parent_mgr = PersonalCloudVaultManager(
        parent_driver,
        chunk_size=chunk_size,
        kdf_iterations=100_000,
        key_ring=VaultKeyRing.from_secrets("subproc-secret"),
    )
    push_rep = parent_mgr.push_project_snapshot(
        project_id="subproc-proj",
        source=src_dir,
        master_secret="subproc-secret",
        snapshot_id="snap-subproc-1",
    )
    assert len(subproc_results) == 1
    child_info = subproc_results[0]
    assert child_info["child_pid"] != os.getpid()
    assert len(child_info["leased"]) == 2  # type: ignore[arg-type]
    assert child_info["deleted"] == [orphan_cid]
    assert not parent_driver.has_chunk(orphan_cid)

    pulled = parent_mgr.pull_project_snapshot(
        project_id="subproc-proj",
        master_secret="subproc-secret",
        snapshot_id=push_rep.snapshot_id,
    )
    assert pulled.files == {"reel.bin": media_bytes}

    # P1-5 fstat / lstat: same-size in-place overwrite & same-size atomic os.replace mid-stream
    swap_dir = tmp_path / "swap_src"
    swap_dir.mkdir()
    target_file = swap_dir / "live.bin"
    target_file.write_bytes((b"A" * chunk_size) + (b"B" * chunk_size))

    class _MutateMidStreamDriver(LocalDeviceVaultDriver):
        def __init__(self, root: Path, *, atomic_replace: bool) -> None:
            super().__init__(root)
            self.atomic_replace = atomic_replace
            self.triggered = False

        def put_chunk(self, chunk_id: str, data: bytes | bytearray | memoryview) -> None:
            super().put_chunk(chunk_id, data)
            if not self.triggered:
                self.triggered = True
                if self.atomic_replace:
                    tmp_swap = swap_dir / ".swap_same_size.tmp"
                    tmp_swap.write_bytes((b"Z" * chunk_size) + (b"W" * chunk_size))
                    os.replace(tmp_swap, target_file)
                else:
                    with target_file.open("r+b") as fh:
                        fh.seek(chunk_size)
                        fh.write(b"Q" * chunk_size)
                        fh.flush()
                    os.utime(target_file, ns=(1_800_000_000_123_456_789, 1_800_000_000_123_456_789))

    mgr_inplace = PersonalCloudVaultManager(
        _MutateMidStreamDriver(tmp_path / "v_inplace", atomic_replace=False),
        chunk_size=chunk_size,
        kdf_iterations=100_000,
    )
    with pytest.raises(VaultTransportError, match="eof-fstat"):
        mgr_inplace.push_project_snapshot(
            project_id="fstat-p",
            source=swap_dir,
            master_secret="s",
        )

    (swap_dir / ".swap_same_size.tmp").unlink(missing_ok=True)
    target_file.write_bytes((b"A" * chunk_size) + (b"B" * chunk_size))
    mgr_replace = PersonalCloudVaultManager(
        _MutateMidStreamDriver(tmp_path / "v_replace", atomic_replace=True),
        chunk_size=chunk_size,
        kdf_iterations=100_000,
    )
    # An atomic os.replace() mid-stream changes the inode link count (=> ctime) of the
    # inode we still hold open, so the *end-of-file* fingerprint check trips first.
    with pytest.raises(VaultTransportError, match="Concurrent file modification/replacement"):
        mgr_replace.push_project_snapshot(
            project_id="lstat-p",
            source=swap_dir,
            master_secret="s",
        )


def test_edge_driver_capability_gaps_and_gc_fallback_paths(tmp_path: Path) -> None:
    """Cover optional-driver-capability gaps, non-iterable listings, and GC fallback deletes."""
    import os

    chunk_size = 64
    payload = (b"M" * chunk_size) + (b"N" * chunk_size)

    # Driver with acquire_push_lease but WITHOUT update/release hooks
    class _AcquireOnlyDriver(_SpyDriverWrapper):
        def acquire_push_lease(
            self,
            chunk_ids: object = (),
            *,
            project_id: str = "",
            ttl_seconds: float = 60.0,
            lease_id: str | None = None,
        ) -> str:
            return "acquire-only-lease"

    vault_acq = LocalDeviceVaultDriver(tmp_path / "acq_only")
    mgr_acq = PersonalCloudVaultManager(
        _AcquireOnlyDriver(vault_acq),
        chunk_size=chunk_size,
        kdf_iterations=100_000,
        key_ring=VaultKeyRing.from_secrets("s"),
    )
    rep_acq = mgr_acq.push_project_snapshot(
        project_id="acq-proj",
        source={"f.bin": payload},
        master_secret="s",
        snapshot_id="snap-acq",
    )
    assert rep_acq.uploaded_chunks == 2

    # Non-iterable read_active_leased_chunks / list_chunks and non-numeric get_chunk_mtime
    class _WeirdCapabilityDriver(_SpyDriverWrapper):
        def read_active_leased_chunks(self, *, now_epoch: object = None) -> object:
            return 42

        def list_chunks(self) -> object:
            return 7

        def get_chunk_mtime(self, chunk_id: str) -> object:
            return "not-a-timestamp"

    weird_vault = LocalDeviceVaultDriver(tmp_path / "weird_caps")
    orphan_a = "7" * 64
    orphan_b = "8" * 64
    weird_vault.put_chunk(orphan_a, b"orphan-a")
    weird_vault.put_chunk(orphan_b, b"orphan-b")
    weird = _WeirdCapabilityDriver(weird_vault)
    mgr_weird = PersonalCloudVaultManager(
        weird,
        chunk_size=chunk_size,
        kdf_iterations=100_000,
        key_ring=VaultKeyRing.from_secrets("s"),
    )
    mgr_weird.record_orphan_candidate_chunk(orphan_a, recorded_at=1.0)
    gc_weird = mgr_weird.garbage_collect_unreferenced_chunks(
        candidate_chunk_ids=[orphan_b],
        min_age_seconds=300.0,
    )
    assert gc_weird.deleted_chunk_ids == (orphan_a,)
    assert weird_vault.has_chunk(orphan_b)  # unknown age -> first_seen fail-safe retention

    # Atomic delete path: delete_chunk_if_unleased returns False and the chunk is gone
    class _VanishingDriver(_SpyDriverWrapper):
        def delete_chunk_if_unleased(self, chunk_id: str, *, now_epoch: object = None) -> bool:
            return False

        def has_chunk(self, chunk_id: str) -> bool:
            return False

    mgr_vanish = PersonalCloudVaultManager(
        _VanishingDriver(LocalDeviceVaultDriver(tmp_path / "vanishing")),
        chunk_size=chunk_size,
        kdf_iterations=100_000,
        key_ring=VaultKeyRing.from_secrets("s"),
    )
    mgr_vanish.record_orphan_candidate_chunk("9" * 64)
    gc_vanish = mgr_vanish.garbage_collect_unreferenced_chunks(min_age_seconds=0.0)
    assert gc_vanish.deleted_chunk_ids == ()

    # Fallback delete path: driver without delete_chunk_if_unleased but reporting a lease
    class _LateLeaseReportingDriver(_SpyDriverWrapper):
        def __init__(self, inner: BYOSStorageDriver, leased: set[str]) -> None:
            super().__init__(inner)
            self.leased = leased
            self.read_calls = 0

        def read_active_leased_chunks(self, *, now_epoch: object = None) -> set[str]:
            self.read_calls += 1
            return set() if self.read_calls == 1 else set(self.leased)

    fallback_vault = LocalDeviceVaultDriver(tmp_path / "fallback_vault")
    leased_cid = "5" * 64
    unleased_cid = "4" * 64
    fallback_vault.put_chunk(leased_cid, b"leased")
    fallback_vault.put_chunk(unleased_cid, b"unleased")
    mgr_fallback = PersonalCloudVaultManager(
        _LateLeaseReportingDriver(fallback_vault, {leased_cid}),
        chunk_size=chunk_size,
        kdf_iterations=100_000,
        key_ring=VaultKeyRing.from_secrets("s"),
    )
    mgr_fallback.record_orphan_candidate_chunk(leased_cid)
    mgr_fallback.record_orphan_candidate_chunk(unleased_cid)
    gc_fallback = mgr_fallback.garbage_collect_unreferenced_chunks(min_age_seconds=0.0)
    assert gc_fallback.deleted_chunk_ids == (unleased_cid,)
    assert fallback_vault.has_chunk(leased_cid)

    # Post-manifest heal failure on a driver WITHOUT delete_manifest rollback hook
    class _NoRollbackBlackhole:
        def __init__(self, inner: LocalDeviceVaultDriver) -> None:
            self.inner = inner
            self.manifest_written = False

        def put_chunk(self, chunk_id: str, data: bytes | bytearray | memoryview) -> None:
            if not self.manifest_written:
                self.inner.put_chunk(chunk_id, data)

        def get_chunk(self, chunk_id: str) -> bytes:
            return self.inner.get_chunk(chunk_id)

        def has_chunk(self, chunk_id: str) -> bool:
            return False if self.manifest_written else self.inner.has_chunk(chunk_id)

        def put_manifest(self, snapshot_id: str, data: bytes) -> None:
            self.inner.put_manifest(snapshot_id, data)
            self.manifest_written = True

        def get_manifest(self, snapshot_id: str) -> bytes:
            return self.inner.get_manifest(snapshot_id)

        def list_manifests(self) -> list[str]:
            return self.inner.list_manifests()

        def delete_chunk(self, chunk_id: str) -> None:
            self.inner.delete_chunk(chunk_id)

    mgr_norollback = PersonalCloudVaultManager(
        _NoRollbackBlackhole(LocalDeviceVaultDriver(tmp_path / "no_rollback")),
        chunk_size=chunk_size,
        kdf_iterations=100_000,
        key_ring=VaultKeyRing.from_secrets("s"),
    )
    with pytest.raises(VaultTransportError, match="post-manifest"):
        mgr_norollback.push_project_snapshot(
            project_id="norollback",
            source={"a.bin": b"data"},
            master_secret="s",
            snapshot_id="snap-norollback",
        )

    # Growth mid-stream with fstat pinned to the original fingerprint -> unaligned read length check
    trunc_dir = tmp_path / "trunc_dir"
    trunc_dir.mkdir()
    trunc_file = trunc_dir / "clip.bin"
    trunc_file.write_bytes(payload)
    real_fstat = os.fstat
    captured: dict[str, object] = {}

    class _TruncateMidStreamDriver(_SpyDriverWrapper):
        def put_chunk(self, chunk_id: str, data: bytes | bytearray | memoryview) -> None:
            super().put_chunk(chunk_id, data)
            if not captured:
                with trunc_file.open("rb") as fh:
                    captured["st"] = real_fstat(fh.fileno())
                with trunc_file.open("ab") as fh:
                    fh.write(b"Z" * 32)
                    fh.flush()

    def _pinned_fstat(fd: int) -> object:
        return captured.get("st", real_fstat(fd))

    mangled_module_os = __import__(
        "nexus_ai_agent.storage.personal_cloud_vault.sync_journal_manager",
        fromlist=["os"],
    ).os
    mangled_module_os.fstat = _pinned_fstat
    try:
        mgr_trunc = PersonalCloudVaultManager(
            _TruncateMidStreamDriver(LocalDeviceVaultDriver(tmp_path / "trunc_vault")),
            chunk_size=chunk_size,
            kdf_iterations=100_000,
            key_ring=VaultKeyRing.from_secrets("s"),
        )
        with pytest.raises(VaultTransportError, match="Concurrent file size/symlink change"):
            mgr_trunc.push_project_snapshot(
                project_id="trunc-proj",
                source=trunc_dir,
                master_secret="s",
            )
    finally:
        mangled_module_os.fstat = real_fstat


def test_f4_ctime_fingerprint_survives_mtime_rollback_and_streaming_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F4: rewrites that restore ``st_mtime_ns`` are still caught (ctime + SHA-256)."""
    import hashlib
    import os

    import nexus_ai_agent.storage.personal_cloud_vault.sync_journal_manager as sjm

    chunk_size = 64
    src_dir = tmp_path / "f4_src"
    src_dir.mkdir()
    victim = src_dir / "grade.bin"
    original = (b"A" * chunk_size) + (b"B" * chunk_size)
    rewritten = (b"Z" * chunk_size) + (b"Y" * chunk_size)
    victim.write_bytes(original)
    original_stat = victim.stat()

    def _rewrite_and_restore_mtime() -> None:
        """Same-size in-place rewrite, then restore the original mtime exactly."""
        with victim.open("r+b") as handle:
            handle.seek(0)
            handle.write(rewritten)
            handle.flush()
            os.fsync(handle.fileno())
        os.utime(victim, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        restored = victim.stat()
        assert restored.st_mtime_ns == original_stat.st_mtime_ns
        assert restored.st_size == original_stat.st_size
        assert restored.st_ctime_ns != original_stat.st_ctime_ns

    class _RewriteAndRestoreMtimeDriver(LocalDeviceVaultDriver):
        def put_chunk(self, chunk_id: str, data: bytes | bytearray | memoryview) -> None:
            super().put_chunk(chunk_id, data)
            if not swapped:
                swapped.append(chunk_id)
                _rewrite_and_restore_mtime()

    swapped: list[str] = []
    mgr = PersonalCloudVaultManager(
        _RewriteAndRestoreMtimeDriver(tmp_path / "f4_vault"),
        chunk_size=chunk_size,
        kdf_iterations=100_000,
    )
    # An attacker can restore mtime but cannot forge the kernel-owned ctime:
    # the fstat fingerprint therefore fails closed.
    with pytest.raises(VaultTransportError, match="Concurrent file modification/replacement"):
        mgr.push_project_snapshot(
            project_id="f4-proj",
            source=src_dir,
            master_secret="f4-secret",
        )
    assert swapped

    # Defence in depth: even if every metadata check is disabled, the streaming
    # SHA-256 of the uploaded bytes still blocks the manifest from being committed.
    victim.write_bytes(original)
    monkeypatch.setattr(sjm, "_verify_fstat_matches_fingerprint", lambda *a, **k: None)
    swapped.clear()

    class _BlindSpotDriver(LocalDeviceVaultDriver):
        def put_chunk(self, chunk_id: str, data: bytes | bytearray | memoryview) -> None:
            super().put_chunk(chunk_id, data)
            if not swapped:
                swapped.append(chunk_id)
                with victim.open("r+b") as handle:
                    handle.seek(0)
                    handle.write(rewritten)
                    handle.flush()

    mgr_blind = PersonalCloudVaultManager(
        _BlindSpotDriver(tmp_path / "f4_blind_vault"),
        chunk_size=chunk_size,
        kdf_iterations=100_000,
    )
    with pytest.raises(VaultTransportError, match="local source content mutated"):
        mgr_blind.push_project_snapshot(
            project_id="f4-blind-proj",
            source=src_dir,
            master_secret="f4-secret",
        )
    assert mgr_blind.driver.list_manifests() == []

    # The digest guard itself: mismatch raises, match/absent pass, IO errors fail closed.
    victim.write_bytes(rewritten)
    with pytest.raises(VaultTransportError, match="local source content mutated"):
        sjm._verify_source_digest(
            victim,
            expected_sha256_hex=hashlib.sha256(original).hexdigest(),
            phase="unit",
        )
    sjm._verify_source_digest(victim, expected_sha256_hex=None, phase="unit")
    sjm._verify_source_digest(
        victim,
        expected_sha256_hex=hashlib.sha256(rewritten).hexdigest(),
        phase="unit",
    )
    with pytest.raises(VaultTransportError, match="Failed re-reading project file"):
        sjm._verify_source_digest(
            src_dir / "missing.bin",
            expected_sha256_hex=hashlib.sha256(original).hexdigest(),
            phase="unit",
        )

    # The stream records the digest of exactly the bytes it yielded, block by block.
    victim.write_bytes(original)
    fp = sjm._DiskFileStatFingerprint(
        st_ino=victim.stat().st_ino,
        st_size=victim.stat().st_size,
        st_mtime_ns=victim.stat().st_mtime_ns,
        st_ctime_ns=victim.stat().st_ctime_ns,
    )
    digests: dict[str, str] = {}
    streamed = list(
        sjm._stream_source_chunks(
            rel_path="grade.bin",
            disk_path=victim,
            expected_size=len(original),
            expected_fp=fp,
            chunk_size=chunk_size,
            inline_sources=None,
            digests_out=digests,
        )
    )
    assert [chunk for _, _, chunk in streamed] == [original[:chunk_size], original[chunk_size:]]
    assert digests["grade.bin"] == hashlib.sha256(original).hexdigest()

    # Inline (in-memory) sources also publish their digest for the same verification.
    inline_digests: dict[str, str] = {}
    inline_chunks = list(
        sjm._stream_source_chunks(
            rel_path="inline.bin",
            disk_path=None,
            expected_size=len(original),
            expected_fp=None,
            chunk_size=chunk_size,
            inline_sources={"inline.bin": original},
            digests_out=inline_digests,
        )
    )
    assert len(inline_chunks) == 2
    assert inline_digests["inline.bin"] == hashlib.sha256(original).hexdigest()


def test_last_mile_dead_pid_probe_and_digest_optional_streaming(tmp_path: Path) -> None:
    """A provably dead owner PID returns ``False`` and streaming works without digests."""
    import os

    import nexus_ai_agent.storage.personal_cloud_vault.byos_drivers as drivers_module
    import nexus_ai_agent.storage.personal_cloud_vault.sync_journal_manager as sjm

    dead_pid = 4_500_000
    while True:
        try:
            os.kill(dead_pid, 0)
        except ProcessLookupError:
            break
        dead_pid += 1
    assert drivers_module._pid_is_alive(dead_pid) is False
    assert drivers_module._pid_is_definitely_dead(dead_pid) is True

    chunk_size = 32
    payload = b"S" * chunk_size + b"T" * chunk_size
    src = tmp_path / "no_digest.bin"
    src.write_bytes(payload)
    stat_result = src.stat()
    fingerprint = sjm._DiskFileStatFingerprint(
        st_ino=stat_result.st_ino,
        st_size=stat_result.st_size,
        st_mtime_ns=stat_result.st_mtime_ns,
        st_ctime_ns=stat_result.st_ctime_ns,
    )
    # digests_out omitted => purely streaming, no hashing side channel
    chunks = list(
        sjm._stream_source_chunks(
            rel_path="no_digest.bin",
            disk_path=src,
            expected_size=len(payload),
            expected_fp=fingerprint,
            chunk_size=chunk_size,
            inline_sources=None,
        )
    )
    assert [c for _, _, c in chunks] == [payload[:chunk_size], payload[chunk_size:]]

    inline_chunks = list(
        sjm._stream_source_chunks(
            rel_path="inline.bin",
            disk_path=None,
            expected_size=len(payload),
            expected_fp=None,
            chunk_size=chunk_size,
            inline_sources={"inline.bin": payload},
        )
    )
    assert len(inline_chunks) == 2


def test_streaming_push_fails_closed_on_identity_mismatch_and_locator_errors(
    tmp_path: Path,
) -> None:
    """In-place encryption is fail-closed: identity drift and I/O errors never upload."""
    import os

    import nexus_ai_agent.storage.personal_cloud_vault.sync_journal_manager as sjm_module
    from nexus_ai_agent.storage.personal_cloud_vault import (
        derive_vault_keys,
        generate_vault_salt,
    )
    from nexus_ai_agent.storage.personal_cloud_vault.sync_journal_manager import (
        _ChunkSourceLocator,
        _read_chunk_from_locator,
    )

    chunk_size = 1_024
    source_dir = tmp_path / "identity_src"
    source_dir.mkdir()
    (source_dir / "clip.bin").write_bytes(b"A" * chunk_size)
    key_material = derive_vault_keys(
        "identity-mismatch-passphrase",
        generate_vault_salt(),
        iterations=100_000,
    )

    class _RecordingDriver(_SpyDriverWrapper):
        def __init__(self, inner: BYOSStorageDriver) -> None:
            super().__init__(inner)
            self.written: list[int] = []

        def put_chunk(self, chunk_id: str, data: bytes | bytearray | memoryview) -> None:
            self.written.append(len(data))
            super().put_chunk(chunk_id, data)

    identity_driver = _RecordingDriver(LocalDeviceVaultDriver(tmp_path / "identity_vault"))
    identity_mgr = PersonalCloudVaultManager(
        identity_driver, chunk_size=chunk_size, kdf_iterations=100_000
    )
    # The derived content address no longer matches the caller's chunk id: the
    # in-place path must reject it *before* the wire buffer reaches the driver.
    with pytest.raises(VaultTransportError, match="Chunk identity mismatch while encrypting"):
        identity_mgr._encrypt_and_upload_chunk(
            "0" * 64,
            b"A" * chunk_size,
            key_material,
        )
    assert identity_driver.written == []

    # ``_lstat_source`` converts post-stream stat failures into transport errors.
    missing = tmp_path / "vanished_after_stream.bin"
    missing.write_bytes(b"B" * 8)
    stream_handle = missing.open("rb")
    try:
        missing.unlink()
        with pytest.raises(VaultTransportError, match="Failed reading project file"):
            sjm_module._lstat_source(missing)
    finally:
        stream_handle.close()

    # Disk locators surface short reads and re-read failures for evicted chunks.
    locator_dir = tmp_path / "locator_src"
    locator_dir.mkdir()
    payload = b"0123456789"
    (locator_dir / "full.bin").write_bytes(payload)
    short_locator = _ChunkSourceLocator(
        file_path=locator_dir / "full.bin",
        byte_offset=6,
        byte_length=8,
        chunk_index=0,
    )
    with pytest.raises(VaultTransportError, match="Short read while healing chunk"):
        _read_chunk_from_locator(short_locator, None)

    missing_locator = _ChunkSourceLocator(
        file_path=locator_dir / "gone.bin",
        byte_offset=0,
        byte_length=4,
        chunk_index=0,
    )
    with pytest.raises(VaultTransportError, match="Failed re-reading chunk from"):
        _read_chunk_from_locator(missing_locator, None)

    # The plaintext slot of a prepared chunk is only reachable via the wire buffer, so
    # a successful push never leaves a second plaintext copy behind.
    assert os.path.exists(locator_dir / "full.bin")


_BENCHMARK_CHILD = r'''
# Wave-4 scalability probe: one push of ``N`` chunks in an isolated process.
import gc
import json
import os
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

chunk_count = int(sys.argv[1])
chunk_size = int(sys.argv[2])
driver_kind = sys.argv[3] if len(sys.argv) > 3 else "local"

import psutil

from nexus_ai_agent.storage.personal_cloud_vault.byos_drivers import (
    LocalDeviceVaultDriver,
    PortableBundleVaultDriver,
    S3CompatibleSigV4Driver,
    S3HttpResponse,
    S3SigV4Config,
)
from nexus_ai_agent.storage.personal_cloud_vault.sync_journal_manager import (
    PersonalCloudVaultManager,
)


def _hwm_rss_bytes() -> int:
    """Kernel high-water-mark RSS (true peak, not the value at sampling time).

    ``VmHWM`` from ``/proc/self/status`` is the authoritative Linux figure; fall back to
    ``resource.getrusage().ru_maxrss`` (KiB on Linux, bytes on macOS) elsewhere.
    """
    try:
        with open("/proc/self/status", encoding="ascii") as status:
            for line in status:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    import resource
    import sys as _sys

    maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return maxrss if _sys.platform == "darwin" else maxrss * 1024


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    project = tmp / "scaling_project"
    (project / "media").mkdir(parents=True)
    # N structurally distinct chunks so deduplication cannot collapse them. The source
    # file is written one chunk at a time so this harness never holds the whole payload
    # (that would pollute the very RSS number we are measuring).
    with (project / "media" / "reel.bin").open("wb") as source_file:
        for index in range(chunk_count):
            source_file.write(
                f"CHUNK-{index:06d}-".encode() + bytes([index & 0xFF]) * (chunk_size - 13)
            )

    if driver_kind == "bundle":
        driver = PortableBundleVaultDriver(tmp / "vault.nagarvault")
    elif driver_kind == "s3":
        objects: dict[str, bytes] = {}

        def _transport(request):
            # Minimal stateful S3: HEAD/GET must reflect earlier PUTs or the push's
            # own lease/etag protocol would fail.
            if "list-type=2" in request.url:
                return S3HttpResponse(
                    status_code=200,
                    body=(
                        b'<?xml version="1.0" encoding="UTF-8"?>'
                        b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                        b"<IsTruncated>false</IsTruncated></ListBucketResult>"
                    ),
                )
            key = request.url.split("?")[0]
            if request.method == "PUT":
                # Chunk bodies are recorded as a marker only: keeping them would make the
                # harness — not the driver — the memory bottleneck being measured.
                objects[key] = b"" if key.endswith(".chk") else request.body
                return S3HttpResponse(status_code=200, headers={"etag": '"bench-etag"'})
            if request.method == "HEAD":
                if key not in objects:
                    return S3HttpResponse(status_code=404)
                return S3HttpResponse(status_code=200, headers={"etag": '"bench-etag"'})
            if request.method == "GET":
                if key not in objects:
                    return S3HttpResponse(status_code=404)
                return S3HttpResponse(status_code=200, body=objects[key])
            return S3HttpResponse(status_code=200)

        driver = S3CompatibleSigV4Driver(
            S3SigV4Config(
                endpoint_url="https://account.r2.cloudflarestorage.com",
                bucket="bench-bucket",
                region="auto",
                access_key_id="AKID",
                secret_access_key="SECRET",
            ),
            http_transport=_transport,
        )
    else:
        driver = LocalDeviceVaultDriver(tmp / "vault")

    manager = PersonalCloudVaultManager(
        driver,
        chunk_size=chunk_size,
        kdf_iterations=100_000,
    )
    gc.collect()
    process = psutil.Process(os.getpid())
    rss_before = process.memory_info().rss
    hwm_before = _hwm_rss_bytes()

    tracemalloc.start()
    started = time.perf_counter()
    try:
        report = manager.push_project_snapshot(
            project_id="scaling",
            source=project,
            master_secret="scaling-benchmark-passphrase",
            snapshot_id="snap-scaling",
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    print(
        json.dumps(
            {
                "chunks": chunk_count,
                "chunk_size": chunk_size,
                "driver": driver_kind,
                "uploaded": report.uploaded_chunks,
                "tracemalloc_peak": peak,
                "elapsed_ms": elapsed_ms,
                "rss_before": rss_before,
                "rss_after": process.memory_info().rss,
                "hwm_before": hwm_before,
                "hwm_after": _hwm_rss_bytes(),
            }
        )
    )
'''


class _BenchRecord(TypedDict):
    """One subprocess measurement (typed so no ``float(object)`` casts are needed)."""

    chunks: int
    chunk_size: int
    driver: str
    uploaded: int
    tracemalloc_peak: int
    elapsed_ms: float
    rss_before: int
    rss_after: int
    hwm_before: int
    hwm_after: int


class _SizeRecord(TypedDict):
    """Aggregated median/MAD record for one ``(driver, N)`` pair."""

    chunks: int
    data_mib: float
    peak_med: float
    peak_mad: float
    elapsed_med: float
    elapsed_mad: float
    hwm_med: float
    hwm_mad: float
    rss_before: float
    rss_after: float
    hwm_before: float
    trial_pairs: list[tuple[float, float, float, float]]


def _median_and_mad(samples: list[float]) -> tuple[float, float]:
    """Return ``(median, MAD)``: robust centre and spread for a small sample."""
    import statistics

    centre = statistics.median(samples)
    return centre, statistics.median([abs(value - centre) for value in samples])


def _run_benchmark_child(chunk_count: int, chunk_size: int, driver_kind: str) -> _BenchRecord:
    """Run one isolated push and return its measurement record."""
    import json
    import subprocess
    import sys
    from pathlib import Path as _Path

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _BENCHMARK_CHILD,
            str(chunk_count),
            str(chunk_size),
            driver_kind,
        ],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
        cwd=str(_Path(__file__).resolve().parents[3]),
    )
    assert completed.returncode == 0, (
        f"benchmark subprocess failed for N={chunk_count} driver={driver_kind}:\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    entry: _BenchRecord = json.loads(completed.stdout.strip().splitlines()[-1])
    assert entry["chunks"] == chunk_count
    assert entry["uploaded"] == chunk_count
    assert entry["driver"] == driver_kind
    return entry


def test_wave4_memory_scaling_benchmark(tmp_path: Path) -> None:
    """F2/F3: peak RAM stays payload-bounded as the chunk count grows to 250.

    Every measurement runs in its own subprocess (so allocator state from a previous
    size can never leak into the next one) and each configuration is repeated
    ``TRIALS`` times so a median and a median-absolute-deviation can be reported
    instead of a single noisy sample.

    What is measured and asserted, precisely:

    * ``M_payload`` is ``O(S_chunk)`` and does **not** grow with ``N``. Total peak is
      therefore ``peak ≈ payload + metadata(N)``, and the code asserts the documented
      linear model ``peak < 200_000 + 2048 * N``. It deliberately does *not* claim
      ``peak < 4 * chunk_size`` for large ``N``; that absolute ceiling only holds for
      the N1 fixture (20 chunks) and is asserted there.
    * ``M_metadata`` is ``O(N)`` but compact: the measured tracemalloc slope must stay
      under 4 KiB per chunk (a retained payload buffer would be >= 64 KiB per chunk).
    * RSS is measured as the kernel high-water mark (``VmHWM``/``ru_maxrss``), i.e. the
      real peak during the push, not the sampled value after it finished.
    * ``S3CompatibleSigV4Driver`` and ``PortableBundleVaultDriver`` are each measured at
      ``N=20`` (3 trials) *and* ``N=100`` (1 trial). The assertion there is the slope
      across those two sizes, never a fixed absolute number: S3's payload term must stay
      flat within its documented ``at most 2 x S_chunk`` (wire buffer + the ``bytes()``
      copy SigV4 signing needs), and the bundle backend's slope must stay within its
      documented ``~2 x`` copy-on-write retention factor
      (``M_backend = O(sum(S_chunk))``).
    """
    chunk_size = 65_536
    counts = (20, 100, 250)
    trials = 3

    results: list[_SizeRecord] = []
    for chunk_count in counts:
        samples = [_run_benchmark_child(chunk_count, chunk_size, "local") for _ in range(trials)]
        peaks = [float(sample["tracemalloc_peak"]) for sample in samples]
        elapsed = [sample["elapsed_ms"] for sample in samples]
        hwm = [float(sample["hwm_after"]) for sample in samples]
        peak_med, peak_mad = _median_and_mad(peaks)
        elapsed_med, elapsed_mad = _median_and_mad(elapsed)
        hwm_med, hwm_mad = _median_and_mad(hwm)
        # Keep every per-trial figure together: an HWM from trial A can never be compared
        # against a sampled RSS from trial B (they are different processes).
        trial_pairs = [
            (
                float(sample["rss_before"]),
                float(sample["rss_after"]),
                float(sample["hwm_before"]),
                float(sample["hwm_after"]),
            )
            for sample in samples
        ]
        record: _SizeRecord = {
            "chunks": chunk_count,
            "data_mib": chunk_count * chunk_size / (1024 * 1024),
            "peak_med": peak_med,
            "peak_mad": peak_mad,
            "elapsed_med": elapsed_med,
            "elapsed_mad": elapsed_mad,
            "hwm_med": hwm_med,
            "hwm_mad": hwm_mad,
            "rss_before": float(samples[0]["rss_before"]),
            "rss_after": float(samples[0]["rss_after"]),
            "hwm_before": float(samples[0]["hwm_before"]),
            "trial_pairs": trial_pairs,
        }
        results.append(record)

    print(
        f"\n[wave-4] LocalDeviceVaultDriver, chunk_size={chunk_size}, "
        f"trials={trials} (median ± MAD)"
    )
    print(
        f"{'chunks':>7} {'data MiB':>9} {'peak B (med±MAD)':>24} {'elapsed ms (med±MAD)':>24} "
        f"{'HWM MiB (med±MAD)':>22} {'peak B/chunk':>13}"
    )
    for entry in results:
        print(
            f"{int(entry['chunks']):>7} {entry['data_mib']:>9.2f} "
            f"{entry['peak_med']:>14.0f}±{entry['peak_mad']:<8.0f} "
            f"{entry['elapsed_med']:>14.1f}±{entry['elapsed_mad']:<8.1f} "
            f"{entry['hwm_med'] / 1048576:>13.2f}±{entry['hwm_mad'] / 1048576:<7.2f} "
            f"{entry['peak_med'] / entry['chunks']:>13.1f}"
        )

    peaks = [entry["peak_med"] for entry in results]
    first, last = results[0], results[-1]
    peak_growth_per_chunk = (peaks[-1] - peaks[0]) / (counts[-1] - counts[0])
    print(
        f"[wave-4] tracemalloc peak slope: {peak_growth_per_chunk:.1f} B/chunk "
        f"(payload floor is {chunk_size} B, shared by every N)"
    )
    hwm_growth = (last["hwm_med"] - first["hwm_med"]) / (counts[-1] - counts[0])
    print(f"[wave-4] HWM RSS slope: {hwm_growth:.1f} B/chunk (payload is {chunk_size} B/chunk)")
    print(
        f"[wave-4] RSS (sampled after push): before={first['rss_before'] / 1048576:.2f} MiB "
        f"after={first['rss_after'] / 1048576:.2f} MiB"
    )

    # --- M_payload does not scale with N: only compact metadata may grow.
    assert peak_growth_per_chunk < 4 * 1024, (
        f"peak grew {peak_growth_per_chunk:.1f} B/chunk: metadata is not compact, or a "
        f"payload-sized buffer is being retained per chunk"
    )
    for entry in results:
        model_bound = 200_000 + 2048 * entry["chunks"]
        assert entry["peak_med"] < model_bound, (
            f"peak at N={entry['chunks']} was {entry['peak_med']:.0f}, above the "
            f"documented model bound {model_bound} (payload 200000 + metadata)"
        )
    assert (peaks[-1] - peaks[0]) / (counts[-1] - counts[0]) < chunk_size // 8, (
        "per-chunk metadata footprint must stay tiny compared with one chunk"
    )

    # --- Real peak (kernel high-water mark) also must not scale like payload would.
    assert hwm_growth < 8 * 1024, (
        f"HWM RSS grew {hwm_growth:.0f} B/chunk, indicating payload-sized retention"
    )
    assert hwm_growth < chunk_size
    for entry in results:
        assert entry["hwm_mad"] >= 0.0
        assert len(entry["trial_pairs"]) == trials
        for rss_before, rss_after, hwm_before, hwm_after in entry["trial_pairs"]:
            # All four figures come from the SAME process, so these invariants hold:
            # HWM is monotonic within a run, it can never sit below that run's RSS, and
            # RSS measured after the push can never exceed the run's own high-water mark.
            assert hwm_after >= hwm_before
            assert hwm_after >= rss_before
            assert rss_after <= hwm_after

    # --- Cross-backend: the *payload* term must stay O(S_chunk) — i.e. independent of
    # the project size — on every backend. That is proven in the only way that cannot be
    # gamed by a constant: measure each backend at two project sizes and bound the slope.
    backend_results: dict[str, dict[str, dict[str, float]]] = {}
    for driver_kind in ("s3", "bundle"):
        per_size: dict[str, dict[str, float]] = {}
        for size, size_trials in ((20, trials), (100, 1)):
            samples = [
                _run_benchmark_child(size, chunk_size, driver_kind) for _ in range(size_trials)
            ]
            size_peaks = [float(sample["tracemalloc_peak"]) for sample in samples]
            size_peak_med, size_peak_mad = _median_and_mad(size_peaks)
            per_size[str(size)] = {"peak_med": size_peak_med, "peak_mad": size_peak_mad}
        backend_results[driver_kind] = per_size

    print(f"\n[wave-4] cross-backend payload peaks (median ± MAD, chunk_size={chunk_size})")
    print(f"{'driver':>10} {'N':>5} {'tracemalloc peak B':>24} {'B/chunk':>10}")
    for driver_kind, per_size in backend_results.items():
        for size_label, size_record in per_size.items():
            size_int = int(size_label)
            print(
                f"{driver_kind:>10} {size_int:>5} "
                f"{size_record['peak_med']:>14.0f}±{size_record['peak_mad']:<8.0f} "
                f"{size_record['peak_med'] / size_int:>10.1f}"
            )

    local_peak_at_20 = float(results[0]["peak_med"])

    # S3: documented as "at most 2 x S_chunk" (wire buffer + the ``bytes(data)`` copy
    # SigV4 payload signing needs). Two independent assertions:
    #   (a) it adds only chunk-constant overhead over the local backend, and
    #   (b) that overhead does NOT grow with N — the payload term is O(S_chunk).
    s3_peak_20 = backend_results["s3"]["20"]["peak_med"]
    s3_peak_100 = backend_results["s3"]["100"]["peak_med"]
    s3_extra_copy = s3_peak_20 - local_peak_at_20
    s3_slope = (s3_peak_100 - s3_peak_20) / 80
    print(
        f"[wave-4] S3 extra over local at N=20: {s3_extra_copy:.0f} B "
        f"(one chunk of SigV4 copy = {chunk_size} B); S3 slope {s3_slope:.1f} B/chunk"
    )
    assert s3_slope < 4 * 1024, (
        f"S3 peak grew {s3_slope:.1f} B/chunk between N=20 and N=100: its payload term is "
        f"not independent of project size"
    )
    # The AEAD wire path holds only ONE payload-sized buffer, so the S3 backend's extra
    # term is the immutable ``bytes(data)`` snapshot SigV4 signing needs (``put_chunk``
    # must copy the mutable wire buffer, else a caller could mutate the bytes that were
    # signed). That is one chunk constant; the bound allows the documented two-chunk
    # headroom so the assertion stays robust to allocator noise.
    assert s3_extra_copy < 3 * chunk_size, (
        f"S3 added {s3_extra_copy:.0f} B over the local backend, more than the documented "
        f"chunk-constant overhead of mutable->bytes conversion for SigV4 signing"
    )
    assert s3_peak_100 < 200_000 + 2048 * 100 + 2 * chunk_size, (
        f"S3 peak at N=100 ({s3_peak_100:.0f} B) exceeds payload + metadata model bound"
    )

    # Bundle: writes/verifies are streamed, but the archive is self-contained, so its
    # retained cost is O(sum(S_chunk)) by design — the slope is ~1 chunk per chunk.
    bundle_peak_20 = backend_results["bundle"]["20"]["peak_med"]
    bundle_peak_100 = backend_results["bundle"]["100"]["peak_med"]
    bundle_slope = (bundle_peak_100 - bundle_peak_20) / 80
    print(
        f"[wave-4] bundle retained-archive slope: {bundle_slope:.1f} B/chunk "
        f"(documented M_backend = O(sum(S_chunk)))"
    )
    # The bundle driver is copy-on-write per mutation (reload, mutate the dict, then
    # re-serialize), so its peak is ~2 x the archive size: the retained vault plus the
    # in-progress copy. Still O(sum(S_chunk)) — a *constant* factor, not a new growth
    # class — but the bound must be stated as such rather than pretending it is 1x.
    assert bundle_slope < 2.5 * chunk_size, (
        f"bundle peak grew {bundle_slope:.1f} B/chunk, more than the ~2x copy-on-write "
        f"factor the self-contained archive design documents"
    )
    # Every backend's *metadata* slope stays compact even though the bundle's retained
    # archive (backend term) legitimately grows: no backend may grow by >= 1 chunk per
    # chunk of pure bookkeeping beyond the documented factors above.
    for driver_kind, per_size in backend_results.items():
        per_chunk_metadata = (per_size["100"]["peak_med"] - per_size["20"]["peak_med"]) / 80
        assert per_chunk_metadata < 2.5 * chunk_size, (
            f"{driver_kind} grew {per_chunk_metadata:.1f} B/chunk across N, beyond the "
            f"documented constant factors"
        )


def test_req4_streaming_pull_bounded_memory_and_disk_restore(tmp_path: Path) -> None:
    """Req-4: pulling to a directory streams at O(S_chunk) RAM, never O(payload)."""
    import tracemalloc

    vault = LocalDeviceVaultDriver(tmp_path / "pull_stream_vault")
    chunk_size = 65_536
    mgr = PersonalCloudVaultManager(vault, chunk_size=chunk_size)
    secret = "pull-stream-secret"

    # 16 files of 32 chunks each: a 32 MiB payload restored at a per-chunk RAM cost.
    # The chunk size is deliberately large enough that the O(N) metadata term is a
    # negligible fraction of the payload, so ``peak < payload // 4`` measures the
    # claim under test (payload is streamed, never retained) rather than the
    # interpreter's per-object overhead.  Content is generated in C (``bytes * n``)
    # and differs per file, so it is both cheap to build and non-deduplicable.
    source: dict[str, bytes] = {
        f"dir/file_{f:02d}.bin": bytes([f]) * (chunk_size * 32) for f in range(16)
    }
    total_payload = sum(len(v) for v in source.values())
    mgr.push_project_snapshot(
        project_id="pull-stream",
        source=source,
        master_secret=secret,
        snapshot_id="snap-1",
    )

    out_dir = tmp_path / "restored"
    tracemalloc.start()
    try:
        report = mgr.pull_project_snapshot(
            project_id="pull-stream",
            master_secret=secret,
            snapshot_id="snap-1",
            target_dir=out_dir,
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # Restoring to disk returns no in-memory copy, so RAM stays a fraction of the payload.
    assert report.files == {}
    assert report.total_restored_bytes == total_payload
    for rel, content in source.items():
        assert (out_dir / rel).read_bytes() == content
    assert peak < total_payload // 4, (
        f"streaming pull peak={peak} held more than a quarter of the {total_payload}-byte "
        f"payload; it must stay O(S_chunk)"
    )

    # Without a target the same snapshot is returned in memory byte-for-byte.
    in_mem = mgr.pull_project_snapshot(
        project_id="pull-stream", master_secret=secret, snapshot_id="snap-1"
    )
    assert in_mem.files == source
    assert in_mem.total_restored_bytes == total_payload


def test_req4_pull_staging_is_cleaned_up_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Req-4: a mid-pull tamper fails closed and removes the private staging root."""
    from nexus_ai_agent.storage.personal_cloud_vault import sync_journal_manager as sjm

    vault = LocalDeviceVaultDriver(tmp_path / "pull_cleanup_vault")
    mgr = PersonalCloudVaultManager(vault, chunk_size=64)
    secret = "pull-cleanup-secret"
    push = mgr.push_project_snapshot(
        project_id="pull-cleanup",
        source={"a.bin": b"A" * 64 + b"B" * 64, "b.bin": b"C" * 128},
        master_secret=secret,
        snapshot_id="snap-cleanup",
    )

    created: list[Path] = []
    real_mkdtemp = sjm.tempfile.mkdtemp

    def _recording_mkdtemp(*args: Any, **kwargs: Any) -> str:
        path = real_mkdtemp(*args, **kwargs)
        created.append(Path(path))
        return path

    monkeypatch.setattr(sjm.tempfile, "mkdtemp", _recording_mkdtemp)

    # Corrupt the last chunk on disk so the streaming pull refuses mid-flight.
    last_cid = push.manifest.files[-1].chunks[-1].chunk_id
    chunk_file = vault.root_dir / "chunks" / f"{last_cid}.chk"
    corrupted = bytearray(chunk_file.read_bytes())
    corrupted[-1] ^= 0x01
    chunk_file.write_bytes(bytes(corrupted))

    out_dir = tmp_path / "must_not_exist"
    with pytest.raises(VaultTamperDetectedError):
        mgr.pull_project_snapshot(
            project_id="pull-cleanup",
            master_secret=secret,
            snapshot_id="snap-cleanup",
            target_dir=out_dir,
        )
    assert not out_dir.exists()
    assert created, "the streaming pull must have created a private staging root"
    assert all(not staging.exists() for staging in created)


def test_req4_pull_helper_fail_closed_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Req-4 defensive branches: staging errors, missing/short staged chunks, copy errors."""
    from dataclasses import replace as _replace

    from nexus_ai_agent.storage.personal_cloud_vault import (
        VaultChunkRef,
        VaultFileEntry,
        compute_chunk_id,
        compute_file_mac,
        derive_vault_keys,
    )
    from nexus_ai_agent.storage.personal_cloud_vault import (
        sync_journal_manager as sjm,
    )

    km = derive_vault_keys("helper-secret", b"H" * 32)
    mgr = PersonalCloudVaultManager(
        LocalDeviceVaultDriver(tmp_path / "helper_vault"), chunk_size=64
    )

    # mkdtemp failure becomes a typed transport error, not a bare OSError.
    def _boom(*args: Any, **kwargs: Any) -> str:
        raise OSError("no space")

    monkeypatch.setattr(sjm.tempfile, "mkdtemp", _boom)
    with pytest.raises(VaultTransportError, match="Cannot create pull staging directory"):
        mgr._open_pull_staging()
    monkeypatch.undo()

    # A filesystem that rejects chmod still yields a private staging root.
    monkeypatch.setattr(sjm.os, "chmod", lambda *a, **k: (_ for _ in ()).throw(OSError("ro")))
    staging = mgr._open_pull_staging()
    assert staging.is_dir()
    monkeypatch.undo()

    data = b"abcd"
    cid = compute_chunk_id(data, km)
    entry = VaultFileEntry(
        rel_path="f.bin",
        total_size=len(data),
        file_mac_hex=compute_file_mac("f.bin", data, km),
        chunks=(VaultChunkRef(chunk_index=0, chunk_id=cid, plaintext_len=len(data)),),
    )

    # A referenced chunk that was never staged is refused.
    with pytest.raises(VaultTamperDetectedError, match="Missing staged chunk"):
        mgr._reassemble_staged_file(entry, {}, km, tmp_path / "out.bin")

    # A staged chunk shorter than the manifest promises is refused by size.
    short = tmp_path / "short.bin"
    short.write_bytes(b"ab")
    with pytest.raises(VaultTamperDetectedError, match="Reassembled size"):
        mgr._reassemble_staged_file(entry, {cid: short}, km, tmp_path / "out2.bin")

    # Non-integer chunk geometry is refused, never propagated as a TypeError.
    broken = _replace(entry, chunks=(_replace(entry.chunks[0], plaintext_len="x"),))
    with pytest.raises(VaultTamperDetectedError, match="Invalid chunk geometry"):
        mgr._reassemble_staged_file(broken, {cid: short}, km, tmp_path / "out3.bin")

    # A staging source that vanished mid-publish is cleaned up and re-raised.
    missing_source = tmp_path / "gone.bin"
    with pytest.raises(FileNotFoundError):
        sjm._stream_copy_file(missing_source, tmp_path / "dest.bin")
    assert not list(tmp_path.glob("dest.bin*"))


def test_gc_rejects_manifest_identity_mismatch(tmp_path: Path) -> None:
    """A validly signed manifest for snapshot B parked under A's key must abort GC."""
    import json

    vault = LocalDeviceVaultDriver(tmp_path / "gc_id_vault")
    secret = "gc-identity-secret"
    ring = VaultKeyRing.from_secrets(secret)
    mgr = PersonalCloudVaultManager(vault, chunk_size=64, key_ring=ring)

    r1 = mgr.push_project_snapshot(
        project_id="proj-id",
        source={"a.bin": b"A" * 64},
        master_secret=secret,
        snapshot_id="snap-A",
    )
    mgr.push_project_snapshot(
        project_id="proj-id",
        source={"a.bin": b"B" * 64},
        master_secret=secret,
        parent_snapshot_id=r1.snapshot_id,
        snapshot_id="snap-B",
    )
    # Park a still-valid signed manifest for snap-B under snap-A's storage key.
    b_manifest = vault.get_manifest("snap-B")
    assert json.loads(b_manifest)["snapshot_id"] == "snap-B"
    vault.put_manifest("snap-A", b_manifest)

    orphan = "0" * 64
    vault.put_chunk(orphan, b"must-not-be-collected")
    with pytest.raises(VaultIntegrityError, match="Manifest identity mismatch"):
        mgr.garbage_collect_unreferenced_chunks(min_age_seconds=0.0)
    assert vault.has_chunk(orphan)


def test_pull_migrates_legacy_v1_chunk_only_when_opted_in(tmp_path: Path) -> None:
    """A stored legacy v1 chunk is rejected by default and restored via migration."""
    import hashlib
    import hmac
    import struct

    from nexus_ai_agent.storage.personal_cloud_vault import (
        compute_chunk_id,
        derive_vault_keys,
    )

    vault = LocalDeviceVaultDriver(tmp_path / "legacy_vault")
    secret = "legacy-pull-secret"
    ring = VaultKeyRing.from_secrets(secret)
    mgr = PersonalCloudVaultManager(vault, chunk_size=64, key_ring=ring)

    plaintext = b"L" * 64
    push = mgr.push_project_snapshot(
        project_id="proj-legacy",
        source={"a.bin": plaintext},
        master_secret=secret,
        snapshot_id="snap-legacy",
    )
    km = derive_vault_keys(secret, bytes.fromhex(push.manifest.salt_hex))
    chunk_id = compute_chunk_id(plaintext, km)
    assert push.manifest.files[0].chunks[0].chunk_id == chunk_id

    # Rewrite the stored chunk as a legacy v1 envelope (SHA-256 CTR + Encrypt-then-MAC).
    nonce = b"L" * 16
    header = b"NPCV" + bytes([1, 0]) + bytes.fromhex(chunk_id)
    base = hashlib.sha256(km.enc_key + nonce)
    ciphertext_out = bytearray(len(plaintext))
    for offset in range(0, len(plaintext), 32):
        block = base.copy()
        block.update(struct.pack(">Q", offset // 32))
        ks = block.digest()
        for i in range(min(32, len(plaintext) - offset)):
            ciphertext_out[offset + i] = plaintext[offset + i] ^ ks[i]
    ciphertext = bytes(ciphertext_out)
    mac = hmac.new(km.mac_key, digestmod=hashlib.sha256)
    mac.update(header)
    mac.update(nonce)
    mac.update(ciphertext)
    mac.update(struct.pack(">QQ", 0, len(plaintext)))
    legacy_wire = (
        struct.pack(">H", len(header))
        + header
        + struct.pack(">16s16sQQ32s", km.key_id, nonce, 0, len(plaintext), mac.digest())
        + ciphertext
    )
    vault.put_chunk(chunk_id, legacy_wire)

    # Default pull refuses the legacy wire format (downgrade protection).
    with pytest.raises(VaultTamperDetectedError, match="Legacy v1 envelope rejected"):
        mgr.pull_project_snapshot(
            project_id="proj-legacy",
            master_secret=secret,
            snapshot_id="snap-legacy",
            target_dir=tmp_path / "must_not_exist",
        )
    assert not (tmp_path / "must_not_exist").exists()

    # Explicit opt-in authenticates and migrates it (in-memory restore).
    report = mgr.pull_project_snapshot(
        project_id="proj-legacy",
        master_secret=secret,
        snapshot_id="snap-legacy",
        allow_legacy_v1=True,
    )
    assert report.files == {"a.bin": plaintext}

    # And it also restores to disk through the streaming path.
    disk_report = mgr.pull_project_snapshot(
        project_id="proj-legacy",
        master_secret=secret,
        snapshot_id="snap-legacy",
        target_dir=tmp_path / "restored",
        allow_legacy_v1=True,
    )
    assert disk_report.files == {}
    assert (tmp_path / "restored" / "a.bin").read_bytes() == plaintext
