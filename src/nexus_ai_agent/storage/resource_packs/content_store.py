"""Local content-addressed store (CAS) shared by every pack and every version.

Layout under ``root``::

    objects/ab/ab12…ef   immutable blobs, named by their SHA-256
    parts/ab12…ef.part   resumable, not-yet-verified downloads
    tmp/                 scratch files for hydration (same filesystem → atomic)

An object only ever enters ``objects/`` through :meth:`LocalContentStore.adopt_file`,
which hashes the candidate first and moves it with :func:`os.replace` — a
reader can therefore never observe a half-written or unverified object.

Nothing is created on disk until the first write: the disk guard of the sync
manager relies on "refuse before writing a single byte".
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
from pathlib import Path

from .errors import IntegrityError
from .pack_manifest import sha256_file

__all__ = ["LocalContentStore", "fsync_directory"]

_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def fsync_directory(path: Path) -> None:
    """Persist a rename on POSIX; a no-op where directories cannot be opened."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:  # pragma: no cover - Windows cannot open a directory handle
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _check_sha(sha256: str) -> str:
    if not _SHA256_RE.fullmatch(sha256):
        raise ValueError(f"not a sha256 hex digest: {sha256!r}")
    return sha256


class LocalContentStore:
    """Content-addressed blob store with verify-before-publish semantics."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.objects_dir = self.root / "objects"
        self.parts_dir = self.root / "parts"
        self.tmp_dir = self.root / "tmp"

    # -- addressing ---------------------------------------------------------

    def object_path(self, sha256: str) -> Path:
        _check_sha(sha256)
        return self.objects_dir / sha256[:2] / sha256

    def part_path(self, sha256: str) -> Path:
        return self.parts_dir / f"{_check_sha(sha256)}.part"

    def new_temp_path(self, sha256: str) -> Path:
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        return self.tmp_dir / f"{_check_sha(sha256)}.{secrets.token_hex(6)}.tmp"

    # -- queries ------------------------------------------------------------

    def index(self) -> dict[str, int]:
        """``sha256 -> size`` of every well-formed object (read-only, creates nothing)."""
        found: dict[str, int] = {}
        if not self.objects_dir.is_dir():
            return found
        for bucket in sorted(self.objects_dir.iterdir()):
            if not bucket.is_dir():
                continue
            for item in sorted(bucket.iterdir()):
                name = item.name
                if _SHA256_RE.fullmatch(name) and name[:2] == bucket.name and item.is_file():
                    found[name] = item.stat().st_size
        return found

    def has(self, sha256: str) -> bool:
        return self.object_path(sha256).is_file()

    def verify_object(self, sha256: str, size_bytes: int) -> bool:
        path = self.object_path(sha256)
        if not path.is_file() or path.stat().st_size != size_bytes:
            return False
        return sha256_file(path)[0] == sha256

    # -- writes ---------------------------------------------------------------

    def adopt_file(self, candidate: Path, sha256: str, size_bytes: int) -> Path:
        """Verify *candidate* and move it atomically into ``objects/``.

        On mismatch the candidate is deleted and :class:`IntegrityError` raised:
        corrupt bytes never become addressable.
        """
        target = self.object_path(sha256)
        digest, size = sha256_file(candidate)
        if size != size_bytes or digest != sha256:
            candidate.unlink(missing_ok=True)
            raise IntegrityError(
                f"rejected blob: expected sha256={sha256} size={size_bytes}, "
                f"got sha256={digest} size={size}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(candidate, target)
        fsync_directory(target.parent)
        return target

    def put_bytes(self, data: bytes, sha256: str) -> Path:
        """Write *data* through a temp file and adopt it (hash-checked)."""
        temp = self.new_temp_path(sha256)
        with temp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        return self.adopt_file(temp, sha256, len(data))

    def evict(self, sha256: str) -> None:
        self.object_path(sha256).unlink(missing_ok=True)

    def materialize(self, sha256: str, destination: Path, *, prefer_hardlink: bool = True) -> None:
        """Place object *sha256* at *destination* (hard link, else byte copy)."""
        source = self.object_path(sha256)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if prefer_hardlink:
            try:
                os.link(source, destination)
                return
            except OSError:
                pass  # cross-device, FAT/exFAT, sandboxed storage: fall back to a copy
        shutil.copyfile(source, destination)

    def collect_garbage(self, keep: set[str]) -> tuple[str, ...]:
        """Delete every object not in *keep*; returns the evicted digests."""
        removed = tuple(sorted(sha for sha in self.index() if sha not in keep))
        for sha in removed:
            self.evict(sha)
        return removed
