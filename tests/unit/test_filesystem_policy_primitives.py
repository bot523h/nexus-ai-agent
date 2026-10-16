"""Additive descriptor-relative primitives on ``WorkspaceFilesystem``.

These are the generic capabilities the execution core's staging layer reuses
(atomic publish, boundary-safe quarantine, symlink-safe tree removal).  Each
test drives a real temporary filesystem; symlinks are the attack surface.
"""

from __future__ import annotations

import errno
import io
import os
import stat
from contextlib import contextmanager
from pathlib import Path

import pytest

from nexus_ai_agent.tools.filesystem_policy import (
    ContentLimitExceeded,
    FilesystemBoundaryError,
    WorkspaceFilesystem,
)


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


def _lose_the_mkdir_race(monkeypatch: pytest.MonkeyPatch, *, as_symlink_to: Path | None = None):
    """Model a concurrent creator that wins the ``os.mkdir`` race.

    The patched ``os.mkdir`` performs the creation (a real directory, or a
    hostile symlink when ``as_symlink_to`` is given) and then raises
    ``FileExistsError`` exactly like a lost race would.
    """
    real_mkdir = os.mkdir

    def racing_mkdir(name, mode=0o777, *, dir_fd=None):
        if as_symlink_to is None:
            real_mkdir(name, mode, dir_fd=dir_fd)
        else:
            os.symlink(as_symlink_to, name, dir_fd=dir_fd)
        raise FileExistsError(17, "File exists", name)

    monkeypatch.setattr(os, "mkdir", racing_mkdir)


def test_ensure_directory_tolerates_a_lost_creation_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A losing concurrent creator is success, not a boundary failure: the
    subsequent ``O_NOFOLLOW`` open re-validates the winner's directory."""
    ws = _ws(tmp_path)
    _lose_the_mkdir_race(monkeypatch)
    created = ws.ensure_directory("leaf")
    assert created.is_dir() and not created.is_symlink()

    monkeypatch.undo()
    _lose_the_mkdir_race(monkeypatch)
    created = ws.ensure_directory("a/b/c")  # exercises the _parent_fd create path too
    assert created.is_dir() and not created.is_symlink()


def test_ensure_directory_race_loser_still_rejects_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The race tolerance must never become a symlink bypass: if the entry the
    loser now finds is a symlink, the no-follow open refuses it."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.txt").write_text("victim")
    ws = _ws(tmp_path)
    _lose_the_mkdir_race(monkeypatch, as_symlink_to=outside)
    with pytest.raises(FilesystemBoundaryError):
        ws.ensure_directory("leaf")
    assert not (outside / "leaf").exists() and (outside / "victim.txt").exists()

    monkeypatch.undo()
    _lose_the_mkdir_race(monkeypatch, as_symlink_to=outside)
    with pytest.raises(FilesystemBoundaryError):
        ws.ensure_directory("a/b")  # the _parent_fd create path
    assert not (outside / "b").exists() and (outside / "victim.txt").exists()


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


def test_write_bytes_round_trips_inside_the_workspace(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    written = ws.write_bytes("nested/artifact.bin", b"\x00payload")
    assert written == ws.root / "nested" / "artifact.bin"
    assert written.read_bytes() == b"\x00payload"


def test_write_bytes_rejects_parent_replaced_with_symlink_after_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A swap in the resolve/open gap is refused by the descriptor walk."""
    ws = _ws(tmp_path)
    (ws.root / "job" / "attempt" / "staging").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "marker").write_bytes(b"untouched")
    original_parent_fd = ws._parent_fd

    @contextmanager
    def swap_before_descriptor_walk(parts: tuple[str, ...], *, create: bool = False):
        if parts == ("job", "attempt", "staging", "victim.bin"):
            (ws.root / "job").rename(ws.root / "job.saved")
            os.symlink(outside, ws.root / "job")
        with original_parent_fd(parts, create=create) as opened:
            yield opened

    monkeypatch.setattr(ws, "_parent_fd", swap_before_descriptor_walk)
    with pytest.raises(FilesystemBoundaryError):
        ws.write_bytes("job/attempt/staging/victim.bin", b"outside-write", create_parents=False)
    assert (outside / "marker").read_bytes() == b"untouched"
    assert not (outside / "attempt").exists()
    assert (ws.root / "job.saved" / "attempt" / "staging").is_dir()


def _swap_job_to_symlink_before_parent_walk(ws: WorkspaceFilesystem, outside: Path, *, victim: str):
    """Patch ``ws._parent_fd`` to swap ``job`` for a symlink just before the
    descriptor walk (i.e. after ``resolve`` validated the real directory)."""
    original_parent_fd = ws._parent_fd

    @contextmanager
    def swap_before_descriptor_walk(parts: tuple[str, ...], *, create: bool = False):
        if parts == ("job", "attempt", "staging", victim):
            (ws.root / "job").rename(ws.root / "job.saved")
            os.symlink(outside, ws.root / "job")
        with original_parent_fd(parts, create=create) as opened:
            yield opened

    return swap_before_descriptor_walk


def test_write_stream_wraps_a_parent_symlink_swap_as_boundary_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parent swapped to a symlink between resolve() and _parent_fd() must
    surface as the typed boundary error, never a raw OSError."""
    ws = _ws(tmp_path)
    (ws.root / "job" / "attempt" / "staging").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "marker").write_bytes(b"untouched")
    monkeypatch.setattr(
        ws,
        "_parent_fd",
        _swap_job_to_symlink_before_parent_walk(ws, outside, victim="victim.bin"),
    )
    with pytest.raises(FilesystemBoundaryError):
        ws.write_stream("job/attempt/staging/victim.bin", io.BytesIO(b"payload"), max_bytes=1024)
    assert (outside / "marker").read_bytes() == b"untouched"
    assert not (outside / "attempt").exists()
    assert not (outside / "victim.bin").exists()
    # The real (renamed-away) tree is intact and holds no staging residue.
    staging_dir = ws.root / "job.saved" / "attempt" / "staging"
    assert staging_dir.is_dir()
    assert not any(staging_dir.iterdir()), "no staging file may survive the failure"


