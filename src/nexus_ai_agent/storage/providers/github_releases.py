from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from .base import ProviderUnavailable, StorageError, filename_to_safe_key, safe_key_to_filename


class GitHubReleasesProvider:
    name = "github_releases"
    _TAG = "nexus-storage"

    def __init__(self, *, token: str, repo: str):
        self._token = token
        self._repo = repo
        self._api = f"https://api.github.com/repos/{repo}"
        self._uploads = f"https://uploads.github.com/repos/{repo}"

    def is_configured(self) -> bool:
        return bool(self._token and self._repo)

    def _headers(self) -> dict[str, str]:
        # Never log this.
        return {
            "Authorization": f"token {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _get_or_create_release(self, client: httpx.AsyncClient) -> dict:
        r = await client.get(f"{self._api}/releases/tags/{self._TAG}", headers=self._headers())
        if r.status_code == 200:
            return r.json()
        if r.status_code not in (404,):
            raise StorageError(f"GitHub release lookup failed: {r.status_code}")

        payload = {
            "tag_name": self._TAG,
            "name": "NEXUS Storage",
            "draft": False,
            "prerelease": False,
        }
        r = await client.post(f"{self._api}/releases", headers=self._headers(), json=payload)
        if r.status_code not in (200, 201):
            raise StorageError(f"GitHub release create failed: {r.status_code}")
        return r.json()

    async def _find_asset(
        self,
        client: httpx.AsyncClient,
        *,
        release: dict,
        asset_name: str,
    ) -> dict | None:
        assets_url = release.get("assets_url")
        r = await client.get(assets_url, headers=self._headers())
        if r.status_code != 200:
            raise StorageError(f"GitHub assets list failed: {r.status_code}")
        for a in r.json():
            if a.get("name") == asset_name:
                return a
        return None

    async def upload(self, *, local_path: Path, remote_key: str) -> None:
        asset_name = safe_key_to_filename(remote_key)
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            release = await self._get_or_create_release(client)
            existing = await self._find_asset(client, release=release, asset_name=asset_name)
            if existing:
                # Delete to overwrite.
                del_url = existing.get("url")
                _ = await client.delete(del_url, headers=self._headers())

            upload_url = f"{self._uploads}/releases/{release['id']}/assets"
            params = {"name": asset_name}
            data = local_path.read_bytes()
            r = await client.post(
                upload_url,
                headers={**self._headers(), "Content-Type": "application/octet-stream"},
                params=params,
                content=data,
            )
            if r.status_code not in (200, 201):
                raise StorageError(f"GitHub upload failed: {r.status_code} {r.text[:200]}")

    async def download(self, *, remote_key: str, local_path: Path) -> None:
        asset_name = safe_key_to_filename(remote_key)
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            release = await self._get_or_create_release(client)
            asset = await self._find_asset(client, release=release, asset_name=asset_name)
            if not asset:
                raise ProviderUnavailable(f"GitHub asset not found: {remote_key}")

            url = asset.get("url")
            r = await client.get(
                url,
                headers={**self._headers(), "Accept": "application/octet-stream"},
            )
            if r.status_code != 200:
                raise StorageError(f"GitHub download failed: {r.status_code}")

            local_path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(local_path.write_bytes, r.content)

    async def list_files(self, *, prefix: str = "") -> list[str]:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            release = await self._get_or_create_release(client)
            r = await client.get(release.get("assets_url"), headers=self._headers())
            if r.status_code != 200:
                raise StorageError(f"GitHub assets list failed: {r.status_code}")
            keys: list[str] = []
            for a in r.json():
                name = a.get("name", "")
                key = filename_to_safe_key(name)
                if key.startswith(prefix):
                    keys.append(key)
            keys.sort()
            return keys
