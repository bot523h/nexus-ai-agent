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
                    os.mkdir(part, mode=0o700, dir_fd=fd)
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


__all__ = ["FilesystemBoundaryError", "WorkspaceFilesystem"]
