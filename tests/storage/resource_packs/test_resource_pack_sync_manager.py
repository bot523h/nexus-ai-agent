"""Login gate, atomic installs, live progress, delta upgrades and pruning."""

from __future__ import annotations

import errno
import hashlib
import importlib
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest
from resource_pack_testkit import (
    FakeClock,
    PackMirror,
    blob,
    core_seed_entries,
    range_http_server,
    tree_snapshot,
)

from nexus_ai_agent.storage.resource_packs import (
    GateState,
    HttpRangePackSource,
    InsufficientStorageError,
    IntegrityError,
    LocalDirectoryPackSource,
    PackLeaseError,
    PackLockTimeoutError,
    PackSyncError,
    PackSyncManager,
    PackVerificationError,
    RangeFetchInterrupted,
    ResourceBlobEntry,
    ResourcePackManifest,
    ResourcePackSyncManager,
    SignatureVerificationError,
    SyncProgress,
    SyncStage,
    UnknownPackError,
    VersionRollbackError,
    default_blob_key,
    parse_semantic_version,
)
from nexus_ai_agent.storage.resource_packs import pack_sync_manager as psm

PLENTY = 10**12


def _manager(
    root: Path,
    catalog: list[ResourcePackManifest],
    mirror: PackMirror,
    *,
    free: int = PLENTY,
    **kwargs: object,
) -> tuple[ResourcePackSyncManager, LocalDirectoryPackSource]:
    source = LocalDirectoryPackSource(mirror.root)
    kwargs.setdefault("allow_unsigned", True)  # keyless fixtures; F1 default is covered below
    manager = ResourcePackSyncManager(
        root,
        catalog,
        source,
        disk_free_probe=lambda _: free,
        clock=FakeClock(),
        chunk_size=4096,
        **kwargs,  # type: ignore[arg-type]
    )
    return manager, source


def _core(mirror: PackMirror, version: str = "1.0.0") -> ResourcePackManifest:
    remote = mirror.add_remote("audio/room_tone.ogg", blob("room-tone", 20_000))
    return mirror.manifest("core", version, [remote, *core_seed_entries()])


def _std_versions(mirror: PackMirror) -> tuple[ResourcePackManifest, ResourcePackManifest]:
    files = {f"tex/{i}.ktx2": blob(f"std-{i}", 3000 + i) for i in range(5)}
    v1 = mirror.manifest(
        "standard",
        "1.0.0",
        [mirror.add_remote(p, d) for p, d in files.items()],
        tier="standard",
        title_fa="بسته استاندارد",
    )
    files["tex/2.ktx2"] = blob("std-2-v2", 3500)
    v2 = mirror.manifest(
        "standard",
        "1.1.0",
        [mirror.add_remote(p, d) for p, d in files.items()],
        tier="standard",
        title_fa="بسته استاندارد",
    )
    return v1, v2


def test_fresh_device_shows_the_pubg_style_gate(mirror: PackMirror, tmp_path: Path) -> None:
    core = _core(mirror)
    std_v1, _ = _std_versions(mirror)
    manager, _ = _manager(tmp_path / "dev", [std_v1, core], mirror)
    gate = manager.get_login_gate_status()
    assert gate.show_resource_gate and gate.state is GateState.INSTALL_REQUIRED
    assert gate.pending_pack_ids == ("core",)
    assert [p.pack_id for p in gate.packs] == ["core", "standard"]  # core first
    assert gate.total_download_bytes == 20_000
    assert gate.total_hydrate_bytes == sum(e.size_bytes for e in core.seed_entries)
    assert "منابع ضروری" in gate.message_fa
    assert not (tmp_path / "dev").exists()  # looking never writes


def test_core_sync_hydrates_downloads_reports_progress_and_opens_the_gate(
    mirror: PackMirror, tmp_path: Path
) -> None:
    core = _core(mirror)
    manager, source = _manager(tmp_path / "dev", [core], mirror)
    events: list[SyncProgress] = []
    result = manager.sync_pack("core", events.append)

    assert result.downloaded_bytes == 20_000
    assert result.hydrated_bytes == sum(e.size_bytes for e in core.seed_entries)
    assert result.verification.verified_files == 4 and not result.already_up_to_date
    assert result.install_dir == manager.active_install_dir("core")
    assert manager.installed_manifest("core") == core
    assert len(source.requested_offsets) == 1  # only the remote blob hit the network

    stages = [e.stage for e in events]
    for stage in SyncStage:
        assert stage in stages
    assert stages[0] is SyncStage.PLANNING and stages[-1] is SyncStage.COMPLETED
    percents = [e.percent for e in events]
    assert percents == sorted(percents) and percents[-1] == 100.0
    final = events[-1]
    assert (
        final.done_bytes
        == final.total_bytes
        == result.plan.network_bytes + (result.plan.hydrate_bytes)
    )
    assert final.files_done == final.files_total == 4
    assert any(e.speed_bytes_per_second > 0 for e in events)
    assert any(e.current_path == "audio/room_tone.ogg" for e in events)
    assert all(e.message_fa for e in events)

    gate = manager.get_login_gate_status()
    assert gate.state is GateState.READY and not gate.show_resource_gate
    assert gate.packs[0].installed_version == "1.0.0"


def test_second_sync_is_a_verified_noop(mirror: PackMirror, tmp_path: Path) -> None:
    core = _core(mirror)
    manager, source = _manager(tmp_path / "dev", [core], mirror)
    first = manager.sync_pack("core")
    events: list[SyncProgress] = []
    again = manager.sync_pack("core", events.append)
    assert again.already_up_to_date and again.install_dir == first.install_dir
    assert len(source.requested_offsets) == 1
    assert events[-1].stage is SyncStage.COMPLETED


def test_delta_upgrade_downloads_only_the_changed_file(mirror: PackMirror, tmp_path: Path) -> None:
    v1, v2 = _std_versions(mirror)
    root = tmp_path / "dev"
    manager_v1, _ = _manager(root, [v1], mirror)
    old = manager_v1.sync_pack("standard")

    manager_v2, source = _manager(root, [v2], mirror)
    gate = manager_v2.get_login_gate_status()
    assert gate.packs[0].state is GateState.UPDATE_REQUIRED
    assert not gate.show_resource_gate  # optional tier never blocks login
    assert gate.packs[0].download_bytes == 3500

    result = manager_v2.sync_pack("standard")
    changed = next(e for e in v2.entries if e.relative_path == "tex/2.ktx2")
    assert source.requested_offsets == [(default_blob_key(changed), 0)]
    assert result.reused_files == 4 and result.downloaded_bytes == 3500
    assert result.plan.from_version == "1.0.0" and result.plan.to_version == "1.1.0"
    assert old.install_dir.is_dir()  # previous version kept until pruned
    assert manager_v2.active_install_dir("standard") == result.install_dir


def test_insufficient_storage_raises_before_a_single_byte(
    mirror: PackMirror, tmp_path: Path
) -> None:
    core = _core(mirror)
    root = tmp_path / "dev"
    manager, source = _manager(root, [core], mirror, free=core.min_free_space_bytes - 1)
    events: list[SyncProgress] = []
    with pytest.raises(InsufficientStorageError) as info:
        manager.sync_pack("core", events.append)
    assert info.value.required_bytes == core.min_free_space_bytes
    assert info.value.available_bytes == core.min_free_space_bytes - 1
    assert not root.exists() and source.requested_offsets == []
    assert [e.stage for e in events] == [SyncStage.PLANNING]
    gate = manager.get_login_gate_status()
    assert gate.state is GateState.BLOCKED_INSUFFICIENT_STORAGE
    assert "فضای خالی" in gate.message_fa


def test_failed_upgrade_never_damages_the_healthy_version(
    mirror: PackMirror, tmp_path: Path
) -> None:
    v1, v2 = _std_versions(mirror)
    root = tmp_path / "dev"
    manager_v1, _ = _manager(root, [v1], mirror)
    healthy = manager_v1.sync_pack("standard")
    before = tree_snapshot(healthy.install_dir)
    pointer_before = (root / "packs/standard/active.json").read_bytes()

    changed = next(e for e in v2.entries if e.relative_path == "tex/2.ktx2")
    (mirror.root / default_blob_key(changed)).write_bytes(blob("poison", 3500))
    manager_v2, _ = _manager(root, [v2], mirror)
    with pytest.raises(IntegrityError):
        manager_v2.sync_pack("standard")

    assert (root / "packs/standard/active.json").read_bytes() == pointer_before
    assert tree_snapshot(healthy.install_dir) == before
    assert manager_v2.installed_manifest("standard") == v1
    remaining = sorted(p.name for p in (root / "packs/standard/versions").iterdir())
    name = healthy.install_dir.name
    assert remaining == [name, f"{name}.manifest.json"]  # no half-installed v2 left behind


