"""Fail-closed filesystem boundary primitives.

The application accepts paths from commands and maintenance configuration. A
string prefix check is not a security boundary: symlinks, alternate spelling,
and a parent-directory swap can redirect an operation after validation. This
module keeps the boundary small and makes the operation use the same physical
path that was validated.

On POSIX, directory file descriptors plus ``O_NOFOLLOW`` are used for every
mutation/read. The conservative fallback for platforms without ``dir_fd``
still rejects absolute paths, traversal, and symlinks before operating.
"""

from __future__ import annotations

import ntpath
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class FilesystemBoundaryError(ValueError):
    """A path cannot be proven to remain inside the configured root."""


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


def _validate_relative(raw: str, *, allow_root: bool = False) -> tuple[str, ...]:
    if not isinstance(raw, str):
        raise FilesystemBoundaryError("path must be a string")
    if "\x00" in raw:
        raise FilesystemBoundaryError("path contains a NUL byte")
    if not raw:
        if allow_root:
            return ()
        raise FilesystemBoundaryError("path must not be empty")

    # Check both syntaxes even on POSIX. Inputs can come from a Windows client
    # or a copied archive name and must not acquire different semantics later.
    drive, _ = ntpath.splitdrive(raw)
    if raw.startswith(("/", "\\")) or drive or ntpath.isabs(raw):
        raise FilesystemBoundaryError("absolute paths are not allowed")

    normalized = raw.replace("\\", "/")
    parts = tuple(part for part in normalized.split("/") if part not in ("", "."))
    if not parts and allow_root:
        return ()
    if any(part == ".." for part in parts):
        raise FilesystemBoundaryError("path traversal is not allowed")
    return parts


