"""Additional W5 contained-filesystem boundary tests."""

import errno
import io
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from nexus_ai_agent.tools import filesystem_policy as policy
from nexus_ai_agent.tools.filesystem_policy import FilesystemBoundaryError, WorkspaceFilesystem


def test_relative_path_and_workspace_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert policy._validate_relative("", allow_root=True) == ()
    assert policy._validate_relative("./", allow_root=True) == ()
    for path in (None, "", "../escape", "C:\\outside", "nul\x00byte"):
        with pytest.raises(FilesystemBoundaryError):
            policy._validate_relative(path)
    for timeout in (True, 0, -1, float("nan"), float("inf"), "1"):
        with pytest.raises(ValueError, match="timeout"):
            WorkspaceFilesystem(tmp_path, lock_timeout_seconds=timeout)
    file = tmp_path / "file"
    file.write_text("safe")
    with pytest.raises(FilesystemBoundaryError, match="not a directory"):
        WorkspaceFilesystem(file)
    monkeypatch.chdir(tmp_path)
    assert WorkspaceFilesystem("relative").root == tmp_path / "relative"
    with pytest.raises(FilesystemBoundaryError, match="does not exist"):
        WorkspaceFilesystem("missing")._root_fd()


def test_open_directory_rejects_file_and_releases_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = tmp_path / "file"
    file.touch()
    original = os.open
    opened: list[int] = []

    def tracking(*args, **kwargs):
        fd = original(*args, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(policy.os, "open", tracking)
    with pytest.raises(NotADirectoryError):
        policy._open_directory(file)
    with pytest.raises(OSError):
        os.fstat(opened[-1])


def test_contained_text_and_directory_operations(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    fs.write_text("nested/note.txt", "hello")
    assert fs.read_text("nested/note.txt") == "hello"
    assert fs.list_names() == ["nested"]
    assert fs.list_names("nested") == ["note.txt"]
    assert fs.exists("nested/note.txt") and not fs.exists("missing")
    assert fs.ensure_directory(".") == tmp_path
    assert sorted(fs.iter_files()) == [tmp_path / "nested" / "note.txt"]
    with pytest.raises(FilesystemBoundaryError, match="directory does not exist"):
        fs.ensure_directory("missing", create=False)
    with pytest.raises(FilesystemBoundaryError, match="unlink a directory"):
        fs.unlink("nested")
    fd = fs._open_dir(())
    os.close(fd)


def test_text_open_and_codec_failure_are_typed(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    with pytest.raises(FilesystemBoundaryError, match="opened safely"):
        fs.read_text("absent")
    (tmp_path / "invalid").write_bytes(b"\xff")
    with pytest.raises(FilesystemBoundaryError, match="could not be read"):
        fs.read_text("invalid")
    with pytest.raises(FilesystemBoundaryError, match="could not be written"):
        fs.write_text("ascii", "snowman \u2603", encoding="ascii")


def test_symlinks_inside_root_and_outside_are_refused(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    (tmp_path / "file").write_text("keep")
    (tmp_path / "link").symlink_to("file")
    with pytest.raises(FilesystemBoundaryError, match="symlinks"):
        fs.resolve("link")
    assert not fs.exists("link")
    assert (tmp_path / "file").read_text() == "keep"


def test_workspace_iter_files_skips_symlinks_and_handles_absent_root(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "file").touch()
    (tmp_path / "dir" / "link").symlink_to("file")
    assert list(fs.iter_files()) == [tmp_path / "dir" / "file"]
    assert list(WorkspaceFilesystem(tmp_path / "missing").iter_files()) == []


def test_stream_failure_never_replaces_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    destination = tmp_path / "destination"
    destination.write_bytes(b"original")
    monkeypatch.setattr(policy.os, "replace", Mock(side_effect=OSError("replace failed")))
    with pytest.raises(FilesystemBoundaryError):
        fs.write_stream("destination", io.BytesIO(b"new"), max_bytes=100)
    assert destination.read_bytes() == b"original"
    assert not list(tmp_path.glob(".staging-*"))


def test_fsync_unsupported_only_suppresses_expected_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    for code in (errno.EINVAL, errno.EBADF):
        monkeypatch.setattr(policy.os, "fsync", Mock(side_effect=OSError(code, "unsupported")))
        WorkspaceFilesystem._fsync_dir_fd(123)
    monkeypatch.setattr(policy.os, "fsync", Mock(side_effect=OSError(errno.EIO, "io error")))
    with pytest.raises(OSError) as caught:
        WorkspaceFilesystem._fsync_dir_fd(123)
    assert caught.value.errno == errno.EIO


def test_rename_failures_keep_source_unchanged(tmp_path: Path) -> None:
    src = WorkspaceFilesystem(tmp_path / "src")
    dst = WorkspaceFilesystem(tmp_path / "dst")
    src.root.mkdir()
    dst.root.mkdir()
    with pytest.raises(FilesystemBoundaryError, match="rename failed inside"):
        src.rename_within("missing", "dest")
    with pytest.raises(FilesystemBoundaryError, match="rename failed across"):
        src.rename_into(dst, "missing", "dest")
    assert not (dst.root / "dest").exists()


@pytest.mark.parametrize(
    "method", ["read_text", "write_text", "write_bytes", "read_bytes", "iter_bytes", "hash_file"]
)
def test_leaf_open_failure_is_typed_and_never_overwrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    (tmp_path / "file").write_bytes(b"original")
    original_open = os.open

    def failing_open(path, *args, **kwargs):
        if path == "file" and kwargs.get("dir_fd") is not None:
            raise OSError(errno.EACCES, "injected")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(policy.os, "open", failing_open)
    with pytest.raises(FilesystemBoundaryError):
        if method in ("write_text", "write_bytes"):
            getattr(fs, method)("file", "new" if method == "write_text" else b"new")
        elif method == "iter_bytes":
            list(fs.iter_bytes("file"))
        else:
            getattr(fs, method)("file")
    assert (tmp_path / "file").read_bytes() == b"original"


@pytest.mark.parametrize("method", ["read_bytes", "iter_bytes", "hash_file"])
def test_file_fstat_failure_is_typed_and_descriptor_is_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    path = tmp_path / "file"
    path.write_bytes(b"data")
    original = os.fstat
    failed_fds: list[int] = []

    def fail_file(fd: int):
        if os.readlink(f"/proc/self/fd/{fd}") == str(path):
            failed_fds.append(fd)
            raise OSError(errno.EIO, "injected")
        return original(fd)

    monkeypatch.setattr(policy.os, "fstat", fail_file)
    with pytest.raises(FilesystemBoundaryError, match="read safely"):
        result = getattr(fs, method)("file")
        if method == "iter_bytes":
            list(result)
    with pytest.raises(OSError):
        original(failed_fds[0])
    assert path.read_bytes() == b"data"


def test_secure_root_open_failure_is_typed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    monkeypatch.setattr(policy, "_open_directory", Mock(side_effect=OSError("injected")))
    with pytest.raises(FilesystemBoundaryError, match="opened safely"):
        fs._root_fd()


def test_require_file_stat_error_is_typed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    (tmp_path / "file").touch()
    original = os.stat

    def failing(path, *args, **kwargs):
        if path == "file" and kwargs.get("dir_fd") is not None:
            raise OSError("injected")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(policy.os, "stat", failing)
    with pytest.raises(FilesystemBoundaryError, match="inspected safely"):
        fs.require_regular_file("file")


def test_candidate_write_failure_cleans_staging_and_never_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    fs = WorkspaceFilesystem(tmp_path)
    (tmp_path / "source").write_bytes(b"data")
    monkeypatch.setattr(policy.os, "write", Mock(return_value=0))
    with pytest.raises(FilesystemBoundaryError, match="candidate write failed"):
        fs.link_within_if_absent(
            "source", "target", sha256=hashlib.sha256(b"data").hexdigest(), size=4
        )
    assert (tmp_path / "source").read_bytes() == b"data"
    assert not (tmp_path / "target").exists()
    assert not list(tmp_path.glob(".pub_*"))


def test_candidate_copy_refuses_source_growth(tmp_path: Path) -> None:
    import hashlib

    fs = WorkspaceFilesystem(tmp_path)
    (tmp_path / "source").write_bytes(b"data")
    with pytest.raises(FilesystemBoundaryError, match="source grew"):
        fs.link_within_if_absent(
            "source", "target", sha256=hashlib.sha256(b"data").hexdigest(), size=2
        )
    assert not (tmp_path / "target").exists()
    assert not list(tmp_path.glob(".pub_*"))


def test_nonposix_directory_operations_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(policy.os, "name", "unsupported")
        with pytest.raises(FilesystemBoundaryError, match="unavailable"):
            fs._root_fd()
        with pytest.raises(FilesystemBoundaryError, match="unavailable"), fs._parent_fd(("file",)):
            pytest.fail("unsupported platform entered")


def test_bad_stream_limit_and_directory_read_are_rejected(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    with pytest.raises(FilesystemBoundaryError, match="max_bytes"):
        fs.write_stream("file", io.BytesIO(b"data"), max_bytes=0)
    (tmp_path / "dir").mkdir()
    with pytest.raises(FilesystemBoundaryError, match="not a regular file"):
        fs.read_bytes("dir")
    assert list(tmp_path.iterdir()) == [tmp_path / "dir"]


def test_iter_files_skips_symlink_and_non_regular_fifo(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    (tmp_path / "file").write_bytes(b"data")
    (tmp_path / "link").symlink_to("file")
    os.mkfifo(tmp_path / "fifo")
    assert list(fs.iter_files()) == [tmp_path / "file"]


def test_iter_files_handles_scandir_and_entry_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    original = os.scandir
    monkeypatch.setattr(policy.os, "scandir", Mock(side_effect=OSError("injected")))
    assert list(fs.iter_files()) == []
    bad_entry = Mock()
    bad_entry.is_symlink.side_effect = OSError("injected")
    monkeypatch.setattr(policy.os, "scandir", Mock(return_value=iter([bad_entry])))
    assert list(fs.iter_files()) == []
    monkeypatch.setattr(policy.os, "scandir", original)


def test_publication_lock_failures_release_all_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import stat

    fs = WorkspaceFilesystem(tmp_path)
    busy = Mock(acquire=Mock(return_value=False))
    with monkeypatch.context() as patch:
        patch.setattr(policy, "_PUBLICATION_LOCK", busy)
        with pytest.raises(TimeoutError), fs._publication_lock():
            pytest.fail("busy lock accepted")
        busy.release.assert_not_called()
    original = os.fstat

    def nonregular(fd: int):
        info = original(fd)
        if os.readlink(f"/proc/self/fd/{fd}").endswith("/.blob_publish.lock"):
            fields = list(info)
            fields[0] = stat.S_IFDIR | 0o700
            return os.stat_result(fields)
        return info

    with monkeypatch.context() as patch:
        patch.setattr(policy.os, "fstat", nonregular)
        with pytest.raises(FilesystemBoundaryError, match="not regular"), fs._publication_lock():
            pytest.fail("unsafe lock accepted")
    with fs._publication_lock():
        pass


def test_publication_refuses_directory_source(tmp_path: Path) -> None:
    import hashlib

    fs = WorkspaceFilesystem(tmp_path)
    (tmp_path / "source").mkdir()
    with pytest.raises(FilesystemBoundaryError, match="not a regular file"):
        fs.link_within_if_absent(
            "source", "destination", sha256=hashlib.sha256(b"").hexdigest(), size=0
        )
    assert not (tmp_path / "destination").exists()


@pytest.mark.parametrize(
    "failure", ["scandir", "missing", "stat", "open", "open_unlink", "rmdir", "unlink"]
)
def test_tree_cleanup_faults_do_not_follow_paths_or_touch_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    tree = tmp_path / "tree"
    tree.mkdir()
    child = tree / ("child" if failure in ("open", "open_unlink", "rmdir") else "file")
    if failure in ("open", "open_unlink", "rmdir"):
        child.mkdir()
    else:
        child.write_bytes(b"inside")
    outside = tmp_path / "outside"
    outside.write_bytes(b"untouched")
    dir_fd = os.open(tree, os.O_RDONLY | os.O_DIRECTORY)
    real_stat, real_open_dir = os.stat, policy._open_directory
    real_unlink, real_rmdir = os.unlink, os.rmdir
    try:
        with monkeypatch.context() as patch:
            if failure == "scandir":
                patch.setattr(policy.os, "scandir", Mock(side_effect=OSError("injected")))
            if failure in ("missing", "stat"):

                def fail_stat(path, *args, **kwargs):
                    if path == child.name and kwargs.get("dir_fd") == dir_fd:
                        raise FileNotFoundError() if failure == "missing" else OSError("injected")
                    return real_stat(path, *args, **kwargs)

                patch.setattr(policy.os, "stat", fail_stat)
            if failure in ("open", "open_unlink"):

                def fail_open(path, **kwargs):
                    if path == child.name:
                        raise OSError("injected")
                    return real_open_dir(path, **kwargs)

                patch.setattr(policy, "_open_directory", fail_open)
            if failure in ("unlink", "open_unlink"):

                def fail_unlink(path, *args, **kwargs):
                    if path == child.name:
                        raise OSError("injected")
                    return real_unlink(path, *args, **kwargs)

                patch.setattr(policy.os, "unlink", fail_unlink)
            if failure == "rmdir":

                def fail_rmdir(path, *args, **kwargs):
                    if path == child.name:
                        raise OSError("injected")
                    return real_rmdir(path, *args, **kwargs)

                patch.setattr(policy.os, "rmdir", fail_rmdir)
            fs._remove_tree_fd(dir_fd)
    finally:
        os.close(dir_fd)
    assert outside.read_bytes() == b"untouched"


def test_binary_write_failure_is_typed_and_closes_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    real_fdopen = os.fdopen
    written_fds: list[int] = []

    class BrokenWriter:
        def __init__(self, fd: int):
            self.fd = fd
            written_fds.append(fd)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            os.close(self.fd)

        def write(self, _data):
            raise OSError("write failed")

    def failing_fdopen(fd: int, mode: str, *args, **kwargs):
        if mode == "wb":
            return BrokenWriter(fd)
        return real_fdopen(fd, mode, *args, **kwargs)

    monkeypatch.setattr(policy.os, "fdopen", failing_fdopen)
    with pytest.raises(FilesystemBoundaryError, match="written safely"):
        fs.write_bytes("file", b"data")
    with pytest.raises(OSError):
        os.fstat(written_fds[0])


def test_failed_directory_listing_is_typed(tmp_path: Path) -> None:
    fs = WorkspaceFilesystem(tmp_path)
    (tmp_path / "file").write_bytes(b"unchanged")
    with pytest.raises(FilesystemBoundaryError, match="directory could not be opened"):
        fs.list_names("file")
    assert (tmp_path / "file").read_bytes() == b"unchanged"
