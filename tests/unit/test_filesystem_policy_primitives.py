"""Additive descriptor-relative primitives on ``WorkspaceFilesystem``.

These are the generic capabilities the execution core's staging layer reuses
(atomic publish, boundary-safe quarantine, symlink-safe tree removal).  Each
test drives a real temporary filesystem; symlinks are the attack surface.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nexus_ai_agent.tools.filesystem_policy import FilesystemBoundaryError, WorkspaceFilesystem


def _ws(tmp_path: Path, name: str = "ws") -> WorkspaceFilesystem:
    root = tmp_path / name
    root.mkdir()
    return WorkspaceFilesystem(root)


def test_ensure_directory_creates_and_is_idempotent(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    created = ws.ensure_directory("a/b/c")
    assert created.is_dir()
    assert ws.ensure_directory("a/b/c").is_dir()  # idempotent


def test_ensure_directory_rejects_a_symlinked_component(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, ws.root / "a")
    with pytest.raises(FilesystemBoundaryError):
        ws.ensure_directory("a/b")
    assert not (outside / "b").exists()


def test_ensure_directory_tolerates_a_concurrent_creator(tmp_path: Path) -> None:
    """Two racing creators must both succeed (idempotent), not fail closed."""
    ws = _ws(tmp_path)
    target = ws.root / "a/b/c"
    # Simulate the losing side of the race: the leaf appears between the failed
    # no-follow open and the os.mkdir (which then raises FileExistsError).
    real_mkdir = os.mkdir

    def racing_mkdir(name, *args, **kwargs):
        real_mkdir(name, *args, **kwargs)
        raise FileExistsError(name)

    os.mkdir = racing_mkdir  # type: ignore[assignment]
    try:
        created = ws.ensure_directory("a/b/c")
    finally:
        os.mkdir = real_mkdir  # type: ignore[assignment]
    assert created == target
    assert target.is_dir()


def test_ensure_directory_still_rejects_a_symlink_in_the_race_window(tmp_path: Path) -> None:
    """Tolerating the concurrent-create race must not weaken no-follow."""
    ws = _ws(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (ws.root / "a").mkdir()
    real_mkdir = os.mkdir

    def racing_mkdir(name, *args, **kwargs):
        # A concurrent creator wins the name with a *symlink*, not a directory.
        os.symlink(outside, ws.root / "a" / name)
        raise FileExistsError(name)

    os.mkdir = racing_mkdir  # type: ignore[assignment]
    try:
        with pytest.raises(FilesystemBoundaryError):
            ws.ensure_directory("a/b")
    finally:
        os.mkdir = real_mkdir  # type: ignore[assignment]
    assert not (outside / "b").exists()


def test_require_regular_file_accepts_a_regular_file(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    (ws.root / "f.bin").write_bytes(b"x")
    assert ws.require_regular_file("f.bin") == ws.root / "f.bin"


def test_require_regular_file_rejects_a_symlink(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"secret")
    os.symlink(outside, ws.root / "link.bin")
    with pytest.raises(FilesystemBoundaryError):
        ws.require_regular_file("link.bin")


def test_require_regular_file_rejects_a_directory(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    (ws.root / "d").mkdir()
    with pytest.raises(FilesystemBoundaryError):
        ws.require_regular_file("d")


def test_require_regular_file_rejects_a_missing_file(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    with pytest.raises(FilesystemBoundaryError):
        ws.require_regular_file("nope.bin")


def test_rename_within_moves_a_file(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    (ws.root / "src.bin").write_bytes(b"payload")
    moved = ws.rename_within("src.bin", "nested/dst.bin")
    assert moved.read_bytes() == b"payload"
    assert not (ws.root / "src.bin").exists()


def test_rename_within_rejects_a_symlinked_source(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"secret")
    os.symlink(outside, ws.root / "link.bin")
    with pytest.raises(FilesystemBoundaryError):
        ws.rename_within("link.bin", "dst.bin")
    assert outside.exists()


def test_rename_into_moves_across_workspaces(tmp_path: Path) -> None:
    source = _ws(tmp_path, "source")
    target = _ws(tmp_path, "target")
    (source.root / "a.bin").write_bytes(b"bytes")
    moved = source.rename_into(target, "a.bin", "final/a.bin")
    assert moved == target.root / "final" / "a.bin"
    assert moved.read_bytes() == b"bytes"
    assert not (source.root / "a.bin").exists()


def test_rename_into_rejects_a_symlinked_target_component(tmp_path: Path) -> None:
    source = _ws(tmp_path, "source")
    target = _ws(tmp_path, "target")
    outside = tmp_path / "outside"
    outside.mkdir()
    (source.root / "a.bin").write_bytes(b"bytes")
    os.symlink(outside, target.root / "final")
    with pytest.raises(FilesystemBoundaryError):
        source.rename_into(target, "a.bin", "final/a.bin")
    assert not (outside / "a.bin").exists()


def test_remove_tree_removes_a_directory_recursively(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    (ws.root / "d" / "sub").mkdir(parents=True)
    (ws.root / "d" / "sub" / "f.bin").write_bytes(b"x")
    ws.remove_tree("d")
    assert not (ws.root / "d").exists()


def test_remove_tree_never_follows_a_symlinked_child(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.bin").write_bytes(b"keep")
    (ws.root / "d").mkdir()
    os.symlink(outside, ws.root / "d" / "escape")
    ws.remove_tree("d")
    assert not (ws.root / "d").exists()
    assert (outside / "keep.bin").read_bytes() == b"keep", "must not delete through a symlink"


def test_remove_tree_tolerates_a_missing_directory(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    ws.remove_tree("nope")  # best-effort: absence is not an error


def test_remove_tree_rejects_the_root(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    with pytest.raises(FilesystemBoundaryError):
        ws.remove_tree(".")
