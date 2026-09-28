from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from .base import ProviderUnavailable, StorageError


class RcloneProvider:
    """
    Optional provider that can target Google Drive (or any rclone-supported storage) using a
    pre-configured rclone remote.

    Requires:
      - `rclone` binary in PATH
      - `rclone config` already set up in the runtime environment
      - NEXUS_RCLONE_REMOTE (e.g., "gdrive:nexus-ai-agent")

    W1 fix: child processes are now cancellation-safe — on CancelledError we
    kill the child and reap it to avoid zombie leak.
    """

    name = "rclone"

    def __init__(self, *, remote: str):
        self._remote = remote.rstrip("/")

    def is_configured(self) -> bool:
        return bool(self._remote) and shutil.which("rclone") is not None

    async def _run_rclone(self, *args: str) -> tuple[bytes, bytes, int]:
        """Run rclone with cancellation-safe child reaping (W1 Law 7)."""
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await proc.communicate()
        except asyncio.CancelledError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await proc.communicate()
            except Exception:
                pass
            raise
        return out, err, proc.returncode or 0

    async def upload(self, *, local_path: Path, remote_key: str) -> None:
        if not self.is_configured():
            raise ProviderUnavailable("rclone is not configured")
        remote_path = f"{self._remote}/{remote_key}"
        out, err, code = await self._run_rclone(
            "rclone",
            "copyto",
            str(local_path),
            remote_path,
            "--retries",
            "3",
            "--low-level-retries",
            "3",
        )
        if code != 0:
            raise StorageError(
                f"rclone upload failed: {(out + err).decode('utf-8', 'ignore')[:200]}"
            )

    async def download(self, *, remote_key: str, local_path: Path) -> None:
        if not self.is_configured():
            raise ProviderUnavailable("rclone is not configured")
        remote_path = f"{self._remote}/{remote_key}"
        local_path.parent.mkdir(parents=True, exist_ok=True)
        out, err, code = await self._run_rclone(
            "rclone",
            "copyto",
            remote_path,
            str(local_path),
            "--retries",
            "3",
            "--low-level-retries",
            "3",
        )
        if code != 0:
            raise ProviderUnavailable(
                f"rclone download failed: {(out + err).decode('utf-8', 'ignore')[:200]}"
            )

    async def list_files(self, *, prefix: str = "") -> list[str]:
        if not self.is_configured():
            return []
        out, _err, code = await self._run_rclone(
            "rclone",
            "lsf",
            f"{self._remote}/{prefix}",
            "--recursive",
        )
        if code != 0:
            return []
        files = [
            line.strip() for line in out.decode("utf-8", "ignore").splitlines() if line.strip()
        ]
        return [f"{prefix.rstrip('/')}/{f}".lstrip("/") for f in files]
