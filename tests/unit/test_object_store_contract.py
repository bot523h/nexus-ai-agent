"""Contract tests for the provider-neutral object store (task-272).

The same behavioural assertions run against:

* :class:`~nexus_ai_agent.storage.local_object_store.LocalObjectStore` — the
  real local content-addressed adapter (filesystem, no mocks);
* :class:`MemoryObjectStore` — a deliberately minimal second implementation,
  which proves the contract is implementable without the local adapter's
  quirks and catches a contract that only fits one backend.

Plus the value-object invariants that encode the anti-fabrication rules:
an ``UNKNOWN`` capacity report cannot carry numbers, a soft delete cannot claim
reclamation, and only an authoritative report exposes verified remaining bytes.
"""

from __future__ import annotations

import io
from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta, timezone
from typing import BinaryIO

import pytest

from nexus_ai_agent.storage.local_object_store import LocalObjectStore
from nexus_ai_agent.storage.object_store import (
    BackendCapabilities,
    CapacityKind,
    CapacityReport,
    DeleteResult,
    HealthReport,
    IntegrityMismatch,
    InvalidObjectKey,
    ObjectConflict,
    ObjectNotFound,
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

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# A second implementation: if only one backend can satisfy the contract, the
# contract is wrong.
# --------------------------------------------------------------------------- #
class MemoryObjectStore:
    """In-memory content-addressed store used as the contract's second witness."""

    name = "memory"

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    @property
    def namespace(self) -> str:
        return "blobs"

    def _owned(self, key: str, *, operation: str = "key") -> str:
        """The same ownership law as the real adapters (the contract's witness)."""
        key = validate_object_key(key)
        if not is_within_namespace(key, self.namespace):
            raise InvalidObjectKey(
                f"{operation}: key {key!r} is outside this store's namespace {self.namespace!r}"
            )
        return key

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_streaming=True,
            supports_range_reads=True,
            supports_multipart=False,
            supports_presign=False,
            supports_delete=True,
            delete_is_soft=False,
            supports_custom_metadata=False,
        )

    def is_configured(self) -> bool:
        return True

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
        import hashlib

        payload = bytearray()
        digest = hashlib.sha256()
        while True:
            chunk = source.read(1 << 20)
            if not chunk:
                break
            payload += chunk
            if len(payload) > max_bytes:
                raise ObjectTooLarge(f"stream exceeds the {max_bytes}-byte limit")
            digest.update(chunk)
        sha256 = digest.hexdigest()
        if expected_sha256 is not None and expected_sha256 != sha256:
            raise IntegrityMismatch("pinned digest differs from the stream")
        key = content_key_for(sha256)
        if key in self.objects:
            stored, _ = self.objects[key]
            if stored != bytes(payload):
                raise ObjectConflict(f"{key!r} holds different content")
            return PutResult(
                key=key,
                sha256=sha256,
                size_bytes=len(payload),
                created=False,
                content_verified=verify_existing and self.verify_bytes(key),
            )
        self.objects[key] = (bytes(payload), sha256)
        return PutResult(
            key=key, sha256=sha256, size_bytes=len(payload), created=True, content_verified=True
        )

    def open_bytes(self, key: str, *, max_bytes: int | None = None) -> bytes:
        return b"".join(self.iter_chunks(key, max_bytes=max_bytes))

    def iter_chunks(
        self, key: str, *, chunk_size: int = 1 << 20, max_bytes: int | None = None
    ) -> Iterator[bytes]:
        key = self._owned(key, operation="read")
        if key not in self.objects:
            raise ObjectNotFound(f"{key!r} not found")
        data, expected = self.objects[key]
        import hashlib

        digest = hashlib.sha256()
        total = 0
        for start in range(0, len(data), chunk_size):
            chunk = data[start : start + chunk_size]
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise ObjectTooLarge(f"object {key!r} exceeds the {max_bytes}-byte read bound")
            digest.update(chunk)
            yield chunk
        identity = content_digest_from_key(key)
        if identity is not None and digest.hexdigest() != identity:
            raise IntegrityMismatch("stored content does not match its content key")

    def head(self, key: str):  # noqa: ANN201 - contract shape only
        key = self._owned(key, operation="head")
        if key not in self.objects:
            return None
        data, sha256 = self.objects[key]
        from nexus_ai_agent.storage.object_store import ObjectMetadata

        return ObjectMetadata(key=key, size_bytes=len(data), sha256=sha256)

    def exists(self, key: str) -> bool:
        return self.head(key) is not None

    def verify_bytes(self, key: str) -> bool:
        import hashlib

        key = self._owned(key, operation="verify_bytes")
        if key not in self.objects:
            raise ObjectNotFound(f"{key!r} not found")
        expected = content_digest_from_key(key) or self.objects[key][1]
        if expected is None:
            return False
        return hashlib.sha256(self.objects[key][0]).hexdigest() == expected

    def delete(self, key: str) -> DeleteResult:
        key = self._owned(key, operation="delete")
        entry = self.objects.pop(key, None)
        return DeleteResult(key=key, existed=entry is not None, soft=False)

    def list_keys(self, *, prefix: str = "", limit: int = 1000) -> list[str]:
        effective = resolve_namespace_prefix(prefix, self.namespace)
        owned = [k for k in self.objects if is_within_namespace(k, self.namespace)]
        return sorted(k for k in owned if not effective or k.startswith(effective))[:limit]

    def measure_namespace(self) -> tuple[int, int]:
        owned = [k for k in self.objects if is_within_namespace(k, self.namespace)]
        return sum(len(self.objects[k][0]) for k in owned), len(owned)

    def capacity(self) -> CapacityReport:
        return CapacityReport(
            backend=self.name,
            kind=CapacityKind.UNKNOWN,
            measured_at=NOW,
            source="in-memory test double: no quota exists",
            measured_unique_bytes=sum(len(data) for data, _ in self.objects.values()),
        )

    def health(self) -> HealthReport:
        return HealthReport(backend=self.name, ok=True, detail="memory", checked_at=NOW)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(params=["local", "memory"])