def test_failure_after_staging_cleans_up_and_keeps_the_pointer(
    mirror: PackMirror, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    v1, v2 = _std_versions(mirror)
    root = tmp_path / "dev"
    _manager(root, [v1], mirror)[0].sync_pack("standard")
    pointer_before = (root / "packs/standard/active.json").read_bytes()

    real_replace = os.replace

    def crash_on_pointer(src: object, dst: object) -> None:
        if str(dst).endswith("active.json"):
            raise OSError("power loss")
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", crash_on_pointer)
    manager_v2, _ = _manager(root, [v2], mirror)
    with pytest.raises(OSError, match="power loss"):
        manager_v2.sync_pack("standard")
    monkeypatch.undo()
    assert (root / "packs/standard/active.json").read_bytes() == pointer_before
    versions = sorted(p.name for p in (root / "packs/standard/versions").iterdir())
    assert len(versions) == 2  # only the healthy v1 tree and its manifest remain
    assert list((root / "packs/standard/staging").iterdir()) == []


def test_real_http_resume_across_two_sync_calls(mirror: PackMirror, tmp_path: Path) -> None:
    size = 300_000
    big = mirror.add_remote("cinema/hero_skin_8k.ktx2", blob("8k", size))
    hd = mirror.manifest("cinema_hd", "1.0.0", [big], tier="cinema_hd", title_fa="سینمایی")
    root = tmp_path / "dev"
    with range_http_server(mirror.root) as server:
        manager = ResourcePackSyncManager(
            root,
            [hd],
            HttpRangePackSource(server.base_url),
            allow_unsigned=True,
            disk_free_probe=lambda _: PLENTY,
            max_attempts=1,
        )
        server.cut_after_bytes = size // 2
        with pytest.raises(RangeFetchInterrupted):
            manager.sync_pack("cinema_hd")
        part = manager.store.part_path(big.sha256)
        assert part.stat().st_size == size // 2
        assert manager.active_install_dir("cinema_hd") is None

        events: list[SyncProgress] = []
        result = manager.sync_pack("cinema_hd", events.append)
    assert server.range_headers == [None, f"bytes={size // 2}-"]
    assert result.downloaded_bytes == size - size // 2
    downloading = [e for e in events if e.stage is SyncStage.DOWNLOADING]
    assert downloading[0].done_bytes == size // 2  # resumed bytes count as done
    assert (result.install_dir / big.relative_path).read_bytes() == blob("8k", size)


@pytest.mark.parametrize("victim", ["audio/room_tone.ogg", "luts/teal_orange.cube"])
def test_rotted_install_is_detected_and_self_healed(
    mirror: PackMirror, tmp_path: Path, victim: str
) -> None:
    core = _core(mirror)
    manager, _ = _manager(tmp_path / "dev", [core], mirror)
    result = manager.sync_pack("core")
    target = result.install_dir / victim
    data = bytearray(target.read_bytes())
    data[0] ^= 0xFF
    target.write_bytes(bytes(data))  # hard-linked: the CAS object rots too
    assert manager.get_login_gate_status().state is GateState.REPAIR_REQUIRED

    repaired = manager.sync_pack("core")
    assert not repaired.already_up_to_date
    assert repaired.downloaded_bytes + repaired.hydrated_bytes > 0
    assert manager.get_login_gate_status().state is GateState.READY


def test_copy_mode_and_hardlink_fallback(
    mirror: PackMirror, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = _core(mirror)
    manager, _ = _manager(tmp_path / "copy", [core], mirror, prefer_hardlinks=False)
    installed = manager.sync_pack("core").install_dir / "audio/room_tone.ogg"
    assert installed.stat().st_nlink == 1

    def no_links(src: object, dst: object) -> None:
        raise OSError("cross-device link")

    monkeypatch.setattr(os, "link", no_links)
    fallback, _ = _manager(tmp_path / "fallback", [core], mirror)
    assert fallback.sync_pack("core").verification.verified_files == 4


def test_corrupt_pointer_means_repair_and_blocks_pruning(
    mirror: PackMirror, tmp_path: Path
) -> None:
    core = _core(mirror)
    root = tmp_path / "dev"
    manager, _ = _manager(root, [core], mirror)
    manager.sync_pack("core")
    pointer = root / "packs/core/active.json"
    for garbage in (b"{not json", json.dumps({"schema": 1}).encode()):
        pointer.write_bytes(garbage)
        assert manager.get_login_gate_status().state is GateState.REPAIR_REQUIRED
        with pytest.raises(PackSyncError):
            manager.prune_old_versions("core")
    traversal = {
        "schema": 1,
        "pack_id": "core",
        "version": "1.0.0",
        "merkle_root": core.merkle_root,
        "install_dir": "../../escape",
    }
    pointer.write_bytes(json.dumps(traversal).encode())
    with pytest.raises(PackSyncError):
        manager.active_install_dir("core")
    with pytest.raises(VersionRollbackError, match="corrupt or unreadable"):
        manager.sync_pack("core")
    # Explicit recovery (allow_rollback=True) rebuilds from the CAS, no re-download
    recovery, _ = _manager(root, [core], mirror, allow_rollback=True)
    rebuilt = recovery.sync_pack("core")
    assert manager.get_login_gate_status().state is GateState.READY
    assert rebuilt.downloaded_bytes == 0 and rebuilt.reused_files == 4


def test_corrupt_active_json_fails_closed_on_rollback_check(
    mirror: PackMirror, tmp_path: Path
) -> None:
    v1, v2 = _std_versions(mirror)
    root = tmp_path / "dev"
    manager, _ = _manager(root, [v1], mirror)
    manager.sync_pack("standard")
    pointer = root / "packs/standard/active.json"
    upgrader, _ = _manager(root, [v2], mirror)
    for corrupt in (
        b"{not json",
        json.dumps({"schema": 1, "pack_id": "standard"}).encode(),
        json.dumps(
            {
                "schema": 1,
                "pack_id": "standard",
                "merkle_root": v1.merkle_root,
                "install_dir": "1.0.0__abc",
            }
        ).encode(),
    ):
        pointer.write_bytes(corrupt)
        with pytest.raises(VersionRollbackError, match="corrupt or unreadable"):
            upgrader.sync_pack("standard")
    assert list((root / "leases").iterdir()) == []


def test_sync_pack_progress_callback_is_reentrant_without_deadlock(
    mirror: PackMirror, tmp_path: Path
) -> None:
    core = _core(mirror)
    v1, _ = _std_versions(mirror)
    root = tmp_path / "dev"
    manager, _ = _manager(root, [core, v1], mirror)
    manager.sync_pack("core")
    reentrant_calls: list[SyncStage] = []

    def callback(event: SyncProgress) -> None:
        reentrant_calls.append(event.stage)
        if event.stage is SyncStage.VERIFYING:
            # Re-enter the same manager instance on the same thread (RLock must not deadlock)
            status = manager.get_login_gate_status()
            assert status.state is GateState.READY
            assert any(
                p.pack_id == "standard" and p.state is GateState.INSTALL_REQUIRED
                for p in status.packs
            )
            assert manager.sync_pack("core").already_up_to_date
            assert manager.collect_garbage(min_age_seconds=0.0) == ()

    result = manager.sync_pack("standard", callback)
    assert result.verification.verified_files == len(v1.entries)
    assert SyncStage.VERIFYING in reentrant_calls
    assert manager.get_login_gate_status().state is GateState.READY


def test_pointer_manifest_mismatch_is_detected(mirror: PackMirror, tmp_path: Path) -> None:
    core = _core(mirror)
    root = tmp_path / "dev"
    manager, _ = _manager(root, [core], mirror)
    manager.sync_pack("core")
    pointer = root / "packs/core/active.json"
    raw = json.loads(pointer.read_bytes())
    raw["merkle_root"] = "0" * 64
    pointer.write_bytes(json.dumps(raw).encode())
    with pytest.raises(PackSyncError, match="does not match"):
        manager.installed_manifest("core")
    raw["install_dir"] = "missing-dir"
    pointer.write_bytes(json.dumps(raw).encode())
    with pytest.raises(PackSyncError, match="unreadable"):
        manager.installed_manifest("core")


def test_prune_keeps_only_the_active_version_and_gc_frees_old_blobs(
    mirror: PackMirror, tmp_path: Path
) -> None:
    v1, v2 = _std_versions(mirror)
    root = tmp_path / "dev"
    old = _manager(root, [v1], mirror)[0].sync_pack("standard")
    manager, _ = _manager(root, [v2], mirror)
    new = manager.sync_pack("standard")
    (root / "packs/standard/staging/leftover").mkdir(parents=True)

    assert manager.prune_old_versions("standard") == (old.install_dir.name,)
    assert not old.install_dir.exists() and new.install_dir.is_dir()
    remaining = sorted(p.name for p in (root / "packs/standard/versions").iterdir())
    assert remaining == [new.install_dir.name, f"{new.install_dir.name}.manifest.json"]
    assert not (root / "packs/standard/staging").exists()
    assert manager.get_login_gate_status().packs[0].state is GateState.READY

    stale = next(e for e in v1.entries if e.relative_path == "tex/2.ktx2")
    assert manager.collect_garbage(min_age_seconds=0.0) == (stale.sha256,)
    assert manager.prune_old_versions("standard") == ()


def test_prune_and_gc_without_installs(mirror: PackMirror, tmp_path: Path) -> None:
    manager, _ = _manager(tmp_path / "dev", [_core(mirror)], mirror)
    assert manager.prune_old_versions("core") == ()
    assert manager.collect_garbage(min_age_seconds=0.0) == ()
    (tmp_path / "dev/packs/core/staging").mkdir(parents=True)  # interrupted, never activated
    assert manager.collect_garbage(min_age_seconds=0.0) == ()
    assert manager.prune_old_versions("core") == ()
    assert not (tmp_path / "dev/packs/core/staging").exists()
    with pytest.raises(UnknownPackError):
        manager.prune_old_versions("../etc")


def test_catalog_and_lookup_errors(mirror: PackMirror, tmp_path: Path) -> None:
    core = _core(mirror)
    with pytest.raises(ValueError, match="duplicate pack_id"):
        _manager(tmp_path / "dev", [core, core], mirror)
    manager, _ = _manager(tmp_path / "dev", [core], mirror)
    with pytest.raises(UnknownPackError):
        manager.sync_pack("missing")


def test_tampered_catalog_is_refused_before_any_download(
    mirror: PackMirror, tmp_path: Path
) -> None:
    core = _core(mirror)
    forged_entry = ResourceBlobEntry("audio/room_tone.ogg", "f" * 64, 20_000, "remote_blob")
    forged = replace(core, entries=(forged_entry, *core.entries[1:]))
    root = tmp_path / "dev"
    manager, source = _manager(root, [forged], mirror)
    with pytest.raises(PackVerificationError, match="merkle_root"):
        manager.sync_pack("core")
    with pytest.raises(PackVerificationError):
        manager.get_login_gate_status()
    assert source.requested_offsets == [] and not root.exists()


def test_default_disk_probe_walks_up_to_an_existing_directory(tmp_path: Path) -> None:
    free = psm._default_disk_free(tmp_path / "not" / "yet" / "created")
    assert isinstance(free, int) and free > 0


# --------------------------------------------------------------------------
# F1 — signatures are mandatory unless explicitly waived
# --------------------------------------------------------------------------


def test_default_manager_without_a_key_refuses_before_any_write(
    mirror: PackMirror, tmp_path: Path
) -> None:
    root = tmp_path / "dev"
    source = LocalDirectoryPackSource(mirror.root)
    manager = ResourcePackSyncManager(root, [_core(mirror)], source)  # all defaults
    assert manager.allow_unsigned is False and manager.trusted_public_key is None
    with pytest.raises(SignatureVerificationError, match="allow_unsigned=True"):
        manager.sync_pack("core")
    with pytest.raises(SignatureVerificationError):
        manager.get_login_gate_status()
    assert source.requested_offsets == [] and not root.exists()
    assert PackSyncManager is ResourcePackSyncManager


def test_policy_flags_are_validated(mirror: PackMirror, tmp_path: Path) -> None:
    source = LocalDirectoryPackSource(mirror.root)
    with pytest.raises(TypeError):
        ResourcePackSyncManager(tmp_path, [], source, allow_unsigned=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ResourcePackSyncManager(tmp_path, [], source, allow_rollback=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="lock_backend"):
        ResourcePackSyncManager(tmp_path, [], source, lock_backend="nfs")  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# F4 — anti-rollback
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ("1.10.2", (1, 10, 2)),
        ("0.0.0", (0, 0, 0)),
        ("01.0.0", None),
        ("1.0", None),
        ("1.0.0-rc1", None),
        ("v1.0.0", None),
    ],
)
def test_semantic_version_parser(text: str, parsed: tuple[int, int, int] | None) -> None:
    assert parse_semantic_version(text) == parsed


def test_downgrade_is_refused_unless_explicitly_allowed(mirror: PackMirror, tmp_path: Path) -> None:
    v1, v2 = _std_versions(mirror)
    root = tmp_path / "dev"
    _manager(root, [v2], mirror)[0].sync_pack("standard")
    before = tree_snapshot(root)
    downgrade, source = _manager(root, [v1], mirror)
    with pytest.raises(VersionRollbackError, match="older than the active"):
        downgrade.sync_pack("standard")
    assert tree_snapshot(root) == before and source.requested_offsets == []
    rolled = _manager(root, [v1], mirror, allow_rollback=True)[0].sync_pack("standard")
    assert rolled.version == "1.0.0"


def test_non_semantic_versions_cannot_prove_monotonicity(
    mirror: PackMirror, tmp_path: Path
) -> None:
    v1, _ = _std_versions(mirror)
    root = tmp_path / "dev"
    _manager(root, [replace(v1, version="2024-rc")], mirror, allow_rollback=True)[0].sync_pack(
        "standard"
    )
    with pytest.raises(VersionRollbackError, match="not comparable"):
        _manager(root, [v1], mirror)[0].sync_pack("standard")


def test_newer_version_landing_mid_sync_wins_over_an_older_flip(
    mirror: PackMirror, tmp_path: Path
) -> None:
    v1, v2 = _std_versions(mirror)
    extra = mirror.add_remote("tex/extra.ktx2", blob("std-extra", 1234))
    v3 = mirror.manifest("standard", "1.2.0", [*v2.entries, extra], tier="standard")
    root = tmp_path / "dev"
    _manager(root, [v1], mirror)[0].sync_pack("standard")
    newer, _ = _manager(root, [v3], mirror)

    def race(event: SyncProgress) -> None:
        if event.stage is SyncStage.ACTIVATING:  # callbacks run outside the storage lock
            newer.sync_pack("standard")

    slow, _ = _manager(root, [v2], mirror)
    with pytest.raises(VersionRollbackError, match="1.2.0 -> 1.1.0"):
        slow.sync_pack("standard", race)
    assert slow.installed_manifest("standard") == v3
    assert not any(p.name.startswith("1.1.0") for p in (root / "packs/standard/versions").iterdir())
    assert list((root / "leases").iterdir()) == []


# --------------------------------------------------------------------------
# F2 — cross-process lock, leases and safe garbage collection
# --------------------------------------------------------------------------

_GC_IN_ANOTHER_PROCESS = """
import json, sys
from pathlib import Path
from nexus_ai_agent.storage.resource_packs import LocalDirectoryPackSource, PackSyncManager
root, mirror = Path(sys.argv[1]), Path(sys.argv[2])
manager = PackSyncManager(root, [], LocalDirectoryPackSource(mirror), lock_timeout_seconds=10.0)
gc = manager.collect_garbage(min_age_seconds=0.0)
pruned = manager.prune_old_versions("standard")
print(json.dumps({"gc": list(gc), "pruned": list(pruned)}))
"""


def _orphan(manager: ResourcePackSyncManager, label: str) -> str:
    data = blob(label, 777)
    sha = hashlib.sha256(data).hexdigest()
    manager.store.put_bytes(data, sha)
    return sha


def test_gc_from_another_process_mid_staging_never_loses_in_flight_blobs(
    mirror: PackMirror, tmp_path: Path
) -> None:
    v1, v2 = _std_versions(mirror)
    root = tmp_path / "dev"
    old = _manager(root, [v1], mirror)[0].sync_pack("standard")
    manager, _ = _manager(root, [v2], mirror)
    orphan = _orphan(manager, "nobody-needs-me")
    fresh = next(e for e in v2.entries if e.relative_path == "tex/2.ktx2")
    seen: dict[str, object] = {}
    src_dir = Path(importlib.import_module("nexus_ai_agent").__file__ or "").parents[1]

    def hostile_gc(event: SyncProgress) -> None:
        # VERIFYING: every CAS write and the whole staging tree are done, the
        # active.json flip has not happened yet — the most dangerous moment.
        if event.stage is not SyncStage.VERIFYING or seen:
            return
        (lease_path,) = (root / "leases").iterdir()
        seen["lease"] = json.loads(lease_path.read_bytes())
        assert manager.store.has(fresh.sha256)  # downloaded, referenced by no active pack
        completed = subprocess.run(
            [sys.executable, "-c", _GC_IN_ANOTHER_PROCESS, str(root), str(mirror.root)],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
            env={**os.environ, "PYTHONPATH": str(src_dir)},
        )
        seen["other"] = json.loads(completed.stdout)

    result = manager.sync_pack("standard", hostile_gc)

    lease = seen["lease"]
    assert isinstance(lease, dict)
    assert lease["pid"] == os.getpid() and lease["pack_id"] == "standard"
    assert lease["install_dir"] == result.install_dir.name
    assert set(lease["digests"]) == {e.sha256 for e in v2.entries}
    assert lease["expires_at"] > time.time()
    assert seen["other"] == {"gc": [orphan], "pruned": []}  # GC ran, but only on true garbage
    assert manager.get_login_gate_status().state is GateState.READY
    assert result.verification.verified_files == len(v2.entries)
    assert old.install_dir.is_dir()  # still there: pruning is a separate, explicit step
    assert list((root / "leases").iterdir()) == []  # the lease is released afterwards


def test_interrupted_flip_keeps_previous_version_and_gc_spares_it(
    mirror: PackMirror, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    v1, v2 = _std_versions(mirror)
    root = tmp_path / "dev"
    first, _ = _manager(root, [v1], mirror)
    first.sync_pack("standard")
    real_replace = os.replace
    gc_results: list[tuple[str, ...]] = []
    janitor, _ = _manager(root, [], mirror)

    def gc_then_crash(src: object, dst: object) -> None:
        if str(dst).endswith("active.json"):
            raise OSError("killed between staging and the pointer flip")
        real_replace(src, dst)  # type: ignore[arg-type]

    def gc_mid_staging(event: SyncProgress) -> None:
        if event.stage is SyncStage.STAGING:
            gc_results.append(janitor.collect_garbage(min_age_seconds=0.0))

    monkeypatch.setattr(os, "replace", gc_then_crash)
    with pytest.raises(OSError, match="killed"):
        _manager(root, [v2], mirror)[0].sync_pack("standard", gc_mid_staging)
    monkeypatch.undo()

    assert gc_results == [()]  # the lease protected every freshly downloaded blob
    assert first.installed_manifest("standard") == v1
    assert first.get_login_gate_status().state is GateState.READY
    assert list((root / "leases").iterdir()) == []
    changed = next(e for e in v2.entries if e.relative_path == "tex/2.ktx2")
    assert janitor.collect_garbage(min_age_seconds=0.0) == (changed.sha256,)  # now unleased
    assert first.get_login_gate_status().state is GateState.READY


def _write_lease(root: Path, name: str, **overrides: object) -> Path:
    record: dict[str, object] = {
        "schema": 1,
        "pid": 4242,
        "pack_id": "standard",
        "install_dir": "9.9.9__feedface0000",
        "expires_at": time.time() + 3600,
        "digests": [],
    }
    record.update(overrides)
    path = root / "leases" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record))
    return path


