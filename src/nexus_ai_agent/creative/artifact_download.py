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

Descriptor lifetime is leak-proof by construction: the authorization hands
the verified descriptor to :class:`AuthorizedDownload`, which owns it through
an :class:`_FdLease` closed exactly once — explicitly (``close()``/context
manager), at stream exhaustion, or by ``weakref.finalize`` when an authorized
download or its stream is dropped without ever being iterated. A generator
body never runs before its first ``next()``, so the streaming helper is a
plain function returning a custom iterator, never a ``yield`` in disguise.
"""

from __future__ import annotations

import os
import re
import stat
import weakref
from collections.abc import Mapping
from dataclasses import dataclass, field
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


def _close_fd_box(box: list[int | None]) -> None:
    """Close the descriptor parked in ``box`` exactly once (finalize target)."""
    fd = box[0]
    box[0] = None
    if fd is not None:
        try:
            os.close(fd)
        except OSError:  # pragma: no cover - an already-closed fd stays closed
            pass


class _FdLease:
    """Atomic ownership of one open descriptor; closed exactly once.

    ``close()`` is idempotent and race-free: the descriptor number is lifted
    out of the box before :func:`os.close` runs, so no fd can ever be closed
    twice. A ``weakref.finalize`` guard closes the descriptor even when the
    owner is simply dropped on the floor without any explicit ``close()``.
    """

    __slots__ = ("_box", "_finalizer", "__weakref__")

    def __init__(self, fd: int) -> None:
        self._box: list[int | None] = [fd]
        self._finalizer = weakref.finalize(self, _close_fd_box, self._box)

    @property
    def fd(self) -> int | None:
        """The owned descriptor, or ``None`` once it has been closed."""
        return self._box[0]

    def close(self) -> None:
        """Close the descriptor (idempotent; safe to call from anywhere)."""
        self._finalizer()


@dataclass(frozen=True)
class AuthorizedDownload:
    path: Path
    size_bytes: int
    sha256: str
    #: The lease of the verified, rewound descriptor the HTTP stream MUST
    #: read from: the exact inode whose bytes were hashed during
    #: authorization. ``None`` only for hand-built objects (tests); the
    #: streaming helper then — and only then — falls back to a single
    #: ``O_NOFOLLOW`` open of ``path``.
    _lease: _FdLease | None = field(default=None, compare=False, repr=False)

    @property
    def fd(self) -> int | None:
        """The verified descriptor (``None`` once closed, or never opened)."""
        return None if self._lease is None else self._lease.fd

    def close(self) -> None:
        """Release the verified descriptor (idempotent)."""
        if self._lease is not None:
            self._lease.close()

    def __enter__(self) -> AuthorizedDownload:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _open_readonly_no_follow(path: Path) -> int:
    """Open a regular file once, never following a final symlink."""
    return os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))


class _AuthorizedChunkIterator:
    """The byte stream of one verified descriptor, closed exactly once.

    Reading stops (and the descriptor closes) at EOF or on any read error;
    dropping the iterator without ever iterating it also closes the
    descriptor, via the lease's ``weakref.finalize`` guard. This is a plain
    iterator class — NOT a generator — precisely so that ownership is taken
    at construction time, before the first ``next()``.
    """

    def __init__(self, lease: _FdLease) -> None:
        self._lease = lease
        self._finalizer = weakref.finalize(self, lease.close)

    def __iter__(self) -> _AuthorizedChunkIterator:
        return self

    def __next__(self) -> bytes:
        fd = self._lease.fd
        if fd is None:
            raise StopIteration
        try:
            chunk = os.read(fd, STREAM_CHUNK_BYTES)
        except OSError:
            self.close()
            raise
        if not chunk:
            self.close()
            raise StopIteration
        return chunk

    def close(self) -> None:
        """Release the descriptor (idempotent)."""
        self._lease.close()

    def __enter__(self) -> _AuthorizedChunkIterator:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


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

    # Open the artifact EXACTLY ONCE. Every check below — regular file, inode
    # continuity against the stat above, and the SHA-256 inside the passport
    # verification — runs against this same descriptor, and the HTTP stream is
    # served FROM it: the bytes that were verified are the bytes that are
    # streamed (BytesVerified == BytesStreamed). The path is never opened a
    # second time, so no symlink swap or in-place edit can slip between the
    # verification and the response body.
    try:
        mp4_fd = _open_readonly_no_follow(resolved)
    except OSError:
        raise DownloadRefused(404, "artifact_not_found") from None
    try:
        opened = os.fstat(mp4_fd)
        if not stat.S_ISREG(opened.st_mode):
            raise DownloadRefused(404, "artifact_not_found")
        if (opened.st_ino, opened.st_dev) != (st.st_ino, st.st_dev):
            # The path resolved to a different inode between the stat and the
            # single open: treat it as the artifact having changed under us.
            raise DownloadRefused(409, "artifact_changed")
        try:
            check = verify_artifact_passport(passport_path, resolved, signing_key, mp4_fd=mp4_fd)
        except PassportError as exc:
            raise DownloadRefused(409, f"passport_{exc.reason}") from exc
        if check.passport.get("output_rel_path") != str(resolved.relative_to(root)):
            raise DownloadRefused(409, "passport_for_other_artifact")
        if int(check.passport["output_size_bytes"]) != opened.st_size:
            raise DownloadRefused(409, "artifact_changed")
        # Rewind the VERIFIED descriptor so the stream starts at byte zero of
        # exactly the inode that was hashed.
        os.lseek(mp4_fd, 0, os.SEEK_SET)
        return AuthorizedDownload(
            path=resolved,
            size_bytes=int(check.passport["output_size_bytes"]),
            sha256=str(check.passport["output_sha256"]),
            _lease=_FdLease(mp4_fd),
        )
    except BaseException:
        os.close(mp4_fd)
        raise


#: Streaming chunk size: the response never holds more than this in memory.
STREAM_CHUNK_BYTES = 64 * 1024


def artifact_rel_path_for_job(row: Mapping[str, object] | None, project_id: str) -> str:
    """Return the MP4 path a job produced, for the authorized project only.

    A job that is missing, belongs to another project or has no artifact gets
    the same ``404`` so the response never reveals another project's job. A
    job that exists in this project but has not produced an artifact yet is a
    ``409``: the state is real, the bytes are not available.
    """
    if row is None:
        raise DownloadRefused(404, "job_not_found")
    input_data = row.get("input_data")
    if not isinstance(input_data, Mapping) or input_data.get("project_id") != project_id:
        raise DownloadRefused(404, "job_not_found")
    result = row.get("result")
    rel = result.get("output_rel_path") if isinstance(result, Mapping) else None
    if not isinstance(rel, str) or not rel:
        raise DownloadRefused(409, "artifact_not_ready")
    return rel


def iter_authorized_chunks(
    source: AuthorizedDownload | Path | int,
) -> _AuthorizedChunkIterator:
    """Return the byte stream of an already-authorized file.

    This is a plain function — deliberately NOT a generator: a generator body
    (and its ``finally``) never runs before the first ``next()``, so a stream
    that is built and then dropped uniterated would leak its descriptor.
    Ownership of the descriptor is taken AT CALL TIME inside the returned
    :class:`_AuthorizedChunkIterator`, whose lease closes it at EOF, on any
    read error, on ``close()`` — or via ``weakref.finalize`` when the stream
    is dropped without ever being iterated.

    ``source`` is the :class:`AuthorizedDownload` itself (preferred): the
    chunks come from the exact descriptor that was verified — the same inode,
    the same bytes, never a second path-based open. An already-open descriptor
    (``int``) is streamed the same way; a bare path is opened once with
    ``O_NOFOLLOW`` for legacy callers.
    """
    if isinstance(source, AuthorizedDownload):
        lease = (
            source._lease
            if source._lease is not None
            else _FdLease(_open_readonly_no_follow(source.path))
        )
    elif isinstance(source, Path):
        lease = _FdLease(_open_readonly_no_follow(source))
    else:
        lease = _FdLease(source)
    return _AuthorizedChunkIterator(lease)


__all__ = [
    "STREAM_CHUNK_BYTES",
    "AuthorizedDownload",
    "DownloadRefused",
    "artifact_rel_path_for_job",
    "authorize_artifact_download",
    "iter_authorized_chunks",
]