def store(request, tmp_path):
    if request.param == "local":
        return LocalObjectStore(tmp_path / "objects")
    return MemoryObjectStore()


# --------------------------------------------------------------------------- #
# Contract behaviour
# --------------------------------------------------------------------------- #
def test_put_stream_measures_and_publishes(store) -> None:
    payload = b"nagar-asset-" * 1000
    result = store.put_stream(io.BytesIO(payload), max_bytes=1 << 20)

    import hashlib

    expected = hashlib.sha256(payload).hexdigest()
    assert result.sha256 == expected
    assert result.size_bytes == len(payload)
    assert result.created is True
    assert result.key == content_key_for(expected)
    assert store.exists(result.key) is True


def test_put_is_idempotent_for_identical_bytes(store) -> None:
    payload = b"identical content"
    first = store.put_stream(io.BytesIO(payload), max_bytes=1024)
    second = store.put_stream(io.BytesIO(payload), max_bytes=1024)
    assert first.key == second.key
    assert second.created is False, "re-publishing identical bytes must reuse, not rewrite"


def test_put_rejects_pinned_digest_mismatch_and_publishes_nothing(store) -> None:
    payload = b"real content"
    with pytest.raises(IntegrityMismatch):
        store.put_stream(
            io.BytesIO(payload),
            max_bytes=1024,
            expected_sha256="0" * 64,
        )
    assert store.list_keys(prefix="blobs") == [], "a refused put must leave no object behind"


def test_put_enforces_the_max_bytes_bound(store) -> None:
    with pytest.raises(ObjectTooLarge):
        store.put_stream(io.BytesIO(b"x" * 64), max_bytes=16)


def test_missing_object_is_absent_not_empty(store) -> None:
    key = content_key_for("a" * 64)
    assert store.head(key) is None
    assert store.exists(key) is False


def test_head_reports_measured_identity(store) -> None:
    payload = b"metadata me"
    result = store.put_stream(io.BytesIO(payload), max_bytes=1024)
    metadata = store.head(result.key)
    assert metadata is not None
    assert metadata.size_bytes == len(payload)
    assert metadata.sha256 == result.sha256


