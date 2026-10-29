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

import hashlib
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
    content_digest_from_key,
    content_key_for,
    is_within_namespace,
    resolve_namespace_prefix,
    utcnow,
    validate_object_key,
)
from nexus_ai_agent.tools.filesystem_policy import FilesystemBoundaryError, WorkspaceFilesystem

_BLOB_PREFIX = "blobs"


def _effective_prefix(prefix: str, *, namespace: str) -> str:
    """Resolve the listing prefix through the contract's single boundary rule.

    Both adapters delegate here (via :func:`resolve_namespace_prefix`) so the
    sibling-namespace rule cannot drift between backends.
    """
    return resolve_namespace_prefix(prefix, namespace)


class LocalObjectStore:
    """Content-addressed blob storage inside one workspace boundary."""

    name = "local"

    @property
    def namespace(self) -> str:
        """The content-addressed layout is this store's whole key space."""
        return _BLOB_PREFIX

    def _require_owned(self, key: str, *, operation: str) -> str:
        """Validate the key shape *and* prove it belongs to this store's namespace.

        Runs before any filesystem access, so a foreign key can never reach
        ``hash_file``/``require_regular_file`` and expose another workspace area
        (``staging/…`` is a valid key shape, and it is not ours).
        """
        key = validate_object_key(key)
        if not is_within_namespace(key, self.namespace):
            raise InvalidObjectKey(
                f"{operation}: key {key!r} is outside this store's namespace {self.namespace!r}"
            )
        return key

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
        """Return the object, refusing to buffer past ``max_bytes``.

        The bound is enforced *inside* the iteration, so an oversized object is
        rejected after reading at most one chunk beyond the limit instead of
        after being materialized whole.
        """
        return b"".join(self.iter_chunks(key, max_bytes=max_bytes))

    def iter_chunks(
        self, key: str, *, chunk_size: int = 1 << 20, max_bytes: int | None = None
    ) -> Iterator[bytes]:
        """Stream one blob chunk by chunk, hashing incrementally as it goes.

        Nothing is materialized: the bytes come from the workspace boundary's
        contained chunked reader. The digest the key encodes is compared at the
        end of a full iteration — a consumer that stops early has consumed bytes
        that were never verified, exactly as the contract documents.
        """
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if max_bytes is not None and max_bytes < 0:
            raise ValueError("max_bytes must not be negative")
        key = self._require_owned(key, operation="read")
        if not self._fs.exists(key):
            raise ObjectNotFound(f"{key!r} not found")

        digest = hashlib.sha256()
        total = 0
        try:
            for chunk in self._fs.iter_bytes(key, chunk_size=chunk_size):
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise ObjectTooLarge(f"object {key!r} exceeds the {max_bytes}-byte read bound")
                digest.update(chunk)
                yield chunk
        except (FilesystemBoundaryError, OSError) as exc:
            raise ObjectStoreError(f"local read failed for {key!r}") from exc

        expected = content_digest_from_key(key)
        if expected is not None and digest.hexdigest() != expected:
            raise IntegrityMismatch("stored content does not match its content key")

    def head(self, key: str) -> ObjectMetadata | None:
        """Measure one blob. The local store has no cheap HEAD: this re-hashes.

        A key outside ``blobs/`` raises :class:`InvalidObjectKey` instead of
        returning ``None``: the staging area, the database file and every other
        workspace path are valid key *shapes*, and reporting them as "absent"
        would let a caller probe them without an error.
        """
        key = self._require_owned(key, operation="head")
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
        performs no write itself. Ownership is enforced first, so no path outside
        ``blobs/`` can be handed out.
        """
        key = self._require_owned(key, operation="blob_path")
        self._fs.require_regular_file(key)
        return self._root.resolve() / key

    def verify_bytes(self, key: str) -> bool:
        """Re-hash the blob and compare it with the digest its key encodes.

        The local key *is* the digest, so this is a genuine byte-level check and
        not a metadata comparison. Bytes are streamed, so a large object is never
        buffered just to be verified.
        """
        key = self._require_owned(key, operation="verify_bytes")
        if not self._fs.exists(key):
            raise ObjectNotFound(f"{key!r} not found")
        expected = content_digest_from_key(key)
        digest = hashlib.sha256()
        try:
            for chunk in self._fs.iter_bytes(key):
                digest.update(chunk)
        except (FilesystemBoundaryError, OSError):
            return False
        if expected is None:
            return False
        return digest.hexdigest() == expected

    def delete(self, key: str) -> DeleteResult:
        """Refuse, but only after the key is proven to be inside this namespace."""
        self._require_owned(key, operation="delete")
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
            # Ownership first: a sibling directory (``blobs-other/…``) is not ours
            # even when it shares our namespace's literal prefix.
            if not is_within_namespace(relative, self.namespace):
                continue
            if effective and not relative.startswith(effective):
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
        measured, _ = self.measure_namespace()
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

    def measure_namespace(self) -> tuple[int, int]:
        """Measure ``(bytes, objects)`` of this store's own namespace.

        Sibling namespaces are excluded by the segment boundary, so
        ``blobs-other/…`` never inflates this store's measurement.
        """
        total = 0
        count = 0
        for path in self._fs.iter_files():
            relative = path.relative_to(self._root.resolve()).as_posix()
            if not is_within_namespace(relative, self.namespace):
                continue
            try:
                total += path.stat().st_size
            except OSError:
                continue
            count += 1
        return total, count

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