class WorkspaceFilesystem:
    """A physically contained, symlink-resistant workspace boundary."""

    def __init__(self, root: str | Path) -> None:
        configured = Path(root).expanduser()
        if not configured.is_absolute():
            configured = Path.cwd() / configured
        self.root = configured.resolve(strict=False)
        if self.root.exists() and not self.root.is_dir():
            raise FilesystemBoundaryError(f"workspace root is not a directory: {self.root}")

    def resolve(self, raw: str, *, allow_root: bool = False) -> Path:
        """Return the canonical path after lexical and physical containment checks."""
        parts = _validate_relative(raw, allow_root=allow_root)
        candidate = self.root.joinpath(*parts)
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(self.root):
            raise FilesystemBoundaryError("path escapes the workspace")
        self._reject_existing_symlink_components(parts)
        return resolved

    def _reject_existing_symlink_components(self, parts: tuple[str, ...]) -> None:
        current = self.root
        for part in parts:
            current /= part
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                return
            if stat.S_ISLNK(mode):
                raise FilesystemBoundaryError("symlinks are not allowed in workspace paths")

    def _root_fd(self) -> int:
        if not self.root.exists():
            raise FilesystemBoundaryError(f"workspace root does not exist: {self.root}")
        if os.name == "posix":
            try:
                return os.open(
                    self.root,
                    os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC,
                )
            except OSError as exc:
                raise FilesystemBoundaryError(
                    f"workspace root could not be opened safely: {self.root}"
                ) from exc
        raise FilesystemBoundaryError(
            "secure directory operations are unavailable on this platform"
        )

    @contextmanager
    def _parent_fd(
        self, parts: tuple[str, ...], *, create: bool = False
    ) -> Iterator[tuple[int, str | None]]:
        if os.name != "posix":
            raise FilesystemBoundaryError(
                "secure directory operations are unavailable on this platform"
            )
        fd = self._root_fd()
        try:
            for part in parts[:-1]:
                try:
                    child = os.open(
                        part,
                        os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC,
                        dir_fd=fd,
                    )
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass  # a concurrent creator won; the no-follow open re-validates
                    child = os.open(
                        part,
                        os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC,
                        dir_fd=fd,
                    )
                os.close(fd)
                fd = child
            yield fd, (parts[-1] if parts else None)
        finally:
            os.close(fd)

    def read_text(self, raw: str, *, encoding: str = "utf-8") -> str:
        parts = _validate_relative(raw)
        self.resolve(raw)
        with self._parent_fd(parts) as (parent_fd, name):
            assert name is not None
            flags = os.O_RDONLY | _NOFOLLOW | _CLOEXEC
            try:
                fd = os.open(name, flags, dir_fd=parent_fd)
            except OSError as exc:
                raise FilesystemBoundaryError("file could not be opened safely") from exc
            try:
                with os.fdopen(fd, "r", encoding=encoding) as handle:
                    return handle.read()
            except (OSError, UnicodeError) as exc:
                raise FilesystemBoundaryError("file could not be read") from exc

    def write_text(self, raw: str, content: str, *, encoding: str = "utf-8") -> Path:
        parts = _validate_relative(raw)
        resolved = self.resolve(raw)
        with self._parent_fd(parts, create=True) as (parent_fd, name):
            assert name is not None
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _NOFOLLOW | _CLOEXEC
            try:
                fd = os.open(name, flags, mode=0o600, dir_fd=parent_fd)
            except OSError as exc:
                raise FilesystemBoundaryError("file could not be opened for safe writing") from exc
            try:
                with os.fdopen(fd, "w", encoding=encoding) as handle:
                    handle.write(content)
            except (OSError, UnicodeError) as exc:
                raise FilesystemBoundaryError("file could not be written") from exc
        return resolved

    def write_bytes(self, raw: str, content: bytes, *, create_parents: bool = True) -> Path:
        """Write bytes via a no-follow leaf open relative to a pinned parent fd.

        The parent chain is opened one component at a time with ``O_NOFOLLOW``
        after the lexical/canonical validation.  A concurrent replacement of
        any ancestor with a symlink therefore fails closed instead of
        redirecting the write outside this workspace.
        """
        parts = _validate_relative(raw)
        resolved = self.resolve(raw)
        try:
            with self._parent_fd(parts, create=create_parents) as (parent_fd, name):
                assert name is not None
                flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _NOFOLLOW | _CLOEXEC
                try:
                    fd = os.open(name, flags, mode=0o600, dir_fd=parent_fd)
                except OSError as exc:
                    raise FilesystemBoundaryError(
                        "file could not be opened for safe writing"
                    ) from exc
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(content)
                except OSError as exc:
                    raise FilesystemBoundaryError("file could not be written safely") from exc
        except FilesystemBoundaryError:
            raise
        except OSError as exc:
            # Includes a component swapped to a symlink between resolve() and
            # opening the descriptor-relative parent chain.
            raise FilesystemBoundaryError("parent path could not be opened safely") from exc
        return resolved

    def list_names(self, raw: str = ".") -> list[str]:
        parts = _validate_relative(raw, allow_root=True) if raw != "." else ()
        self.resolve(raw, allow_root=True)
        if not parts:
            root_fd = self._root_fd()
            try:
                with os.scandir(root_fd) as entries:
                    return sorted(entry.name for entry in entries)
            finally:
                os.close(root_fd)
        with self._parent_fd(parts) as (parent_fd, name):
            assert name is not None
            try:
                directory_fd = os.open(
                    name,
                    os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                raise FilesystemBoundaryError("directory could not be opened safely") from exc
            try:
                with os.scandir(directory_fd) as entries:
                    return sorted(entry.name for entry in entries)
            finally:
                os.close(directory_fd)

    def iter_files(self) -> Iterator[Path]:
        """Yield regular files without following directory or file symlinks."""
        if not self.root.exists():
            return
        pending = [self.root]
        while pending:
            directory = pending.pop()
            try:
                entries = list(os.scandir(directory))
            except OSError:
                continue
            for entry in entries:
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        yield Path(entry.path)
                except OSError:
                    continue

    def unlink(self, raw: str) -> None:
        """Unlink one contained file without following a swapped parent/leaf."""
        parts = _validate_relative(raw)
        self.resolve(raw)
        with self._parent_fd(parts) as (parent_fd, name):
            assert name is not None
            try:
                os.unlink(name, dir_fd=parent_fd)
            except FileNotFoundError:
                return
            except IsADirectoryError as exc:
                raise FilesystemBoundaryError("refusing to unlink a directory") from exc

    # -- descriptor-relative directory / rename / remove primitives ---------- #
    # These are the additive, generic capabilities an atomic publish and a
    # boundary-safe quarantine need.  They operate relative to an already-open
    # directory fd with ``O_NOFOLLOW``, so a symlink swapped in after a lexical
    # check cannot redirect the operation outside the workspace.
    def _open_dir(self, parts: tuple[str, ...]) -> int:
        """Open a contained directory fd, rejecting every symlink component."""
        if not parts:
            return self._root_fd()
        try:
            with self._parent_fd(parts) as (parent_fd, name):
                assert name is not None
                return os.open(
                    name, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC, dir_fd=parent_fd
                )
        except OSError as exc:
            raise FilesystemBoundaryError("directory could not be opened safely") from exc

    def ensure_directory(self, raw: str, *, create: bool = True) -> Path:
        """Create (idempotently) a contained directory, rejecting symlinks."""
        parts = _validate_relative(raw)
        resolved = self.resolve(raw)
        if not parts:
            return resolved
        try:
            with self._parent_fd(parts, create=create) as (parent_fd, name):
                assert name is not None
                try:
                    fd = os.open(
                        name, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC, dir_fd=parent_fd
                    )
                except FileNotFoundError:
                    if not create:
                        raise FilesystemBoundaryError("directory does not exist") from None
                    try:
                        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
                    except FileExistsError:
                        pass  # a concurrent creator won; the no-follow open re-validates
                    fd = os.open(
                        name, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC, dir_fd=parent_fd
                    )
                os.close(fd)
        except OSError as exc:
            raise FilesystemBoundaryError("directory could not be created safely") from exc
        return resolved

    def require_regular_file(self, raw: str) -> Path:
        """Prove ``raw`` is a contained *regular* file (never a symlink/dir)."""
        parts = _validate_relative(raw)
        resolved = self.resolve(raw)
        with self._parent_fd(parts) as (parent_fd, name):
            assert name is not None
            try:
                info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                raise FilesystemBoundaryError("file does not exist") from None
            except OSError as exc:
                raise FilesystemBoundaryError("file could not be inspected safely") from exc
        if not stat.S_ISREG(info.st_mode):
            raise FilesystemBoundaryError("path is not a regular file")
        return resolved

    def rename_within(self, source: str, destination: str, *, create_parents: bool = True) -> Path:
        """Atomically rename one contained path to another inside this workspace.

        Both sides are resolved descriptor-relative, so a symlinked source or
        destination component can never redirect the rename out of the
        workspace.
        """
        src_parts = _validate_relative(source)
        dst_parts = _validate_relative(destination)
        resolved = self.resolve(destination)
        self.resolve(source)
        try:
            with self._parent_fd(src_parts) as (src_fd, src_name):
                with self._parent_fd(dst_parts, create=create_parents) as (dst_fd, dst_name):
                    assert src_name is not None and dst_name is not None
                    os.rename(src_name, dst_name, src_dir_fd=src_fd, dst_dir_fd=dst_fd)
        except OSError as exc:
            raise FilesystemBoundaryError("rename failed inside the workspace") from exc
        return resolved

    def rename_into(
        self,
        destination_workspace: WorkspaceFilesystem,
        source: str,
        destination: str,
        *,
        create_parents: bool = True,
    ) -> Path:
        """Atomically rename a contained path into another workspace boundary.

        ``self`` owns the source; ``destination_workspace`` owns the target.
        Both directories are held open, so neither side can be swapped to a
        symlink between validation and the rename.
        """
        src_parts = _validate_relative(source)
        dst_parts = _validate_relative(destination)
        resolved = destination_workspace.resolve(destination)
        self.resolve(source)
        try:
            with self._parent_fd(src_parts) as (src_fd, src_name):
                with destination_workspace._parent_fd(dst_parts, create=create_parents) as (
                    dst_fd,
                    dst_name,
                ):
                    assert src_name is not None and dst_name is not None
                    os.rename(src_name, dst_name, src_dir_fd=src_fd, dst_dir_fd=dst_fd)
        except OSError as exc:
            raise FilesystemBoundaryError("rename failed across workspaces") from exc
        return resolved

    def remove_tree(self, raw: str) -> None:
        """Recursively remove a contained directory without following symlinks.

        Absence is tolerated (best-effort cleanup); a symlinked target or
        ancestor is refused rather than followed.
        """
        parts = _validate_relative(raw)
        if not parts:
            raise FilesystemBoundaryError("refusing to remove the workspace root")
        self.resolve(raw)
        try:
            fd = self._open_dir(parts)
        except FilesystemBoundaryError:
            return  # absent or symlinked: nothing safe to remove
        try:
            self._remove_tree_fd(fd)
        finally:
            os.close(fd)
        try:
            with self._parent_fd(parts) as (parent_fd, name):
                assert name is not None
                try:
                    os.rmdir(name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
        except FilesystemBoundaryError:
            raise
        except OSError as exc:
            # The parent can be swapped after the tree fd is closed; never let
            # that late failure escape as an untyped filesystem exception.
            raise FilesystemBoundaryError("directory could not be removed safely") from exc

    def _remove_tree_fd(self, dir_fd: int) -> None:
        try:
            with os.scandir(dir_fd) as entries:
                names = [entry.name for entry in entries]
        except OSError:
            return
        for name in names:
            try:
                info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError:
                continue
            if stat.S_ISDIR(info.st_mode):
                try:
                    child = os.open(
                        name, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC, dir_fd=dir_fd
                    )
                except OSError:
                    # A symlink or an unreadable entry: unlink it, never recurse.
                    try:
                        os.unlink(name, dir_fd=dir_fd)
                    except OSError:
                        pass
                    continue
                try:
                    self._remove_tree_fd(child)
                finally:
                    os.close(child)
                try:
                    os.rmdir(name, dir_fd=dir_fd)
                except OSError:
                    pass
            else:
                try:
                    os.unlink(name, dir_fd=dir_fd)
                except OSError:
                    pass


__all__ = ["FilesystemBoundaryError", "WorkspaceFilesystem"]