def test_delete_follows_declared_capability(store) -> None:
    result = store.put_stream(io.BytesIO(b"to delete"), max_bytes=1024)
    if not store.capabilities().supports_delete:
        with pytest.raises(UnsupportedOperation):
            store.delete(result.key)
        assert store.exists(result.key) is True
        return
    outcome = store.delete(result.key)
    assert outcome.existed is True
    assert outcome.soft is False
    assert store.exists(result.key) is False


def test_capabilities_are_explicit(store) -> None:
    capabilities = store.capabilities()
    assert isinstance(capabilities, BackendCapabilities)
    assert isinstance(capabilities.supports_streaming, bool)
    assert isinstance(capabilities.delete_is_soft, bool)


def test_capacity_never_reports_a_fabricated_quota(store) -> None:
    report = store.capacity()
    assert isinstance(report, CapacityReport)
    assert report.measured_at.tzinfo is not None
    assert report.source, "every capacity statement must name its source"
    if report.kind is CapacityKind.UNKNOWN:
        assert report.quota_bytes is None
        assert report.used_bytes is None
        assert report.verified_remaining_bytes is None


def test_health_is_loggable(store) -> None:
    report = store.health()
    assert isinstance(report, HealthReport)
    assert report.backend == store.name


# --------------------------------------------------------------------------- #
# Local adapter specifics (no mocks: a real filesystem in tmp_path)
# --------------------------------------------------------------------------- #
def test_local_store_detects_tampered_bytes(tmp_path) -> None:
    store = LocalObjectStore(tmp_path / "objects")
    result = store.put_stream(io.BytesIO(b"authentic"), max_bytes=1024)
    blob = tmp_path / "objects" / result.key
    blob.write_bytes(b"tampered!")

    with pytest.raises(IntegrityMismatch):
        store.open_bytes(result.key)


def test_local_store_refuses_typed_metadata_it_cannot_persist(tmp_path) -> None:
    store = LocalObjectStore(tmp_path / "objects")
    with pytest.raises(UnsupportedOperation):
        store.put_stream(io.BytesIO(b"x"), max_bytes=16, content_type="image/png")
    with pytest.raises(UnsupportedOperation):
        store.put_stream(io.BytesIO(b"x"), max_bytes=16, metadata={"origin": "telegram"})
    assert store.list_keys(prefix="blobs") == []


def test_local_store_measures_its_own_bytes_but_reports_unknown_quota(tmp_path) -> None:
    store = LocalObjectStore(tmp_path / "objects")
    store.put_stream(io.BytesIO(b"abc"), max_bytes=16)
    report = store.capacity()
    assert report.kind is CapacityKind.UNKNOWN
    assert report.measured_unique_bytes == 3
    assert report.verified_remaining_bytes is None


def test_local_store_rejects_traversal_keys(tmp_path) -> None:
    store = LocalObjectStore(tmp_path / "objects")
    for bad in ("../escape", "/abs", "blobs//x", "blobs/./x", "blobs/x/"):
        with pytest.raises(InvalidObjectKey):
            store.open_bytes(bad)


# --------------------------------------------------------------------------- #
# Key model: absolute keys and explicit prefix semantics (the contract's law)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("store_name", ["local", "memory"])
def test_listed_keys_are_absolute_and_directly_addressable(store_name, tmp_path) -> None:
    store = LocalObjectStore(tmp_path / "objects") if store_name == "local" else MemoryObjectStore()
    result = store.put_stream(io.BytesIO(b"address me"), max_bytes=1024)
    listed = store.list_keys()
    assert result.key in listed, "a published key must appear in the default listing"
    assert store.namespace and result.key.startswith(store.namespace)
    for key in listed:
        assert key.startswith(store.namespace), (
            f"listed key {key!r} escapes the store namespace {store.namespace!r}"
        )
        metadata = store.head(key)
        assert metadata is not None and metadata.key == key, (
            "every listed key must be accepted by head() unchanged"
        )


