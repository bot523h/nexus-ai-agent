from __future__ import annotations

import asyncio
import errno
import os
import secrets
import shutil
import stat
import weakref
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import unquote

from .base import ProviderUnavailable, StorageError

_MAX_REMOTE_KEY_LENGTH = 4096
_MAX_PERCENT_DECODE_PASSES = 32
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_READ_FILE_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_CREATE_FILE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


def _supports_descriptor_containment() -> bool:
    required = (os.open, os.mkdir, os.stat, os.unlink, os.rename)
    return (
        os.name == "posix"
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and all(function in os.supports_dir_fd for function in required)
        and os.listdir in os.supports_fd
    )


class LocalCacheProvider:
    name = "local_cache"

    def __init__(self, cache_dir: Path):
        if not _supports_descriptor_containment():
            raise StorageError(
                "Local cache requires POSIX descriptor-relative filesystem operations "
                "with O_NOFOLLOW support"
            )

        root = Path(cache_dir)
        root.mkdir(parents=True, exist_ok=True)
        self.cache_dir = root.resolve(strict=True)
        root_fd: int | None = None
        try:
            self._check_root_ancestor_chain(self.cache_dir)
            root_fd = os.open(self.cache_dir, _DIRECTORY_FLAGS)
            opened = os.fstat(root_fd)
            named = os.stat(self.cache_dir, follow_symlinks=False)
            if (
                not self._is_private_directory(opened)
                or not self._is_private_directory(named)
                or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            ):
                raise StorageError("Local-cache root changed or is writable by another user")
        except StorageError:
            if root_fd is not None:
                os.close(root_fd)
            raise
        except OSError as exc:
            if root_fd is not None:
                os.close(root_fd)
            raise StorageError("Unable to pin the local-cache root directory") from exc
        assert root_fd is not None

        # Keep a directory descriptor for the lifetime of the provider. Every
        # key-derived read/write is relative to this inode, not a path that can
        # be redirected after validation. The finalizer closes it when the
        # provider is discarded; callers may also close it explicitly.
        self._root_fd = root_fd
        self._root_stat = (opened.st_dev, opened.st_ino)
        self._root_finalizer = weakref.finalize(self, os.close, root_fd)

    def close(self) -> None:
        """Release the pinned cache-root descriptor; subsequent I/O fails closed."""
        if self._root_finalizer.alive:
            self._root_finalizer()

    def is_configured(self) -> bool:
        return True

    @staticmethod
    def _is_private_directory(metadata: os.stat_result) -> bool:
        return (
            stat.S_ISDIR(metadata.st_mode)
            and metadata.st_uid == os.geteuid()
            and not metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        )

    @classmethod
    def _check_root_ancestor_chain(cls, root: Path) -> None:
        """Ensure other UIDs cannot move the pinned root by changing a parent.

        A root-owned sticky ancestor such as ``/tmp`` is safe for an
        euid-owned cache entry: the sticky bit prevents another UID from
        renaming that entry. Non-sticky group/world-writable ancestors and
        ancestors owned by an unrelated UID are refused.
        """
        effective_uid = os.geteuid()
        current = root
        is_cache_root = True
        while True:
            metadata = os.stat(current, follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode):
                raise StorageError("Local-cache root ancestry contains a non-directory")
            if is_cache_root:
                if not cls._is_private_directory(metadata):
                    raise StorageError(
                        "Local-cache root must be owned by this process and not be "
                        "group/world writable"
                    )
            else:
                if metadata.st_uid not in (effective_uid, 0):
                    raise StorageError("Local-cache root has an untrusted parent directory")
                writable_by_others = metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                sticky = metadata.st_mode & stat.S_ISVTX
                if writable_by_others and not sticky:
                    raise StorageError("Local-cache root has a writable non-sticky ancestor")
            if current.parent == current:
                return
            current = current.parent
            is_cache_root = False

    @staticmethod
    def _components(
        value: str,
        *,
        allow_empty: bool = False,
        allow_trailing_slash: bool = False,
    ) -> tuple[str, ...]:
        if not isinstance(value, str):
            raise StorageError("Local-cache key must be a string")
        if len(value) > _MAX_REMOTE_KEY_LENGTH:
            raise StorageError("Local-cache key is too long")
        if "\x00" in value:
            raise StorageError("Local-cache key contains a NUL byte")
        if not value:
            if allow_empty:
                return ()
            raise StorageError("Local-cache key must not be empty")
        if "\\" in value:
            # Remote keys use POSIX separators. Reject Windows separators
            # rather than letting their meaning depend on the host OS.
            raise StorageError("Local-cache key contains a Windows separator")
        if PurePosixPath(value).is_absolute():
            raise StorageError("Local-cache key must be relative")

        windows_path = PureWindowsPath(value)
        if windows_path.drive or windows_path.root:
            raise StorageError("Local-cache key must not contain a drive or root")

        parts = value.split("/")
        if allow_trailing_slash and parts[-1] == "":
            parts.pop()
        if not parts or any(part in ("", ".", "..") for part in parts):
            raise StorageError("Local-cache key contains an unsafe path component")
        return tuple(parts)

    @classmethod
    def _validated_components(
        cls,
        value: str,
        *,
        allow_empty: bool = False,
        allow_trailing_slash: bool = False,
    ) -> tuple[str, ...]:
        """Validate literal and repeatedly URL-decoded spellings of a key.

        The local filename preserves the exact remote key (percent escapes
        remain literal), but unsafe decoded interpretations are refused so an
        encoded traversal cannot become dangerous at a later boundary or on a
        different platform.
        """
        components = cls._components(
            value,
            allow_empty=allow_empty,
            allow_trailing_slash=allow_trailing_slash,
        )
        decoded = value
        for _ in range(_MAX_PERCENT_DECODE_PASSES + 1):
            cls._components(
                decoded,
                allow_empty=allow_empty,
                allow_trailing_slash=allow_trailing_slash,
            )
            next_decoded = unquote(decoded)
            if next_decoded == decoded:
                return components
            decoded = next_decoded
        raise StorageError("Local-cache key has excessive percent-encoding")

    def _root(self) -> Path:
        """Return the canonical path for path-reporting APIs, not for I/O."""
        if not self._root_finalizer.alive:
            raise StorageError("Local-cache provider is closed")
        try:
            root = self.cache_dir.resolve(strict=True)
            named = os.stat(self.cache_dir, follow_symlinks=False)
            opened = os.fstat(self._root_fd)
        except (OSError, RuntimeError) as exc:
            raise StorageError("Local-cache root is unavailable") from exc
        if (
            root != self.cache_dir
            or (named.st_dev, named.st_ino) != self._root_stat
            or (opened.st_dev, opened.st_ino) != self._root_stat
        ):
            raise StorageError("Local-cache root changed through a symlink or rename")
        return root

    def _duplicate_root_fd(self) -> int:
        if not self._root_finalizer.alive:
            raise StorageError("Local-cache provider is closed")
        try:
            named = os.stat(self.cache_dir, follow_symlinks=False)
            opened = os.fstat(self._root_fd)
            if (
                not self._is_private_directory(named)
                or not self._is_private_directory(opened)
                or (named.st_dev, named.st_ino) != self._root_stat
                or (opened.st_dev, opened.st_ino) != self._root_stat
            ):
                raise StorageError("Local-cache root changed through a symlink or rename")
            return os.dup(self._root_fd)
        except OSError as exc:
            raise StorageError("Unable to access the pinned local-cache root") from exc

    def _open_directory_chain(self, components: tuple[str, ...], *, create: bool) -> int:
        """Open each key directory with openat + O_NOFOLLOW, anchored to root fd."""
        descriptor = self._duplicate_root_fd()
        try:
            for component in components:
                try:
                    next_descriptor = os.open(
                        component,
                        _DIRECTORY_FLAGS,
                        dir_fd=descriptor,
                    )
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        # Another writer may have created it; the following
                        # O_NOFOLLOW open decides whether it is a real directory.
                        pass
                    next_descriptor = os.open(
                        component,
                        _DIRECTORY_FLAGS,
                        dir_fd=descriptor,
                    )
                try:
                    next_metadata = os.fstat(next_descriptor)
                except BaseException:
                    os.close(next_descriptor)
                    raise
                if not self._is_private_directory(next_metadata):
                    os.close(next_descriptor)
                    raise StorageError(
                        "Local-cache subdirectory has unsafe ownership or permissions"
                    )
                os.close(descriptor)
                descriptor = next_descriptor
            return descriptor
        except FileNotFoundError:
            os.close(descriptor)
            raise
        except OSError as exc:
            os.close(descriptor)
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise StorageError("Local-cache key traverses a symlink or non-directory") from exc
            raise StorageError("Unable to open a local-cache directory safely") from exc
        except BaseException:
            os.close(descriptor)
            raise

    def _path_for_components(self, components: tuple[str, ...]) -> Path:
        root = self._root()
        candidate = root
        for component in components:
            candidate = candidate / component
            try:
                if candidate.is_symlink():
                    raise StorageError("Local-cache key traverses a symlink")
            except OSError as exc:
                raise StorageError("Unable to inspect local-cache path") from exc

        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise StorageError("Local-cache key escapes its root") from exc
        return resolved

    def path_for_key(self, remote_key: str) -> Path:
        """Return a contained path for display; upload/download do I/O by fd."""
        components = self._validated_components(remote_key)
        return self._path_for_components(components)

    @staticmethod
    def _copy_into_cache(provider: LocalCacheProvider, local_path: Path, remote_key: str) -> None:
        components = provider._validated_components(remote_key)
        parent_fd = provider._open_directory_chain(components[:-1], create=True)
        temp_name = ""
        temp_fd: int | None = None
        try:
            for _ in range(8):
                temp_name = f".nexus-upload-{secrets.token_hex(16)}.tmp"
                try:
                    temp_fd = os.open(
                        temp_name,
                        _CREATE_FILE_FLAGS,
                        0o600,
                        dir_fd=parent_fd,
                    )
                    break
                except FileExistsError:
                    continue
            if temp_fd is None:
                raise StorageError("Unable to allocate a unique local-cache staging file")

            try:
                with (
                    Path(local_path).open("rb") as source,
                    os.fdopen(temp_fd, "wb", closefd=True) as destination,
                ):
                    temp_fd = None
                    shutil.copyfileobj(source, destination)
                    destination.flush()
                    os.fsync(destination.fileno())
            except OSError as exc:
                raise StorageError("Unable to write the local-cache staging file") from exc

            try:
                # The temporary file was created in the already-open parent.
                # rename/replace changes only the final directory entry; it
                # never follows a symlink or hardlink at the remote-key leaf.
                os.replace(
                    temp_name,
                    components[-1],
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
            except OSError as exc:
                raise StorageError("Unable to atomically publish the local-cache file") from exc
            temp_name = ""
        finally:
            if temp_fd is not None:
                os.close(temp_fd)
            if temp_name:
                try:
                    os.unlink(temp_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
                except OSError:
                    # The entry may have been moved by a concurrent writer;
                    # it is never followed when cleanup unlinks by dir fd.
                    pass
            os.close(parent_fd)

    async def upload(self, *, local_path: Path, remote_key: str) -> None:
        # Reject invalid keys before scheduling any filesystem work.
        self._validated_components(remote_key)
        await asyncio.to_thread(self._copy_into_cache, self, local_path, remote_key)

    @staticmethod
    def _copy_from_cache(provider: LocalCacheProvider, remote_key: str, local_path: Path) -> None:
        components = provider._validated_components(remote_key)
        try:
            parent_fd = provider._open_directory_chain(components[:-1], create=False)
        except FileNotFoundError:
            raise ProviderUnavailable(f"Cache miss for key '{remote_key}'") from None

        source_fd: int | None = None
        try:
            try:
                source_fd = os.open(
                    components[-1],
                    _READ_FILE_FLAGS,
                    dir_fd=parent_fd,
                )
            except FileNotFoundError:
                raise ProviderUnavailable(f"Cache miss for key '{remote_key}'") from None
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise StorageError("Local-cache key traverses a symlink or non-file") from exc
                raise StorageError("Unable to open the local-cache file safely") from exc

            metadata = os.fstat(source_fd)
            root_metadata = os.fstat(provider._root_fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                or metadata.st_dev != root_metadata.st_dev
                or metadata.st_nlink != 1
            ):
                raise StorageError("Local-cache entry is not a private regular file")

            local_path = Path(local_path)
            local_path.parent.mkdir(parents=True, exist_ok=True)
            with os.fdopen(source_fd, "rb", closefd=True) as source:
                source_fd = None
                with local_path.open("wb") as destination:
                    shutil.copyfileobj(source, destination)
        except OSError as exc:
            raise StorageError("Unable to copy the local-cache file safely") from exc
        finally:
            if source_fd is not None:
                os.close(source_fd)
            os.close(parent_fd)

    async def download(self, *, remote_key: str, local_path: Path) -> None:
        # Invalid keys fail closed; they are not treated as cache misses.
        self._validated_components(remote_key)
        await asyncio.to_thread(self._copy_from_cache, self, remote_key, local_path)

    def _path_for_prefix(self, prefix: str) -> Path:
        components = self._validated_components(
            prefix,
            allow_empty=True,
            allow_trailing_slash=True,
        )
        return self._path_for_components(components)

    @staticmethod
    def _list_from_directory_fd(
        descriptor: int,
        prefix: tuple[str, ...],
        root_device: int,
        output: list[str],
    ) -> None:
        for name in sorted(os.listdir(descriptor)):
            try:
                metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except OSError:
                continue

            if stat.S_ISDIR(metadata.st_mode):
                try:
                    child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
                except OSError:
                    # A replaced symlink or directory is skipped, never followed.
                    continue
                try:
                    if not LocalCacheProvider._is_private_directory(os.fstat(child_fd)):
                        raise StorageError(
                            "Local-cache subdirectory has unsafe ownership or permissions"
                        )
                    LocalCacheProvider._list_from_directory_fd(
                        child_fd,
                        (*prefix, name),
                        root_device,
                        output,
                    )
                finally:
                    os.close(child_fd)
                continue

            if not stat.S_ISREG(metadata.st_mode):
                continue
            key = "/".join((*prefix, name))
            try:
                LocalCacheProvider._validated_components(key)
            except StorageError:
                continue

            try:
                file_fd = os.open(name, _READ_FILE_FLAGS, dir_fd=descriptor)
            except OSError:
                continue
            try:
                opened = os.fstat(file_fd)
                if (
                    stat.S_ISREG(opened.st_mode)
                    and opened.st_uid == os.geteuid()
                    and not opened.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                    and opened.st_dev == root_device
                    and opened.st_nlink == 1
                ):
                    output.append(key)
            finally:
                os.close(file_fd)

    async def list_files(self, *, prefix: str = "") -> list[str]:
        components = self._validated_components(
            prefix,
            allow_empty=True,
            allow_trailing_slash=True,
        )
        try:
            start_fd = self._open_directory_chain(components, create=False)
        except FileNotFoundError:
            return []

        keys: list[str] = []
        try:
            root_device = os.fstat(self._root_fd).st_dev
            self._list_from_directory_fd(start_fd, components, root_device, keys)
        except OSError as exc:
            raise StorageError("Unable to list local-cache files safely") from exc
        finally:
            os.close(start_fd)
        keys.sort()
        return keys
