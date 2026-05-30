from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from .base import ProviderUnavailable


class LocalCacheProvider:
    name = "local_cache"

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def is_configured(self) -> bool:
        return True

    def path_for_key(self, remote_key: str) -> Path:
        return self.cache_dir / remote_key

    async def upload(self, *, local_path: Path, remote_key: str) -> None:
        dest = self.path_for_key(remote_key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copy2, local_path, dest)

    async def download(self, *, remote_key: str, local_path: Path) -> None:
        src = self.path_for_key(remote_key)
        if not src.exists():
            raise ProviderUnavailable(f"Cache miss for key '{remote_key}'")
        local_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copy2, src, local_path)

    async def list_files(self, *, prefix: str = "") -> list[str]:
        root = self.cache_dir / prefix
        if not root.exists():
            return []
        keys: list[str] = []
        for p in root.rglob("*"):
            if p.is_file():
                keys.append(str(p.relative_to(self.cache_dir)))
        keys.sort()
        return keys