def test_gc_age_gate_leases_and_protected_digests(mirror: PackMirror, tmp_path: Path) -> None:
    root = tmp_path / "dev"
    manager, _ = _manager(root, [], mirror)
    young = _orphan(manager, "young")
    assert manager.collect_garbage() == ()  # default 1 h grace period
    leased, protected = _orphan(manager, "leased"), _orphan(manager, "protected")
    _write_lease(root, "1-live.json", expires_at=time.time() + 86_400, digests=[leased])
    expired = _write_lease(root, "2-dead.json", expires_at=time.time() - 1, digests=[young])
    (root / "leases" / ".3-partial.json.tmp").write_text("{half")  # in-progress write: ignored
    later, _ = _manager(root, [], mirror, wall_clock=lambda: time.time() + 7200)
    assert later.collect_garbage(protected_digests={protected}) == (young,)
    assert not expired.exists()  # a crashed owner's lease is reclaimed
    assert manager.store.has(leased) and manager.store.has(protected)
    with pytest.raises(ValueError):
        manager.collect_garbage(min_age_seconds=-1)
    with pytest.raises(ValueError):
        manager.collect_garbage(min_age_seconds=float("nan"))
    with pytest.raises(TypeError):
        manager.collect_garbage(protected_digests=leased)


@pytest.mark.parametrize(
    "content",
    [
        b"{not json",
        json.dumps([1, 2]).encode(),
        json.dumps({"schema": 1}).encode(),
        b"BAD-DIGEST",
        b"BAD-EXPIRY",
        b"BAD-DIR",
    ],
)
def test_corrupt_lease_makes_gc_and_prune_delete_nothing(
    mirror: PackMirror, tmp_path: Path, content: bytes
) -> None:
    v1, v2 = _std_versions(mirror)
    root = tmp_path / "dev"
    _manager(root, [v1], mirror)[0].sync_pack("standard")
    manager, _ = _manager(root, [v2], mirror)
    manager.sync_pack("standard")
    orphan = _orphan(manager, "orphan")
    if content == b"BAD-DIGEST":
        _write_lease(root, "x.json", digests=["not-a-digest"])
    elif content == b"BAD-EXPIRY":
        _write_lease(root, "x.json", expires_at=True)
    elif content == b"BAD-DIR":
        _write_lease(root, "x.json", install_dir="../../escape")
    else:
        (root / "leases").mkdir(exist_ok=True)
        (root / "leases" / "x.json").write_bytes(content)
    before = tree_snapshot(root / "cas"), tree_snapshot(root / "packs")
    with pytest.raises(PackLeaseError):
        manager.collect_garbage(min_age_seconds=0.0)
    with pytest.raises(PackLeaseError):
        manager.prune_old_versions("standard")
    assert (tree_snapshot(root / "cas"), tree_snapshot(root / "packs")) == before
    assert manager.store.has(orphan)