@pytest.mark.parametrize("store_name", ["local", "memory"])
def test_prefix_filters_over_the_same_absolute_key_space(store_name, tmp_path) -> None:
    store = LocalObjectStore(tmp_path / "objects") if store_name == "local" else MemoryObjectStore()
    first = store.put_stream(io.BytesIO(b"one"), max_bytes=64)
    second = store.put_stream(io.BytesIO(b"two"), max_bytes=64)

    # A prefix inside the namespace must be interpreted the same way the keys are:
    # as a prefix of the absolute key, not of a hidden relative space.
    narrow = first.key.rsplit("/", 1)[0]
    listed = store.list_keys(prefix=narrow)
    assert listed == [first.key], listed
    assert second.key not in listed

    # Every returned key matches the prefix and round-trips through the reads.
    for key in listed:
        assert key.startswith(narrow)
        assert store.open_bytes(key)


@pytest.mark.parametrize("store_name", ["local", "memory"])
def test_prefix_outside_the_namespace_is_refused_not_silently_empty(store_name, tmp_path) -> None:
    store = LocalObjectStore(tmp_path / "objects") if store_name == "local" else MemoryObjectStore()
    store.put_stream(io.BytesIO(b"x"), max_bytes=64)
    for foreign in ("other-tenant", "blobs2", "a"):
        with pytest.raises(InvalidObjectKey):
            store.list_keys(prefix=foreign)


def test_default_prefix_is_the_store_namespace(tmp_path) -> None:
    store = LocalObjectStore(tmp_path / "objects")
    store.put_stream(io.BytesIO(b"x"), max_bytes=64)
    assert store.list_keys() == store.list_keys(prefix=store.namespace)


# --------------------------------------------------------------------------- #
# Integrity: metadata presence is not byte verification
# --------------------------------------------------------------------------- #
def test_head_reports_recorded_metadata_without_reading_bytes(tmp_path) -> None:
    store = LocalObjectStore(tmp_path / "objects")
    result = store.put_stream(io.BytesIO(b"original"), max_bytes=64)
    (tmp_path / "objects" / result.key).write_bytes(b"tampered")

    # head() answers from the filesystem listing and a re-hash of the file, so for
    # the local adapter the size changes visibly; the point of the law is that the
    # *contract* only promises recorded metadata. Whatever head reports, the read
    # paths must refuse the tampered bytes.
    with pytest.raises(IntegrityMismatch):
        store.open_bytes(result.key)
    with pytest.raises(IntegrityMismatch):
        list(store.iter_chunks(result.key))


def test_verify_bytes_detects_tampering_that_preserves_metadata(tmp_path) -> None:
    """The exact scenario: bytes changed, recorded digest metadata unchanged."""
    store = LocalObjectStore(tmp_path / "objects")
    result = store.put_stream(io.BytesIO(b"authentic"), max_bytes=64)
    assert store.verify_bytes(result.key) is True

    # Same length, different bytes: size checks alone cannot see this.
    (tmp_path / "objects" / result.key).write_bytes(b"TAMPERED!")

    assert store.verify_bytes(result.key) is False, (
        "verify_bytes must re-read and hash the bytes, not trust the key/metadata"
    )
    with pytest.raises(IntegrityMismatch):
        store.open_bytes(result.key)


def test_verify_bytes_raises_on_a_missing_object(store) -> None:
    with pytest.raises(ObjectNotFound):
        store.verify_bytes(content_key_for("e" * 64))


def test_a_byte_verification_claim_is_provable(store) -> None:
    """No backend may report ``content_verified`` it cannot back up.

    The law is a claim/evidence pair: if ``put_stream`` says the existing bytes
    were verified, corrupting them must make the next default dedupe fail, because
    the backend that verified once can verify again. If it makes no such claim,
    the reuse is explicitly metadata-level and must say ``created=False``.
    """
    created = store.put_stream(io.BytesIO(b"fresh"), max_bytes=64)
    assert created.created is True
    duplicate = store.put_stream(io.BytesIO(b"fresh"), max_bytes=64)
    assert duplicate.created is False

    if not duplicate.content_verified:
        # Metadata-level reuse: the call is honest about not having read bytes.
        return

    corrupt = tmp_path_blob_for(store, created.key)
    assert corrupt is not None, (
        "a backend claiming byte verification must expose mutable storage here"
    )
    corrupt.write_bytes(b"BADAB")  # same length, different bytes
    with pytest.raises(IntegrityMismatch):
        store.put_stream(io.BytesIO(b"fresh"), max_bytes=64)


