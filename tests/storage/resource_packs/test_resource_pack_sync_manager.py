"""Login gate, atomic installs, live progress, delta upgrades and pruning."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

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
    PackSyncError,
    PackVerificationError,
    RangeFetchInterrupted,
    ResourceBlobEntry,
    ResourcePackManifest,
    ResourcePackSyncManager,
    SyncProgress,
    SyncStage,
    UnknownPackError,
    default_blob_key,
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
    rebuilt = manager.sync_pack("core")  # rebuilds from the CAS, no re-download
    assert manager.get_login_gate_status().state is GateState.READY
    assert rebuilt.downloaded_bytes == 0 and rebuilt.reused_files == 4


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
    assert manager.collect_garbage() == (stale.sha256,)
    assert manager.prune_old_versions("standard") == ()


def test_prune_and_gc_without_installs(mirror: PackMirror, tmp_path: Path) -> None:
    manager, _ = _manager(tmp_path / "dev", [_core(mirror)], mirror)
    assert manager.prune_old_versions("core") == ()
    assert manager.collect_garbage() == ()
    (tmp_path / "dev/packs/core/staging").mkdir(parents=True)  # interrupted, never activated
    assert manager.collect_garbage() == ()
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
