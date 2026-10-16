from __future__ import annotations

import asyncio
import re
from pathlib import Path

import httpx

from .base import ProviderUnavailable, StorageError

#: One path segment of a repository id or a revision: no ``/``, no ``@``, no
#: relative components — the key is interpolated into a URL, so it is validated
#: before it can reach the network.
_SAFE_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}")
#: A file path inside the repository: segments separated by ``/``.
_SAFE_PATH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}")


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
        """``hf://<owner>/<name>[@<revision>]/<path>`` → ``(repo_id, revision, filename)``.

        The repository id contains a ``/``, so only the **last** ``@`` can
        introduce the revision: splitting at the first ``/`` (the pre-audit
        behaviour) turned the module's own documented example
        ``hf://TheBloke/Mistral-7B-Instruct-v0.2-GGUF@main/mistral-7b-….gguf``
        into the 404 URL ``…/TheBloke/resolve/main/Mistral-7B-…@main/…`` — the
        provider could not download any model it advertised.  Audit
        2026-10-16, defect P1-1.
        """
        if not remote_key.startswith("hf://"):
            raise ProviderUnavailable("Not a HuggingFace key")
        rest = remote_key.removeprefix("hf://")
        if "@" in rest:
            repo, _, tail = rest.rpartition("@")
            rev, _, filename = tail.partition("/")
            if not rev:
                raise ProviderUnavailable("Invalid HuggingFace key: empty revision")
        else:
            # No revision marker: the repository id is always the first two
            # segments (``<owner>/<name>``) and the rest is the file path.
            head, _, remainder = rest.partition("/")
            name, _, filename = remainder.partition("/")
            repo, rev = f"{head}/{name}", "main"
        if not repo or not filename:
            raise ProviderUnavailable("Invalid HuggingFace key")
        if repo.count("/") != 1 or len(repo.split("/")) != 2:
            raise ProviderUnavailable("Invalid HuggingFace key: repo id must be <owner>/<name>")
        if not _SAFE_PART.fullmatch(rev):
            raise ProviderUnavailable("Invalid HuggingFace key: unsafe revision")
        owner, name = repo.split("/")
        if not (_SAFE_PART.fullmatch(owner) and _SAFE_PART.fullmatch(name)):
            raise ProviderUnavailable("Invalid HuggingFace key: unsafe repo id")
        if not _SAFE_PATH.fullmatch(filename) or ".." in filename.split("/"):
            raise ProviderUnavailable("Invalid HuggingFace key: unsafe filename")
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
