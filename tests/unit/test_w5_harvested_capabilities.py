"""Regression tests for content-addressed publication under collision."""

from __future__ import annotations

import io
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from nexus_ai_agent.creative.assets.blob_store import LocalContentAddressedBlobStore
from nexus_ai_agent.creative.assets.errors import ContentIntegrityError


@pytest.mark.parametrize("winner", [b"payload", b"corrupt"])
def test_interleaved_destination_is_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, winner: bytes
) -> None:
    store = LocalContentAddressedBlobStore(tmp_path)
    staging, digest, size = store.stage(io.BytesIO(b"payload"), max_bytes=100)
    key = store.blob_key_for(digest)
    real_link = os.link

    def competing_link(src: str, dst: str, **kwargs: object) -> None:
        target = tmp_path / key
        target.write_bytes(winner)
        real_link(src, dst, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "link", competing_link)
    if winner == b"payload":
        assert store.store(staging, key) == (digest, size, True)
        assert not (tmp_path / staging).exists()
    else:
        with pytest.raises(ContentIntegrityError):
            store.store(staging, key)
    assert (tmp_path / key).read_bytes() == winner


def test_source_replacement_never_publishes_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalContentAddressedBlobStore(tmp_path)
    staging, digest, _ = store.stage(io.BytesIO(b"payload"), max_bytes=100)
    key = store.blob_key_for(digest)
    real_link = os.link

    def replace_source(src: str, dst: str, **kwargs: object) -> None:
        path = tmp_path / staging
        path.unlink()
        path.write_bytes(b"forged")
        real_link(src, dst, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "link", replace_source)
    assert store.store(staging, key) == (digest, len(b"payload"), False)
    assert (tmp_path / key).read_bytes() == b"payload"


def test_source_mutated_after_copy_cannot_modify_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalContentAddressedBlobStore(tmp_path)
    staging, digest, _ = store.stage(io.BytesIO(b"payload"), max_bytes=100)
    key = store.blob_key_for(digest)
    real_link = os.link

    def modify_source(src: str, dst: str, **kwargs: object) -> None:
        (tmp_path / staging).write_bytes(b"forged")
        real_link(src, dst, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "link", modify_source)
    assert store.store(staging, key) == (digest, len(b"payload"), False)
    assert (tmp_path / key).read_bytes() == b"payload"


def test_changed_source_before_copy_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalContentAddressedBlobStore(tmp_path)
    staging, digest, _ = store.stage(io.BytesIO(b"payload"), max_bytes=100)
    key = store.blob_key_for(digest)
    real_publish = store._fs.link_within_if_absent

    def mutate(source: str, destination: str, *, sha256: str, size: int) -> bool:
        (tmp_path / staging).write_bytes(b"forged")
        return real_publish(source, destination, sha256=sha256, size=size)

    monkeypatch.setattr(store._fs, "link_within_if_absent", mutate)
    with pytest.raises(ContentIntegrityError):
        store.store(staging, key)
    assert not (tmp_path / key).exists()
    assert not list((tmp_path / key).parent.glob(".pub_*"))


@pytest.mark.parametrize("replace_destination", [False, True])
def test_corrupted_candidate_rolls_back_only_its_own_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replace_destination: bool
) -> None:
    store = LocalContentAddressedBlobStore(tmp_path)
    staging, digest, _ = store.stage(io.BytesIO(b"payload"), max_bytes=100)
    key = store.blob_key_for(digest)
    target = tmp_path / key
    real_link = os.link
    real_digest = store._fs._fd_digest
    calls = 0

    def corrupt_candidate(src: str, dst: str, **kwargs: object) -> None:
        parent = kwargs["src_dir_fd"]
        assert isinstance(parent, int)
        os.chmod(src, 0o600, dir_fd=parent, follow_symlinks=False)
        fd = os.open(src, os.O_WRONLY | os.O_TRUNC, dir_fd=parent)
        try:
            os.write(fd, b"forged")
        finally:
            os.close(fd)
        real_link(src, dst, **kwargs)  # type: ignore[arg-type]

    def replacement(fd: int) -> tuple[str, int]:
        nonlocal calls
        calls += 1
        result = real_digest(fd)
        if calls == 2 and replace_destination:
            target.unlink()
            target.write_bytes(b"other process")
        return result

    monkeypatch.setattr(os, "link", corrupt_candidate)
    monkeypatch.setattr(store._fs, "_fd_digest", replacement)
    with pytest.raises(ContentIntegrityError):
        store.store(staging, key)
    if replace_destination:
        assert target.read_bytes() == b"other process"
    else:
        assert not target.exists()


def test_concurrent_publication_has_one_winner(tmp_path: Path) -> None:
    store = LocalContentAddressedBlobStore(tmp_path)
    a, digest, size = store.stage(io.BytesIO(b"payload"), max_bytes=100)
    b, _, _ = store.stage(io.BytesIO(b"payload"), max_bytes=100)
    key = store.blob_key_for(digest)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda staging: store.store(staging, key), (a, b)))
    assert sorted(result[2] for result in results) == [False, True]
    assert all(result[:2] == (digest, size) for result in results)
    assert (tmp_path / key).read_bytes() == b"payload"


def test_root_and_staging_permissions_and_symlink_refusal(tmp_path: Path) -> None:
    import stat

    root = tmp_path / "root"
    root.mkdir(mode=0o755)
    (root / "staging").mkdir(mode=0o755)
    LocalContentAddressedBlobStore(root)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "staging").stat().st_mode) == 0o700
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(OSError):
        LocalContentAddressedBlobStore(link)


def test_locked_publication_times_out(tmp_path: Path) -> None:
    import fcntl

    store = LocalContentAddressedBlobStore(tmp_path, lock_timeout_seconds=0.01)
    staging, digest, _ = store.stage(io.BytesIO(b"payload"), max_bytes=100)
    fd = os.open(tmp_path / ".blob_publish.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(TimeoutError, match="publication lock timed out"):
            store.store(staging, store.blob_key_for(digest))
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_async_publication_and_directory_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import stat

    store = LocalContentAddressedBlobStore(tmp_path)
    stage, digest, size = store.stage(io.BytesIO(b"payload"), max_bytes=100)
    real_fsync = os.fsync
    directories: list[int] = []

    def tracking_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directories.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", tracking_fsync)
    assert asyncio.run(store.publish_from_staging_async(stage, store.blob_key_for(digest))) == (
        digest,
        size,
        False,
    )
    assert directories
