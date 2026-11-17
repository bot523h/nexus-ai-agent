"""Atomic installer, login gate and live progress for resource packs.

On-disk layout under ``root``::

    .sync_gc.lock                       cross-process storage lock (flock / O_EXCL)
    leases/<pid>-<token>.json           in-flight leases of running syncs
    cas/                                shared content-addressed store
    packs/<pack_id>/active.json         the ONLY commit point (atomic pointer)
    packs/<pack_id>/versions/<dir>/     immutable, verified install trees
    packs/<pack_id>/versions/<dir>.manifest.json
    packs/<pack_id>/staging/<dir>/      work in progress (always disposable)

Security posture (all fail-closed by default)
---------------------------------------------
* **Signatures (F1)** — every target manifest must carry a valid Ed25519
  signature from ``trusted_public_key``.  Without a key, or for an unsigned
  manifest, nothing is planned, written or downloaded; only an explicit
  ``allow_unsigned=True`` relaxes this.
* **Anti-rollback (F4)** — a target whose ``MAJOR.MINOR.PATCH`` is strictly
  below the active version is refused (``VersionRollbackError``), checked
  before any work *and again* under the storage lock right before the pointer
  flip, so a concurrent newer install cannot be overwritten by an older one.
  Non-semantic versions cannot prove monotonicity and are refused too unless
  ``allow_rollback=True``.
* **Concurrency (F2)** — a sync registers a *lease* (PID, expiry, every digest
  of the target manifest, its staging/install directory name) under the
  cross-process lock before it writes anything.  ``collect_garbage`` and
  ``prune_old_versions`` run under the same lock and never touch leased
  digests or directories; GC additionally spares blobs younger than
  ``min_age_seconds`` and refuses to run at all if a lease file is corrupt.
  The lock is only held for short critical sections — never while user
  callbacks run — so progress handlers may safely call GC or prune.

A sync builds a complete new tree in ``staging``, verifies it against the RFC
6962 root and the signature, moves it into ``versions`` and finally flips
``active.json`` with :func:`os.replace`.  Any failure before the flip leaves
the previous version — and the pointer to it — untouched; interrupted
downloads keep their ``.part`` files so the next attempt resumes.
"""

from __future__ import annotations

import importlib
import json
import math
import os
import re
import secrets
import shutil
import stat
import threading
import time
import uuid
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import ModuleType, TracebackType
from typing import Literal

from .content_store import LocalContentStore, fsync_directory
from .delta_and_hydration_planner import SyncAction, SyncPlan, build_sync_plan
from .errors import (
    InsufficientStorageError,
    ManifestValidationError,
    PackLeaseError,
    PackLockTimeoutError,
    PackSyncError,
    PackVerificationError,
    UnknownPackError,
    VersionRollbackError,
)
from .pack_manifest import (
    PackTier,
    PackVerificationReport,
    ResourceBlobEntry,
    ResourcePackManifest,
    enforce_manifest_signature,
    manifest_from_json,
    manifest_to_json,
    verify_installed_pack,
    verify_manifest_integrity,
)
from .resumable_range_fetcher import PackSource, ResumableRangeFetcher
from .seed_hydrator import hydrate_entry_to_store

