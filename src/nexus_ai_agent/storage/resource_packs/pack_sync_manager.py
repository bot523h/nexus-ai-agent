"""Atomic installer, login gate and live progress for resource packs.

On-disk layout under ``root``::

    cas/                                shared content-addressed store
    packs/<pack_id>/active.json         the ONLY commit point (atomic pointer)
    packs/<pack_id>/versions/<dir>/     immutable, verified install trees
    packs/<pack_id>/versions/<dir>.manifest.json
    packs/<pack_id>/staging/<dir>/      work in progress (always disposable)

A sync builds a complete new tree in ``staging``, verifies it against the RFC
6962 root (and the Ed25519 signature when a trusted key is configured), moves
it into ``versions`` and finally flips ``active.json`` with :func:`os.replace`.
Any failure before the flip leaves the previous version — and the pointer to
it — untouched; interrupted downloads keep their ``.part`` files so the next
attempt resumes instead of restarting.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .content_store import LocalContentStore, fsync_directory
from .delta_and_hydration_planner import SyncAction, SyncPlan, build_sync_plan
from .errors import (
    InsufficientStorageError,
    ManifestValidationError,
    PackSyncError,
    PackVerificationError,
    UnknownPackError,
)
from .pack_manifest import (
    PackTier,
    PackVerificationReport,
    ResourceBlobEntry,
    ResourcePackManifest,
    manifest_from_json,
    manifest_to_json,
    verify_installed_pack,
    verify_manifest_integrity,
    verify_manifest_signature,
)
from .resumable_range_fetcher import PackSource, ResumableRangeFetcher
from .seed_hydrator import hydrate_entry_to_store

__all__ = [
    "GateState",
    "LoginResourceGateStatus",
    "PackGateStatus",
    "ProgressCallback",
    "ResourcePackSyncManager",
    "SyncProgress",
    "SyncResult",
    "SyncStage",
]

_PACK_ID_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_DIR_NAME_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,95}")
_POINTER_KEYS = frozenset({"schema", "pack_id", "version", "merkle_root", "install_dir"})
_TIER_ORDER: dict[PackTier, int] = {"core": 0, "standard": 1, "cinema_hd": 2}


class SyncStage(str, Enum):
    PLANNING = "PLANNING"
    DOWNLOADING = "DOWNLOADING"
    HYDRATING = "HYDRATING"
    STAGING = "STAGING"
    VERIFYING = "VERIFYING"
    ACTIVATING = "ACTIVATING"
    COMPLETED = "COMPLETED"


_STAGE_MESSAGES_FA: dict[SyncStage, str] = {
    SyncStage.PLANNING: "در حال بررسی بسته‌های منابع…",
    SyncStage.DOWNLOADING: "در حال دانلود منابع…",
    SyncStage.HYDRATING: "در حال آماده‌سازی محلی منابع روی دستگاه شما…",
    SyncStage.STAGING: "در حال نصب منابع…",
    SyncStage.VERIFYING: "در حال تأیید صحت رمزنگاری منابع…",
    SyncStage.ACTIVATING: "در حال فعال‌سازی نسخه جدید…",
    SyncStage.COMPLETED: "منابع آماده است.",
}


@dataclass(frozen=True)
class SyncProgress:
    """One live progress event for the studio's PUBG-style resource screen."""

    pack_id: str
    stage: SyncStage
    done_bytes: int
    total_bytes: int
    percent: float
    speed_bytes_per_second: float
    current_path: str | None
    files_done: int
    files_total: int
    message_fa: str


ProgressCallback = Callable[[SyncProgress], None]


@dataclass(frozen=True)
class SyncResult:
    pack_id: str
    version: str
    install_dir: Path
    plan: SyncPlan
    downloaded_bytes: int
    hydrated_bytes: int
    reused_files: int
    already_up_to_date: bool
    verification: PackVerificationReport
    elapsed_seconds: float


class GateState(str, Enum):
    READY = "READY"
    UPDATE_REQUIRED = "UPDATE_REQUIRED"
    INSTALL_REQUIRED = "INSTALL_REQUIRED"
    REPAIR_REQUIRED = "REPAIR_REQUIRED"
    BLOCKED_INSUFFICIENT_STORAGE = "BLOCKED_INSUFFICIENT_STORAGE"


