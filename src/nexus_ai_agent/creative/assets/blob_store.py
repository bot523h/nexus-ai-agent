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

import hashlib
import secrets
from pathlib import Path
from typing import BinaryIO

from nexus_ai_agent.creative.assets.errors import (
    BlobStoreError,
    ContentIntegrityError,
    ContentTooLarge,
)
from nexus_ai_agent.tools.filesystem_policy import (
    ContentLimitExceeded,
    FilesystemBoundaryError,
    WorkspaceFilesystem,
)

_STAGING_PREFIX = "staging"
_BLOB_PREFIX = "blobs"


class LocalContentAddressedBlobStore:
    """A filesystem blob store keyed by SHA-256, inside one workspace boundary."""

    def __init__(self, root: str | Path) -> None:
        Path(root).expanduser().mkdir(parents=True, exist_ok=True)
        self._fs = WorkspaceFilesystem(root)
        self._fs.ensure_directory(_STAGING_PREFIX)
        self._fs.ensure_directory(_BLOB_PREFIX)

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
        mismatched bytes).  A successful move is atomic within the filesystem.
        """
        try:
            sha256, size = self._fs.hash_file(staging_key)
        except (FilesystemBoundaryError, OSError) as exc:
            raise BlobStoreError("staged content is missing or unreadable") from exc
        if blob_key != self.blob_key_for(sha256):
            raise BlobStoreError("blob key does not match the measured staged content")
        if self._fs.exists(blob_key):
            try:
                existing = self._fs.hash_file(blob_key)
            except (FilesystemBoundaryError, OSError) as exc:
                raise ContentIntegrityError("existing blob is unreadable") from exc
            if existing != (sha256, size):
                raise ContentIntegrityError("existing blob does not match its content key")
            self._fs.unlink(staging_key)
            return sha256, size, True
        try:
            self._fs.rename_within(staging_key, blob_key)
            final = self._fs.hash_file(blob_key)
        except (FilesystemBoundaryError, OSError) as exc:
            raise BlobStoreError("blob publication failed inside the boundary") from exc
        if final != (sha256, size):
            raise ContentIntegrityError("published blob does not match the staged bytes")
        return sha256, size, False

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
