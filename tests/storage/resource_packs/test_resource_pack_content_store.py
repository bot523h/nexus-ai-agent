"""Local content-addressed store: verify-before-publish and read-only queries."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from nexus_ai_agent.storage.resource_packs import IntegrityError, LocalContentStore


def test_put_index_materialize_and_gc(tmp_path: Path) -> None:
    store = LocalContentStore(tmp_path / "cas")
    assert store.index() == {} and not (tmp_path / "cas").exists()  # queries create nothing
    data = b"hello pack"
    sha = hashlib.sha256(data).hexdigest()
    path = store.put_bytes(data, sha)
    assert path == store.object_path(sha) and store.has(sha)
    assert store.index() == {sha: len(data)}
    assert store.verify_object(sha, len(data)) and not store.verify_object(sha, 3)
    store.materialize(sha, tmp_path / "out/linked.bin")
    store.materialize(sha, tmp_path / "out/copied.bin", prefer_hardlink=False)
    assert (tmp_path / "out/linked.bin").stat().st_nlink == 2  # CAS object + install link
    assert (tmp_path / "out/copied.bin").read_bytes() == data
    assert store.collect_garbage({sha}) == ()
    assert store.collect_garbage(set()) == (sha,) and store.index() == {}


def test_corrupt_candidates_never_become_addressable(tmp_path: Path) -> None:
    store = LocalContentStore(tmp_path / "cas")
    sha = hashlib.sha256(b"expected").hexdigest()
    with pytest.raises(IntegrityError, match="rejected blob"):
        store.put_bytes(b"something else", sha)
    assert not store.has(sha)
    assert list(store.tmp_dir.iterdir()) == []  # the rejected temp file is gone


def test_index_ignores_foreign_files_and_bad_names(tmp_path: Path) -> None:
    store = LocalContentStore(tmp_path / "cas")
    good = b"good"
    sha = hashlib.sha256(good).hexdigest()
    store.put_bytes(good, sha)
    (store.objects_dir / "README").write_text("not a bucket")
    (store.objects_dir / sha[:2] / "notes.txt").write_text("junk")
    misplaced = "ff" + sha[2:]
    (store.objects_dir / sha[:2] / misplaced).write_text("wrong bucket")
    assert store.index() == {sha: len(good)}
    with pytest.raises(ValueError, match="not a sha256"):
        store.object_path("../../etc/passwd")