__all__ = [
    "GateState",
    "LockBackend",
    "LoginResourceGateStatus",
    "PackGateStatus",
    "PackSyncManager",
    "ProgressCallback",
    "ResourcePackSyncManager",
    "SyncProgress",
    "SyncResult",
    "SyncStage",
    "parse_semantic_version",
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


_SEMVER_RE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_LOCK_FILE = ".sync_gc.lock"
_LEASE_KEYS = frozenset({"schema", "pid", "pack_id", "install_dir", "expires_at", "digests"})
_MANIFEST_SUFFIX = ".manifest.json"

LockBackend = Literal["auto", "flock", "exclusive_file"]


def parse_semantic_version(version: str) -> tuple[int, int, int] | None:
    """``"1.10.2" -> (1, 10, 2)``; anything not strictly ``MAJOR.MINOR.PATCH`` -> ``None``."""
    match = _SEMVER_RE.fullmatch(version)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


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


def _load_fcntl() -> ModuleType | None:
    try:
        return importlib.import_module("fcntl")
    except ImportError:  # Windows and some sandboxes have no fcntl
        return None


def _pid_is_dead(pid: int) -> bool:
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _read_lock_state(path: Path) -> tuple[int, float, dict[str, object] | None] | None:
    try:
        lstat = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(lstat.st_mode):
        return lstat.st_ino, lstat.st_mtime, None
    try:
        raw = path.read_bytes()
        parsed = json.loads(raw)
    except (OSError, ValueError):
        parsed = None
    return lstat.st_ino, lstat.st_mtime, parsed if isinstance(parsed, dict) else None


def _parse_lock_record(data: dict[str, object] | None) -> tuple[int, float, str] | None:
    if data is None:
        return None
    pid = data.get("pid")
    expires_at = data.get("expires_at")
    token = data.get("owner_token")
    if (
        isinstance(pid, int)
        and not isinstance(pid, bool)
        and isinstance(expires_at, (int, float))
        and not isinstance(expires_at, bool)
        and math.isfinite(float(expires_at))
        and isinstance(token, str)
        and bool(token)
    ):
        return pid, float(expires_at), token
    return None


class _StorageLock:
    """Exclusive cross-process lock on ``<root>/.sync_gc.lock``.

    ``flock`` (POSIX) is released by the kernel when the owner dies.  The
    portable fallback creates ``.sync_gc.lock.excl`` atomically with
    ``O_CREAT | O_EXCL`` and records the owner PID, expiry and a unique
    ``owner_token``; stale locks are broken race-freely via inode/token
    verification under a short-lived break guard.
    """

    _BREAK_GUARD_TTL = 30.0

    def __init__(
        self,
        root: Path,
        *,
        backend: LockBackend,
        timeout: float,
        stale_after: float,
        wall_clock: Callable[[], float],
    ) -> None:
        fcntl = None if backend == "exclusive_file" else _load_fcntl()
        if backend == "flock" and fcntl is None:
            raise ValueError("lock_backend='flock' requested but fcntl is unavailable")
        self._fcntl = fcntl
        self.path = root / (_LOCK_FILE if fcntl is not None else f"{_LOCK_FILE}.excl")
        self._break_path = self.path.with_name(f"{self.path.name}.break")
        self.timeout = timeout
        self.stale_after = stale_after
        self.wall_clock = wall_clock
        self._fd: int | None = None
        self._st_ino: int | None = None
        self._owner_token: str | None = None

    def _owner_record(
        self,
        token: str,
        *,
        ttl: float | None = None,
        target_lock_ino: int | None = None,
    ) -> bytes:
        now = self.wall_clock()
        lifetime = self.stale_after if ttl is None else ttl
        payload: dict[str, object] = {
            "pid": os.getpid(),
            "owner_token": token,
            "created_at": now,
            "expires_at": now + lifetime,
        }
        if target_lock_ino is not None:
            payload["target_lock_ino"] = target_lock_ino
        return json.dumps(payload).encode()

    def _is_stale_record(
        self, data: dict[str, object] | None, mtime: float, *, fallback_ttl: float | None = None
    ) -> bool:
        parsed = _parse_lock_record(data)
        if parsed is not None:
            pid, expires_at, _ = parsed
            return self.wall_clock() > expires_at or _pid_is_dead(pid)
        ttl = self.stale_after if fallback_ttl is None else fallback_ttl
        return (self.wall_clock() - mtime) > ttl

    def _is_stale_break_guard(self, data: dict[str, object] | None, mtime: float) -> bool:
        parsed = _parse_lock_record(data)
        if parsed is not None:
            pid, expires_at, _ = parsed
            return self.wall_clock() > expires_at and _pid_is_dead(pid)
        return (self.wall_clock() - mtime) > self._BREAK_GUARD_TTL

    def _guard_still_owned(self, guard: tuple[int, str]) -> bool:
        guard_ino, guard_token = guard
        state = _read_lock_state(self._break_path)
        if state is None:
            return False
        cur_ino, _, cur_data = state
        cur_token = cur_data.get("owner_token") if cur_data is not None else None
        return cur_ino == guard_ino and cur_token == guard_token

    def _reclaim_stale_break_guard(self) -> None:
        state = _read_lock_state(self._break_path)
        if state is None:
            return
        guard_ino, guard_mtime, guard_data = state
        if not self._is_stale_break_guard(guard_data, guard_mtime):
            return
        guard_token = guard_data.get("owner_token") if guard_data is not None else None
        link_path = self._break_path.with_name(
            f".{self._break_path.name}.{os.getpid()}.{uuid.uuid4().hex}.link"
        )
        trash = self._break_path.with_name(
            f".{self._break_path.name}.{os.getpid()}.{uuid.uuid4().hex}.stale"
        )
        try:
            stat_before = os.lstat(self._break_path)
            if stat_before.st_ino != guard_ino or not stat.S_ISREG(stat_before.st_mode):
                return
            os.link(self._break_path, link_path)
            linked_state = _read_lock_state(link_path)
            linked_token = (
                linked_state[2].get("owner_token")
                if linked_state is not None and linked_state[2] is not None
                else None
            )
            if (
                linked_state is None
                or linked_state[0] != guard_ino
                or linked_token != guard_token
                or os.lstat(self._break_path).st_ino != guard_ino
            ):
                return
            os.replace(self._break_path, trash)
        except FileNotFoundError:
            return
        finally:
            link_path.unlink(missing_ok=True)
            trash.unlink(missing_ok=True)

    def _try_enter_break_guard(
        self, *, target_lock_ino: int | None = None
    ) -> tuple[int, str] | None:
        token = f"{os.getpid()}:{uuid.uuid4().hex}"
        try:
            fd = os.open(self._break_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            self._reclaim_stale_break_guard()
            return None
        try:
            os.write(
                fd,
                self._owner_record(
                    token, ttl=self._BREAK_GUARD_TTL, target_lock_ino=target_lock_ino
                ),
            )
            os.fsync(fd)
            ino = os.fstat(fd).st_ino
        finally:
            os.close(fd)
        return ino, token

    def _exit_break_guard(self, guard: tuple[int, str]) -> None:
        if self._guard_still_owned(guard):
            self._break_path.unlink(missing_ok=True)

    def _try_acquire(self) -> bool:
        token = f"{os.getpid()}:{uuid.uuid4().hex}"
        if self._fcntl is not None:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                self._fcntl.flock(fd, self._fcntl.LOCK_EX | self._fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                return False
            os.ftruncate(fd, 0)
            os.write(fd, self._owner_record(token))
            self._fd = fd
            self._owner_token = token
            return True
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            self._break_if_stale()
            return False
        try:
            os.write(fd, self._owner_record(token))
            os.fsync(fd)
            self._st_ino = os.fstat(fd).st_ino
            self._owner_token = token
        finally:
            os.close(fd)
        return True

    def _break_if_stale(self) -> None:
        state = _read_lock_state(self.path)
        if state is None:
            return
        target_ino, mtime, data = state
        if not self._is_stale_record(data, mtime):
            return
        target_token = data.get("owner_token") if data is not None else None
        guard = self._try_enter_break_guard(target_lock_ino=target_ino)
        if guard is None:
            return
        try:
            current = _read_lock_state(self.path)
            if current is None:
                return
            cur_ino, cur_mtime, cur_data = current
            cur_token = cur_data.get("owner_token") if cur_data is not None else None
            if (
                cur_ino != target_ino
                or cur_token != target_token
                or not self._is_stale_record(cur_data, cur_mtime)
                or not self._guard_still_owned(guard)
            ):
                return
            trash = self.path.with_name(f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.stale")
            os.replace(self.path, trash)
            trash.unlink(missing_ok=True)
        finally:
            self._exit_break_guard(guard)

    def __enter__(self) -> _StorageLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while not self._try_acquire():
            if time.monotonic() >= deadline:
                raise PackLockTimeoutError(
                    f"storage lock {self.path} still held after {self.timeout}s"
                )
            time.sleep(0.02)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._fcntl is not None and self._fd is not None:
            self._fcntl.flock(self._fd, self._fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
            self._owner_token = None
        else:
            deadline = time.monotonic() + 0.5
            guard = self._try_enter_break_guard(target_lock_ino=self._st_ino)
            while guard is None and time.monotonic() < deadline:
                time.sleep(0.005)
                guard = self._try_enter_break_guard()
            try:
                if guard is not None:
                    current = _read_lock_state(self.path)
                    if current is not None:
                        cur_ino, _, cur_data = current
                        cur_token = cur_data.get("owner_token") if cur_data is not None else None
                        if cur_ino == self._st_ino and cur_token == self._owner_token:
                            trash = self.path.with_name(
                                f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.release"
                            )
                            os.replace(self.path, trash)
                            trash.unlink(missing_ok=True)
            finally:
                if guard is not None:
                    self._exit_break_guard(guard)
                self._st_ino = None
                self._owner_token = None


@dataclass(frozen=True)
class _Lease:
    path: Path
    pack_id: str
    install_dir: str
    expires_at: float
    digests: frozenset[str]


def _parse_lease(path: Path) -> _Lease:
    try:
        raw = json.loads(path.read_bytes())
    except (OSError, ValueError) as error:
        raise PackLeaseError(f"unreadable lease {path.name}: {error}") from error
    valid = (
        isinstance(raw, dict)
        and set(raw) == _LEASE_KEYS
        and raw["schema"] == 1
        and isinstance(raw["pid"], int)
        and isinstance(raw["pack_id"], str)
        and _PACK_ID_RE.fullmatch(raw["pack_id"]) is not None
        and isinstance(raw["install_dir"], str)
        and _DIR_NAME_RE.fullmatch(raw["install_dir"]) is not None
        and isinstance(raw["expires_at"], (int, float))
        and not isinstance(raw["expires_at"], bool)
        and isinstance(raw["digests"], list)
        and all(isinstance(d, str) and _SHA256_RE.fullmatch(d) for d in raw["digests"])
    )
    if not valid:
        raise PackLeaseError(f"malformed lease {path.name}: garbage collection refused")
    return _Lease(
        path,
        raw["pack_id"],
        raw["install_dir"],
        float(raw["expires_at"]),
        frozenset(raw["digests"]),
    )


class ResourcePackSyncManager:
    """Plans, acquires, verifies and atomically activates resource packs."""

    def __init__(
        self,
        root: Path,
        catalog: Iterable[ResourcePackManifest],
        source: PackSource,
        *,
        trusted_public_key: bytes | None = None,
        allow_unsigned: bool = False,
        allow_rollback: bool = False,
        disk_free_probe: Callable[[Path], int] = _default_disk_free,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        chunk_size: int = 256 * 1024,
        max_attempts: int = 3,
        prefer_hardlinks: bool = True,
        lock_backend: LockBackend = "auto",
        lock_timeout_seconds: float = 30.0,
        lock_stale_after_seconds: float = 600.0,
        lease_ttl_seconds: float = 6 * 3600.0,
    ) -> None:
        if not isinstance(allow_unsigned, bool) or not isinstance(allow_rollback, bool):
            raise TypeError("allow_unsigned and allow_rollback must be bools")
        if lock_backend not in ("auto", "flock", "exclusive_file"):
            raise ValueError(f"unknown lock_backend {lock_backend!r}")
        self.root = Path(root)
        self._catalog: dict[str, ResourcePackManifest] = {}
        for manifest in catalog:
            if manifest.pack_id in self._catalog:
                raise ValueError(f"duplicate pack_id in catalog: {manifest.pack_id!r}")
            self._catalog[manifest.pack_id] = manifest
        self.trusted_public_key = trusted_public_key
        self.allow_unsigned = allow_unsigned
        self.allow_rollback = allow_rollback
        self.store = LocalContentStore(self.root / "cas")
        self.fetcher = ResumableRangeFetcher(
            source, self.store, chunk_size=chunk_size, max_attempts=max_attempts
        )
        self._disk_free = disk_free_probe
        self._clock = clock
        self._wall_clock = wall_clock
        self.prefer_hardlinks = prefer_hardlinks
        self._lock_backend: LockBackend = lock_backend
        self._lock_timeout = lock_timeout_seconds
        self._lock_stale_after = lock_stale_after_seconds
        self._lease_ttl = lease_ttl_seconds
        self._lock = threading.RLock()  # serialises syncs while allowing re-entrant callbacks

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
                (versions / f"{pointer.install_dir}{_MANIFEST_SUFFIX}").read_bytes()
            )
        except (OSError, ManifestValidationError) as error:
            raise PackSyncError(f"{pack_id}: installed manifest unreadable: {error}") from error
        if manifest.pack_id != pack_id or manifest.merkle_root != pointer.merkle_root:
            raise PackSyncError(f"{pack_id}: installed manifest does not match the pointer")
        return manifest, versions / pointer.install_dir

    def active_install_dir(self, pack_id: str) -> Path | None:
        pointer = self._read_pointer(pack_id)
        if pointer is None:
            return None
        return self._pack_dir(pack_id) / "versions" / pointer.install_dir

    def installed_manifest(self, pack_id: str) -> ResourcePackManifest | None:
        loaded = self._load_installed(pack_id)
        return None if loaded is None else loaded[0]

    # -- trust policy ---------------------------------------------------------

    def _check_rollback(self, target: ResourcePackManifest) -> None:
        if self.allow_rollback:
            return
        try:
            pointer = self._read_pointer(target.pack_id)
        except PackSyncError as error:
            raise VersionRollbackError(
                pack_id=target.pack_id,
                active_version="<corrupt>",
                target_version=target.version,
                reason=f"active pointer is corrupt or unreadable ({error})",
            ) from error
        if pointer is None:
            return
        active, wanted = (
            parse_semantic_version(pointer.version),
            parse_semantic_version(target.version),
        )
        if active is None or wanted is None:
            raise VersionRollbackError(
                pack_id=target.pack_id,
                active_version=pointer.version,
                target_version=target.version,
                reason="versions are not comparable MAJOR.MINOR.PATCH values",
            )
        if wanted < active:
            raise VersionRollbackError(
                pack_id=target.pack_id,
                active_version=pointer.version,
                target_version=target.version,
                reason="the target is older than the active version",
            )

    def _verify_target(self, manifest: ResourcePackManifest) -> None:
        """Signature policy, honest totals/root and anti-rollback — before any I/O."""
        enforce_manifest_signature(
            manifest, self.trusted_public_key, allow_unsigned=self.allow_unsigned
        )
        verify_manifest_integrity(manifest)
        self._check_rollback(manifest)

    def _verify_tree(
        self, manifest: ResourcePackManifest, directory: Path
    ) -> PackVerificationReport:
        return verify_installed_pack(
            manifest,
            directory,
            trusted_public_key=self.trusted_public_key,
            allow_unsigned=self.allow_unsigned,
        )

    # -- cross-process lock & leases -------------------------------------------

    def _storage_lock(self) -> _StorageLock:
        return _StorageLock(
            self.root,
            backend=self._lock_backend,
            timeout=self._lock_timeout,
            stale_after=self._lock_stale_after,
            wall_clock=self._wall_clock,
        )

    def _live_leases(self) -> list[_Lease]:
        """Every unexpired lease; expired ones (crashed owners) are removed.

        Call only while holding the storage lock.  A corrupt lease raises
        :class:`PackLeaseError` — callers must then delete nothing.
        """
        directory = self.root / "leases"
        if not directory.is_dir():
            return []
        now = self._wall_clock()
        live: list[_Lease] = []
        for path in sorted(directory.iterdir()):
            if path.name.startswith("."):
                continue  # temp file of an in-progress atomic lease write
            lease = _parse_lease(path)
            if lease.expires_at < now:
                path.unlink(missing_ok=True)
                continue
            live.append(lease)
        return live

    def _in_flight_blobs(self) -> frozenset[str]:
        """Digests protected by live leases (requires the storage lock)."""
        return frozenset(d for lease in self._live_leases() for d in lease.digests)

    def _write_lease(self, lease: _Lease) -> None:
        record = {
            "schema": 1,
            "pid": os.getpid(),
            "pack_id": lease.pack_id,
            "install_dir": lease.install_dir,
            "expires_at": self._wall_clock() + self._lease_ttl,
            "digests": sorted(lease.digests),
        }
        _write_atomic(lease.path, json.dumps(record, sort_keys=True).encode())

    def _open_lease(self, pack_id: str, install_dir: str, digests: frozenset[str]) -> _Lease:
        name = f"{os.getpid()}-{secrets.token_hex(6)}.json"
        lease = _Lease(self.root / "leases" / name, pack_id, install_dir, 0.0, digests)
        with self._storage_lock():
            self._write_lease(lease)
        return lease

    def _renew_lease(self, lease: _Lease) -> None:
        with self._storage_lock():
            self._write_lease(lease)

    # -- login gate -----------------------------------------------------------

    def _pack_gate(
        self, target: ResourcePackManifest, cas_index: dict[str, int], available: int
    ) -> PackGateStatus:
        try:
            self._verify_target(target)  # a tampered/unsigned/rolled-back catalog is an incident
        except VersionRollbackError as error:
            if error.active_version != "<corrupt>":
                raise
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
                self._verify_tree(loaded[0], loaded[1])
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
        the gate (fail closed).  Read-only: never writes, never locks.
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

        Signature, integrity and anti-rollback checks and the disk guard all
        run before a single byte is written.  Any later failure leaves the
        active version untouched (``.part`` files survive for resumption).
        """
        with self._lock:
            return self._sync_locked(pack_id, progress_callback)

    def _sync_locked(self, pack_id: str, progress_callback: ProgressCallback | None) -> SyncResult:
        target = self._target(pack_id)
        self._verify_target(target)
        try:
            loaded = self._load_installed(pack_id)
        except PackSyncError:
            loaded = None  # corrupt pointer/manifest: rebuild from scratch
        installed = None if loaded is None else loaded[0]
        available = self._disk_free(self.root)
        plan = build_sync_plan(self.store.index(), installed, target, available)
        reporter = _ProgressReporter(
            pack_id,
            progress_callback,
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
                report = self._verify_tree(target, loaded[1])
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

        dir_name = f"{target.version}__{secrets.token_hex(6)}"
        lease = self._open_lease(pack_id, dir_name, frozenset(e.sha256 for e in target.entries))
        try:
            # re-plan now that the lease protects every target digest from GC
            plan = build_sync_plan(self.store.index(), installed, target, available)
            reporter.total = plan.network_bytes + plan.hydrate_bytes
            return self._install(target, plan, dir_name, lease, reporter)
        finally:
            lease.path.unlink(missing_ok=True)

    def _install(
        self,
        target: ResourcePackManifest,
        plan: SyncPlan,
        dir_name: str,
        lease: _Lease,
        reporter: _ProgressReporter,
    ) -> SyncResult:
        downloaded = hydrated = 0
        for item in plan.download:
            self._renew_lease(lease)
            downloaded += self._download(item.entry, reporter)
        for item in plan.hydrate:
            self._renew_lease(lease)
            hydrated += self._hydrate(item.entry, reporter)

        pack_dir = self._pack_dir(target.pack_id)
        staging = pack_dir / "staging" / dir_name
        final_dir = pack_dir / "versions" / dir_name
        manifest_path = pack_dir / "versions" / f"{dir_name}{_MANIFEST_SUFFIX}"
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
            report = self._verify_tree(target, staging)
            reporter.emit(SyncStage.ACTIVATING)  # callbacks run outside the lock
            with self._storage_lock():
                self._check_rollback(target)  # a newer version may have landed meanwhile
                _write_atomic(manifest_path, manifest_to_json(target))
                final_dir.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staging, final_dir)
                pointer = {
                    "schema": 1,
                    "pack_id": target.pack_id,
                    "version": target.version,
                    "merkle_root": target.merkle_root,
                    "install_dir": dir_name,
                }
                _write_atomic(
                    pack_dir / "active.json", json.dumps(pointer, sort_keys=True).encode()
                )
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
            pack_id=target.pack_id,
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

        Runs under the cross-process lock; never deletes the active version,
        anything owned by a live lease, or anything at all while the active
        pointer or a lease is unreadable (when unsure, keep).  Returns the
        removed version directory names.
        """
        pack_dir = self._pack_dir(pack_id)
        with self._storage_lock():
            pointer = self._read_pointer(pack_id)
            leased = {
                lease.install_dir for lease in self._live_leases() if lease.pack_id == pack_id
            }
            staging = pack_dir / "staging"
            if staging.is_dir():
                for child in sorted(staging.iterdir()):
                    if child.name not in leased:
                        shutil.rmtree(child, ignore_errors=True)
                if not any(staging.iterdir()):
                    staging.rmdir()
            versions = pack_dir / "versions"
            if pointer is None or not versions.is_dir():
                return ()
            keep = leased | {pointer.install_dir}
            removed: list[str] = []
            for child in sorted(versions.iterdir()):
                name = child.name
                if child.is_dir():
                    if name not in keep:
                        shutil.rmtree(child)
                        removed.append(name)
                elif name.endswith(_MANIFEST_SUFFIX) and name[: -len(_MANIFEST_SUFFIX)] not in keep:
                    child.unlink()
            return tuple(removed)

    def collect_garbage(
        self,
        *,
        min_age_seconds: float = 3600.0,
        protected_digests: Collection[str] = (),
    ) -> tuple[str, ...]:
        """Evict CAS objects that nothing needs any more.

        Kept: blobs referenced by any active install, blobs in a live lease
        (in-flight syncs, even in other processes), ``protected_digests``, and
        every blob younger than ``min_age_seconds`` (grace period).  Runs under
        the cross-process lock and refuses to delete anything if an active
        pointer or a lease file is unreadable.
        """
        if not isinstance(min_age_seconds, (int, float)) or not (
            math.isfinite(min_age_seconds) and min_age_seconds >= 0
        ):
            raise ValueError("min_age_seconds must be a finite number >= 0")
        if isinstance(protected_digests, str):
            raise TypeError("protected_digests must be a collection of digests, not a str")
        keep = set(protected_digests)
        with self._storage_lock():
            keep |= self._in_flight_blobs()
            packs_root = self.root / "packs"
            if packs_root.is_dir():
                for pack_dir in sorted(packs_root.iterdir()):
                    loaded = self._load_installed(pack_dir.name)
                    if loaded is not None:
                        keep.update(entry.sha256 for entry in loaded[0].entries)
            return self.store.collect_garbage(
                keep, min_age_seconds=float(min_age_seconds), now=self._wall_clock()
            )


#: Short alias used by the security audit and the studio integration layer.
PackSyncManager = ResourcePackSyncManager
