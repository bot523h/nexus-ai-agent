"""Authorize a studio artifact download: passport first, bytes second.

The gate returns a verified path, or raises :class:`DownloadRefused` with an
HTTP status that the route can return as-is:

* ``422`` — the requested path is not a well-formed artifact path.
* ``403`` — traversal, a symlink on the path, or a path outside the workspace.
* ``404`` — no artifact at that path.
* ``409`` — the artifact exists but its passport is missing, invalid, not
  ``PASS``, sealed for a different file, or does not match the bytes on disk.

Nothing is read or streamed until every check has passed. The passport check
re-hashes the MP4 on disk, so a byte changed after the render is refused.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from nexus_ai_agent.creative.artifact_passport import (
    PASSPORT_SUFFIX,
    PassportError,
    verify_artifact_passport,
)
from nexus_ai_agent.creative.materializer import MaterializationError, refuse_symlink_chain

_ARTIFACT_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,255}\.mp4$")
MAX_REL_PATH_CHARS = 256


class DownloadRefused(Exception):
    """A typed refusal with the HTTP status the route should answer with."""

    def __init__(self, status_code: int, reason: str) -> None:
        super().__init__(f"{status_code} {reason}")
        self.status_code = status_code
        self.reason = reason


@dataclass(frozen=True)
class AuthorizedDownload:
    path: Path
    size_bytes: int
    sha256: str


def _syntactically_valid(rel_path: object) -> str:
    if not isinstance(rel_path, str) or len(rel_path) > MAX_REL_PATH_CHARS:
        raise DownloadRefused(422, "malformed_path")
    if rel_path.startswith("/") or "\\" in rel_path or "\x00" in rel_path:
        raise DownloadRefused(422, "malformed_path")
    if not _ARTIFACT_PATH.fullmatch(rel_path):
        raise DownloadRefused(422, "malformed_path")
    return rel_path


def authorize_artifact_download(
    workspace_root: Path,
    rel_path: str,
    signing_key: str | bytes,
) -> AuthorizedDownload:
    """Return the verified artifact, or raise :class:`DownloadRefused`."""
    rel = _syntactically_valid(rel_path)
    if ".." in rel.split("/"):
        raise DownloadRefused(403, "path_traversal")
    root = Path(workspace_root).resolve()
    joined = root / rel
    # ``rel`` has already been proven relative, separator-free and free of ``..``,
    # so ``root / rel`` cannot escape ``root``: this guard is defence in depth
    # against a future edit of ``_syntactically_valid`` and is unreachable today.
    if not joined.is_relative_to(root):  # pragma: no cover - structurally unreachable
        raise DownloadRefused(403, "path_outside_workspace")
    try:
        refuse_symlink_chain(root, joined)
    except MaterializationError as exc:
        raise DownloadRefused(403, exc.reason) from exc
    resolved = joined.resolve()
    if not resolved.is_relative_to(root):
        raise DownloadRefused(403, "path_outside_workspace")

    try:
        st = os.lstat(resolved)
    except FileNotFoundError:
        raise DownloadRefused(404, "artifact_not_found") from None
    # ``refuse_symlink_chain`` already refused every symlink component (including
    # the artifact itself), so ``resolved`` can only be a regular file, a directory
    # or a special file here; anything but a regular file is reported as missing.
    if not stat.S_ISREG(st.st_mode):
        raise DownloadRefused(404, "artifact_not_found")

    passport_path = resolved.parent / f"{resolved.name[: -len('.mp4')]}{PASSPORT_SUFFIX}"
    if not passport_path.exists():
        raise DownloadRefused(409, "passport_missing")
    try:
        check = verify_artifact_passport(passport_path, resolved, signing_key)
    except PassportError as exc:
        raise DownloadRefused(409, f"passport_{exc.reason}") from exc
    if check.passport.get("output_rel_path") != str(resolved.relative_to(root)):
        raise DownloadRefused(409, "passport_for_other_artifact")
    return AuthorizedDownload(
        path=resolved,
        size_bytes=int(check.passport["output_size_bytes"]),
        sha256=str(check.passport["output_sha256"]),
    )


__all__ = ["AuthorizedDownload", "DownloadRefused", "authorize_artifact_download"]