#: Severity used to summarise several packs into one gate state.
_GATE_SEVERITY: dict[GateState, int] = {
    GateState.READY: 0,
    GateState.UPDATE_REQUIRED: 1,
    GateState.INSTALL_REQUIRED: 2,
    GateState.REPAIR_REQUIRED: 3,
    GateState.BLOCKED_INSUFFICIENT_STORAGE: 4,
}

_GATE_MESSAGES_FA: dict[GateState, str] = {
    GateState.READY: "همه منابع ضروری آماده است.",
    GateState.UPDATE_REQUIRED: "نسخه جدید منابع ضروری آماده دریافت است.",
    GateState.INSTALL_REQUIRED: "برای ورود به استودیو، منابع ضروری باید دانلود و آماده‌سازی شوند.",
    GateState.REPAIR_REQUIRED: "برخی منابع آسیب دیده‌اند و باید ترمیم شوند.",
    GateState.BLOCKED_INSUFFICIENT_STORAGE: "فضای خالی دستگاه برای آماده‌سازی منابع کافی نیست.",
}


@dataclass(frozen=True)
class PackGateStatus:
    pack_id: str
    title_fa: str
    tier: PackTier
    mandatory: bool
    state: GateState
    installed_version: str | None
    target_version: str
    download_bytes: int
    hydrate_bytes: int


@dataclass(frozen=True)
class LoginResourceGateStatus:
    """Whether the user may enter the studio, or must see the resource gate first."""

    state: GateState
    show_resource_gate: bool
    packs: tuple[PackGateStatus, ...]
    pending_pack_ids: tuple[str, ...]
    total_download_bytes: int
    total_hydrate_bytes: int
    message_fa: str


@dataclass(frozen=True)
class _ActivePointer:
    version: str
    merkle_root: str
    install_dir: str


class _ProgressReporter:
    def __init__(
        self,
        pack_id: str,
        callback: ProgressCallback | None,
        clock: Callable[[], float],
        total_bytes: int,
        files_total: int,
    ) -> None:
        self.pack_id = pack_id
        self.callback = callback
        self.clock = clock
        self.started = clock()
        self.total = total_bytes
        self.files_total = files_total
        self.done = 0
        self.network = 0
        self.files_done = 0

    def emit(self, stage: SyncStage, path: str | None = None) -> None:
        if self.callback is None:
            return
        elapsed = self.clock() - self.started
        done = min(self.done, self.total)
        if self.total:
            percent = 100.0 * done / self.total
        else:
            percent = 100.0 if stage is SyncStage.COMPLETED else 0.0
        self.callback(
            SyncProgress(
                pack_id=self.pack_id,
                stage=stage,
                done_bytes=done,
                total_bytes=self.total,
                percent=round(percent, 2),
                speed_bytes_per_second=self.network / elapsed if elapsed > 0 else 0.0,
                current_path=path,
                files_done=self.files_done,
                files_total=self.files_total,
                message_fa=_STAGE_MESSAGES_FA[stage],
            )
        )


def _default_disk_free(path: Path) -> int:
    probe = path
    while not probe.exists():
        probe = probe.parent
    return shutil.disk_usage(probe).free


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    with temp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    fsync_directory(path.parent)


