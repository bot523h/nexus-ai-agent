from __future__ import annotations

import asyncio
from pathlib import Path

from nexus_ai_agent.observability.logging import get_logger

from .providers import (
    HuggingFaceProvider,
    LocalCacheProvider,
    ProviderConfig,
    ProviderUnavailable,
    RcloneProvider,
    StorageError,
    StorageProvider,
)

log = get_logger(__name__)


class AIStorageManager:
    """
    Unified storage layer with:
      - provider routing by size
      - retry (3x)
      - provider fallback
      - local caching
      - offline-safe behavior (cache-first)
    """

    def __init__(self, *, cache_dir: Path, config: ProviderConfig):
        self.cache = LocalCacheProvider(cache_dir)

        self.github: StorageProvider | None = None
        if config.github_token and config.github_repo:
            try:
                from .providers.github_releases import GitHubReleasesProvider

                self.github = GitHubReleasesProvider(
                    token=config.github_token,
                    repo=config.github_repo,
                )
            except Exception:
                # Provider module or deps not available; remain disabled.
                self.github = None

        self.mega: StorageProvider | None = None
        if config.mega_email and config.mega_password:
            try:
                from .providers.mega import MegaProvider

                self.mega = MegaProvider(email=config.mega_email, password=config.mega_password)
            except Exception:
                self.mega = None

        self.hf = HuggingFaceProvider(token=config.huggingface_token)

        self.rclone: RcloneProvider | None = None
        if config.rclone_remote:
            self.rclone = RcloneProvider(remote=config.rclone_remote)

    def get_best_provider(self, *, size_bytes: int) -> str:
        if size_bytes < 50 * 1024 * 1024:
            return "github_releases"
        if size_bytes < 500 * 1024 * 1024:
            return "rclone" if (self.rclone and self.rclone.is_configured()) else "github_releases"
        return "mega"

    def _upload_candidates(self, *, preferred: str) -> list[StorageProvider]:
        ordered: list[StorageProvider] = []
        providers: dict[str, StorageProvider] = {}
        if self.github and self.github.is_configured():
            providers[self.github.name] = self.github
        if self.rclone and self.rclone.is_configured():
            providers[self.rclone.name] = self.rclone
        if self.mega and self.mega.is_configured():
            providers[self.mega.name] = self.mega

        if preferred in providers:
            ordered.append(providers.pop(preferred))
        ordered.extend(providers.values())
        return ordered

    def _download_candidates(self) -> list[StorageProvider]:
        ordered: list[StorageProvider] = []
        if self.mega and self.mega.is_configured():
            ordered.append(self.mega)
        if self.github and self.github.is_configured():
            ordered.append(self.github)
        if self.rclone and self.rclone.is_configured():
            ordered.append(self.rclone)
        ordered.append(self.hf)
        return ordered

    async def cache_locally(self, *, local_path: Path, remote_key: str) -> Path:
        await self.cache.upload(local_path=local_path, remote_key=remote_key)
        return self.cache.path_for_key(remote_key)

    async def upload(self, *, local_path: Path, remote_key: str) -> None:
        size = local_path.stat().st_size
        preferred = self.get_best_provider(size_bytes=size)
        candidates = self._upload_candidates(preferred=preferred)
        if not candidates:
            raise ProviderUnavailable("No remote storage providers are configured")

        # Always cache locally first so offline-mode can proceed even if upload fails.
        await self.cache_locally(local_path=local_path, remote_key=remote_key)

        last_err: Exception | None = None
        for provider in candidates:
            for attempt in range(1, 4):
                try:
                    log.info(
                        "storage_upload_attempt",
                        provider=provider.name,
                        key=remote_key,
                        attempt=attempt,
                        bytes=size,
                    )
                    await provider.upload(local_path=local_path, remote_key=remote_key)
                    log.info(
                        "storage_upload_ok",
                        provider=provider.name,
                        key=remote_key,
                        bytes=size,
                    )
                    return
                except ProviderUnavailable as e:
                    last_err = e
                    break  # no point retrying if provider isn't usable for this key
                except Exception as e:  # pragma: no cover
                    last_err = e
                    await asyncio.sleep(0.5 * attempt)
                    continue

        raise StorageError(f"Upload failed for key '{remote_key}': {last_err}") from last_err

    async def download(self, *, remote_key: str, local_path: Path) -> Path:
        # Cache-first.
        try:
            await self.cache.download(remote_key=remote_key, local_path=local_path)
            log.info("storage_cache_hit", key=remote_key)
            return local_path
        except ProviderUnavailable:
            log.info("storage_cache_miss", key=remote_key)

        last_err: Exception | None = None
        for provider in self._download_candidates():
            for attempt in range(1, 4):
                try:
                    log.info(
                        "storage_download_attempt",
                        provider=provider.name,
                        key=remote_key,
                        attempt=attempt,
                    )
                    await provider.download(remote_key=remote_key, local_path=local_path)
                    await self.cache_locally(local_path=local_path, remote_key=remote_key)
                    log.info("storage_download_ok", provider=provider.name, key=remote_key)
                    return local_path
                except ProviderUnavailable as e:
                    last_err = e
                    break
                except Exception as e:  # pragma: no cover
                    last_err = e
                    await asyncio.sleep(0.5 * attempt)
                    continue

        raise StorageError(f"Download failed for key '{remote_key}': {last_err}") from last_err

    async def list_files(self, *, prefix: str = "") -> list[str]:
        keys: set[str] = set()
        for provider in [self.cache] + self._download_candidates():
            try:
                for k in await provider.list_files(prefix=prefix):
                    keys.add(k)
            except Exception:
                continue
        return sorted(keys)