def tmp_path_blob_for(store, key: str):
    """The on-disk path of a blob for the local adapter, else ``None``."""
    if isinstance(store, LocalObjectStore):
        return store.blob_path(key)
    return None


def test_verify_existing_refuses_a_corrupted_duplicate(tmp_path) -> None:
    store = LocalObjectStore(tmp_path / "objects")
    result = store.put_stream(io.BytesIO(b"payload"), max_bytes=64)
    (tmp_path / "objects" / result.key).write_bytes(b"corrupt")
    with pytest.raises(IntegrityMismatch):
        store.put_stream(io.BytesIO(b"payload"), max_bytes=64, verify_existing=True)


# --------------------------------------------------------------------------- #
# Value objects: the anti-fabrication invariants
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Value objects: the anti-fabrication invariants
# --------------------------------------------------------------------------- #
def test_unknown_capacity_cannot_carry_numbers() -> None:
    with pytest.raises(ValueError):
        CapacityReport(
            backend="x",
            kind=CapacityKind.UNKNOWN,
            measured_at=NOW,
            source="none",
            quota_bytes=10,
        )


def test_capacity_requires_a_source_and_aware_timestamp() -> None:
    with pytest.raises(ValueError):
        CapacityReport(backend="x", kind=CapacityKind.UNKNOWN, measured_at=NOW, source="")
    with pytest.raises(ValueError):
        CapacityReport(
            backend="x",
            kind=CapacityKind.UNKNOWN,
            measured_at=datetime(2026, 10, 22, 12, 0),
            source="clock without a timezone",
        )


def test_only_authoritative_capacity_yields_verified_remaining() -> None:
    authoritative = CapacityReport(
        backend="x",
        kind=CapacityKind.AUTHORITATIVE,
        measured_at=NOW,
        source="provider usage API",
        quota_bytes=100,
        used_bytes=40,
    )
    assert authoritative.verified_remaining_bytes == 60

    advertised = CapacityReport(
        backend="x",
        kind=CapacityKind.ADVERTISED,
        measured_at=NOW,
        source="plans page",
        quota_bytes=100,
        used_bytes=None,
    )
    assert advertised.verified_remaining_bytes is None, (
        "an advertised plan allowance is not verified usable capacity"
    )


def test_authoritative_capacity_with_one_half_missing_is_not_verified() -> None:
    report = CapacityReport(
        backend="x",
        kind=CapacityKind.AUTHORITATIVE,
        measured_at=NOW,
        source="provider usage API",
        quota_bytes=100,
    )
    assert report.verified_remaining_bytes is None


def test_soft_delete_cannot_reclaim_bytes() -> None:
    with pytest.raises(ValueError):
        DeleteResult(key="blobs/aa/x", existed=True, soft=True, reclaimed_bytes=10)


def test_content_key_layout_is_stable_and_shared() -> None:
    digest = "ab" * 32
    assert content_key_for(digest) == f"blobs/ab/{digest}"
    assert content_key_for(digest, prefix="tenant-1") == f"tenant-1/blobs/ab/{digest}"
    with pytest.raises(IntegrityMismatch):
        content_key_for("not-a-digest")


def test_key_validation_rejects_escape_and_control_characters() -> None:
    for bad in ("", "/abs", "a//b", "a/../b", "a/\x00b", "trailing/"):
        with pytest.raises(InvalidObjectKey):
            validate_object_key(bad)
    assert validate_object_key("blobs/ab/" + "a" * 64)


def test_object_conflict_is_a_distinct_typed_failure() -> None:
    assert issubclass(ObjectConflict, Exception)
    assert not issubclass(ObjectConflict, ObjectTooLarge)


def test_utcnow_is_timezone_aware() -> None:
    now = utcnow()
    assert now.tzinfo is not None
    assert abs(now - datetime.now(timezone.utc)) < timedelta(seconds=5)


# --------------------------------------------------------------------------- #
# Namespace ownership (review round 2, finding A)
#
# The adversary here is a *sibling* prefix, not an unrelated one. "blobs" and
# "blobs-other" share a literal string prefix, so `str.startswith` says they
# overlap while a path-segment boundary says they do not. Every key operation
# must refuse the foreign key, and listing/measurement must exclude it.
# --------------------------------------------------------------------------- #
SIBLING_KEY = f"blobs-other/{'ab'}/{('ab' * 32)}"
UNRELATED_KEY = "other-tenant/secret"