def test_prune_spares_directories_owned_by_a_live_lease(mirror: PackMirror, tmp_path: Path) -> None:
    v1, _ = _std_versions(mirror)
    root = tmp_path / "dev"
    manager, _ = _manager(root, [v1], mirror)
    manager.sync_pack("standard")
    owned = "9.9.9__feedface0000"
    for parent in ("staging", "versions"):
        (root / "packs/standard" / parent / owned).mkdir(parents=True)
    (root / "packs/standard/versions" / f"{owned}.manifest.json").write_text("{}")
    lease = _write_lease(root, "1-live.json", install_dir=owned)
    _write_lease(root, "2-other-pack.json", pack_id="core", install_dir="1.0.0__abc")
    assert manager.prune_old_versions("standard") == ()
    assert (root / "packs/standard/staging" / owned).is_dir()
    assert (root / "packs/standard/versions" / f"{owned}.manifest.json").is_file()
    lease.unlink()
    assert manager.prune_old_versions("standard") == (owned,)
    assert not (root / "packs/standard/staging").exists()


def test_flock_contention_times_out_without_deleting(mirror: PackMirror, tmp_path: Path) -> None:
    root = tmp_path / "dev"
    holder, _ = _manager(root, [], mirror)
    orphan = _orphan(holder, "orphan")
    waiter, _ = _manager(root, [], mirror, lock_timeout_seconds=0.1)
    with holder._storage_lock():
        assert (root / ".sync_gc.lock").is_file()
        with pytest.raises(PackLockTimeoutError):
            waiter.collect_garbage(min_age_seconds=0.0)
    assert holder.store.has(orphan)
    assert waiter.collect_garbage(min_age_seconds=0.0) == (orphan,)  # released on exit


