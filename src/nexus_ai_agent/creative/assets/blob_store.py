"""Local, content-addressed blob store (the V1 storage adapter).

The physical key is derived from the *measured* digest, never the caller's
filename.  All filesystem access goes through the single
:class:`~nexus_ai_agent.tools.filesystem_policy.WorkspaceFilesystem` boundary,
so there is no second security layer: containment, symlink rejection and
descriptor-relative ``O_NOFOLLOW`` operations are inherited, not re-implemented.

This adapter is composed explicitly by the application; there is no silent
fallback from a (future) remote store to local disk.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import secrets
import stat
import threading
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import BinaryIO

from nexus_ai_agent.creative.assets.admission import (
    AdmissionLease,
    asset_admission_lock,
    async_asset_admission_lock,
)
from nexus_ai_agent.creative.assets.errors import (
    BlobStoreError,
    ContentIntegrityError,
    ContentTooLarge,
)
from nexus_ai_agent.tools.filesystem_policy import (
    ContentLimitExceeded,
    FilesystemBoundaryError,
    FilesystemIntegrityError,
    WorkspaceFilesystem,
)

_STAGING_PREFIX = "staging"
_BLOB_PREFIX = "blobs"
_LOGGER = logging.getLogger(__name__)
# Shared by all store instances in this process, keyed by canonical root and staging key.
_IN_FLIGHT_LOCK = threading.Lock()
_IN_FLIGHT_PUBLICATIONS: dict[tuple[Path, str], list[tuple[threading.Event, threading.Event]]] = {}


class LocalContentAddressedBlobStore:
    """A filesystem blob store keyed by SHA-256, inside one workspace boundary."""

    def __init__(self, root: str | Path, *, lock_timeout_seconds: float = 10.0) -> None:
        path = Path(root).expanduser()
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
                raise BlobStoreError("blob root is not a directory")
            os.fchmod(fd, 0o700)
            if stat.S_IMODE(os.fstat(fd).st_mode) != 0o700:
                raise BlobStoreError("blob root permissions are unsafe")
        finally:
            os.close(fd)
        self._fs = WorkspaceFilesystem(root, lock_timeout_seconds=lock_timeout_seconds)
        self._admission_timeout_seconds = lock_timeout_seconds
        self._pending_staging_cleanups: set[str] = set()
        self._cleanup_lock = threading.Lock()
        self._fs.ensure_directory(_STAGING_PREFIX)
        self._fs.ensure_directory(_BLOB_PREFIX)
        stage_fd = self._fs._open_dir((_STAGING_PREFIX,))
        try:
            os.fchmod(stage_fd, 0o700)
            if stat.S_IMODE(os.fstat(stage_fd).st_mode) != 0o700:
                raise BlobStoreError("staging permissions are unsafe")
        finally:
            os.close(stage_fd)

    def admission(self) -> AbstractAsyncContextManager[AdmissionLease]:
        """Hold shared admission for the surrounding async transaction."""
        return async_asset_admission_lock(
            self._fs.root, exclusive=False, timeout_seconds=self._admission_timeout_seconds
        )

    @staticmethod
    def blob_key_for(sha256: str) -> str:
        """Return the deterministic content key for a 64-hex digest."""
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise BlobStoreError("digest is not a 64-character lowercase hex SHA-256")
        return f"{_BLOB_PREFIX}/{sha256[:2]}/{sha256}"

    def new_staging_key(self) -> str:
        """Allocate a fresh, unguessable staging key (recorded before staging)."""
        return f"{_STAGING_PREFIX}/{secrets.token_hex(16)}"

    def stage_to(self, staging_key: str, source: BinaryIO, *, max_bytes: int) -> tuple[str, int]:
        """Stream ``source`` into the pre-allocated ``staging_key``.

        Returns ``(sha256_hex, size_bytes)`` measured from the written bytes.
        ``max_bytes`` is enforced while reading; oversize input raises
        :class:`~nexus_ai_agent.creative.assets.errors.ContentTooLarge` and
        leaves no staging residue.
        """
        try:
            with asset_admission_lock(
                self._fs.root, exclusive=False, timeout_seconds=self._admission_timeout_seconds
            ):
                _, sha256, size = self._fs.write_stream(staging_key, source, max_bytes=max_bytes)
        except ContentLimitExceeded as exc:
            raise ContentTooLarge(str(exc)) from exc
        except (FilesystemBoundaryError, OSError) as exc:
            raise BlobStoreError("staging write failed inside the boundary") from exc
        return sha256, size

    def stage(self, source: BinaryIO, *, max_bytes: int) -> tuple[str, str, int]:
        """Stream ``source`` into a fresh staging file (key generated here)."""
        staging_key = self.new_staging_key()
        sha256, size = self.stage_to(staging_key, source, max_bytes=max_bytes)
        return staging_key, sha256, size

    def hash_stream(self, source: BinaryIO, *, max_bytes: int) -> tuple[str, int]:
        """Measure ``(sha256, size)`` of a stream without any filesystem effect.

        Used to prove a replayed idempotency key carries the same content; the
        bounded read makes it safe against an unbounded or hostile stream.
        """
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = source.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise ContentTooLarge(f"stream exceeds the {max_bytes}-byte limit")
            digest.update(chunk)
        return digest.hexdigest(), size

    def store(self, staging_key: str, blob_key: str) -> tuple[str, int, bool]:
        """Publish staged bytes to ``blob_key``; return ``(sha256, size, dedup)``.

        The staged digest is re-measured, the key is proven to match it, and an
        existing blob is verified before reuse (never silently overwritten with
        mismatched bytes). Publication is an atomic no-overwrite hard link.
        """
        with asset_admission_lock(
            self._fs.root, exclusive=False, timeout_seconds=self._admission_timeout_seconds
        ):
            return self._store_admitted(staging_key, blob_key)

    def _store_admitted(self, staging_key: str, blob_key: str) -> tuple[str, int, bool]:
        parts = staging_key.split("/")
        if len(parts) != 2 or parts[0] != _STAGING_PREFIX or parts[1] in ("", ".", ".."):
            raise BlobStoreError("staging key is outside the staging namespace")
        try:
            sha256, size = self._fs.hash_file(staging_key)
        except (FilesystemBoundaryError, OSError) as exc:
            raise BlobStoreError("staged content is missing or unreadable") from exc
        if blob_key != self.blob_key_for(sha256):
            raise BlobStoreError("blob key does not match the measured staged content")
        try:
            published = self._fs.link_within_if_absent(
                staging_key, blob_key, sha256=sha256, size=size
            )
        except FilesystemIntegrityError as exc:
            raise ContentIntegrityError("publication failed integrity verification") from exc
        except TimeoutError:
            raise
        except (FilesystemBoundaryError, OSError) as exc:
            raise BlobStoreError("blob publication failed inside the boundary") from exc
        try:
            self._fs.unlink(staging_key)
        except (OSError, FilesystemBoundaryError):
            with self._cleanup_lock:
                self._pending_staging_cleanups.add(staging_key)
            try:
                self._record_cleanup_marker(staging_key)
            except (OSError, FilesystemBoundaryError):
                _LOGGER.warning("blob published; durable cleanup marker could not be recorded")
            _LOGGER.warning("blob published; staging cleanup deferred")
        return sha256, size, not published

    def _record_cleanup_marker(self, staging_key: str) -> None:
        """Atomically persist a cleanup hint; ingest state remains authoritative.

        ``write_stream`` uses a no-follow temporary file, fsyncs its bytes,
        replaces the marker, and fsyncs the parent directory. A pre-existing
        corrupt marker is replaced rather than treated as proof of persistence.
        """
        payload = staging_key.encode("utf-8")
        marker = ".cleanup-" + hashlib.sha256(payload).hexdigest()
        self._fs.write_stream(f"{_STAGING_PREFIX}/{marker}", io.BytesIO(payload), max_bytes=4096)

    def cleanup_orphaned_staging(
        self, *, active_staging_keys: set[str], max_entries: int = 100
    ) -> int:
        """Retry marked removals using a caller-supplied, authoritative active set.

        Caller must fence new ingest/recovery work while collecting and using the
        active set. Unmarked files are never guessed to be orphans. Advisory
        coordination does not protect against a non-cooperating same-UID writer.
        """
        if type(max_entries) is not int or max_entries < 1:
            raise ValueError("max_entries must be a positive integer")
        parent = self._fs._open_dir((_STAGING_PREFIX,))
        removed = 0
        try:
            for marker in sorted(os.listdir(parent)):
                if removed >= max_entries:
                    break
                if not marker.startswith(".cleanup-") or len(marker) != 73:
                    continue
                try:
                    fd = os.open(
                        marker,
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                        dir_fd=parent,
                    )
                    try:
                        if not stat.S_ISREG(os.fstat(fd).st_mode):
                            continue
                        payload = os.read(fd, 4097)
                    finally:
                        os.close(fd)
                    if len(payload) > 4096 or hashlib.sha256(payload).hexdigest() != marker[9:]:
                        continue
                    key = payload.decode("utf-8")
                    parts = key.split("/")
                    if (
                        len(parts) != 2
                        or parts[0] != _STAGING_PREFIX
                        or parts[1] in ("", ".", "..")
                        or key in active_staging_keys
                        or key in self.in_flight_staging_keys()
                    ):
                        continue
                    try:
                        info = os.stat(parts[1], dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        info = None
                    if info is not None and not stat.S_ISREG(info.st_mode):
                        continue
                    self._fs.unlink(key)
                    os.unlink(marker, dir_fd=parent)
                    os.fsync(parent)
                    with self._cleanup_lock:
                        self._pending_staging_cleanups.discard(key)
                    removed += 1
                except (OSError, FilesystemBoundaryError, UnicodeError):
                    continue
            return removed
        finally:
            os.close(parent)

    def reconcile_staging_cleanups(
        self, *, active_staging_keys: frozenset[str] = frozenset()
    ) -> tuple[str, ...]:
        """Retry this process's deferred cleanups; return keys still pending.

        This in-memory queue does not survive process restart; durable ingest
        recovery remains responsible for crash reconciliation.
        """
        with self._cleanup_lock:
            for key in tuple(self._pending_staging_cleanups):
                if key in active_staging_keys or key in self.in_flight_staging_keys():
                    continue
                try:
                    self._fs.unlink(key)
                except (OSError, FilesystemBoundaryError):
                    continue
                self._pending_staging_cleanups.discard(key)
            return tuple(sorted(self._pending_staging_cleanups))

    def publish_from_staging(self, staging_key: str, blob_key: str) -> tuple[str, int, bool]:
        """Publish a staged blob using the canonical synchronous operation."""
        return self.store(staging_key, blob_key)

    async def store_async(self, staging_key: str, blob_key: str) -> tuple[str, int, bool]:
        """Publish through the tracked, admission-fenced async path."""
        return await self.publish_from_staging_async(staging_key, blob_key)

    async def publish_from_staging_async(
        self, staging_key: str, blob_key: str
    ) -> tuple[str, int, bool]:
        """Track the worker until it actually finishes, including after cancellation.

        This is process-local coordination, not a cross-process admission fence.
        """
        identity = (self._fs.root, staging_key)
        completed, detached = threading.Event(), threading.Event()
        entry = (completed, detached)
        with _IN_FLIGHT_LOCK:
            _IN_FLIGHT_PUBLICATIONS.setdefault(identity, []).append(entry)

        def finish() -> None:
            with _IN_FLIGHT_LOCK:
                entries = _IN_FLIGHT_PUBLICATIONS[identity]
                entries.remove(entry)
                if not entries:
                    del _IN_FLIGHT_PUBLICATIONS[identity]
                completed.set()

        # Acquire before submitting the publication worker.  Otherwise an
        # executor-queued worker can outlive a cancelled caller without owning
        # a kernel lock, allowing another process to enter maintenance first.
        lease = asset_admission_lock(
            self._fs.root, exclusive=False, timeout_seconds=self._admission_timeout_seconds
        )
        loop = asyncio.get_running_loop()
        try:
            acquisition = loop.run_in_executor(None, lease.__enter__)
        except BaseException:
            finish()
            raise

        def abandon_acquisition(done: asyncio.Future[AdmissionLease]) -> None:
            try:
                if not done.cancelled() and done.exception() is None:
                    lease.__exit__(None, None, None)
            finally:
                finish()

        try:
            await asyncio.shield(acquisition)
        except asyncio.CancelledError:
            detached.set()
            acquisition.add_done_callback(abandon_acquisition)
            raise
        except BaseException:
            finish()
            raise

        def publish() -> tuple[str, int, bool]:
            try:
                return self.publish_from_staging(staging_key, blob_key)
            finally:
                try:
                    lease.__exit__(None, None, None)
                finally:
                    finish()

        try:
            future = loop.run_in_executor(None, publish)
        except BaseException:
            try:
                lease.__exit__(None, None, None)
            finally:
                finish()
            raise

        def consume_result(done: asyncio.Future[tuple[str, int, bool]]) -> None:
            if not done.cancelled():
                done.exception()

        future.add_done_callback(consume_result)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            detached.set()
            raise

    async def wait_for_in_flight_publication(
        self, staging_key: str, *, timeout_seconds: float = 0.1
    ) -> bool:
        """Wait briefly for detached process-local publication workers."""
        with _IN_FLIGHT_LOCK:
            entries = tuple(_IN_FLIGHT_PUBLICATIONS.get((self._fs.root, staging_key), ()))
        clock = asyncio.get_running_loop().time
        deadline = clock() + timeout_seconds
        for completed, detached in entries:
            if detached.is_set() and not await asyncio.to_thread(
                completed.wait, max(0.0, deadline - clock())
            ):
                return False
        return True

    def in_flight_staging_keys(self) -> set[str]:
        """Return a snapshot, not a substitute for a cross-process fence."""
        with _IN_FLIGHT_LOCK:
            return {key for root, key in _IN_FLIGHT_PUBLICATIONS if root == self._fs.root}

    def verify(self, blob_key: str, *, sha256: str, size_bytes: int) -> bool:
        """True iff the stored blob exists and matches the recorded identity."""
        try:
            return self._fs.hash_file(blob_key) == (sha256, size_bytes)
        except (FilesystemBoundaryError, OSError):
            return False

    def open(self, blob_key: str) -> bytes:
        """Return the stored bytes, re-verifying them against the content key.

        Because the key embeds the digest, the returned bytes are checked to
        hash back to that digest: a tampered or substituted blob raises rather
        than being served.
        """
        try:
            data = self._fs.read_bytes(blob_key)
        except (FilesystemBoundaryError, OSError) as exc:
            raise ContentIntegrityError("stored content is missing or unreadable") from exc
        expected = blob_key.rsplit("/", 1)[-1]
        if hashlib.sha256(data).hexdigest() != expected:
            raise ContentIntegrityError("stored content does not match its content key")
        return data

    def remove_staging(self, staging_key: str) -> None:
        """Best-effort removal of a staging file (cleanup on controlled failure)."""
        with asset_admission_lock(
            self._fs.root, exclusive=False, timeout_seconds=self._admission_timeout_seconds
        ):
            try:
                self._fs.unlink(staging_key)
            except (FilesystemBoundaryError, OSError):
                return

    def stage_identity(self, staging_key: str) -> tuple[str, int]:
        """Measure ``(sha256, size)`` of a staged file (recovery path)."""
        try:
            return self._fs.hash_file(staging_key)
        except (FilesystemBoundaryError, OSError) as exc:
            raise BlobStoreError("staged content is missing or unreadable") from exc

    def read_staging_header(self, staging_key: str, size: int) -> bytes:
        """Read only the leading bytes of a staged file for classification."""
        try:
            return self._fs.read_bytes(staging_key, max_bytes=size)
        except (FilesystemBoundaryError, OSError) as exc:
            raise BlobStoreError("staged content is missing or unreadable") from exc
