from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

from .base import ProviderUnavailable, StorageError

# rclone can echo the remote's configured credentials back in error output
# (for example an ``http://user:pass@host`` webdav/s3 endpoint).  Diagnostics are
# surfaced to operators and logs, so redact anything that looks like an inline
# credential and cap the length before it is ever stored or printed.
_CREDENTIAL_RE = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)[^/@\s]*@")
_DIAGNOSTIC_LIMIT = 200


def _bounded_diagnostics(raw: bytes) -> str:
    """Decode, redact inline credentials, and bound provider output for logging."""
    text = raw.decode("utf-8", "ignore")
    text = _CREDENTIAL_RE.sub(r"\g<scheme><redacted>@", text)
    return text[:_DIAGNOSTIC_LIMIT]


class RcloneProvider:
    """
    Optional provider that can target Google Drive (or any rclone-supported storage) using a
    pre-configured rclone remote.

    Requires:
      - `rclone` binary in PATH
      - `rclone config` already set up in the runtime environment
      - NEXUS_RCLONE_REMOTE (e.g., "gdrive:nexus-ai-agent")
    """

    name = "rclone"

    def __init__(self, *, remote: str):
        self._remote = remote.rstrip("/")

    def is_configured(self) -> bool:
        return bool(self._remote) and shutil.which("rclone") is not None

    async def upload(self, *, local_path: Path, remote_key: str) -> None:
        if not self.is_configured():
            raise ProviderUnavailable("rclone is not configured")
        remote_path = f"{self._remote}/{remote_key}"
        proc = await asyncio.create_subprocess_exec(
            "rclone",
            "copyto",
            str(local_path),
            remote_path,
            "--retries",
            "3",
            "--low-level-retries",
            "3",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise StorageError(
                f"rclone upload failed: {(out + err).decode('utf-8', 'ignore')[:200]}"
            )

    async def download(self, *, remote_key: str, local_path: Path) -> None:
        if not self.is_configured():
            raise ProviderUnavailable("rclone is not configured")
        remote_path = f"{self._remote}/{remote_key}"
        local_path.parent.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            "rclone",
            "copyto",
            remote_path,
            str(local_path),
            "--retries",
            "3",
            "--low-level-retries",
            "3",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise ProviderUnavailable(
                f"rclone download failed: {(out + err).decode('utf-8', 'ignore')[:200]}"
            )

    async def list_files(self, *, prefix: str = "") -> list[str]:
        if not self.is_configured():
            # An unconfigured provider is a caller error, not an empty remote:
            # returning [] here would let callers treat "no credentials / no
            # binary" as "the remote has no files" (REL-006).
            raise ProviderUnavailable("rclone is not configured")
        proc = await asyncio.create_subprocess_exec(
            "rclone",
            "lsf",
            f"{self._remote}/{prefix}",
            "--recursive",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            # A failed listing must not masquerade as a valid empty listing: the
            # housekeeping/rotation callers prune backups from this result, so a
            # silent [] would look like "nothing to keep" and risk deleting the
            # wrong set.  Raise a typed error with bounded, redacted diagnostics.
            raise StorageError(
                f"rclone list failed (exit {proc.returncode}): {_bounded_diagnostics(out + err)}"
            )
        files = [
            line.strip() for line in out.decode("utf-8", "ignore").splitlines() if line.strip()
        ]
        return [f"{prefix.rstrip('/')}/{f}".lstrip("/") for f in files]