def test_exclusive_file_lock_fallback(mirror: PackMirror, tmp_path: Path) -> None:
    v1, _ = _std_versions(mirror)
    root = tmp_path / "dev"
    manager, _ = _manager(
        root, [v1], mirror, lock_backend="exclusive_file", lock_timeout_seconds=0.1
    )
    manager.sync_pack("standard")
    lock_file = root / ".sync_gc.lock.excl"
    assert not lock_file.exists()  # released after every critical section
    orphan = _orphan(manager, "orphan")

    lock_file.write_text(
        '{"pid": 1, "expires_at": 0}'
    )  # incomplete/legacy record: waits for mtime grace
    with pytest.raises(PackLockTimeoutError):
        manager.collect_garbage(min_age_seconds=0.0)
    stale = time.time() - 3600
    os.utime(lock_file, (stale, stale))  # owner crashed long ago: break the lock
    assert manager.collect_garbage(min_age_seconds=0.0) == (orphan,)
    assert not lock_file.exists()
    lock = manager._storage_lock()
    lock._break_if_stale()  # nothing to break: a no-op, not an error


def test_exclusive_file_lock_token_ownership_and_dead_pid_break(
    mirror: PackMirror, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.storage.resource_packs import pack_sync_manager as psm

    root = tmp_path / "dev"
    now = [1000.0]
    m1, _ = _manager(
        root,
        [],
        mirror,
        lock_backend="exclusive_file",
        lock_timeout_seconds=0.2,
        lock_stale_after_seconds=5.0,
        wall_clock=lambda: now[0],
    )
    m2, _ = _manager(
        root,
        [],
        mirror,
        lock_backend="exclusive_file",
        lock_timeout_seconds=0.2,
        lock_stale_after_seconds=5.0,
        wall_clock=lambda: now[0],
    )
    lock_file = root / ".sync_gc.lock.excl"

    # 1. Owner writes pid, expires_at, owner_token; advancing wall_clock past TTL breaks it,
    #    and stale owner's __exit__ never unlinks the new owner's lock!
    with m1._storage_lock() as l1:
        record = json.loads(lock_file.read_text())
        assert record["pid"] == os.getpid()
        assert isinstance(record["owner_token"], str) and ":" in record["owner_token"]
        assert l1._owner_token == record["owner_token"]
        assert l1._st_ino == os.lstat(lock_file).st_ino
        now[0] += 10.0  # expire l1
        with m2._storage_lock() as l2:
            record2 = json.loads(lock_file.read_text())
            assert record2["owner_token"] == l2._owner_token != l1._owner_token
        assert not lock_file.exists()
        # Re-create a live lock owned by m2 while l1 is still in scope: l1 must NOT delete m2's lock
        l2_live = m2._storage_lock().__enter__()
    try:
        assert lock_file.exists()
        assert json.loads(lock_file.read_text())["owner_token"] == l2_live._owner_token
    finally:
        l2_live.__exit__(None, None, None)

    # 2. Dead PID (ESRCH) breaks immediately even before expires_at
    dead = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True,
        text=True,
        check=True,
    )
    dead_pid = int(dead.stdout.strip())
    lock_file.write_text(
        json.dumps(
            {"pid": dead_pid, "expires_at": now[0] + 3600, "owner_token": f"{dead_pid}:dead"}
        )
    )
    with m2._storage_lock():
        assert json.loads(lock_file.read_text())["pid"] == os.getpid()

    # 3. _pid_is_dead handles non-positive, PermissionError (alive), and ESRCH
    assert psm._pid_is_dead(0) is True
    monkeypatch.setattr(os, "kill", lambda _pid, _sig: (_ for _ in ()).throw(PermissionError()))
    assert psm._pid_is_dead(99999) is False
    monkeypatch.setattr(os, "kill", lambda _pid, _sig: (_ for _ in ()).throw(ProcessLookupError()))
    assert psm._pid_is_dead(99999) is True
    monkeypatch.undo()

    # 4. Break-guard edge paths: corrupt/vanished break guard, token mismatch, vanished lock
    lock = m2._storage_lock()
    lock._break_path.write_text("{corrupt")
    stale_t = now[0] - 100.0
    os.utime(lock._break_path, (stale_t, stale_t))
    assert lock._try_enter_break_guard() is None  # broke stale guard
    guard = lock._try_enter_break_guard()
    assert guard is not None
    # Swapped guard on disk is never deleted by _exit_break_guard
    lock._break_path.unlink()
    lock._break_path.write_text(
        json.dumps({"pid": os.getpid(), "expires_at": now[0] + 60, "owner_token": "other:1"})
    )
    lock._exit_break_guard(guard)
    assert lock._break_path.exists()
    # Expired break guard owned by a LIVE pid must NEVER be broken
    lock._break_path.unlink()
    lock._break_path.write_text(
        json.dumps({"pid": os.getpid(), "expires_at": now[0] - 10, "owner_token": "live:1"})
    )
    assert lock._try_enter_break_guard() is None
    assert lock._break_path.exists()
    # Stale break guard (expired AND dead pid) that vanishes mid-replace is handled cleanly
    lock._break_path.unlink()
    lock._break_path.write_text(
        json.dumps({"pid": 999_999_999, "expires_at": now[0] - 10, "owner_token": "other:1"})
    )
    real_replace = os.replace

    def vanish_guard(src: object, dst: object) -> None:
        if str(src) == str(lock._break_path):
            raise FileNotFoundError("vanished")
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", vanish_guard)
    assert lock._try_enter_break_guard() is None
    monkeypatch.undo()
    lock._break_path.unlink(missing_ok=True)
    lock._exit_break_guard(guard)  # vanished before _exit_break_guard

    # Fresh break guard blocks a competing _break_if_stale()
    lock_file.write_text("{corrupt")
    os.utime(lock_file, (stale_t, stale_t))
    lock._break_path.write_text(
        json.dumps({"pid": os.getpid(), "expires_at": now[0] + 60, "owner_token": "other:1"})
    )
    lock._break_if_stale()
    assert lock_file.exists()
    lock._break_path.unlink()
    lock_file.unlink()

    # Race check inside _break_if_stale: inode changes or lock vanishes before re-check
    lock_file.write_text("{corrupt")
    os.utime(lock_file, (stale_t, stale_t))
    real_enter = lock._try_enter_break_guard

    def swap_lock_before_recheck(*_args: object, **_kwargs: object) -> tuple[int, str] | None:
        g = real_enter(**_kwargs)  # type: ignore[arg-type]
        lock_file.unlink()
        lock_file.write_text(
            json.dumps({"pid": os.getpid(), "expires_at": now[0] + 60, "owner_token": "fresh:1"})
        )
        return g

    monkeypatch.setattr(lock, "_try_enter_break_guard", swap_lock_before_recheck)
    lock._break_if_stale()
    assert json.loads(lock_file.read_text())["owner_token"] == "fresh:1"  # preserved!
    monkeypatch.undo()

    os.utime(lock_file, (stale_t, stale_t))
    lock_file.write_text("{corrupt")
    os.utime(lock_file, (stale_t, stale_t))

    def vanish_lock_before_recheck(*_args: object, **_kwargs: object) -> tuple[int, str] | None:
        g = real_enter(**_kwargs)  # type: ignore[arg-type]
        lock_file.unlink()
        return g

    monkeypatch.setattr(lock, "_try_enter_break_guard", vanish_lock_before_recheck)
    lock._break_if_stale()
    monkeypatch.undo()

    # __exit__ retry loop when break guard is held transiently, or stays busy until timeout
    with m2._storage_lock() as l3:
        calls = [0]
        orig_enter = l3._try_enter_break_guard

        def busy_once(*_args: object, **_kwargs: object) -> tuple[int, str] | None:
            calls[0] += 1
            if calls[0] == 1:
                return None
            return orig_enter(**_kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(l3, "_try_enter_break_guard", busy_once)
    monkeypatch.undo()
    assert calls[0] >= 2

    with m2._storage_lock() as l4:
        monkeypatch.setattr(l4, "_try_enter_break_guard", lambda *_args, **_kwargs: None)
        ticks = iter([0.0, 1.0, 2.0])
        monkeypatch.setattr(time, "monotonic", lambda: next(ticks, 2.0))
    monkeypatch.undo()
    assert lock_file.exists()
    lock_file.unlink()

    # __exit__ when lock file vanished while held
    with m2._storage_lock():
        lock_file.unlink()

    # Non-regular lock path (e.g. symlink) falls back safely
    symlink_target = root / "target.txt"
    symlink_target.write_text("x")
    lock_file.symlink_to(symlink_target)
    state = psm._read_lock_state(lock_file)
    assert state is not None and state[2] is None
    lock_file.unlink()


_CONTEND_STALE_LOCK = """
import json, os, sys, time
from pathlib import Path
from nexus_ai_agent.storage.resource_packs import LocalDirectoryPackSource, PackSyncManager

root, mirror, active_flag, log_path = (Path(a) for a in sys.argv[1:5])
mgr = PackSyncManager(
    root, [], LocalDirectoryPackSource(mirror),
    lock_backend="exclusive_file",
    lock_timeout_seconds=10.0,
    lock_stale_after_seconds=0.05,
)
with mgr._storage_lock() as lock:
    # Record entry and verify mutual exclusion via exclusive active_flag
    fd = os.open(active_flag, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    os.close(fd)
    time.sleep(0.03)
    on_disk = json.loads((root / ".sync_gc.lock.excl").read_text())
    assert on_disk["owner_token"] == lock._owner_token, (on_disk, lock._owner_token)
    active_flag.unlink()
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(f"{os.getpid()}:{lock._owner_token}\\n")
"""


def test_multiprocess_contention_breaking_stale_exclusive_lock_preserves_mutual_exclusion(
    mirror: PackMirror, tmp_path: Path
) -> None:
    root = tmp_path / "dev"
    root.mkdir(parents=True, exist_ok=True)
    active_flag = tmp_path / "in_critical_section"
    log_path = tmp_path / "winners.log"
    src_dir = Path(importlib.import_module("nexus_ai_agent").__file__ or "").parents[1]

    # Seed an already-expired exclusive_file lock held by a dead PID
    lock_file = root / ".sync_gc.lock.excl"
    lock_file.write_text(
        json.dumps(
            {"pid": 999_999_999, "expires_at": time.time() - 10.0, "owner_token": "999:stale"}
        )
    )

    procs = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                _CONTEND_STALE_LOCK,
                str(root),
                str(mirror.root),
                str(active_flag),
                str(log_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "PYTHONPATH": str(src_dir)},
        )
        for _ in range(5)
    ]
    for proc in procs:
        out, err = proc.communicate(timeout=30)
        assert proc.returncode == 0, f"worker failed: stdout={out!r} stderr={err!r}"
    winners = log_path.read_text().splitlines()
    assert len(winners) == 5 and len(set(winners)) == 5
    assert not lock_file.exists()
    assert not active_flag.exists()


