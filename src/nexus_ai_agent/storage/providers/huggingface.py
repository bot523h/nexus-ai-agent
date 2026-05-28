from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from .base import ProviderUnavailable, StorageError


class HuggingFaceProvider:
    """
    Read-only provider for fetching model files from Hugging Face.

    Keys use this convention:
      hf://<repo_id>@<revision>/<filename>
    Example:
      hf://TheBloke/Mistral-7B-Instruct-v0.2-GGUF@main/mistral-7b-instruct-v0.2.Q4_K_M.gguf
    """

    name = "huggingface"

    def __init__(self, *, token: str | None = None):
        self._token = token

    def is_configured(self) -> bool:
        # Works for public models even without a token.
        return True

    def _headers(self) -> dict[str, str]:
        if not self._token:
            return {}
        return {"Authorization": f"Bearer {self._token}"}

    def _parse_key(self, remote_key: str) -> tuple[str, str, str]:
        if not remote_key.startswith("hf://"):
            raise ProviderUnavailable("Not a HuggingFace key")
        rest = remote_key.removeprefix("hf://")
        repo_and_rev, _, filename = rest.partition("/")
        repo, _, rev = repo_and_rev.partition("@")
        rev = rev or "main"
        if not repo or not filename:
            raise ProviderUnavailable("Invalid HuggingFace key")
        return repo, rev, filename

    async def upload(self, *, local_path: Path, remote_key: str) -> None:
        _ = local_path
        _ = remote_key
        raise ProviderUnavailable("HuggingFace provider is read-only in this runtime")

    async def download(self, *, remote_key: str, local_path: Path) -> None:
        repo, rev, filename = self._parse_key(remote_key)
        url = f"https://huggingface.co/{repo}/resolve/{rev}/{filename}"
        async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as client:
            r = await client.get(url, headers=self._headers())
            if r.status_code == 404:
                raise ProviderUnavailable("Model not found on Hugging Face")
            if r.status_code != 200:
                raise StorageError(f"HuggingFace download failed: {r.status_code}")
            local_path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(local_path.write_bytes, r.content)

    async def list_files(self, *, prefix: str = "") -> list[str]:
        _ = prefix
        return []

