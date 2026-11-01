"""Provider-neutral object-storage contract for files Nagar itself owns.

Why a third seam
----------------
The tree already carried two storage abstractions, and neither can carry a
user asset safely:

* ``storage/providers/base.py::StorageProvider`` is a *path-based maintenance*
  seam (``upload(local_path, remote_key)``): no checksum, no idempotency, no
  capability declaration, no capacity semantics. Cloudflare R2 backs it as the
  "technical blob tier" (database backups, heavy RAG documents).
* ``creative/assets/ports.py::BlobStore`` is an *ingest staging* seam: its
  contract is a filesystem staging dance (``new_staging_key`` → ``stage_to`` →
  ``store``). A remote object store cannot honestly implement it, and forcing it
  to would hide the network behind filesystem-shaped methods.

This module is therefore the typed contract for **content-addressed, immutable
blob storage** — the layer the Asset Library will eventually sit on. It is
deliberately narrow: only the operations the application needs today, each with
an explicit failure mode. It is *not* a registry, not a placement engine and not
a connector SDK; those are later waves and must extend this seam rather than
compete with it.

Two rules are encoded in the types, because both were violated in the tree:

1. **A measurement is not a capacity.** :class:`CapacityReport` refuses to carry
   quota/usage numbers while claiming :attr:`CapacityKind.UNKNOWN`, and only an
   ``AUTHORITATIVE`` report exposes :attr:`CapacityReport.verified_remaining_bytes`.
   Advertised plan allowances stay labelled as such and never become "available".
2. **Deletion is not always reclamation.** :class:`DeleteResult` carries
   ``soft`` and ``reclaimed_bytes`` separately, because e.g. Neon Object Storage
   soft-deletes on the branch and storage configuration is stored-but-not-
   enforced; reporting that as freed space would be a lie.

No web framework, Telegram, ORM or cloud SDK type appears here.

Key model — the ambiguity this contract removes
----------------------------------------------
* A **key is backend-absolute and opaque**: it is exactly the string the backend
  stores, it is safe to persist in a database, and it is what ``head``,
  ``open_bytes``, ``iter_chunks``, ``verify_bytes`` and ``delete`` accept.
* ``namespace`` is the store's root prefix (``""`` when the store owns the whole
  key space). Every key the store writes starts with ``namespace``.
* ``list_keys(prefix=...)`` filters over those **same absolute keys**: every
  returned key starts with the effective prefix, and every returned key is
  accepted by ``head``/``open_bytes`` on the same store. The effective prefix is
  ``prefix`` when supplied, else ``namespace``. A prefix **outside** the store's
  namespace raises :class:`InvalidObjectKey` rather than silently listing another
  tenant's keys (or returning an empty page that hides the mistake).
* No implementation may keep a second, invisible key space: a provider bucket
  namespace is the store's ``namespace``, never a hidden transform applied on
  the way in and forgotten on the way out.

Namespace ownership — enforced, not assumed
------------------------------------------
The namespace boundary is a **path-segment** boundary: ``nagar/assets`` owns
``nagar/assets/...`` but never ``nagar/assets-other/...``. A bare
``str.startswith`` check cannot express that, so every adapter uses
:func:`is_within_namespace` instead.

Every operation that accepts a key (``head``, ``exists``, ``open_bytes``,
``iter_chunks``, ``verify_bytes``, ``delete``) validates both the key *shape*
and its namespace membership **before issuing any provider request**. A key
outside the store's namespace raises :class:`InvalidObjectKey`; it is never
reported as ``None``/absent, because "not found" would hide a caller that is
addressing another tenant. ``list_keys`` and ``measure_namespace`` apply the
same boundary to what the provider returns, so a namespace can neither read
nor count its sibling's objects.

An empty namespace (``""``) means "this store owns the whole key space" and is
only legal where a backend says so explicitly; it is never the silent default
for a shared bucket.

Integrity model — what each operation actually guarantees
---------------------------------------------------------
``put_stream``
    The *bytes sent* were hashed and their length enforced while reading; after
    upload the object's **size and recorded digest metadata** are re-read. The
    bytes are **not** read back. ``PutResult.content_verified`` says whether a
    byte-level check happened (see ``verify_existing``).
``put_stream(verify_existing=True)``
    Before reporting ``created=False`` for an existing key, the stored bytes are
    re-read and hashed; a mismatch raises :class:`IntegrityMismatch`.
``head``
    Reports **recorded metadata**; it does not read object bytes.
``open_bytes`` / ``iter_chunks``
    The bytes returned are hashed and compared with the recorded digest; a
    mismatch raises :class:`IntegrityMismatch`.
``verify_bytes``
    An explicit, full byte-level re-read and digest comparison.

Metadata presence is therefore **not** proof that the stored bytes were
verified. A backend that cannot cheaply verify bytes must still implement
``verify_bytes`` and must not describe ``head`` as verification.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import BinaryIO, Protocol, runtime_checkable

#: Directory component of the content-addressed key layout. Identical to the
#: local store's layout (``blobs/<2>/<sha256>``) so a digest computed on one
#: backend addresses the same blob on another.
KEY_PREFIX = "blobs"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


# --------------------------------------------------------------------------- #
# Error taxonomy
# --------------------------------------------------------------------------- #
class ObjectStoreError(RuntimeError):
    """Base class for every typed object-store failure.

    Messages are allowed to name the *key* and the backend. They must never
    carry credentials, pre-signed query strings or provider response bodies:
    those end up in logs and in job records.
    """

    code = "object_store_error"


class BackendNotConfigured(ObjectStoreError):
    """The backend exists in code but has no usable configuration.

    Raised at call time, never at import time: a missing credential must not be
    able to break application startup.
    """

    code = "backend_not_configured"


class ObjectNotFound(ObjectStoreError):
    """The requested key does not exist on this backend."""

    code = "object_not_found"


class ObjectTooLarge(ObjectStoreError):
    """The stream exceeded the caller's ``max_bytes`` bound."""

    code = "object_too_large"


