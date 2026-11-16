"""Pure delta / deduplication / hydration planner.

:func:`build_sync_plan` decides, for every entry of the *target* manifest, the
cheapest way to obtain its exact bytes — with no I/O and no side effects, so
the decision can be unit-tested exhaustively and shown in the UI before any
byte moves:

1. ``REUSE_LOCAL_CAS``   — a blob with the same SHA-256 (and size) already
   lives in the local content-addressed store, from *any* pack or version:
   zero download, zero recomputation.  Two target entries sharing one digest
   are acquired once; the second one is a reuse of the first.
2. ``HYDRATE_FROM_SEED`` — new bytes, but the manifest carries a local DNA
   seed recipe: bake on the device.
3. ``DOWNLOAD_RANGE``    — new bytes that only the network can provide:
   resumable HTTP Range download.

The disk guard compares ``available_disk_bytes`` against the target's
``min_free_space_bytes`` and reports ``BLOCKED_INSUFFICIENT_STORAGE``; the
sync manager turns that into an exception *before* writing anything.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from .pack_manifest import ResourceBlobEntry, ResourcePackManifest

__all__ = [
    "PlannedEntry",
    "StorageGuardStatus",
    "SyncAction",
    "SyncPlan",
    "build_sync_plan",
]


class SyncAction(str, Enum):
    REUSE_LOCAL_CAS = "REUSE_LOCAL_CAS"
    HYDRATE_FROM_SEED = "HYDRATE_FROM_SEED"
    DOWNLOAD_RANGE = "DOWNLOAD_RANGE"


class StorageGuardStatus(str, Enum):
    OK = "OK"
    BLOCKED_INSUFFICIENT_STORAGE = "BLOCKED_INSUFFICIENT_STORAGE"


@dataclass(frozen=True)
class PlannedEntry:
    entry: ResourceBlobEntry
    action: SyncAction
    #: True when the same path+digest was already part of the installed version.
    unchanged_from_installed: bool
    #: True when the digest is acquired by an earlier entry of this same plan.
    deduplicated_within_plan: bool = False


@dataclass(frozen=True)
class SyncPlan:
    pack_id: str
    from_version: str | None
    to_version: str
    entries: tuple[PlannedEntry, ...]
    removed_paths: tuple[str, ...]
    storage_guard: StorageGuardStatus
    required_free_bytes: int
    available_disk_bytes: int

    def _with(self, action: SyncAction) -> tuple[PlannedEntry, ...]:
        return tuple(item for item in self.entries if item.action is action)

    @property
    def reuse(self) -> tuple[PlannedEntry, ...]:
        return self._with(SyncAction.REUSE_LOCAL_CAS)

    @property
    def hydrate(self) -> tuple[PlannedEntry, ...]:
        return self._with(SyncAction.HYDRATE_FROM_SEED)

    @property
    def download(self) -> tuple[PlannedEntry, ...]:
        return self._with(SyncAction.DOWNLOAD_RANGE)

    @property
    def network_bytes(self) -> int:
        return sum(item.entry.size_bytes for item in self.download)

    @property
    def hydrate_bytes(self) -> int:
        return sum(item.entry.size_bytes for item in self.hydrate)

    @property
    def reused_bytes(self) -> int:
        return sum(item.entry.size_bytes for item in self.reuse)

    @property
    def is_blocked(self) -> bool:
        return self.storage_guard is StorageGuardStatus.BLOCKED_INSUFFICIENT_STORAGE

    @property
    def requires_acquisition(self) -> bool:
        return bool(self.download or self.hydrate)


def build_sync_plan(
    local_cas_index: Mapping[str, int],
    installed_manifest: ResourcePackManifest | None,
    target_manifest: ResourcePackManifest,
    available_disk_bytes: int,
) -> SyncPlan:
    """Plan the sync of *target_manifest* (pure: no I/O, inputs are not mutated).

    ``local_cas_index`` maps ``sha256 -> size_bytes`` of blobs already on the
    device; a digest whose recorded size disagrees with the manifest is not
    trusted for reuse.
    """
    if isinstance(available_disk_bytes, bool) or not isinstance(available_disk_bytes, int):
        raise TypeError("available_disk_bytes must be an int")
    if available_disk_bytes < 0:
        raise ValueError("available_disk_bytes must be >= 0")
    if installed_manifest is not None and installed_manifest.pack_id != target_manifest.pack_id:
        raise ValueError(
            f"installed pack {installed_manifest.pack_id!r} is not "
            f"target pack {target_manifest.pack_id!r}"
        )

    installed_pairs: set[tuple[str, str]] = set()
    installed_paths: set[str] = set()
    if installed_manifest is not None:
        installed_pairs = {(e.relative_path, e.sha256) for e in installed_manifest.entries}
        installed_paths = {e.relative_path for e in installed_manifest.entries}

    acquired_in_plan: set[str] = set()
    planned: list[PlannedEntry] = []
    for entry in target_manifest.entries:
        unchanged = (entry.relative_path, entry.sha256) in installed_pairs
        if local_cas_index.get(entry.sha256) == entry.size_bytes:
            planned.append(PlannedEntry(entry, SyncAction.REUSE_LOCAL_CAS, unchanged))
        elif entry.sha256 in acquired_in_plan:
            planned.append(
                PlannedEntry(
                    entry, SyncAction.REUSE_LOCAL_CAS, unchanged, deduplicated_within_plan=True
                )
            )
        else:
            acquired_in_plan.add(entry.sha256)
            action = (
                SyncAction.HYDRATE_FROM_SEED
                if entry.acquisition_mode == "local_dna_seed"
                else SyncAction.DOWNLOAD_RANGE
            )
            planned.append(PlannedEntry(entry, action, unchanged))

    target_paths = {e.relative_path for e in target_manifest.entries}
    required = target_manifest.min_free_space_bytes
    guard = (
        StorageGuardStatus.BLOCKED_INSUFFICIENT_STORAGE
        if available_disk_bytes < required
        else StorageGuardStatus.OK
    )
    return SyncPlan(
        pack_id=target_manifest.pack_id,
        from_version=None if installed_manifest is None else installed_manifest.version,
        to_version=target_manifest.version,
        entries=tuple(planned),
        removed_paths=tuple(sorted(installed_paths - target_paths)),
        storage_guard=guard,
        required_free_bytes=required,
        available_disk_bytes=available_disk_bytes,
    )
