from __future__ import annotations

import asyncio
import shutil
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import unquote

from .base import ProviderUnavailable, StorageError

_MAX_REMOTE_KEY_LENGTH = 4096
_MAX_PERCENT_DECODE_PASSES = 32


class LocalCacheProvider:
    name = "local_cache"

    def __init__(self, cache_dir: Path):
        root = Path(cache_dir)
        root.mkdir(parents=True, exist_ok=True)
        # Pin the configured cache root to its canonical location. A configured
        # root may itself be a symlink; after this point key paths are relative
        # to the resolved directory, not to the symlink's parent.
        self.cache_dir = root.resolve(strict=True)

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
        except (OSError, RuntimeError) as exc:
            raise StorageError("Local-cache root is unavailable") from exc
        if root != self.cache_dir:
            raise StorageError("Local-cache root changed through a symlink")
        return root

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
    def _copy_into_cache(provider: LocalCacheProvider, local_path: Path, remote_key: str) -> None:
        dest = provider.path_for_key(remote_key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Re-check after creating missing parents so a symlink introduced in
        # an existing component cannot redirect the copy outside the cache.
        dest = provider.path_for_key(remote_key)
        shutil.copy2(local_path, dest)

    async def upload(self, *, local_path: Path, remote_key: str) -> None:
        # Validate before dispatching to the worker thread or making directories.
        self.path_for_key(remote_key)
        await asyncio.to_thread(self._copy_into_cache, self, local_path, remote_key)

    @staticmethod
    def _copy_from_cache(provider: LocalCacheProvider, remote_key: str, local_path: Path) -> None:
        src = provider.path_for_key(remote_key)
        if not src.is_file():
            raise ProviderUnavailable(f"Cache miss for key '{remote_key}'")
        # Re-check immediately before opening the key-derived source path.
        src = provider.path_for_key(remote_key)
        if not src.is_file():
            raise ProviderUnavailable(f"Cache miss for key '{remote_key}'")
        local_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, local_path)

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