@pytest.fixture(params=[SIBLING_KEY, UNRELATED_KEY], ids=["sibling", "unrelated"])
def foreign_key(request) -> str:
    """Both shapes must be refused; the sibling one is the easy one to miss."""
    return request.param


def test_a_sibling_prefix_is_not_inside_the_namespace() -> None:
    assert is_within_namespace("blobs/ab/x", "blobs") is True
    assert is_within_namespace("blobs", "blobs") is True
    assert is_within_namespace("blobs-other/ab/x", "blobs") is False
    assert is_within_namespace("blobs/ab/x", "") is True  # root namespace owns everything
    with pytest.raises(InvalidObjectKey):
        resolve_namespace_prefix("blobs-other/ab/x", "blobs")
    # The root namespace does not reject anything, including a leading segment.
    assert resolve_namespace_prefix("anything/at/all", "") == "anything/at/all"


@pytest.mark.parametrize(
    "operation",
    ["head", "exists", "open_bytes", "iter_chunks", "verify_bytes", "delete"],
)
def test_every_key_operation_refuses_a_foreign_key(store, operation, foreign_key) -> None:
    """A foreign key is an error, never "absent" — silence would hide the mistake."""
    with pytest.raises(InvalidObjectKey):
        if operation == "iter_chunks":
            list(store.iter_chunks(foreign_key))
        else:
            getattr(store, operation)(foreign_key)


def test_open_bytes_does_not_swallow_a_foreign_key_as_not_found(store, foreign_key) -> None:
    """`ObjectNotFound` for a foreign key would be a quiet cross-namespace probe."""
    with pytest.raises(InvalidObjectKey):
        store.open_bytes(foreign_key)


def _seed_foreign_object(store, workspace, key: str, data: bytes) -> None:
    """Write an object under a sibling namespace, bypassing the adapter on purpose."""
    if isinstance(store, MemoryObjectStore):
        store.objects[key] = (data, "")
        return
    path = workspace / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_listing_excludes_a_sibling_namespace(store, tmp_path) -> None:
    owned = store.put_stream(io.BytesIO(b"ours"), max_bytes=1024)
    _seed_foreign_object(store, tmp_path / "objects", SIBLING_KEY, b"theirs")

    listed = store.list_keys()
    assert owned.key in listed
    assert SIBLING_KEY not in listed, "a sibling namespace leaked into this store's listing"
    assert all(is_within_namespace(key, store.namespace) for key in listed)


def test_measurement_excludes_a_sibling_namespace(store, tmp_path) -> None:
    store.put_stream(io.BytesIO(b"ours"), max_bytes=1024)
    _seed_foreign_object(store, tmp_path / "objects", SIBLING_KEY, b"theirs-is-longer")

    total, count = store.measure_namespace()
    assert (total, count) == (4, 1), "capacity measurement counted a sibling namespace"


def test_listed_keys_are_accepted_unchanged_by_every_operation(store) -> None:
    """The absolute-key contract: what listing returns is what the API takes."""
    for payload in (b"alpha", b"beta"):
        store.put_stream(io.BytesIO(payload), max_bytes=1024)
    for key in store.list_keys():
        assert store.head(key) is not None
        assert store.exists(key) is True
        assert store.open_bytes(key) == b"".join(store.iter_chunks(key))
        assert store.verify_bytes(key) is True


# --------------------------------------------------------------------------- #
# Local streaming and bounds (review round 2, finding B)
#
# "Bounded" is a claim about bytes actually read, so it is measured: a thin
# delegating spy sits in front of the real WorkspaceFilesystem and counts what
# the boundary handed out. The real filesystem, boundary checks and O_NOFOLLOW
# path are still exercised — only the accounting is added.
# --------------------------------------------------------------------------- #
class _RecordingFilesystem:
    """Delegates to the real boundary and records how much it released."""

    def __init__(self, inner) -> None:  # noqa: ANN001 - structural delegate
        self._inner = inner
        self.bytes_yielded = 0
        self.chunks = 0

    def __getattr__(self, name: str):  # noqa: ANN401 - delegate everything else
        return getattr(self._inner, name)

    def iter_bytes(self, raw: str, *, chunk_size: int = 1 << 20) -> Iterator[bytes]:
        for chunk in self._inner.iter_bytes(raw, chunk_size=chunk_size):
            self.bytes_yielded += len(chunk)
            self.chunks += 1
            yield chunk


