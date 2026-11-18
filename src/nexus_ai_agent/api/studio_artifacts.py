"""Read-only bridge from Studio delivery to the artifact authorization gate."""

from __future__ import annotations

import hashlib
import hmac
import os
import stat
import tempfile
from pathlib import Path
from typing import BinaryIO

from nexus_ai_agent.creative.artifact_download import DownloadRefused, authorize_artifact_download


def verified_download_snapshot(
    root: Path, relative_path: str, key: str
) -> tuple[BinaryIO, str, int]:
    """Verify then snapshot the exact bytes to serve, rejecting post-check swaps.

    The private temporary file is completely hashed before any response bytes are
    sent. The returned open file is owned and closed by the response iterator.
    """
    authorized = authorize_artifact_download(root, relative_path, key)
    snapshot = tempfile.TemporaryFile(mode="w+b")
    try:
        digest = hashlib.sha256()
        size = 0
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(authorized.path, flags)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise DownloadRefused(409, "artifact_changed")
            while chunk := source.read(65_536):
                size += len(chunk)
                if size > authorized.size_bytes:
                    raise DownloadRefused(409, "artifact_changed")
                digest.update(chunk)
                snapshot.write(chunk)
        if size != authorized.size_bytes or not hmac.compare_digest(
            digest.hexdigest(), authorized.sha256
        ):
            raise DownloadRefused(409, "artifact_changed")
        snapshot.seek(0)
        return snapshot, authorized.sha256, size
    except BaseException:
        snapshot.close()
        raise
