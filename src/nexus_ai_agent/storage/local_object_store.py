"""Local content-addressed backend implementing the object-store contract.

This adapter is deliberately a thin shell over the *existing*
:class:`~nexus_ai_agent.creative.assets.blob_store.LocalContentAddressedBlobStore`
and the single :class:`~nexus_ai_agent.tools.filesystem_policy.WorkspaceFilesystem`
boundary. Nothing about containment, symlink rejection or ``O_NOFOLLOW`` is
re-implemented here: a second filesystem implementation would be a second
security layer, and that is exactly what the Asset Library refuses to have.

Two honest limitations are declared rather than papered over:

* **No delete.** Content-addressed blobs are shared by every asset version that
  hashes to them; removing one without a reference count silently corrupts the
  others. Garbage collection belongs to the Asset Library lifecycle, so
  :meth:`delete` raises :class:`UnsupportedOperation` and the capability says so.
* **No metadata sidecar.** The local store persists bytes only (asset metadata
  lives in the database), so a caller that demands typed metadata is told so
  instead of having it dropped.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import BinaryIO

from nexus_ai_agent.creative.assets.blob_store import LocalContentAddressedBlobStore
from nexus_ai_agent.creative.assets.errors import (
    BlobStoreError,
    ContentIntegrityError,
    ContentTooLarge,
)
from nexus_ai_agent.storage.object_store import (
    BackendCapabilities,
    CapacityKind,
    CapacityReport,
    DeleteResult,
    HealthReport,
    IntegrityMismatch,
    ObjectMetadata,
    ObjectStoreError,
    ObjectTooLarge,
    PutResult,
    UnsupportedOperation,
    content_key_for,
    utcnow,
    validate_object_key,
)
from nexus_ai_agent.tools.filesystem_policy import FilesystemBoundaryError, WorkspaceFilesystem

_BLOB_PREFIX = "blobs"


class LocalObjectStore:
    """Content-addressed blob storage inside one workspace boundary."""

    name = "local"

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).expanduser()
        self._blobs = LocalContentAddressedBlobStore(self._root)
        # Same boundary class, same root: listing/measuring a blob has no place
        # in the ingest-staging API, and its own ``_fs`` is private.
        self._fs = WorkspaceFilesystem(self._root)

    # -- capabilities ------------------------------------------------------ #
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_streaming=True,
            supports_range_reads=True,
            supports_multipart=False,
            supports_presign=False,
            supports_delete=False,
            delete_is_soft=False,
            supports_custom_metadata=False,
            max_object_bytes=None,
            notes=(
                "local content-addressed store: immutable blobs are shared across assets, "
                "so deletion is owned by the Asset Library lifecycle, not by this adapter"
            ),
        )

    def is_configured(self) -> bool:
        return self._root.exists()

    # -- writes ------------------------------------------------------------ #
    def put_stream(
        self,
        source: BinaryIO,
        *,
        max_bytes: int,
        content_type: str | None = None,
        expected_sha256: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> PutResult:
        if content_type is not None or metadata:
            raise UnsupportedOperation(
                "the local content-addressed store has no metadata sidecar; "
                "content type and custom metadata belong to the database record"
            )
        staging_key = self._blobs.new_staging_key()
        try:
            sha256, size = self._blobs.stage_to(staging_key, source, max_bytes=max_bytes)
        except ContentTooLarge as exc:
            raise ObjectTooLarge(str(exc)) from exc
        except (BlobStoreError, FilesystemBoundaryError, OSError) as exc:
            raise ObjectStoreError("local staging write failed inside the boundary") from exc

        if expected_sha256 is not None and sha256 != expected_sha256:
            self._blobs.remove_staging(staging_key)
            raise IntegrityMismatch("stream digest does not match the pinned expected_sha256")

        key = content_key_for(sha256)
        try:
            stored_sha, stored_size, deduplicated = self._blobs.store(staging_key, key)
        except ContentIntegrityError as exc:
            raise IntegrityMismatch(str(exc)) from exc
        except (BlobStoreError, FilesystemBoundaryError, OSError) as exc:
            raise ObjectStoreError("local blob publication failed inside the boundary") from exc

        if (stored_sha, stored_size) != (sha256, size):
            raise IntegrityMismatch("published blob does not match the measured staged bytes")
        if not self._blobs.verify(key, sha256=sha256, size_bytes=size):
            raise IntegrityMismatch("published blob failed post-write verification")
        return PutResult(
            key=key,
            sha256=sha256,
            size_bytes=size,
            created=not deduplicated,
        )

    # -- reads ------------------------------------------------------------- #
    def open_bytes(self, key: str, *, max_bytes: int | None = None) -> bytes:
        key = validate_object_key(key)
        try:
            data = self._blobs.open(key)
        except ContentIntegrityError as exc:
            raise IntegrityMismatch(str(exc)) from exc
        except (BlobStoreError, FilesystemBoundaryError, OSError) as exc:
            raise ObjectStoreError(f"local read failed for {key!r}") from exc
        if max_bytes is not None and len(data) > max_bytes:
            raise ObjectTooLarge(f"object {key!r} exceeds the {max_bytes}-byte read bound")
        return data

    def iter_chunks(self, key: str, *, chunk_size: int = 1 << 20) -> Iterator[bytes]:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        data = self.open_bytes(key)
        for start in range(0, len(data), chunk_size):
            yield data[start : start + chunk_size]

    def head(self, key: str) -> ObjectMetadata | None:
        """Measure one blob. The local store has no cheap HEAD: this re-hashes."""
        key = validate_object_key(key)
        try:
            sha256, size = self._fs.hash_file(key)
        except (FilesystemBoundaryError, OSError):
            return None
        return ObjectMetadata(key=key, size_bytes=size, sha256=sha256)

    def exists(self, key: str) -> bool:
        return self.head(key) is not None

    def delete(self, key: str) -> DeleteResult:
        raise UnsupportedOperation(
            "content-addressed blobs are shared across assets; deletion is owned by "
            "the Asset Library lifecycle (see the Asset Library retention design)"
        )

    def list_keys(self, *, prefix: str = "", limit: int = 1000) -> list[str]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if prefix:
            prefix = validate_object_key(prefix)
        keys: list[str] = []
        for path in self._fs.iter_files():
            relative = path.relative_to(self._root.resolve()).as_posix()
            if not relative.startswith(f"{_BLOB_PREFIX}/"):
                continue
            if prefix and not relative.startswith(prefix):
                continue
            keys.append(relative)
            if len(keys) >= limit:
                break
        return sorted(keys)

    # -- truth ------------------------------------------------------------- #
    def capacity(self) -> CapacityReport:
        """Report a measurement, not a quota.

        The volume's free space is shared with the database, the job sidecar and
        every other tenant of the host, so turning it into a "Nagar quota" would
        be fiction. What is measured is exactly what this backend owns: the bytes
        of its own content-addressed blobs.
        """
        measured = 0
        for path in self._fs.iter_files():
            if path.relative_to(self._root.resolve()).as_posix().startswith(f"{_BLOB_PREFIX}/"):
                try:
                    measured += path.stat().st_size
                except OSError:
                    continue
        return CapacityReport(
            backend=self.name,
            kind=CapacityKind.UNKNOWN,
            measured_at=utcnow(),
            source="local filesystem: no Nagar-owned quota is configured for this volume",
            measured_unique_bytes=measured,
            note=(
                "volume free space is shared with the application database and is not a Nagar quota"
            ),
        )

    def health(self) -> HealthReport:
        ok = (self._root / _BLOB_PREFIX).is_dir() and (self._root / "staging").is_dir()
        return HealthReport(
            backend=self.name,
            ok=ok,
            detail="blob and staging directories present"
            if ok
            else "workspace directories missing",
            checked_at=utcnow(),
        )