class IntegrityMismatch(ObjectStoreError):
    """Stored bytes do not match the identity the caller (or key) claims."""

    code = "integrity_mismatch"


class ObjectConflict(ObjectStoreError):
    """A different object already occupies the key; refusing to overwrite."""

    code = "object_conflict"


class AuthorizationDenied(ObjectStoreError):
    """The credential lacks the required scope/permission for this operation."""

    code = "authorization_denied"


class TransientBackendError(ObjectStoreError):
    """A retryable provider failure that outlived the bounded retry budget."""

    code = "transient_backend_error"


class BackendUnavailable(ObjectStoreError):
    """The backend (or the bucket it names) is not reachable at all."""

    code = "backend_unavailable"


class UnsupportedOperation(ObjectStoreError):
    """The backend declares, in :class:`BackendCapabilities`, that it cannot do this."""

    code = "unsupported_operation"


class InvalidObjectKey(ObjectStoreError):
    """A key that could escape the backend namespace or corrupt a protocol."""

    code = "invalid_object_key"


# --------------------------------------------------------------------------- #
# Keys and streams
# --------------------------------------------------------------------------- #
def content_key_for(sha256: str, *, prefix: str = "") -> str:
    """Return the deterministic key for a 64-hex digest.

    Content addressing is the idempotency mechanism: the same bytes always
    address the same key, so a retried upload is a no-op rather than a second
    object. ``prefix`` is a caller-owned namespace (e.g. a per-tenant path) and
    is validated like any other key fragment.
    """
    if not isinstance(sha256, str) or not _SHA256.match(sha256):
        raise IntegrityMismatch("digest is not a 64-character lowercase hex SHA-256")
    key = f"{KEY_PREFIX}/{sha256[:2]}/{sha256}"
    if not prefix:
        return key
    return f"{validate_object_key(prefix)}/{key}"


def content_digest_from_key(key: str) -> str | None:
    """The SHA-256 a content-addressed key encodes, or ``None`` when it encodes none.

    The key is the immutable half of the identity; the stored metadata digest is
    the mutable half. Callers that compare against *only* one of the two accept a
    tampered object, so adapters resolve both through this helper and refuse a
    contradiction (see :func:`nexus.storage.neon_object_storage` ``_identity_digest``).
    """
    candidate = key.rsplit("/", 1)[-1]
    return candidate if _SHA256.match(candidate) else None


def is_within_namespace(key: str, namespace: str) -> bool:
    """True iff ``key`` lies inside ``namespace`` at a path-segment boundary.

    ``"nagar/assets-other/x"`` is **not** inside ``"nagar/assets"``. This is the
    check a bare ``startswith`` gets wrong, and getting it wrong is how one
    tenant's namespace exposes another tenant's objects.
    """
    if not namespace:
        return True
    return key == namespace or key.startswith(f"{namespace}/")


def resolve_namespace_prefix(prefix: str, namespace: str) -> str:
    """Resolve a caller's listing prefix against the store's namespace.

    ``prefix`` defaults to the whole namespace. A prefix outside the namespace is
    refused rather than translated or silently emptied, because an empty page
    reads as "no assets" and hides the mistake.
    """
    effective = prefix or namespace
    if effective:
        validate_object_key(effective)
    if not is_within_namespace(effective, namespace):
        raise InvalidObjectKey(
            f"prefix {effective!r} is outside this store's namespace {namespace!r}"
        )
    return effective