def test_lock_backend_selection_without_fcntl(
    mirror: PackMirror, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flock, _ = _manager(tmp_path / "a", [], mirror, lock_backend="flock")
    assert flock._storage_lock().path.name == ".sync_gc.lock"
    real_import = importlib.import_module

    def no_fcntl(name: str, package: str | None = None) -> ModuleType:
        if name == "fcntl":
            raise ImportError("no fcntl on this platform")
        return real_import(name, package)

    monkeypatch.setattr(importlib, "import_module", no_fcntl)
    auto, _ = _manager(tmp_path / "b", [], mirror)
    assert auto._storage_lock().path.name == ".sync_gc.lock.excl"
    assert auto.collect_garbage() == ()
    with pytest.raises(ValueError, match="fcntl"):
        _manager(tmp_path / "c", [], mirror, lock_backend="flock")[0]._storage_lock()


def test_break_guard_ttl_and_finally_lifetime(
    mirror: PackMirror, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr, _ = _manager(tmp_path / "guard", [], mirror, lock_backend="exclusive_file")
    lock = mgr._storage_lock()
    assert lock._BREAK_GUARD_TTL >= 30.0

    lock.path.parent.mkdir(parents=True, exist_ok=True)
    lock.path.write_text(
        json.dumps(
            {
                "pid": 999_999_999,
                "expires_at": time.time() - 10.0,
                "owner_token": "999:stale",
            }
        )
    )

    observed_during_replace: list[bool] = []
    observed_during_unlink: list[bool] = []
    real_replace = os.replace
    real_unlink = Path.unlink

    def spy_replace(src: object, dst: object) -> None:
        if str(src) == str(lock.path):
            observed_during_replace.append(lock._break_path.exists())
        real_replace(src, dst)  # type: ignore[arg-type]

    def spy_unlink(self_path: Path, missing_ok: bool = False) -> None:
        if self_path.name.startswith(f".{lock.path.name}.") and self_path.name.endswith(".stale"):
            observed_during_unlink.append(lock._break_path.exists())
            raise OSError("simulated unlink failure")
        real_unlink(self_path, missing_ok=missing_ok)

    monkeypatch.setattr(os, "replace", spy_replace)
    monkeypatch.setattr(Path, "unlink", spy_unlink)
    with pytest.raises(OSError, match="simulated unlink failure"):
        lock._break_if_stale()
    monkeypatch.undo()

    for leftover in lock.path.parent.glob(f".{lock.path.name}.*.stale"):
        leftover.unlink(missing_ok=True)

    assert observed_during_replace == [True]
    assert observed_during_unlink == [True]
    assert not lock._break_path.exists()


_CONTEND_DEAD_BREAK = """
import json, sys, time
from pathlib import Path
from nexus_ai_agent.storage.resource_packs import LocalDirectoryPackSource, ResourcePackSyncManager
root = Path(sys.argv[1])
mirror = Path(sys.argv[2])
log = Path(sys.argv[3])
mgr = ResourcePackSyncManager(
    root, [], LocalDirectoryPackSource(mirror),
    allow_unsigned=True, disk_free_probe=lambda _: 10**12,
    lock_backend="exclusive_file", lock_timeout_seconds=10.0, lock_stale_after_seconds=0.05,
)
with mgr._storage_lock():
    with open(log, "a") as fh:
        fh.write(f"ok\\n")
"""


def test_break_guard_reclaim_fencing_and_multiprocess_dead_vs_live_pid(
    mirror: PackMirror, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "fencing"
    now = [1000.0]
    mgr, _ = _manager(
        root,
        [],
        mirror,
        lock_backend="exclusive_file",
        lock_timeout_seconds=0.2,
        lock_stale_after_seconds=5.0,
        wall_clock=lambda: now[0],
    )
    lock = mgr._storage_lock()
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    lock.path.write_text(
        json.dumps({"pid": 999_999_999, "expires_at": now[0] - 10.0, "owner_token": "999:stale"})
    )
    lock_ino = os.lstat(lock.path).st_ino

    # Guard JSON includes pid, owner_token, created_at, expires_at, target_lock_ino
    guard = lock._try_enter_break_guard(target_lock_ino=lock_ino)
    assert guard is not None
    guard_json = json.loads(lock._break_path.read_text())
    assert guard_json["pid"] == os.getpid()
    assert guard_json["owner_token"] == guard[1]
    assert guard_json["created_at"] == now[0]
    assert guard_json["expires_at"] == now[0] + lock._BREAK_GUARD_TTL
    assert guard_json["target_lock_ino"] == lock_ino
    lock._exit_break_guard(guard)

    # Fencing check inside _break_if_stale: abort if .break is swapped before os.replace
    real_enter = lock._try_enter_break_guard

    def swap_break_before_replace(*, target_lock_ino: int | None = None) -> tuple[int, str] | None:
        g = real_enter(target_lock_ino=target_lock_ino)
        lock._break_path.unlink()
        lock._break_path.write_text(
            json.dumps({"pid": os.getpid(), "expires_at": now[0] + 60, "owner_token": "hijack:1"})
        )
        return g

    monkeypatch.setattr(lock, "_try_enter_break_guard", swap_break_before_replace)
    lock._break_if_stale()
    monkeypatch.undo()
    assert lock.path.exists()
    lock._break_path.unlink(missing_ok=True)

    # _reclaim_stale_break_guard edge cases: vanished, symlink/non-regular, swapped inode/token
    lock._reclaim_stale_break_guard()  # vanished
    target = tmp_path / "symlink_target"
    target.write_text("x")
    lock._break_path.symlink_to(target)
    stale_t = now[0] - 100.0
    os.utime(lock._break_path, (stale_t, stale_t), follow_symlinks=False)
    lock._reclaim_stale_break_guard()  # non-regular file refused
    lock._break_path.unlink()

    lock._break_path.write_text(
        json.dumps({"pid": 999_999_999, "expires_at": now[0] - 10, "owner_token": "dead:1"})
    )
    real_lstat = os.lstat
    calls = [0]

    def swap_inode_on_recheck(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if str(path) == str(lock._break_path):
            calls[0] += 1
            if calls[0] == 3:
                lock._break_path.unlink()
                lock._break_path.write_text(
                    json.dumps(
                        {"pid": os.getpid(), "expires_at": now[0] + 60, "owner_token": "new:1"}
                    )
                )
        return real_lstat(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "lstat", swap_inode_on_recheck)
    lock._reclaim_stale_break_guard()  # aborted before touching newly replaced .break!
    monkeypatch.undo()
    assert json.loads(lock._break_path.read_text())["owner_token"] == "new:1"
    lock._break_path.unlink()

    lock._break_path.write_text(
        json.dumps({"pid": 999_999_999, "expires_at": now[0] - 10, "owner_token": "dead:2"})
    )
    real_link = os.link

    def swap_token_during_link(src: object, dst: object) -> None:
        real_link(src, dst)  # type: ignore[arg-type]
        Path(str(dst)).write_text(
            json.dumps({"pid": os.getpid(), "expires_at": now[0] + 60, "owner_token": "diff:1"})
        )

    monkeypatch.setattr(os, "link", swap_token_during_link)
    lock._reclaim_stale_break_guard()
    monkeypatch.undo()
    assert lock._break_path.exists()
    lock._break_path.unlink()

    # Deterministic failpoint: after the final inode/token fencing checks, a
    # rival replaces .break immediately before os.replace.  The real
    # os.replace then moves the rival's inode; reclaim must detect that inode
    # mismatch, restore the live guard, and fail closed.
    lock._break_path.write_text(
        json.dumps({"pid": 999_999_999, "expires_at": now[0] - 10, "owner_token": "dead:3"})
    )
    real_replace = os.replace

    def replace_after_competitor(src: object, dst: object) -> None:
        if str(src) == str(lock._break_path):
            lock._break_path.unlink()
            lock._break_path.write_text(
                json.dumps(
                    {"pid": os.getpid(), "expires_at": now[0] + 60, "owner_token": "rival:1"}
                )
            )
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", replace_after_competitor)
    assert lock._reclaim_stale_break_guard() is False
    monkeypatch.undo()
    assert lock._break_path.exists()
    assert json.loads(lock._break_path.read_text())["owner_token"] == "rival:1"
    lock._break_path.unlink()

    # A second race fills the destination between os.replace and the recovery
    # hardlink.  FileExistsError is fail-closed and the newly-created guard is
    # preserved.
    lock._break_path.write_text(
        json.dumps({"pid": 999_999_999, "expires_at": now[0] - 10, "owner_token": "dead:4"})
    )

    def replace_then_create_rival(src: object, dst: object) -> None:
        lock._break_path.unlink()
        lock._break_path.write_text(
            json.dumps({"pid": os.getpid(), "expires_at": now[0] + 60, "owner_token": "rival:1b"})
        )
        real_replace(src, dst)  # type: ignore[arg-type]
        lock._break_path.write_text(
            json.dumps({"pid": os.getpid(), "expires_at": now[0] + 60, "owner_token": "rival:2"})
        )

    monkeypatch.setattr(os, "replace", replace_then_create_rival)
    assert lock._reclaim_stale_break_guard() is False
    monkeypatch.undo()
    assert json.loads(lock._break_path.read_text())["owner_token"] == "rival:2"
    lock._break_path.unlink()

    # Recovery faults must preserve the moved inode instead of allowing the
    # finally block to delete the only remaining ownership evidence.  An
    # earlier scenario already left a `.stale` file in this directory, so
    # `glob(...) [-1]` is NOT deterministic across filesystems (CI 44131476170):
    # identify ONLY the file this very step created by a set difference, and
    # remove it afterwards so it cannot interfere with the next scenario.
    stale_pattern = f".{lock._break_path.name}.*.stale"
    for recovery_error in (
        PermissionError("permission denied during recovery"),
        OSError(errno.ENOSPC, "no space left during recovery"),
    ):
        lock._break_path.write_text(
            json.dumps({"pid": 999_999_999, "expires_at": now[0] - 10, "owner_token": "dead:fault"})
        )
        recovery_trash: list[Path] = []
        existing_stale = set(lock._break_path.parent.glob(stale_pattern))

        def replace_and_record_rival(
            src: object,
            dst: object,
            *,
            trash_ref: list[Path] = recovery_trash,
            before: set[Path] = existing_stale,
        ) -> None:
            lock._break_path.unlink()
            lock._break_path.write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "expires_at": now[0] + 60,
                        "owner_token": "live:fault",
                    }
                )
            )
            real_replace(src, dst)  # type: ignore[arg-type]
            new_stale = set(lock._break_path.parent.glob(stale_pattern)) - before
            trash_ref.extend(sorted(new_stale))

        def fail_recovery_link(
            src: object, dst: object, *, error: OSError = recovery_error
        ) -> None:
            if str(dst) == str(lock._break_path):
                raise error
            real_link(src, dst)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "replace", replace_and_record_rival)
        monkeypatch.setattr(os, "link", fail_recovery_link)
        with pytest.raises(type(recovery_error)):
            lock._reclaim_stale_break_guard()
        monkeypatch.undo()
        assert not lock._break_path.exists()
        assert len(recovery_trash) == 1
        assert json.loads(recovery_trash[-1].read_text())["owner_token"] == "live:fault"
        recovery_trash[-1].unlink()

    # Drop the unrelated `.stale` left by the earlier race: it is a live guard
    # and would make the fail-closed quarantine check block later contenders.
    for leftover in lock._break_path.parent.glob(stale_pattern):
        leftover.unlink()

    # Multi-process race: dead-PID .break + dead-PID lock are reclaimed cleanly,
    # whereas an expired live-PID .break is NOT reclaimed while the live owner runs.
    src_dir = Path(importlib.import_module("nexus_ai_agent").__file__ or "").parents[1]
    mp_root = tmp_path / "mp_break"
    mp_root.mkdir()
    mp_lock = mp_root / ".sync_gc.lock.excl"
    mp_break = mp_root / ".sync_gc.lock.excl.break"
    mp_lock.write_text(
        json.dumps({"pid": 999_999_999, "expires_at": time.time() - 10.0, "owner_token": "999:l"})
    )
    mp_break.write_text(
        json.dumps({"pid": 999_999_999, "expires_at": time.time() - 10.0, "owner_token": "999:b"})
    )
    log_path = tmp_path / "mp_break.log"
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                _CONTEND_DEAD_BREAK,
                str(mp_root),
                str(mirror.root),
                str(log_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "PYTHONPATH": str(src_dir)},
        )
        for _ in range(4)
    ]
    for proc in procs:
        out, err = proc.communicate(timeout=30)
        assert proc.returncode == 0, f"worker failed: stdout={out!r} stderr={err!r}"
    assert log_path.read_text().splitlines() == ["ok"] * 4

    # Live-PID expired .break blocks acquisition (timeout) rather than breaking a live holder
    live_root = tmp_path / "live_break"
    live_root.mkdir()
    (live_root / ".sync_gc.lock.excl").write_text(
        json.dumps({"pid": 999_999_999, "expires_at": time.time() - 10.0, "owner_token": "999:l"})
    )
    (live_root / ".sync_gc.lock.excl.break").write_text(
        json.dumps({"pid": os.getpid(), "expires_at": time.time() - 10.0, "owner_token": "live:b"})
    )
    live_mgr, _ = _manager(
        live_root,
        [],
        mirror,
        lock_backend="exclusive_file",
        lock_timeout_seconds=0.05,
        lock_stale_after_seconds=0.01,
    )
    with pytest.raises(PackLockTimeoutError):
        with live_mgr._storage_lock():
            pass


