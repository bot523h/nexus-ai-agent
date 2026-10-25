"""Unit tests for the local content-addressed blob store and the streaming /
atomic filesystem primitives it reuses from ``WorkspaceFilesystem``.

These exercise the *real* adapter against real bytes on a real temporary
filesystem — a port is not proof of an implementation.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

from nexus_ai_agent.creative.assets.blob_store import LocalContentAddressedBlobStore
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

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


@pytest.fixture()
def store(tmp_path: Path) -> LocalContentAddressedBlobStore:
    return LocalContentAddressedBlobStore(tmp_path / "blobstore")


def test_blob_key_is_deterministic_and_content_derived(
    store: LocalContentAddressedBlobStore,
) -> None:
    import hashlib

    digest = hashlib.sha256(PNG).hexdigest()
    key = store.blob_key_for(digest)
    assert key == f"blobs/{digest[:2]}/{digest}"
    assert key.startswith("blobs/")
    with pytest.raises(BlobStoreError):
        store.blob_key_for("not-a-digest")
    with pytest.raises(BlobStoreError):
        store.blob_key_for(digest.upper())


def test_store_measures_bytes_and_deduplicates(store: LocalContentAddressedBlobStore) -> None:
    import hashlib

    staging, sha, size = store.stage(io.BytesIO(PNG), max_bytes=1024)
    assert sha == hashlib.sha256(PNG).hexdigest()
    assert size == len(PNG)
    blob_key = store.blob_key_for(sha)

    measured_sha, measured_size, dedup = store.store(staging, blob_key)
    assert (measured_sha, measured_size, dedup) == (sha, size, False)
    assert store.verify(blob_key, sha256=sha, size_bytes=size)

    # A second ingest of identical bytes reuses the same physical blob.
    staging2, sha2, _ = store.stage(io.BytesIO(PNG), max_bytes=1024)
    assert sha2 == sha
    _, _, dedup2 = store.store(staging2, store.blob_key_for(sha2))
    assert dedup2 is True


def test_oversize_stream_is_refused_and_leaves_no_staging(
    store: LocalContentAddressedBlobStore, tmp_path: Path
) -> None:
    with pytest.raises(ContentTooLarge):
        store.stage(io.BytesIO(b"x" * 5000), max_bytes=100)
    # No staging residue remains after a refused ingest.
    staging_dir = tmp_path / "blobstore" / "staging"
    assert list(staging_dir.iterdir()) == []


def test_mismatched_blob_key_is_refused(store: LocalContentAddressedBlobStore) -> None:
    staging, sha, _ = store.stage(io.BytesIO(PNG), max_bytes=1024)
    wrong_key = store.blob_key_for("a" * 64)
    with pytest.raises(BlobStoreError):
        store.store(staging, wrong_key)


def test_missing_blob_verify_is_false_and_open_raises(
    store: LocalContentAddressedBlobStore,
) -> None:
    key = store.blob_key_for("a" * 64)
    assert store.verify(key, sha256="a" * 64, size_bytes=1) is False
    with pytest.raises(ContentIntegrityError):
        store.open(key)


def test_tampered_blob_is_detected(store: LocalContentAddressedBlobStore, tmp_path: Path) -> None:
    staging, sha, size = store.stage(io.BytesIO(PNG), max_bytes=1024)
    blob_key = store.blob_key_for(sha)
    store.store(staging, blob_key)
    # Corrupt the stored bytes on disk, then re-verify.
    target = tmp_path / "blobstore" / blob_key
    assert target.exists()
    target.write_bytes(b"tampered")
    assert store.verify(blob_key, sha256=sha, size_bytes=size) is False
    with pytest.raises(ContentIntegrityError):
        store.open(blob_key)


def test_workspace_write_stream_rejects_traversal(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    with pytest.raises(FilesystemBoundaryError):
        fs.write_stream("../escape", io.BytesIO(PNG), max_bytes=1024)


def test_workspace_write_stream_refuses_a_symlinked_leaf(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    outside = tmp_path.parent / "outside-target.bin"
    outside.write_bytes(b"do-not-touch")
    link = tmp_path / "leaf.bin"
    os.symlink(outside, link)
    # A pre-existing symlink leaf must never be written through: the boundary
    # refuses the path outright rather than following it.
    with pytest.raises(FilesystemBoundaryError):
        fs.write_stream("leaf.bin", io.BytesIO(PNG), max_bytes=1024)
    assert outside.read_bytes() == b"do-not-touch"
    assert link.is_symlink()


def test_workspace_hash_file_is_bounded_and_regular_only(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    fs.write_bytes("data/blob.bin", PNG)
    digest, size = fs.hash_file("data/blob.bin")
    import hashlib

    assert digest == hashlib.sha256(PNG).hexdigest()
    assert size == len(PNG)
    with pytest.raises(FilesystemBoundaryError):
        fs.hash_file("data")  # a directory is not a regular file


def test_workspace_write_stream_enforces_limit(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    with pytest.raises(ContentLimitExceeded):
        fs.write_stream("big.bin", io.BytesIO(b"y" * 200), max_bytes=50)
    assert not (tmp_path / "big.bin").exists()