def validate_object_key(key: str) -> str:
    """Validate a backend-neutral object key; fail closed before any network call.

    Keys are relative, slash-separated, and may not contain control characters,
    empty/``.``/``..`` segments, or a leading/trailing slash. Backends must call
    this on every path that accepts a key, because keys travel into HTTP
    envelopes and provider paths.
    """
    if not isinstance(key, str) or not key:
        raise InvalidObjectKey("object key must be a non-empty string")
    if any(ord(char) < 32 or char == "\x7f" for char in key):
        raise InvalidObjectKey("object key must not contain control characters")
    if key.startswith("/") or key.endswith("/"):
        raise InvalidObjectKey("object key must be relative and must not end with '/'")
    if any(part in {"", ".", ".."} for part in key.split("/")):
        raise InvalidObjectKey("object key must not contain empty, '.', or '..' segments")
    return key


def sha256_stream(source: BinaryIO, *, max_bytes: int) -> tuple[str, int]:
    """Measure ``(sha256, size)`` of a stream, enforcing ``max_bytes`` while reading.

    The bound is what makes this safe against a hostile or unbounded stream: the
    digest is computed over bytes actually read, not over a Content-Length claim.
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = source.read(1 << 20)
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise ObjectTooLarge(f"stream exceeds the {max_bytes}-byte limit")
        digest.update(chunk)
    return digest.hexdigest(), size


# --------------------------------------------------------------------------- #
# Values
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BackendCapabilities:
    """What a backend actually supports — declared, never assumed.

    Every field here is a claim a test can check. ``notes`` records provider
    limitations that are not expressible as a boolean (e.g. "soft delete").
    """

    supports_streaming: bool
    supports_range_reads: bool
    supports_multipart: bool
    supports_presign: bool
    supports_delete: bool
    delete_is_soft: bool
    supports_custom_metadata: bool
    max_object_bytes: int | None = None
    notes: str = ""


@dataclass(frozen=True)
class ObjectMetadata:
    """Measured facts about one stored object."""

    key: str
    size_bytes: int
    sha256: str | None = None
    content_type: str | None = None
    etag: str | None = None
    last_modified: datetime | None = None
    custom: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PutResult:
    """Outcome of a content-addressed publish.

    ``created`` is ``False`` when identical bytes were already present and the
    store reused them — the idempotent path, not an error and not a rewrite.
    """

    key: str
    sha256: str
    size_bytes: int
    created: bool
    etag: str | None = None
    content_verified: bool = False
    """True only when this call read the stored bytes back and hashed them.

    ``False`` means size + recorded digest metadata were checked (see the
    module's integrity table); it never means the bytes were verified.
    """


@dataclass(frozen=True)
class DeleteResult:
    """Outcome of a delete, distinguishing removal from reclamation.

    ``soft`` means the provider keeps the bytes (soft delete, versioning or a
    stored-but-unenforced lifecycle rule), so ``reclaimed_bytes`` must stay 0.
    """

    key: str
    existed: bool
    soft: bool
    reclaimed_bytes: int = 0

    def __post_init__(self) -> None:
        if self.soft and self.reclaimed_bytes:
            raise ValueError("a soft delete cannot reclaim bytes")


class CapacityKind(str, Enum):
    """Provenance of a capacity figure. Never upgrade one kind into another."""

    AUTHORITATIVE = "authoritative"
    """Returned by a provider quota/usage API for this exact account/bucket."""

    ADVERTISED = "advertised"
    """A plan allowance stated by the provider. Not a measurement of this account."""

    UNKNOWN = "unknown"
    """No dependable figure exists (no API, no measurement, or a failed call)."""


@dataclass(frozen=True)
class CapacityReport:
    """A timestamped, sourced statement about capacity — or an honest "unknown".

    ``measured_unique_bytes`` is the bytes of *our* objects observed via the
    provider's listing API. It is a measurement, not a quota, and it must never
    be added to another backend's figure to produce a "total".

    ``replicated_bytes`` records bytes that are copies (backup/version) and are
    therefore excluded from unique capacity by construction.
    """

    backend: str
    kind: CapacityKind
    measured_at: datetime
    source: str
    quota_bytes: int | None = None
    used_bytes: int | None = None
    measured_unique_bytes: int | None = None
    replicated_bytes: int | None = None
    note: str = ""

    def __post_init__(self) -> None:
        if not self.backend:
            raise ValueError("capacity report must name the backend")
        if not self.source:
            raise ValueError(
                "capacity report must name its measurement source; an unsourced "
                "figure is indistinguishable from an invention"
            )
        if self.measured_at.tzinfo is None:
            raise ValueError("measured_at must be timezone-aware (UTC)")
        if self.kind is CapacityKind.UNKNOWN and (
            self.quota_bytes is not None or self.used_bytes is not None
        ):
            raise ValueError(
                "an UNKNOWN capacity report must not carry quota/used numbers; "
                "report the unknown, do not manufacture a measurement"
            )
        for name in ("quota_bytes", "used_bytes", "measured_unique_bytes", "replicated_bytes"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must not be negative")

    @property
    def verified_remaining_bytes(self) -> int | None:
        """Remaining bytes only when an authoritative quota and usage both exist."""
        if self.kind is not CapacityKind.AUTHORITATIVE:
            return None
        if self.quota_bytes is None or self.used_bytes is None:
            return None
        return max(0, self.quota_bytes - self.used_bytes)


@dataclass(frozen=True)
class HealthReport:
    """Cheap liveness probe result, safe to log (no credentials, no URLs with auth)."""

    backend: str
    ok: bool
    detail: str
    checked_at: datetime

    def __post_init__(self) -> None:
        if self.checked_at.tzinfo is None:
            raise ValueError("checked_at must be timezone-aware (UTC)")


def utcnow() -> datetime:
    """A single clock helper so every report is timezone-aware in one way."""
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Contract
# --------------------------------------------------------------------------- #
@runtime_checkable
class ObjectStore(Protocol):
    """Content-addressed immutable blob storage.

    Implementations are synchronous on purpose: the Asset Library and the
    maintenance tier already own their own async scheduling, and a network-bound
    synchronous adapter composes cleanly with ``asyncio.to_thread``. An adapter
    that needs to be async must not pretend to be one of these.

    The key model and the integrity model are defined in the module docstring;
    they are contract, not convention.
    """

    name: str

    @property
    def namespace(self) -> str:
        """The root key prefix this store owns (``""`` for the whole key space).

        Keys returned by ``put_stream``/``list_keys`` always start with it, and
        ``list_keys`` refuses a prefix outside it.
        """
        ...

    def capabilities(self) -> BackendCapabilities: ...

    def is_configured(self) -> bool: ...

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
        """Stream ``source`` into content-addressed storage and publish it.

        ``expected_sha256`` lets a caller pin the identity it already measured
        (a replayed idempotency key, a resumed upload); a mismatch raises
        :class:`IntegrityMismatch` and publishes nothing.

        When the same bytes already exist, the call reuses them
        (``created=False``). That reuse is decided from recorded metadata by
        default; pass ``verify_existing=True`` to re-read and hash the stored
        bytes first, so a silently corrupted object is reported as
        :class:`IntegrityMismatch` instead of being handed back as a valid
        dedupe. ``PutResult.content_verified`` reports which happened.
        """
        ...

    def open_bytes(self, key: str, *, max_bytes: int | None = None) -> bytes:
        """Return the whole object, refusing to buffer more than ``max_bytes``.

        The bound must be enforced *while reading*, not after the object has
        already been materialized: an adapter that reads first and checks second
        has no bound at all.
        """
        ...

    def iter_chunks(
        self, key: str, *, chunk_size: int = 1 << 20, max_bytes: int | None = None
    ) -> Iterator[bytes]:
        """Yield the object chunk by chunk, hashing incrementally.

        A consumer that iterates to completion gets :class:`IntegrityMismatch`
        when the bytes do not match the identity the key encodes. A consumer that
        stops early has consumed bytes that were not yet verified — the contract
        never promises a verified prefix.
        """
        ...

    def head(self, key: str) -> ObjectMetadata | None:
        """Recorded metadata, or ``None`` when absent. This does not read bytes."""
        ...

    def exists(self, key: str) -> bool: ...

    def verify_bytes(self, key: str) -> bool:
        """Re-read the object and compare its digest with the identity it claims.

        The identity is the digest encoded in the key **and** the digest recorded
        in metadata; when both exist and disagree, the object has been modified
        without changing its key and :class:`IntegrityMismatch` is raised.

        The only supported statement that the *stored bytes* are intact. Returns
        ``False`` only when the object exists but carries no identity at all —
        neither a content-addressed key nor a recorded digest — because then the
        comparison is impossible and "verified" would be a guess. Raises
        :class:`ObjectNotFound` when the object does not exist.
        """
        ...

    def delete(self, key: str) -> DeleteResult: ...

    def list_keys(self, *, prefix: str = "", limit: int = 1000) -> list[str]:
        """Absolute keys under ``prefix`` (default: ``namespace``), bounded.

        Every returned key starts with the effective prefix and is accepted by
        ``head``/``open_bytes``. Raises :class:`InvalidObjectKey` for a prefix
        outside this store's ``namespace``, and never returns a key from a
        sibling namespace (``nagar/assets-other`` is not ``nagar/assets``).
        """
        ...

    def measure_namespace(self) -> tuple[int, int]:
        """Measure ``(bytes, objects)`` present under this store's namespace.

        A listing-based measurement of *our* objects — not a quota, and never
        added to another backend's numbers. Sibling namespaces are excluded by
        the same segment boundary as every other operation.
        """
        ...

    def capacity(self) -> CapacityReport: ...

    def health(self) -> HealthReport: ...
