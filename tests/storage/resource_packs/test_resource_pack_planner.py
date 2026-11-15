"""Pure delta / dedup / hydration planner."""

from __future__ import annotations

import copy

import pytest
from resource_pack_testkit import LUT_RECIPE, PackMirror, blob, remote_entry

from nexus_ai_agent.storage.resource_packs import (
    ResourcePackManifest,
    StorageGuardStatus,
    SyncAction,
    build_manifest,
    build_sync_plan,
    describe_seed_entry,
)


def _five_file_versions() -> tuple[ResourcePackManifest, ResourcePackManifest]:
    files = {f"tex/{i}.bin": blob(f"v1-{i}", 1000 + i) for i in range(5)}
    v1 = build_manifest(
        pack_id="std",
        title_fa="استاندارد",
        tier="standard",
        version="1.0.0",
        entries=[remote_entry(p, d) for p, d in files.items()],
    )
    files["tex/3.bin"] = blob("v2-3", 1500)  # exactly one file changes
    v2 = build_manifest(
        pack_id="std",
        title_fa="استاندارد",
        tier="standard",
        version="1.1.0",
        entries=[remote_entry(p, d) for p, d in files.items()],
    )
    return v1, v2


def test_upgrade_reuses_the_four_unchanged_files() -> None:
    v1, v2 = _five_file_versions()
    cas = {e.sha256: e.size_bytes for e in v1.entries}
    plan = build_sync_plan(cas, v1, v2, available_disk_bytes=10**9)
    assert (plan.from_version, plan.to_version) == ("1.0.0", "1.1.0")
    assert len(plan.reuse) == 4 and all(p.unchanged_from_installed for p in plan.reuse)
    assert [p.entry.relative_path for p in plan.download] == ["tex/3.bin"]
    assert plan.hydrate == () and plan.network_bytes == 1500
    assert plan.reused_bytes == sum(1000 + i for i in (0, 1, 2, 4))
    assert plan.storage_guard is StorageGuardStatus.OK and plan.requires_acquisition


def test_cross_pack_dedup_reuses_any_blob_already_on_the_device() -> None:
    shared = blob("shared-noise", 4096)
    other_pack_cas = {remote_entry("x", shared).sha256: len(shared)}
    target = build_manifest(
        pack_id="hd",
        title_fa="سینمایی",
        tier="cinema_hd",
        version="1",
        entries=[remote_entry("noise.bin", shared), remote_entry("new.bin", b"new")],
    )
    plan = build_sync_plan(other_pack_cas, None, target, 10**6)
    actions = {p.entry.relative_path: p.action for p in plan.entries}
    assert actions == {
        "noise.bin": SyncAction.REUSE_LOCAL_CAS,
        "new.bin": SyncAction.DOWNLOAD_RANGE,
    }
    assert not plan.entries[0].unchanged_from_installed and plan.from_version is None


def test_duplicate_digests_inside_one_pack_are_acquired_once() -> None:
    data = blob("dup", 64)
    seed = describe_seed_entry("luts/a.cube", LUT_RECIPE)
    seed_copy = describe_seed_entry("luts/b.cube", LUT_RECIPE)
    target = build_manifest(
        pack_id="p",
        title_fa="ت",
        tier="standard",
        version="1",
        entries=[remote_entry("a.bin", data), remote_entry("b.bin", data), seed, seed_copy],
    )
    plan = build_sync_plan({}, None, target, 10**9)
    by_path = {p.entry.relative_path: p for p in plan.entries}
    assert by_path["a.bin"].action is SyncAction.DOWNLOAD_RANGE
    assert by_path["b.bin"].action is SyncAction.REUSE_LOCAL_CAS
    assert by_path["b.bin"].deduplicated_within_plan
    assert by_path["luts/a.cube"].action is SyncAction.HYDRATE_FROM_SEED
    assert by_path["luts/b.cube"].action is SyncAction.REUSE_LOCAL_CAS
    assert plan.network_bytes == 64 and plan.hydrate_bytes == seed.size_bytes


def test_seed_entries_hydrate_and_never_touch_the_network() -> None:
    seed = describe_seed_entry("luts/a.cube", LUT_RECIPE)
    target = build_manifest(pack_id="c", title_fa="ه", tier="core", version="1", entries=[seed])
    plan = build_sync_plan({}, None, target, 10**9)
    assert [p.action for p in plan.entries] == [SyncAction.HYDRATE_FROM_SEED]
    assert plan.network_bytes == 0


def test_cas_object_with_wrong_size_is_not_trusted_for_reuse() -> None:
    entry = remote_entry("a.bin", b"abc")
    target = build_manifest(pack_id="p", title_fa="ت", tier="core", version="1", entries=[entry])
    plan = build_sync_plan({entry.sha256: 2}, None, target, 10**9)
    assert plan.entries[0].action is SyncAction.DOWNLOAD_RANGE


def test_disk_guard_blocks_below_min_free_space(mirror: PackMirror) -> None:
    entry = mirror.add_remote("a.bin", b"x" * 10)
    target = mirror.manifest("p", "1", [entry], min_free_space_bytes=500)
    blocked = build_sync_plan({}, None, target, available_disk_bytes=499)
    assert blocked.storage_guard is StorageGuardStatus.BLOCKED_INSUFFICIENT_STORAGE
    assert blocked.is_blocked and blocked.required_free_bytes == 500
    ok = build_sync_plan({}, None, target, available_disk_bytes=500)
    assert not ok.is_blocked


def test_removed_paths_and_noop_plans() -> None:
    v1, v2 = _five_file_versions()
    smaller = build_manifest(
        pack_id="std", title_fa="ا", tier="standard", version="2.0.0", entries=v2.entries[:3]
    )
    cas = {e.sha256: e.size_bytes for e in v2.entries}
    plan = build_sync_plan(cas, v2, smaller, 10**9)
    assert plan.removed_paths == ("tex/3.bin", "tex/4.bin")
    assert not plan.requires_acquisition


def test_planner_is_pure_and_validates_inputs() -> None:
    v1, v2 = _five_file_versions()
    cas = {e.sha256: e.size_bytes for e in v1.entries}
    frozen = copy.deepcopy(cas)
    assert build_sync_plan(cas, v1, v2, 10**9) == build_sync_plan(cas, v1, v2, 10**9)
    assert cas == frozen
    with pytest.raises(ValueError, match=">= 0"):
        build_sync_plan(cas, v1, v2, -1)
    with pytest.raises(TypeError):
        build_sync_plan(cas, v1, v2, True)
    other = build_manifest(
        pack_id="other", title_fa="د", tier="standard", version="1", entries=v1.entries
    )
    with pytest.raises(ValueError, match="is not target pack"):
        build_sync_plan(cas, other, v2, 10**9)