def test_break_if_stale_fences_a_live_lock_created_before_os_replace(
    mirror: PackMirror, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock installed between the final check and os.replace is never broken."""
    now = [1000.0]
    mgr, _ = _manager(
        tmp_path / "toctou",
        [],
        mirror,
        lock_backend="exclusive_file",
        lock_timeout_seconds=0.2,
        lock_stale_after_seconds=5.0,
        wall_clock=lambda: now[0],
    )
    lock = mgr._storage_lock()
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    real_replace = os.replace
    real_link = os.link
    stale_pattern = f".{lock.path.name}.*.stale"
    installed_ino: list[int] = []

    def seed_stale_lock() -> None:
        lock._break_path.unlink(missing_ok=True)
        for leftover in lock.path.parent.glob(stale_pattern):
            leftover.unlink()
        lock.path.write_text(
            json.dumps(
                {"pid": 999_999_999, "expires_at": now[0] - 10.0, "owner_token": "dead:toctou"}
            )
        )
        stale_t = now[0] - 100.0
        os.utime(lock.path, (stale_t, stale_t))

    def live_record() -> str:
        return json.dumps(
            {"pid": os.getpid(), "expires_at": now[0] + 60, "owner_token": "live:toctou"}
        )

    def install_live_lock() -> None:
        # Build the rival at a separate path first so its inode is provably
        # distinct from the stale lock's inode, then move it into place.
        holder = lock.path.with_name(f"{lock.path.name}.fresh")
        holder.write_text(live_record())
        installed_ino.append(os.lstat(holder).st_ino)
        lock.path.unlink()
        real_replace(holder, lock.path)

    def swap_before_replace(src: object, dst: object) -> None:
        if str(src) == str(lock.path):
            install_live_lock()
        real_replace(src, dst)  # type: ignore[arg-type]

    # 1. The fresh live lock is restored through a no-clobber hardlink, with
    #    its inode intact, and the break guard is released.
    seed_stale_lock()
    monkeypatch.setattr(os, "replace", swap_before_replace)
    lock._break_if_stale()
    monkeypatch.undo()
    assert lock.path.exists()
    assert json.loads(lock.path.read_text())["owner_token"] == "live:toctou"
    assert os.lstat(lock.path).st_ino == installed_ino[-1]
    assert not lock._break_path.exists()
    assert not list(lock.path.parent.glob(stale_pattern))

    # 2. A third owner filling self.path between os.replace and the recovery
    #    hardlink makes os.link fail with FileExistsError: nobody's live lock is
    #    broken and the moved inode is kept as ownership evidence.
    seed_stale_lock()

    def swap_then_third_owner(src: object, dst: object) -> None:
        if str(src) == str(lock.path):
            install_live_lock()
        real_replace(src, dst)  # type: ignore[arg-type]
        if str(src) == str(lock.path):
            lock.path.write_text(
                json.dumps(
                    {"pid": os.getpid(), "expires_at": now[0] + 60, "owner_token": "third:owner"}
                )
            )

    monkeypatch.setattr(os, "replace", swap_then_third_owner)
    lock._break_if_stale()
    monkeypatch.undo()
    assert json.loads(lock.path.read_text())["owner_token"] == "third:owner"
    evidence = list(lock.path.parent.glob(stale_pattern))
    assert len(evidence) == 1
    assert json.loads(evidence[0].read_text())["owner_token"] == "live:toctou"
    for path in evidence:
        path.unlink()

    # 3. An unexpected recovery fault propagates WITHOUT deleting the trash,
    #    so the moved live lock remains recoverable evidence.
    seed_stale_lock()

    def fail_restore_link(src: object, dst: object) -> None:
        if str(dst) == str(lock.path):
            raise PermissionError("permission denied during restore")
        real_link(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", swap_before_replace)
    monkeypatch.setattr(os, "link", fail_restore_link)
    with pytest.raises(PermissionError, match="permission denied during restore"):
        lock._break_if_stale()
    monkeypatch.undo()
    assert not lock.path.exists()
    evidence = list(lock.path.parent.glob(stale_pattern))
    assert len(evidence) == 1
    assert json.loads(evidence[0].read_text())["owner_token"] == "live:toctou"
    for path in evidence:
        path.unlink()

    # 4. An unlink + recreate may reuse the same inode.  The owner token is
    #    the second half of the fence: the reused-inode live lock is preserved.
    seed_stale_lock()

    def overwrite_before_replace(src: object, dst: object) -> None:
        if str(src) == str(lock.path):
            lock.path.write_text(live_record())
            installed_ino.append(os.lstat(lock.path).st_ino)
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", overwrite_before_replace)
    lock._break_if_stale()
    monkeypatch.undo()
    assert lock.path.exists()
    assert json.loads(lock.path.read_text())["owner_token"] == "live:toctou"
    assert not lock._break_path.exists()
    assert not list(lock.path.parent.glob(stale_pattern))


def test_quarantined_live_break_guard_is_fail_closed_for_the_next_contender(
    mirror: PackMirror, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live guard stranded in .stale must not allow a second .break owner."""
    now = [1000.0]
    mgr, _ = _manager(
        tmp_path / "quarantine",
        [],
        mirror,
        lock_backend="exclusive_file",
        lock_timeout_seconds=0.2,
        lock_stale_after_seconds=5.0,
        wall_clock=lambda: now[0],
    )
    lock = mgr._storage_lock()
    rival = mgr._storage_lock()
    root = lock._break_path.parent
    root.mkdir(parents=True, exist_ok=True)
    stale_pattern = f".{lock._break_path.name}.*.stale"
    real_replace = os.replace
    real_link = os.link
    real_read = psm._read_lock_state

    # Reproduce the gap: reclaim moves a rival's LIVE guard to .stale, the
    # recovery hardlink fails (ENOSPC), and .break is left empty.
    lock._break_path.write_text(
        json.dumps({"pid": 999_999_999, "expires_at": now[0] - 10, "owner_token": "dead:guard"})
    )

    def replace_with_live_rival(src: object, dst: object) -> None:
        if str(src) == str(lock._break_path):
            lock._break_path.unlink()
            lock._break_path.write_text(
                json.dumps(
                    {"pid": os.getpid(), "expires_at": now[0] + 60, "owner_token": "rival:live"}
                )
            )
        real_replace(src, dst)  # type: ignore[arg-type]

    def fail_recovery_link(src: object, dst: object) -> None:
        if str(dst) == str(lock._break_path):
            raise OSError(errno.ENOSPC, "no space left during recovery")
        real_link(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", replace_with_live_rival)
    monkeypatch.setattr(os, "link", fail_recovery_link)
    with pytest.raises(OSError, match="no space left during recovery"):
        lock._reclaim_stale_break_guard()
    monkeypatch.undo()
    quarantined = sorted(root.glob(stale_pattern))
    assert len(quarantined) == 1
    assert not lock._break_path.exists()

    # The next contender must NOT be handed a fresh .break guard while the
    # live rival is still quarantined: exclusivity holds (fail closed).
    monkeypatch.setattr(os, "link", fail_recovery_link)
    assert rival._try_enter_break_guard() is None
    monkeypatch.undo()
    assert not lock._break_path.exists()
    assert quarantined[0].exists()
    assert json.loads(quarantined[0].read_text())["owner_token"] == "rival:live"

    # Once recovery is possible again the guard is restored at the live path,
    # but this caller still fails closed: it never minted a competing token.
    assert rival._try_enter_break_guard() is None
    assert json.loads(lock._break_path.read_text())["owner_token"] == "rival:live"
    assert not quarantined[0].exists()
    lock._break_path.unlink()

    # A dead/expired quarantine is not a live owner: entry stays available.
    dead = root / f".{lock._break_path.name}.dead.stale"
    dead.write_text(
        json.dumps({"pid": 999_999_999, "expires_at": now[0] - 10, "owner_token": "dead:guard"})
    )
    guard = rival._try_enter_break_guard()
    assert guard is not None
    rival._exit_break_guard(guard)
    dead.unlink()

    # A candidate that vanishes between glob and lstat protects nobody.
    vanishing = root / f".{lock._break_path.name}.vanished.stale"
    vanishing.write_text("unreadable")

    def vanish_on_read(path: object, *args: object, **kwargs: object) -> object:
        if str(path) == str(vanishing):
            return None
        return real_read(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(psm, "_read_lock_state", vanish_on_read)
    guard = rival._try_enter_break_guard()
    monkeypatch.undo()
    assert guard is not None
    rival._exit_break_guard(guard)
    vanishing.unlink()