def test_write_stream_wraps_a_staging_open_failure_as_boundary_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raw OSError from the staging os.open is wrapped at this boundary."""
    ws = _ws(tmp_path)
    (ws.root / "job").mkdir()
    real_open = os.open

    def failing_staging_open(name, flags, mode=0o777, *, dir_fd=None):
        if isinstance(name, str) and name.startswith(".staging-"):
            raise OSError(errno.ENOSPC, "no space left on device", name)
        return real_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", failing_staging_open)
    with pytest.raises(FilesystemBoundaryError):
        ws.write_stream("job/victim.bin", io.BytesIO(b"payload"), max_bytes=1024)
    monkeypatch.undo()
    # Neither the target nor any staging residue exists after the failure.
    assert not (ws.root / "job" / "victim.bin").exists()
    assert not any(p.name.startswith(".staging-") for p in (ws.root / "job").iterdir())


def test_write_stream_keeps_content_limit_exceeded_typed(
    tmp_path: Path,
) -> None:
    """ContentLimitExceeded is a FilesystemBoundaryError subclass and must keep
    its own type across the boundary so callers can switch on it exactly."""
    ws = _ws(tmp_path)
    with pytest.raises(ContentLimitExceeded):
        ws.write_stream("big.bin", io.BytesIO(b"y" * 200), max_bytes=50)
    # No partial target and no staging residue survive the refused stream.
    assert not (ws.root / "big.bin").exists()
    assert not any(p.name.startswith(".staging-") for p in ws.root.iterdir())


def test_write_stream_cleans_staging_when_the_stream_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read failure mid-stream still removes the staging file and wraps the
    error at the boundary."""
    ws = _ws(tmp_path)
    (ws.root / "job").mkdir()

    class ExplodingStream(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            raise OSError(errno.EIO, "input stream died")

    with pytest.raises(FilesystemBoundaryError):
        ws.write_stream("job/victim.bin", ExplodingStream(), max_bytes=1024)
    assert not (ws.root / "job" / "victim.bin").exists()
    assert not any(p.name.startswith(".staging-") for p in (ws.root / "job").iterdir())


def test_remove_tree_wraps_parent_reopen_symlink_race_as_boundary_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parent swap after recursive removal stays a typed boundary failure."""
    ws = _ws(tmp_path)
    target = ws.root / "job" / "target"
    (target / "nested").mkdir(parents=True)
    (target / "nested" / "file.bin").write_bytes(b"inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "marker").write_bytes(b"untouched")
    original_parent_fd = ws._parent_fd
    calls = 0

    @contextmanager
    def swap_before_parent_reopen(parts: tuple[str, ...], *, create: bool = False):
        nonlocal calls
        if parts == ("job", "target"):
            calls += 1
            if calls == 2:  # after _remove_tree_fd, before the final rmdir
                (ws.root / "job").rename(ws.root / "job.saved")
                os.symlink(outside, ws.root / "job")
        with original_parent_fd(parts, create=create) as opened:
            yield opened

    monkeypatch.setattr(ws, "_parent_fd", swap_before_parent_reopen)
    with pytest.raises(FilesystemBoundaryError):
        ws.remove_tree("job/target")
    assert calls == 2
    assert (outside / "marker").read_bytes() == b"untouched"
    assert (ws.root / "job.saved" / "target").is_dir()
    assert not (ws.root / "job.saved" / "target" / "nested").exists()


def test_require_regular_file_wraps_a_missing_parent_as_boundary_error(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    with pytest.raises(FilesystemBoundaryError):
        ws.require_regular_file("missing/child.bin")


def test_require_regular_file_wraps_parent_symlink_swap_as_boundary_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _ws(tmp_path)
    target = ws.root / "job" / "attempt" / "artifact.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "marker").write_bytes(b"untouched")
    original_parent_fd = ws._parent_fd

    @contextmanager
    def swap_before_descriptor_walk(parts: tuple[str, ...], *, create: bool = False):
        if parts == ("job", "attempt", "artifact.bin"):
            (ws.root / "job").rename(ws.root / "job.saved")
            os.symlink(outside, ws.root / "job")
        with original_parent_fd(parts, create=create) as opened:
            yield opened

    monkeypatch.setattr(ws, "_parent_fd", swap_before_descriptor_walk)
    with pytest.raises(FilesystemBoundaryError):
        ws.require_regular_file("job/attempt/artifact.bin")
    assert (outside / "marker").read_bytes() == b"untouched"
    assert (ws.root / "job.saved" / "attempt" / "artifact.bin").read_bytes() == b"inside"


def _run_with_deadline(ws: WorkspaceFilesystem, call: str, seconds: float = 5.0) -> str:
    """Run ``ws.<call>`` in a child process so a regression hangs the child, not pytest."""
    import subprocess
    import sys

    code = (
        "import sys\n"
        "from nexus_ai_agent.tools.filesystem_policy import (\n"
        "    FilesystemBoundaryError, WorkspaceFilesystem)\n"
        f"ws = WorkspaceFilesystem({str(ws.root)!r})\n"
        "try:\n"
        f"    ws.{call}\n"
        "    print('OK')\n"
        "except FilesystemBoundaryError:\n"
        "    print('BOUNDARY')\n"
    )
    try:
        done = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "HANG"
    return done.stdout.strip() or done.stderr.strip()[-300:]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs need POSIX")
@pytest.mark.parametrize(
    ("call", "expected"),
    [
        # A FIFO is not a directory: opening it as one must be refused (fail closed).
        ('list_names("pipe")', "BOUNDARY"),
        ('ensure_directory("pipe")', "BOUNDARY"),
        # remove_tree is documented best-effort cleanup: it skips a target it cannot
        # safely open as a directory and returns normally, leaving the FIFO alone.
        ('remove_tree("pipe")', "OK"),
    ],
)
def test_a_fifo_in_the_workspace_never_blocks_directory_opens(
    tmp_path: Path, call: str, expected: str
) -> None:
    """DoS contract (CWE-400): a planted FIFO is refused promptly, never waited on.

    The outcome is asserted exactly per primitive so that losing the descriptor
    type check (which would let ``ensure_directory`` return OK) fails the test.
    """
    ws = _ws(tmp_path)
    os.mkfifo(ws.root / "pipe")
    outcome = _run_with_deadline(ws, call)
    assert outcome != "HANG", f"{call} blocked on a FIFO"
    assert outcome == expected, f"{call}: expected {expected}, got {outcome}"
    assert stat.S_ISFIFO(os.lstat(ws.root / "pipe").st_mode), "the FIFO must be left untouched"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs need POSIX")
def test_a_fifo_as_an_intermediate_component_never_blocks(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    os.mkfifo(ws.root / "pipe")
    assert _run_with_deadline(ws, 'list_names("pipe/child")') == "BOUNDARY"


def test_a_path_through_a_regular_file_is_a_typed_boundary_error(tmp_path: Path) -> None:
    """``file/child`` must raise FilesystemBoundaryError, not a raw NotADirectoryError."""
    ws = _ws(tmp_path)
    (ws.root / "f.txt").write_text("x")
    for call in (
        lambda: ws.list_names("f.txt/child"),
        lambda: ws.ensure_directory("f.txt/child"),
        lambda: ws.write_text("f.txt/child", "x"),
        lambda: ws.read_text("f.txt/child"),
    ):
        with pytest.raises(FilesystemBoundaryError):
            call()


def test_final_symlink_to_a_directory_is_refused_by_every_directory_primitive(
    tmp_path: Path,
) -> None:
    """The O_DIRECTORY|O_NOFOLLOW hole: a final symlink to a dir must not be followed."""
    ws = _ws(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.txt").write_text("v")
    os.symlink(outside, ws.root / "link")
    with pytest.raises(FilesystemBoundaryError):
        ws.list_names("link")
    with pytest.raises(FilesystemBoundaryError):
        ws.ensure_directory("link")
    # Every primitive rejects the symlink at resolve() before any open happens,
    # so nothing outside the workspace is ever reached.
    with pytest.raises(FilesystemBoundaryError):
        ws.remove_tree("link")
    assert (outside / "victim.txt").read_text() == "v"


def test_intermediate_symlink_and_traversal_are_refused(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, ws.root / "mid")
    with pytest.raises(FilesystemBoundaryError):
        ws.ensure_directory("mid/child")
    assert not (outside / "child").exists()
    for bad in ("../escape", "a/../../escape", "/abs"):
        with pytest.raises(FilesystemBoundaryError):
            ws.ensure_directory(bad)
