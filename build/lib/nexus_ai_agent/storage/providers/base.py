from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class StorageError(RuntimeError):
    pass


class ProviderUnavailable(StorageError):
    pass


class StorageProvider(Protocol):
    name: str

    def is_configured(self) -> bool: ...

    async def upload(self, *, local_path: Path, remote_key: str) -> None: ...

    async def download(self, *, remote_key: str, local_path: Path) -> None: ...

    async def list_files(self, *, prefix: str = "") -> list[str]: ...


def safe_key_to_filename(remote_key: str) -> str:
    """
    Remote keys may contain "/" (prefixing). For providers that require a single filename
    (e.g., GitHub Release assets), encode safely and reversibly.
    """
    return remote_key.replace("/", "__")


def filename_to_safe_key(filename: str) -> str:
    return filename.replace("__", "/")


@dataclass(frozen=True)
class ProviderConfig:
    github_token: str | None = None
    github_repo: str | None = None  # "owner/repo"
    mega_email: str | None = None
    mega_password: str | None = None
    huggingface_token: str | None = None
    rclone_remote: str | None = None  # e.g. "gdrive:nexus-ai-agent"
    gdrive_bearer_token: str | None = None
