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
    InvalidObjectKey,
    ObjectMetadata,
    ObjectNotFound,
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


def _effective_prefix(prefix: str, *, namespace: str) -> str:
    """Resolve the listing prefix and refuse one outside this store's namespace.

    The rule lives in one place per adapter because it is part of the contract
    (see ``object_store``'s key model): a caller that passes a bucket-absolute
    prefix belonging to another store gets a loud error instead of an empty page
    that looks like "no assets".
    """
    if not prefix:
        return namespace
    validate_object_key(prefix)
    if namespace and not (prefix == namespace or prefix.startswith(f"{namespace}/")):
        raise InvalidObjectKey(f"prefix {prefix!r} is outside this store's namespace {namespace!r}")
    return prefix


class LocalObjectStore:
    """Content-addressed blob storage inside one workspace boundary."""

    name = "local"

    @property
    def namespace(self) -> str:
        """The content-addressed layout is this store's whole key space."""
        return _BLOB_PREFIX

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
        verify_existing: bool = False,
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
        if deduplicated and verify_existing and not self.verify_bytes(key):
            raise IntegrityMismatch(
                f"existing blob {key!r} does not hash to the requested content; "
                "refusing to reuse it"
            )
        # The local store re-hashes both the staged bytes and the published blob
        # inside ``store()``, so this adapter's publish path is byte-verified.
        return PutResult(
            key=key,
            sha256=sha256,
            size_bytes=size,
            created=not deduplicated,
            content_verified=True,
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

    def blob_path(self, key: str) -> Path:
        """The on-disk path of one blob (test/inspection seam, not a write path).

        Exposed so a test can corrupt a blob and prove the byte-verification law;
        it deliberately returns a path inside the existing workspace boundary and
        performs no write itself.
        """
        key = validate_object_key(key)
        self._fs.require_regular_file(key)
        return self._root.resolve() / key

    def verify_bytes(self, key: str) -> bool:
        """Re-hash the blob and compare it with the digest its key encodes.

        The local key *is* the digest, so this is a genuine byte-level check and
        not a metadata comparison.
        """
        key = validate_object_key(key)
        metadata = self.head(key)
        if metadata is None:
            raise ObjectNotFound(f"{key!r} not found")
        expected = key.rsplit("/", 1)[-1]
        return metadata.sha256 == expected

    def delete(self, key: str) -> DeleteResult:
        raise UnsupportedOperation(
            "content-addressed blobs are shared across assets; deletion is owned by "
            "the Asset Library lifecycle (see the Asset Library retention design)"
        )

    def list_keys(self, *, prefix: str = "", limit: int = 1000) -> list[str]:
        """Absolute blob keys under ``prefix`` (default: the store namespace).

        Keys are exactly the strings ``head``/``open_bytes`` accept; a prefix
        outside ``blobs/`` is refused rather than silently returning nothing.
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        effective = _effective_prefix(prefix, namespace=self.namespace)
        keys: list[str] = []
        for path in self._fs.iter_files():
            relative = path.relative_to(self._root.resolve()).as_posix()
            if not relative.startswith(effective):
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
