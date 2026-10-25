"""Adversarial filesystem-security tests for the Asset Library boundary.

These attack the real :class:`WorkspaceFilesystem` boundary through the blob
store: symlinked ancestors and leaves, traversal (including mixed separators),
absolute/NUL/``..`` paths, non-regular sources, destination collisions and
failed-publication cleanup.  A failure here is a real security regression, so
there are no broad skips: the only guard is the genuine one that the
descriptor-relative boundary is POSIX-only by design.

Note on quarantine: the Asset Library has no mutable "quarantine" area.  Content
is immutable and content-addressed, and physical isolation is achieved by the
single boundary plus verified keys, so the "symlinked quarantine root/destination"
class of attack is covered by the shard-directory and destination-symlink tests
below rather than a separate component.
"""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path

import pytest

from nexus_ai_agent.creative.assets.blob_store import LocalContentAddressedBlobStore
from nexus_ai_agent.creative.assets.errors import (
    BlobStoreError,
    ContentIntegrityError,
)
from nexus_ai_agent.tools.filesystem_policy import (
    FilesystemBoundaryError,
    WorkspaceFilesystem,
)

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="the secure filesystem boundary is POSIX-only"
)

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(48))


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    path = tmp_path / "store"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture()
def store(root: Path) -> LocalContentAddressedBlobStore:
    return LocalContentAddressedBlobStore(root)


def _outside(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    return outside


def test_symlinked_staging_directory_is_refused(root: Path, tmp_path: Path) -> None:
    store = LocalContentAddressedBlobStore(root)
    outside = _outside(tmp_path)
    # Replace the real staging dir with a symlink pointing outside the root.
    staging = root / "staging"
    for f in staging.iterdir():
        f.unlink()
    staging.rmdir()
    os.symlink(outside, staging)

    with pytest.raises(BlobStoreError):
        store.stage_to("staging/x", io.BytesIO(PNG), max_bytes=1024)
    assert list(outside.iterdir()) == []  # nothing escaped


def test_symlinked_blob_shard_directory_is_refused(root: Path, tmp_path: Path) -> None:
    store = LocalContentAddressedBlobStore(root)
    outside = _outside(tmp_path)
    staging_key, sha, _ = store.stage(io.BytesIO(PNG), max_bytes=1024)
    blob_key = store.blob_key_for(sha)
    shard = root / "blobs" / sha[:2]
    # Swap the shard directory for a symlink to an outside directory.
    shard.mkdir(parents=True, exist_ok=True)
    shard.rmdir()
    os.symlink(outside, shard)

    with pytest.raises((BlobStoreError, ContentIntegrityError)):
        store.store(staging_key, blob_key)
    assert list(outside.iterdir()) == []


def test_symlinked_staged_source_is_refused(root: Path, tmp_path: Path) -> None:
    store = LocalContentAddressedBlobStore(root)
    outside = _outside(tmp_path)
    target = outside / "secret.bin"
    target.write_bytes(PNG)
    link = root / "staging" / "linked"
    os.symlink(target, link)
    with pytest.raises(BlobStoreError):
        store.store("staging/linked", store.blob_key_for(hashlib.sha256(PNG).hexdigest()))


def test_non_regular_staged_source_is_refused(root: Path) -> None:
    store = LocalContentAddressedBlobStore(root)
    (root / "staging" / "adir").mkdir()
    with pytest.raises(BlobStoreError):
        store.store("staging/adir", store.blob_key_for(hashlib.sha256(PNG).hexdigest()))


def test_traversal_and_absolute_and_nul_are_refused(root: Path) -> None:
    fs = WorkspaceFilesystem(root)
    for bad in ("../escape", "a/../../b", "..", "/etc/passwd", "\\windows\\x", "a\x00b"):
        with pytest.raises(FilesystemBoundaryError):
            fs.write_bytes(bad, b"x")


def test_mixed_separator_traversal_is_refused(root: Path) -> None:
    fs = WorkspaceFilesystem(root)
    with pytest.raises(FilesystemBoundaryError):
        fs.write_bytes("safe\\..\\..\\escape", b"x")


def test_blob_key_cannot_be_under_staging(root: Path) -> None:
    store = LocalContentAddressedBlobStore(root)
    staging_key, _, _ = store.stage(io.BytesIO(PNG), max_bytes=1024)
    with pytest.raises(BlobStoreError):
        store.store(staging_key, "staging/moved")


def test_provider_controlled_absolute_destination_is_refused(root: Path) -> None:
    store = LocalContentAddressedBlobStore(root)
    with pytest.raises(BlobStoreError):
        store.blob_key_for("/etc/passwd")
    with pytest.raises(BlobStoreError):
        store.blob_key_for("..")
    with pytest.raises(BlobStoreError):
        store.blob_key_for("a" * 63)


def test_publish_over_existing_mismatched_blob_is_refused(root: Path) -> None:
    store = LocalContentAddressedBlobStore(root)
    staging_key, sha, _ = store.stage(io.BytesIO(PNG), max_bytes=1024)
    blob_key = store.blob_key_for(sha)
    # Plant a corrupt blob at the (correct) content key before publishing.
    target = root / blob_key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"corrupt-and-wrong")

    with pytest.raises(ContentIntegrityError):
        store.store(staging_key, blob_key)
    # The corrupt bytes are not silently accepted or overwritten as valid.
    assert target.read_bytes() == b"corrupt-and-wrong"


def test_failed_publication_leaves_no_partial_blob(root: Path) -> None:
    store = LocalContentAddressedBlobStore(root)
    staging_key, sha, _ = store.stage(io.BytesIO(PNG), max_bytes=1024)
    # A key that does not match the staged content is refused before any move.
    with pytest.raises(BlobStoreError):
        store.store(staging_key, store.blob_key_for("a" * 64))
    assert not (root / "blobs" / "aa").exists()


def test_destination_collision_deduplicates_without_data_loss(root: Path) -> None:
    store = LocalContentAddressedBlobStore(root)
    key = store.blob_key_for(hashlib.sha256(PNG).hexdigest())
    s1, sha1, _ = store.stage(io.BytesIO(PNG), max_bytes=1024)
    store.store(s1, store.blob_key_for(sha1))
    s2, sha2, _ = store.stage(io.BytesIO(PNG), max_bytes=1024)
    _, _, dedup = store.store(s2, store.blob_key_for(sha2))
    assert dedup is True
    assert store.open(key) == PNG


def test_symlinked_leaf_cannot_be_overwritten_through(root: Path, tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(root)
    outside = _outside(tmp_path)
    victim = outside / "victim.bin"
    victim.write_bytes(b"original")
    link = root / "leaf.bin"
    os.symlink(victim, link)
    with pytest.raises(FilesystemBoundaryError):
        fs.write_bytes("leaf.bin", b"overwritten")
    assert victim.read_bytes() == b"original"