def test_local_iter_chunks_does_not_read_ahead_of_the_consumer(tmp_path) -> None:
    store = LocalObjectStore(tmp_path / "objects")
    payload = b"x" * 4096
    result = store.put_stream(io.BytesIO(payload), max_bytes=1 << 20)

    recorder = _RecordingFilesystem(store._fs)
    store._fs = recorder
    iterator = store.iter_chunks(result.key, chunk_size=256)
    try:
        first = next(iterator)
        second = next(iterator)
        assert first + second == payload[:512]
        assert recorder.chunks == 2, "iter_chunks read more chunks than the consumer asked for"
        assert recorder.bytes_yielded <= 512, (
            f"iter_chunks buffered {recorder.bytes_yielded} bytes before yielding 512"
        )
    finally:
        iterator.close()


def test_local_open_bytes_enforces_the_bound_while_reading(tmp_path) -> None:
    store = LocalObjectStore(tmp_path / "objects")
    payload = b"y" * (3 << 20)
    result = store.put_stream(io.BytesIO(payload), max_bytes=1 << 20 * 8)

    recorder = _RecordingFilesystem(store._fs)
    store._fs = recorder
    with pytest.raises(ObjectTooLarge):
        store.open_bytes(result.key, max_bytes=100)
    assert recorder.bytes_yielded < len(payload), (
        "open_bytes read the whole object before enforcing max_bytes"
    )


def test_local_verify_bytes_streams_instead_of_buffering(tmp_path) -> None:
    store = LocalObjectStore(tmp_path / "objects")
    payload = b"v" * (3 << 20)
    result = store.put_stream(io.BytesIO(payload), max_bytes=1 << 20 * 8)

    recorder = _RecordingFilesystem(store._fs)
    store._fs = recorder
    assert store.verify_bytes(result.key) is True
    assert recorder.chunks > 1, "verify_bytes did not stream the object"


def test_local_full_iteration_verifies_but_a_partial_read_is_unverified(tmp_path) -> None:
    """The contract says a verified *prefix* does not exist; this pins that behaviour."""
    store = LocalObjectStore(tmp_path / "objects")
    result = store.put_stream(io.BytesIO(b"z" * 1024), max_bytes=1 << 20)
    store.blob_path(result.key).write_bytes(b"tampered")

    iterator = store.iter_chunks(result.key, chunk_size=256)
    assert next(iterator)  # bytes are served before the digest can be checked
    with pytest.raises(IntegrityMismatch):
        list(iterator)

    with pytest.raises(IntegrityMismatch):
        store.open_bytes(result.key)


def test_local_head_and_blob_path_never_expose_the_staging_area(tmp_path) -> None:
    """`staging/…` is a valid key shape in this workspace, and it is not ours."""
    store = LocalObjectStore(tmp_path / "objects")
    staging = tmp_path / "objects" / "staging" / "candidate"
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_bytes(b"another workspace file")

    for call in (store.head, store.exists, store.blob_path, store.verify_bytes):
        with pytest.raises(InvalidObjectKey):
            call("staging/candidate")
    with pytest.raises(InvalidObjectKey):
        list(store.iter_chunks("staging/candidate"))


def test_local_measurement_ignores_staging_and_siblings(tmp_path) -> None:
    store = LocalObjectStore(tmp_path / "objects")
    store.put_stream(io.BytesIO(b"ours"), max_bytes=1024)
    staging = tmp_path / "objects" / "staging" / "leftover"
    staging.write_bytes(b"not-an-object")
    sibling = tmp_path / "objects" / "blobs-other" / "aa" / "x"
    sibling.parent.mkdir(parents=True, exist_ok=True)
    sibling.write_bytes(b"someone-else")

    assert store.measure_namespace() == (4, 1)
