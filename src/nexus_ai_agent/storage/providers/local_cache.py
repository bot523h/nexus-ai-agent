from __future__ import annotations

import asyncio
import errno
import os
import re
import shutil
import stat
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import unquote

from .base import ProviderUnavailable, StorageError

_MAX_REMOTE_KEY_LENGTH = 4096
_MAX_PERCENT_DECODE_PASSES = 32
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)

_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


class LocalCacheProvider:
    name = "local_cache"

    def __init__(self, cache_dir: Path):
        root = Path(cache_dir)
        root.mkdir(parents=True, exist_ok=True)
        # Pin the configured cache root to its canonical location and inode.
        # A configured root may itself be a symlink; after this point key paths
        # are resolved relative to the pinned directory descriptor.
        self.cache_dir = root.resolve(strict=True)
        root_stat = os.stat(self.cache_dir, follow_symlinks=False)
        if not stat.S_ISDIR(root_stat.st_mode):
            raise StorageError("Local-cache root is not a directory")
        self._pinned_root_id = (root_stat.st_dev, root_stat.st_ino)

    def is_configured(self) -> bool:
        return True

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
        if _CONTROL_CHAR_RE.search(value):
            raise StorageError("Local-cache key contains a control character")
        if not value:
            if allow_empty:
                return ()
            raise StorageError("Local-cache key must not be empty")
        if "\\" in value:
            # Remote keys use POSIX separators. Reject Windows separators
            # rather than letting their meaning depend on the host OS.
            raise StorageError("Local-cache key contains a Windows separator")
        if ":" in value:
            raise StorageError("Local-cache key must not contain a drive or stream separator")
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
        for part in parts:
            if part != part.rstrip(". "):
                raise StorageError("Local-cache key component has trailing dots or spaces")
            stem = part.split(".", 1)[0].upper()
            if stem in _WINDOWS_RESERVED_NAMES:
                raise StorageError("Local-cache key contains a reserved device name")
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

        The local path preserves the exact remote key (percent escapes remain
        literal filename characters), but unsafe decoded interpretations are
        refused so an encoded traversal cannot become dangerous at a later
        boundary or on a different platform.
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
        try:
            root = self.cache_dir.resolve(strict=True)
            root_stat = os.stat(self.cache_dir, follow_symlinks=False)
        except (OSError, RuntimeError) as exc:
            raise StorageError("Local-cache root is unavailable") from exc
        if root != self.cache_dir or not stat.S_ISDIR(root_stat.st_mode):
            raise StorageError("Local-cache root changed through a symlink")
        if (root_stat.st_dev, root_stat.st_ino) != self._pinned_root_id:
            raise StorageError("Local-cache root inode changed")
        return root

    def _open_pinned_root_fd(self) -> int:
        self._root()
        try:
            root_fd = os.open(
                self.cache_dir,
                os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
            )
        except OSError as exc:
            raise StorageError("Unable to open pinned local-cache root") from exc
        try:
            fd_stat = os.fstat(root_fd)
            if not stat.S_ISDIR(fd_stat.st_mode):
                raise StorageError("Pinned local-cache root is not a directory")
            if (fd_stat.st_dev, fd_stat.st_ino) != self._pinned_root_id:
                raise StorageError("Pinned local-cache root inode mismatch")
        except BaseException:
            os.close(root_fd)
            raise
        return root_fd

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
        components = self._validated_components(remote_key)
        return self._path_for_components(components)

    @staticmethod
    def _open_parent_dir_fd(
        provider: LocalCacheProvider,
        components: tuple[str, ...],
        *,
        create_missing: bool,
        remote_key: str,
    ) -> int:
        dir_fd = provider._open_pinned_root_fd()
        try:
            for component in components[:-1]:
                if create_missing:
                    try:
                        os.mkdir(component, 0o755, dir_fd=dir_fd)
                    except FileExistsError:
                        pass
                    except OSError as exc:
                        raise StorageError(
                            "Unable to create local-cache directory component"
                        ) from exc
                try:
                    next_fd = os.open(
                        component,
                        os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
                        dir_fd=dir_fd,
                    )
                except FileNotFoundError as exc:
                    if not create_missing:
                        raise ProviderUnavailable(f"Cache miss for key '{remote_key}'") from exc
                    raise StorageError("Local-cache directory component disappeared") from exc
                except OSError as exc:
                    raise StorageError(
                        "Local-cache key traverses a symlink or non-directory"
                    ) from exc
                try:
                    next_stat = os.fstat(next_fd)
                    if not stat.S_ISDIR(next_stat.st_mode):
                        os.close(next_fd)
                        raise StorageError("Local-cache intermediate component is not a directory")
                except BaseException:
                    os.close(next_fd)
                    raise
                os.close(dir_fd)
                dir_fd = next_fd
            return dir_fd
        except BaseException:
            os.close(dir_fd)
            raise

    @staticmethod
    def _copy_into_cache(provider: LocalCacheProvider, local_path: Path, remote_key: str) -> None:
        components = provider._validated_components(remote_key)
        try:
            src_fd = os.open(local_path, os.O_RDONLY | _O_CLOEXEC)
        except OSError as exc:
            raise StorageError("Unable to open local source file for cache upload") from exc

        try:
            src_stat = os.fstat(src_fd)
            if not stat.S_ISREG(src_stat.st_mode):
                raise StorageError("Local source path is not a regular file")
            dir_fd = provider._open_parent_dir_fd(
                provider,
                components,
                create_missing=True,
                remote_key=remote_key,
            )
            try:
                leaf = components[-1]
                try:
                    dst_fd = os.open(
                        leaf,
                        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_NOFOLLOW | _O_CLOEXEC,
                        0o644,
                        dir_fd=dir_fd,
                    )
                except OSError as exc:
                    raise StorageError(
                        "Local-cache destination is a symlink or cannot be opened safely"
                    ) from exc
                try:
                    dst_stat = os.fstat(dst_fd)
                    if not stat.S_ISREG(dst_stat.st_mode):
                        raise StorageError("Local-cache destination is not a regular file")
                    with os.fdopen(dst_fd, "wb") as dst_file:
                        dst_fd = -1
                        with os.fdopen(src_fd, "rb") as src_file:
                            src_fd = -1
                            shutil.copyfileobj(src_file, dst_file)
                            dst_file.flush()
                finally:
                    if dst_fd >= 0:
                        os.close(dst_fd)
            finally:
                os.close(dir_fd)
        finally:
            if src_fd >= 0:
                os.close(src_fd)

    async def upload(self, *, local_path: Path, remote_key: str) -> None:
        # Validate before dispatching to the worker thread; worker I/O then
        # traverses component-by-component from the pinned root fd with O_NOFOLLOW.
        self.path_for_key(remote_key)
        await asyncio.to_thread(self._copy_into_cache, self, local_path, remote_key)

    @staticmethod
    def _copy_from_cache(provider: LocalCacheProvider, remote_key: str, local_path: Path) -> None:
        components = provider._validated_components(remote_key)
        dir_fd = provider._open_parent_dir_fd(
            provider,
            components,
            create_missing=False,
            remote_key=remote_key,
        )
        try:
            leaf = components[-1]
            try:
                src_fd = os.open(
                    leaf,
                    os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC,
                    dir_fd=dir_fd,
                )
            except FileNotFoundError as exc:
                raise ProviderUnavailable(f"Cache miss for key '{remote_key}'") from exc
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.EISDIR, errno.EPERM, errno.EACCES):
                    raise StorageError(
                        "Local-cache source is a symlink or non-regular file"
                    ) from exc
                raise StorageError("Unable to open local-cache entry safely") from exc

            try:
                src_stat = os.fstat(src_fd)
                if not stat.S_ISREG(src_stat.st_mode):
                    raise StorageError("Local-cache source is not a regular file")
                local_path.parent.mkdir(parents=True, exist_ok=True)
                dst_fd = os.open(
                    local_path,
                    os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_NOFOLLOW | _O_CLOEXEC,
                    0o644,
                )
                try:
                    with os.fdopen(src_fd, "rb") as src_file:
                        src_fd = -1
                        with os.fdopen(dst_fd, "wb") as dst_file:
                            dst_fd = -1
                            shutil.copyfileobj(src_file, dst_file)
                            dst_file.flush()
                finally:
                    if dst_fd >= 0:
                        os.close(dst_fd)
            finally:
                if src_fd >= 0:
                    os.close(src_fd)
        finally:
            os.close(dir_fd)

    async def download(self, *, remote_key: str, local_path: Path) -> None:
        # Invalid keys fail closed; they are not treated as cache misses.
        self.path_for_key(remote_key)
        await asyncio.to_thread(self._copy_from_cache, self, remote_key, local_path)

    def _path_for_prefix(self, prefix: str) -> Path:
        components = self._validated_components(
            prefix,
            allow_empty=True,
            allow_trailing_slash=True,
        )
        return self._path_for_components(components)

    async def list_files(self, *, prefix: str = "") -> list[str]:
        root = self._path_for_prefix(prefix)
        if not root.is_dir():
            return []

        keys: list[str] = []
        for path in root.rglob("*"):
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                key = path.relative_to(self.cache_dir).as_posix()
                # Also reject cache entries whose own path now traverses a
                # symlink or has an encoded traversal spelling.
                self.path_for_key(key)
            except (OSError, ValueError, StorageError):
                continue
            keys.append(key)
        keys.sort()
        return keys
