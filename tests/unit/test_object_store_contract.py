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
    content_key_for,
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
            return PutResult(key=key, sha256=sha256, size_bytes=len(payload), created=False)
        self.objects[key] = (bytes(payload), sha256)
        return PutResult(key=key, sha256=sha256, size_bytes=len(payload), created=True)

    def open_bytes(self, key: str, *, max_bytes: int | None = None) -> bytes:
        key = validate_object_key(key)
        if key not in self.objects:
            raise ObjectNotFound(f"{key!r} not found")
        return self.objects[key][0]

    def iter_chunks(self, key: str, *, chunk_size: int = 1 << 20) -> Iterator[bytes]:
        data = self.open_bytes(key)
        for start in range(0, len(data), chunk_size):
            yield data[start : start + chunk_size]

    def head(self, key: str):  # noqa: ANN201 - contract shape only
        key = validate_object_key(key)
        if key not in self.objects:
            return None
        data, sha256 = self.objects[key]
        from nexus_ai_agent.storage.object_store import ObjectMetadata

        return ObjectMetadata(key=key, size_bytes=len(data), sha256=sha256)

    def exists(self, key: str) -> bool:
        return self.head(key) is not None

    def delete(self, key: str) -> DeleteResult:
        key = validate_object_key(key)
        entry = self.objects.pop(key, None)
        return DeleteResult(key=key, existed=entry is not None, soft=False)

    def list_keys(self, *, prefix: str = "", limit: int = 1000) -> list[str]:
        return sorted(k for k in self.objects if k.startswith(prefix))[:limit]

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