class ResourcePackSyncManager:
    """Plans, acquires, verifies and atomically activates resource packs."""

    def __init__(
        self,
        root: Path,
        catalog: Iterable[ResourcePackManifest],
        source: PackSource,
        *,
        trusted_public_key: bytes | None = None,
        disk_free_probe: Callable[[Path], int] = _default_disk_free,
        clock: Callable[[], float] = time.monotonic,
        chunk_size: int = 256 * 1024,
        max_attempts: int = 3,
        prefer_hardlinks: bool = True,
    ) -> None:
        self.root = Path(root)
        self._catalog: dict[str, ResourcePackManifest] = {}
        for manifest in catalog:
            if manifest.pack_id in self._catalog:
                raise ValueError(f"duplicate pack_id in catalog: {manifest.pack_id!r}")
            self._catalog[manifest.pack_id] = manifest
        self.trusted_public_key = trusted_public_key
        self.store = LocalContentStore(self.root / "cas")
        self.fetcher = ResumableRangeFetcher(
            source, self.store, chunk_size=chunk_size, max_attempts=max_attempts
        )
        self._disk_free = disk_free_probe
        self._clock = clock
        self.prefer_hardlinks = prefer_hardlinks
        self._lock = threading.Lock()

    # -- paths & pointer ------------------------------------------------------

    def _pack_dir(self, pack_id: str) -> Path:
        if not _PACK_ID_RE.fullmatch(pack_id):
            raise UnknownPackError(f"invalid pack id {pack_id!r}")
        return self.root / "packs" / pack_id

    def _target(self, pack_id: str) -> ResourcePackManifest:
        try:
            return self._catalog[pack_id]
        except KeyError:
            raise UnknownPackError(f"pack {pack_id!r} is not in the catalog") from None

    def _read_pointer(self, pack_id: str) -> _ActivePointer | None:
        path = self._pack_dir(pack_id) / "active.json"
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_bytes())
        except (OSError, ValueError) as error:
            raise PackSyncError(f"{pack_id}: unreadable active pointer: {error}") from error
        if (
            not isinstance(raw, dict)
            or set(raw) != _POINTER_KEYS
            or raw["schema"] != 1
            or raw["pack_id"] != pack_id
            or not all(isinstance(raw[k], str) for k in ("version", "merkle_root", "install_dir"))
            or not _DIR_NAME_RE.fullmatch(raw["install_dir"])
        ):
            raise PackSyncError(f"{pack_id}: malformed active pointer")
        return _ActivePointer(raw["version"], raw["merkle_root"], raw["install_dir"])

    def _load_installed(self, pack_id: str) -> tuple[ResourcePackManifest, Path] | None:
        pointer = self._read_pointer(pack_id)
        if pointer is None:
            return None
        versions = self._pack_dir(pack_id) / "versions"
        try:
            manifest = manifest_from_json(
                (versions / f"{pointer.install_dir}.manifest.json").read_bytes()
            )
        except (OSError, ManifestValidationError) as error:
            raise PackSyncError(f"{pack_id}: installed manifest unreadable: {error}") from error
        if manifest.pack_id != pack_id or manifest.merkle_root != pointer.merkle_root:
            raise PackSyncError(f"{pack_id}: installed manifest does not match the pointer")
        return manifest, versions / pointer.install_dir

    def active_install_dir(self, pack_id: str) -> Path | None:
        pointer = self._read_pointer(pack_id)
        return (
            None
            if pointer is None
            else self._pack_dir(pack_id) / "versions" / (pointer.install_dir)
        )

    def installed_manifest(self, pack_id: str) -> ResourcePackManifest | None:
        loaded = self._load_installed(pack_id)
        return None if loaded is None else loaded[0]

    def _verify_target(self, manifest: ResourcePackManifest) -> None:
        verify_manifest_integrity(manifest)
        if self.trusted_public_key is not None:
            verify_manifest_signature(manifest, self.trusted_public_key)

    # -- login gate -----------------------------------------------------------

    def _pack_gate(
        self, target: ResourcePackManifest, cas_index: dict[str, int], available: int
    ) -> PackGateStatus:
        self._verify_target(target)  # a tampered catalog is an incident, not a state
        corrupted = False
        try:
            loaded = self._load_installed(target.pack_id)
        except PackSyncError:
            loaded, corrupted = None, True
        installed = None if loaded is None else loaded[0]
        if loaded is None:
            state = GateState.REPAIR_REQUIRED if corrupted else GateState.INSTALL_REQUIRED
        elif loaded[0].merkle_root != target.merkle_root:
            state = GateState.UPDATE_REQUIRED
        else:
            try:
                verify_installed_pack(
                    loaded[0], loaded[1], trusted_public_key=self.trusted_public_key
                )
                state = GateState.READY
            except PackVerificationError:
                state = GateState.REPAIR_REQUIRED
        download = hydrate = 0
        if state is not GateState.READY:
            plan = build_sync_plan(cas_index, installed, target, available)
            download, hydrate = plan.network_bytes, plan.hydrate_bytes
            if plan.is_blocked:
                state = GateState.BLOCKED_INSUFFICIENT_STORAGE
        return PackGateStatus(
            pack_id=target.pack_id,
            title_fa=target.title_fa,
            tier=target.tier,
            mandatory=target.mandatory,
            state=state,
            installed_version=None if installed is None else installed.version,
            target_version=target.version,
            download_bytes=download,
            hydrate_bytes=hydrate,
        )

    def get_login_gate_status(self) -> LoginResourceGateStatus:
        """Decide whether the studio opens directly or shows the resource gate.

        Every mandatory (core) pack must be installed at the catalog's exact
        Merkle root *and* pass full on-disk verification; anything else shows
        the gate (fail closed).
        """
        cas_index = self.store.index()
        available = self._disk_free(self.root)
        targets = sorted(self._catalog.values(), key=lambda m: (_TIER_ORDER[m.tier], m.pack_id))
        packs = tuple(self._pack_gate(t, cas_index, available) for t in targets)
        pending = tuple(p for p in packs if p.mandatory and p.state is not GateState.READY)
        state = max(
            (p.state for p in pending), key=_GATE_SEVERITY.__getitem__, default=GateState.READY
        )
        return LoginResourceGateStatus(
            state=state,
            show_resource_gate=bool(pending),
            packs=packs,
            pending_pack_ids=tuple(p.pack_id for p in pending),
            total_download_bytes=sum(p.download_bytes for p in pending),
            total_hydrate_bytes=sum(p.hydrate_bytes for p in pending),
            message_fa=_GATE_MESSAGES_FA[state],
        )

    # -- sync -------------------------------------------------------------------

    def sync_pack(
        self, pack_id: str, progress_callback: ProgressCallback | None = None
    ) -> SyncResult:
        """Bring *pack_id* to the catalog version, atomically.

        Raises :class:`InsufficientStorageError` before writing any byte when
        the disk guard blocks; any other failure leaves the active version
        untouched (``.part`` files survive for resumption).
        """
        with self._lock:
            return self._sync_locked(pack_id, progress_callback)

    def _sync_locked(self, pack_id: str, callback: ProgressCallback | None) -> SyncResult:
        target = self._target(pack_id)
        self._verify_target(target)
        try:
            loaded = self._load_installed(pack_id)
        except PackSyncError:
            loaded = None  # corrupt pointer/manifest: rebuild from scratch
        installed = None if loaded is None else loaded[0]
        plan = build_sync_plan(self.store.index(), installed, target, self._disk_free(self.root))
        reporter = _ProgressReporter(
            pack_id,
            callback,
            self._clock,
            plan.network_bytes + plan.hydrate_bytes,
            len(target.entries),
        )
        reporter.emit(SyncStage.PLANNING)
        if plan.is_blocked:
            raise InsufficientStorageError(
                pack_id=pack_id,
                required_bytes=plan.required_free_bytes,
                available_bytes=plan.available_disk_bytes,
            )
        if loaded is not None and loaded[0].merkle_root == target.merkle_root:
            try:
                report = verify_installed_pack(
                    target, loaded[1], trusted_public_key=self.trusted_public_key
                )
            except PackVerificationError:
                pass  # damaged install of the right version: repair below
            else:
                reporter.files_done = len(target.entries)
                reporter.emit(SyncStage.COMPLETED)
                return SyncResult(
                    pack_id=pack_id,
                    version=target.version,
                    install_dir=loaded[1],
                    plan=plan,
                    downloaded_bytes=0,
                    hydrated_bytes=0,
                    reused_files=len(target.entries),
                    already_up_to_date=True,
                    verification=report,
                    elapsed_seconds=self._clock() - reporter.started,
                )

        downloaded = hydrated = 0
        for item in plan.download:
            downloaded += self._download(item.entry, reporter)
        for item in plan.hydrate:
            hydrated += self._hydrate(item.entry, reporter)

        pack_dir = self._pack_dir(pack_id)
        dir_name = f"{target.version}__{secrets.token_hex(6)}"
        staging = pack_dir / "staging" / dir_name
        final_dir = pack_dir / "versions" / dir_name
        manifest_path = pack_dir / "versions" / f"{dir_name}.manifest.json"
        activated = False
        try:
            reporter.emit(SyncStage.STAGING)
            for item in plan.entries:
                if item.action is SyncAction.REUSE_LOCAL_CAS:
                    downloaded, hydrated = self._ensure_reusable(
                        item.entry, reporter, downloaded, hydrated
                    )
                self.store.materialize(
                    item.entry.sha256,
                    staging / item.entry.relative_path,
                    prefer_hardlink=self.prefer_hardlinks,
                )
            reporter.emit(SyncStage.VERIFYING)
            report = verify_installed_pack(
                target, staging, trusted_public_key=self.trusted_public_key
            )
            reporter.emit(SyncStage.ACTIVATING)
            _write_atomic(manifest_path, manifest_to_json(target))
            final_dir.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging, final_dir)
            pointer = {
                "schema": 1,
                "pack_id": pack_id,
                "version": target.version,
                "merkle_root": target.merkle_root,
                "install_dir": dir_name,
            }
            _write_atomic(pack_dir / "active.json", json.dumps(pointer, sort_keys=True).encode())
            activated = True
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            if not activated:
                shutil.rmtree(final_dir, ignore_errors=True)
                manifest_path.unlink(missing_ok=True)
        reporter.files_done = len(target.entries)
        reporter.done = reporter.total
        reporter.emit(SyncStage.COMPLETED)
        return SyncResult(
            pack_id=pack_id,
            version=target.version,
            install_dir=final_dir,
            plan=plan,
            downloaded_bytes=downloaded,
            hydrated_bytes=hydrated,
            reused_files=len(plan.reuse),
            already_up_to_date=False,
            verification=report,
            elapsed_seconds=self._clock() - reporter.started,
        )

    def _download(self, entry: ResourceBlobEntry, reporter: _ProgressReporter) -> int:
        base = reporter.done
        part = self.store.part_path(entry.sha256)
        if part.is_file():
            reporter.done += min(part.stat().st_size, entry.size_bytes)  # resumed bytes

        def on_bytes(count: int) -> None:
            reporter.done += count
            reporter.network += count
            reporter.emit(SyncStage.DOWNLOADING, entry.relative_path)

        reporter.emit(SyncStage.DOWNLOADING, entry.relative_path)
        outcome = self.fetcher.fetch(entry, on_bytes)
        reporter.done = base + entry.size_bytes
        reporter.files_done += 1
        return outcome.bytes_transferred

    def _hydrate(self, entry: ResourceBlobEntry, reporter: _ProgressReporter) -> int:
        reporter.emit(SyncStage.HYDRATING, entry.relative_path)
        hydrate_entry_to_store(entry, self.store)
        reporter.done += entry.size_bytes
        reporter.files_done += 1
        reporter.emit(SyncStage.HYDRATING, entry.relative_path)
        return entry.size_bytes

    def _ensure_reusable(
        self,
        entry: ResourceBlobEntry,
        reporter: _ProgressReporter,
        downloaded: int,
        hydrated: int,
    ) -> tuple[int, int]:
        """Re-check a reused CAS object; self-heal it if it rotted on disk."""
        if self.store.verify_object(entry.sha256, entry.size_bytes):
            reporter.files_done += 1
            return downloaded, hydrated
        self.store.evict(entry.sha256)
        reporter.total += entry.size_bytes
        if entry.acquisition_mode == "local_dna_seed":
            return downloaded, hydrated + self._hydrate(entry, reporter)
        return downloaded + self._download(entry, reporter), hydrated

    # -- housekeeping -----------------------------------------------------------

    def prune_old_versions(self, pack_id: str) -> tuple[str, ...]:
        """Delete every inactive version directory (and stale staging) of *pack_id*.

        Never deletes anything while the active pointer is unreadable — when
        unsure, keep.  Returns the removed version directory names.
        """
        with self._lock:
            pack_dir = self._pack_dir(pack_id)
            pointer = self._read_pointer(pack_id)
            shutil.rmtree(pack_dir / "staging", ignore_errors=True)
            versions = pack_dir / "versions"
            if pointer is None or not versions.is_dir():
                return ()
            removed: list[str] = []
            for child in sorted(versions.iterdir()):
                name = child.name
                if child.is_dir() and name != pointer.install_dir:
                    shutil.rmtree(child)
                    removed.append(name)
                elif name.endswith(".manifest.json") and name != (
                    f"{pointer.install_dir}.manifest.json"
                ):
                    child.unlink()
            return tuple(removed)

    def collect_garbage(self) -> tuple[str, ...]:
        """Evict CAS objects no active version of any installed pack references."""
        with self._lock:
            keep: set[str] = set()
            packs_root = self.root / "packs"
            if packs_root.is_dir():
                for pack_dir in sorted(packs_root.iterdir()):
                    loaded = self._load_installed(pack_dir.name)
                    if loaded is not None:
                        keep.update(entry.sha256 for entry in loaded[0].entries)
            return self.store.collect_garbage(keep)
