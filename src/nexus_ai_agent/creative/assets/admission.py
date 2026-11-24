"""Cooperative POSIX admission lock for a local asset-library root.

All cooperating writers and any future sweeper must use the same lock inode.
This is not an authority against a same-UID process that replaces the lock file.
"""

from __future__ import annotations

import asyncio
import fcntl
import math
import os
import stat
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Literal

_LOCK_NAME = ".asset-library-maintenance.lock"
_LEASE_ISSUER = object()


class AdmissionLease:
    """A live cooperative lock, valid only until its owning context exits.

    Reuse is not an authorization boundary against code in this interpreter or
    a non-cooperating process. The owner must outlive every reusing operation.
    """

    def __init__(
        self, root: Path, root_fd: int, fd: int, *, exclusive: bool, issuer: object
    ) -> None:
        if issuer is not _LEASE_ISSUER:
            raise PermissionError("asset admission leases must be issued by the lock")
        self._root_path = Path(os.path.abspath(root))
        self._mode: Literal["shared", "exclusive"] = "exclusive" if exclusive else "shared"
        info = os.fstat(fd)
        self._inode = (info.st_dev, info.st_ino)
        self._root_fd = root_fd
        self._fd = fd
        self._active = True

    @property
    def root_path(self) -> Path:
        return self._root_path

    @property
    def mode(self) -> Literal["shared", "exclusive"]:
        return self._mode

    @property
    def inode(self) -> tuple[int, int]:
        return self._inode

    @property
    def is_active(self) -> bool:
        return self._active

    def validate_for(self, root_path: Path, *, require_exclusive: bool = False) -> None:
        if (
            not self._active
            or Path(os.path.abspath(root_path)) != self.root_path
            or (require_exclusive and self.mode != "exclusive")
        ):
            raise PermissionError("invalid asset admission lease")
        try:
            opened = os.fstat(self._fd)
            current = os.stat(_LOCK_NAME, dir_fd=self._root_fd, follow_symlinks=False)
            root = os.stat(root_path, follow_symlinks=False)
            opened_root = os.fstat(self._root_fd)
        except OSError as exc:
            raise PermissionError("invalid asset admission lease") from exc
        if (
            not stat.S_ISREG(current.st_mode)
            or (opened.st_dev, opened.st_ino) != self.inode
            or (current.st_dev, current.st_ino) != self.inode
            or (root.st_dev, root.st_ino) != (opened_root.st_dev, opened_root.st_ino)
        ):
            raise PermissionError("asset admission lease inode changed")

    def _invalidate(self) -> None:
        self._active = False


@contextmanager
def asset_admission_lock(
    root: Path,
    *,
    exclusive: bool,
    timeout_seconds: float = 10.0,
    admission_lease: AdmissionLease | None = None,
) -> Iterator[AdmissionLease]:
    """Hold a shared or exclusive kernel lock until the context exits.

    The lock is advisory and process cooperating. Never unlink the lock file.
    """
    if type(exclusive) is not bool:
        raise ValueError("exclusive must be boolean")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("invalid admission timeout")
    if admission_lease is not None:
        if type(admission_lease) is not AdmissionLease:
            raise PermissionError("invalid asset admission lease")
        AdmissionLease.validate_for(admission_lease, root, require_exclusive=exclusive)
        yield admission_lease
        return
    root_fd = os.open(root, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
            raise ValueError("asset root is not a directory")
        fd = os.open(
            _LOCK_NAME,
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            0o600,
            dir_fd=root_fd,
        )
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise PermissionError("unsafe asset admission lock")
            deadline = time.monotonic() + timeout_seconds
            mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            while True:
                try:
                    fcntl.flock(fd, mode | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("asset admission lock timed out") from None
                    time.sleep(min(0.01, remaining))
            current = os.stat(_LOCK_NAME, dir_fd=root_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise PermissionError("asset admission lock was replaced")
            issued = AdmissionLease(root, root_fd, fd, exclusive=exclusive, issuer=_LEASE_ISSUER)
            try:
                yield issued
            finally:
                issued._invalidate()
        finally:
            os.close(fd)
    finally:
        os.close(root_fd)


@asynccontextmanager
async def async_asset_admission_lock(
    root: Path,
    *,
    exclusive: bool,
    timeout_seconds: float = 10.0,
    admission_lease: AdmissionLease | None = None,
) -> AsyncIterator[AdmissionLease]:
    """Acquire off-loop; cancellation cannot leak a late-acquired lease.

    The worker and caller synchronize the transfer of the context-manager
    lease. In particular, the worker closes it itself if the loop is already
    shutting down after cancellation, without relying on an event-loop callback.
    """
    if admission_lease is not None:
        with asset_admission_lock(
            root,
            exclusive=exclusive,
            timeout_seconds=timeout_seconds,
            admission_lease=admission_lease,
        ) as existing:
            yield existing
        return
    lease = asset_admission_lock(root, exclusive=exclusive, timeout_seconds=timeout_seconds)
    guard = threading.Lock()
    abandoned = False
    acquired = False
    issued: AdmissionLease | None = None

    def acquire() -> None:
        nonlocal acquired, issued
        issued = lease.__enter__()
        with guard:
            if abandoned:
                lease.__exit__(None, None, None)
            else:
                acquired = True

    future = asyncio.get_running_loop().run_in_executor(None, acquire)
    try:
        await asyncio.shield(future)
    except asyncio.CancelledError:
        with guard:
            abandoned = True
            if acquired:
                lease.__exit__(None, None, None)
                acquired = False

        # If acquisition fails after the caller has left, consume that exception.
        def consume(done: asyncio.Future[None]) -> None:
            if not done.cancelled():
                done.exception()

        future.add_done_callback(consume)
        raise
    try:
        assert issued is not None
        yield issued
    finally:
        lease.__exit__(None, None, None)
