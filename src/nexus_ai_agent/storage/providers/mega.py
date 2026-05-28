from __future__ import annotations

import asyncio
from pathlib import Path

from .base import ProviderUnavailable, StorageError, filename_to_safe_key, safe_key_to_filename


class MegaProvider:
    name = "mega"

    def __init__(self, *, email: str, password: str, root_folder: str = "NEXUS"):
        self._email = email
        self._password = password
        self._root = root_folder

    def is_configured(self) -> bool:
        return bool(self._email and self._password)

    def _get_client(self):
        try:
            from mega import Mega  # type: ignore
        except Exception as e:  # pragma: no cover
            raise ProviderUnavailable("mega.py is not installed") from e

        mega = Mega()
        # Never log credentials.
        return mega.login(self._email, self._password)

    async def upload(self, *, local_path: Path, remote_key: str) -> None:
        def _run() -> None:
            client = self._get_client()
            root = client.find(self._root)
            if not root:
                root = client.create_folder(self._root)
            name = safe_key_to_filename(remote_key)
            client.upload(str(local_path), dest=root, dest_filename=name)

        await asyncio.to_thread(_run)

    async def download(self, *, remote_key: str, local_path: Path) -> None:
        def _run() -> None:
            client = self._get_client()
            root = client.find(self._root)
            if not root:
                raise ProviderUnavailable("MEGA root folder not found")
            name = safe_key_to_filename(remote_key)
            node = client.find(name, root)
            if not node:
                raise ProviderUnavailable("File not found on MEGA")
            local_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_dir = local_path.parent
            client.download(node, dest_path=str(tmp_dir))
            downloaded = tmp_dir / name
            if not downloaded.exists():
                raise StorageError("MEGA download produced no file")
            downloaded.replace(local_path)

        await asyncio.to_thread(_run)

    async def list_files(self, *, prefix: str = "") -> list[str]:
        def _run() -> list[str]:
            client = self._get_client()
            root = client.find(self._root)
            if not root:
                return []
            nodes = client.get_files()
            keys: list[str] = []
            for _k, node in nodes.items():
                if node.get("p") != root:
                    continue
                name = node.get("a", {}).get("n", "")
                key = filename_to_safe_key(name)
                if key.startswith(prefix):
                    keys.append(key)
            keys.sort()
            return keys

        return await asyncio.to_thread(_run)
