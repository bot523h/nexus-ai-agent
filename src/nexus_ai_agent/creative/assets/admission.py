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

_LOCK_NAME = ".asset-library-maintenance.lock"


@contextmanager
def asset_admission_lock(
    root: Path, *, exclusive: bool, timeout_seconds: float = 10.0
) -> Iterator[None]:
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
            yield
        finally:
            os.close(fd)
    finally:
        os.close(root_fd)


@asynccontextmanager
async def async_asset_admission_lock(
    root: Path, *, exclusive: bool, timeout_seconds: float = 10.0
) -> AsyncIterator[None]:
    """Acquire off-loop; cancellation cannot leak a late-acquired lease.

    The worker and caller synchronize the transfer of the context-manager
    lease. In particular, the worker closes it itself if the loop is already
    shutting down after cancellation, without relying on an event-loop callback.
    """
    lease = asset_admission_lock(root, exclusive=exclusive, timeout_seconds=timeout_seconds)
    guard = threading.Lock()
    abandoned = False
    acquired = False

    def acquire() -> None:
        nonlocal acquired
        lease.__enter__()
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
        yield
    finally:
        lease.__exit__(None, None, None)
