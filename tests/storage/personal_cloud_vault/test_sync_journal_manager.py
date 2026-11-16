"""Tests for PersonalCloudVaultManager: deduplication, conflicts, .nagarvault, and GC."""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus_ai_agent.storage.personal_cloud_vault import (
    BYOSStorageDriver,
    LocalDeviceVaultDriver,
    PersonalCloudVaultManager,
    PersonalVaultError,
    PortableBundleVaultDriver,
    VaultAuthenticationError,
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

    def put_chunk(self, chunk_id: str, data: bytes) -> None:
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
    mgr = PersonalCloudVaultManager(vault, chunk_size=64)
    secret = "gc-secret"

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

    # Default GC keeps both snap-1 and snap-2 active -> deletes only orphan_cid
    gc1 = mgr.garbage_collect_unreferenced_chunks()
    assert gc1.deleted_chunk_ids == (orphan_cid,)
    assert gc1.deleted_count == 1
    assert not vault.has_chunk(orphan_cid)

    # Prune snap-1 by keeping only active_snapshot_ids=["snap-2"] -> deletes B, retains A & C
    gc2 = mgr.garbage_collect_unreferenced_chunks(active_snapshot_ids=[r2.snapshot_id])
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
    mgr_min = PersonalCloudVaultManager(spy_minimal, chunk_size=64)
    dangling = "e" * 64
    spy_minimal.put_chunk(dangling, b"orphan")
    mgr_min.record_orphan_candidate_chunk(dangling)
    gc_min = mgr_min.garbage_collect_unreferenced_chunks(candidate_chunk_ids=["d" * 64])
    assert gc_min.deleted_chunk_ids == (dangling,)


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

    # 5) Source file read OSError during directory collection
    unreadable_src = tmp_path / "unreadable_src"
    unreadable_src.mkdir()
    (unreadable_src / "doc.bin").write_bytes(b"data")
    orig_read = Path.read_bytes

    def _fail_doc_read(self_path: Path) -> bytes:
        if self_path.name == "doc.bin":
            raise OSError("disk read error")
        return orig_read(self_path)

    monkeypatch.setattr(Path, "read_bytes", _fail_doc_read)
    with pytest.raises(VaultTransportError):
        mgr.push_project_snapshot(
            project_id="proj-io",
            source=unreadable_src,
            master_secret=secret,
        )
