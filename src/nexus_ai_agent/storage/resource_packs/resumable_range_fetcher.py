"""PUBG-style resumable blob downloader over HTTP ``Range`` requests.

Contract
--------
* A transfer writes into ``parts/<sha256>.part``.  If it is cut (dropped
  connection, short body, process killed), the ``.part`` file stays; the next
  attempt reads its size and asks for ``Range: bytes=<size>-``.
* ``206 Partial Content`` → append (the ``Content-Range`` start must equal the
  offset we asked for, otherwise the reply is refused); ``200 OK`` → the
  server ignored the range, so the ``.part`` file is truncated and rewritten
  from byte zero.
* The writer never accepts more bytes than the manifest's ``size_bytes``: a
  hostile or broken server cannot fill the disk.
* When complete, the whole file is hashed and only then moved atomically into
  the CAS; a digest mismatch deletes the ``.part`` file and raises
  :class:`IntegrityError` (fail closed — no retry loop on corrupt content).

Sources are injectable: :class:`LocalDirectoryPackSource` (tests, offline
mirrors, side-loaded SD cards) and :class:`HttpRangePackSource` (GitHub
Releases, a CDN or any object store with public HTTP access), built only on
:mod:`urllib.request`.
"""

from __future__ import annotations

import http.client
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Protocol

from .content_store import LocalContentStore
from .errors import IntegrityError, RangeFetchError, RangeFetchInterrupted
from .pack_manifest import ResourceBlobEntry

__all__ = [
    "FetchOutcome",
    "HttpRangePackSource",
    "LocalDirectoryPackSource",
    "PackSource",
    "RangeReply",
    "ResumableRangeFetcher",
    "default_blob_key",
]

_CONTENT_RANGE_RE = re.compile(r"bytes (\d+)-(\d+)/(\d+|\*)")
_USER_AGENT = "nexus-resource-packs/1"


class _Readable(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...

    def close(self) -> None: ...


@dataclass
class RangeReply:
    """One open response of a :class:`PackSource`; use it as a context manager."""

    status: int
    start_offset: int
    total_size: int | None
    body: _Readable

    def read(self, size: int) -> bytes:
        return self.body.read(size)

    def __enter__(self) -> RangeReply:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.body.close()


class PackSource(Protocol):
    """Where remote blobs come from."""

    def open_range(self, key: str, offset: int) -> RangeReply: ...


def default_blob_key(entry: ResourceBlobEntry) -> str:
    """Content-addressed remote layout: ``blobs/ab/ab12…``."""
    return f"blobs/{entry.sha256[:2]}/{entry.sha256}"


class LocalDirectoryPackSource:
    """Serve blobs from a local directory (offline mirror / tests).

    ``honor_ranges=False`` emulates a server that ignores ``Range`` and always
    answers ``200`` from byte zero.
    """

    def __init__(self, root: Path, *, honor_ranges: bool = True) -> None:
        self.root = Path(root).resolve()
        self.honor_ranges = honor_ranges
        self.requested_offsets: list[tuple[str, int]] = []

    def open_range(self, key: str, offset: int) -> RangeReply:
        self.requested_offsets.append((key, offset))
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise RangeFetchError(f"key escapes the source root: {key!r}", status=400)
        if not path.is_file():
            raise RangeFetchError(f"blob not found: {key!r}", status=404)
        size = path.stat().st_size
        if offset > size:
            raise RangeFetchError(f"offset {offset} beyond size {size}", status=416)
        handle: BinaryIO = path.open("rb")
        if self.honor_ranges and offset:
            handle.seek(offset)
            return RangeReply(206, offset, size, handle)
        return RangeReply(200, 0, size, handle)


class HttpRangePackSource:
    """HTTP(S) source with ``Range`` support, on :mod:`urllib.request` only.

    Only ``http``/``https`` base URLs are accepted (``file:`` and friends
    would turn a manifest into a local-file read primitive).
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        headers: Mapping[str, str] | None = None,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"base_url must be an absolute http(s) URL: {base_url!r}")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = dict(headers or {})
        self._opener = opener or urllib.request.build_opener()

    def url_for(self, key: str) -> str:
        return f"{self.base_url}/{urllib.parse.quote(key.lstrip('/'), safe='/')}"

    def open_range(self, key: str, offset: int) -> RangeReply:
        headers = {"User-Agent": _USER_AGENT, "Accept-Encoding": "identity", **self.headers}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        request = urllib.request.Request(self.url_for(key), headers=headers, method="GET")
        try:
            response = self._opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            error.close()
            raise RangeFetchError(f"HTTP {error.code} for {key!r}", status=error.code) from error
        except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
            raise RangeFetchInterrupted(
                f"transport failure for {key!r}: {error}", bytes_on_disk=offset
            ) from error
        status = int(response.status)
        if status == 206:
            match = _CONTENT_RANGE_RE.fullmatch(response.headers.get("Content-Range", "").strip())
            if match is None or int(match.group(1)) != offset:
                response.close()
                raise RangeFetchError(
                    f"206 reply with a Content-Range that does not start at {offset}", status=206
                )
            total = None if match.group(3) == "*" else int(match.group(3))
            return RangeReply(206, offset, total, response)
        if status == 200:
            length = response.headers.get("Content-Length")
            return RangeReply(200, 0, int(length) if length else None, response)
        response.close()
        raise RangeFetchError(f"unexpected HTTP status {status} for {key!r}", status=status)


@dataclass(frozen=True)
class FetchOutcome:
    sha256: str
    path: Path
    bytes_transferred: int
    resumed_from: int
    attempts: int
    already_present: bool


class ResumableRangeFetcher:
    """Resumable, size-capped, hash-verified blob downloads into a CAS."""

    def __init__(
        self,
        source: PackSource,
        store: LocalContentStore,
        *,
        chunk_size: int = 256 * 1024,
        max_attempts: int = 3,
        key_for: Callable[[ResourceBlobEntry], str] = default_blob_key,
    ) -> None:
        if chunk_size <= 0 or max_attempts <= 0:
            raise ValueError("chunk_size and max_attempts must be positive")
        self.source = source
        self.store = store
        self.chunk_size = chunk_size
        self.max_attempts = max_attempts
        self.key_for = key_for

    def fetch(
        self, entry: ResourceBlobEntry, on_bytes: Callable[[int], None] | None = None
    ) -> FetchOutcome:
        sha = entry.sha256
        if self.store.verify_object(sha, entry.size_bytes):
            return FetchOutcome(sha, self.store.object_path(sha), 0, 0, 0, True)
        part = self.store.part_path(sha)
        part.parent.mkdir(parents=True, exist_ok=True)
        transferred = 0
        resumed_from = min(part.stat().st_size if part.is_file() else 0, entry.size_bytes)
        attempt = 0
        while True:
            attempt += 1
            try:
                transferred += self._attempt(entry, part, on_bytes)
                break
            except RangeFetchInterrupted:
                if attempt >= self.max_attempts:
                    raise
            except RangeFetchError as error:
                if error.status != 416 or attempt >= self.max_attempts:
                    raise
                part.unlink(missing_ok=True)  # our offset is stale: restart from zero
        path = self.store.adopt_file(part, sha, entry.size_bytes)
        return FetchOutcome(sha, path, transferred, resumed_from, attempt, False)

    def _attempt(
        self, entry: ResourceBlobEntry, part: Path, on_bytes: Callable[[int], None] | None
    ) -> int:
        size = entry.size_bytes
        offset = part.stat().st_size if part.is_file() else 0
        if offset > size:
            part.unlink()
            offset = 0
        if offset == size:
            return 0  # complete on disk already; hashing decides
        with self.source.open_range(self.key_for(entry), offset) as reply:
            if reply.status == 206 and reply.start_offset == offset:
                mode, written = "ab", offset
            elif reply.status == 200:
                mode, written = "wb", 0
            else:
                raise RangeFetchError(
                    f"refusing reply status={reply.status} start={reply.start_offset} "
                    f"for offset {offset}",
                    status=reply.status,
                )
            if reply.total_size is not None and reply.total_size != size:
                raise IntegrityError(
                    f"{entry.relative_path}: server reports {reply.total_size} bytes, "
                    f"manifest says {size}"
                )
            received = 0
            with part.open(mode) as handle:
                try:
                    while written < size:
                        chunk = reply.read(min(self.chunk_size, size - written))
                        if not chunk:
                            break
                        handle.write(chunk)
                        written += len(chunk)
                        received += len(chunk)
                        if on_bytes is not None:
                            on_bytes(len(chunk))
                    overflow = reply.read(1) if written == size else b""
                except (OSError, http.client.HTTPException) as error:
                    handle.flush()
                    raise RangeFetchInterrupted(
                        f"{entry.relative_path}: transfer interrupted at byte {written}: {error}",
                        bytes_on_disk=written,
                    ) from error
                finally:
                    handle.flush()
                    os.fsync(handle.fileno())
            if overflow:
                part.unlink(missing_ok=True)
                raise IntegrityError(f"{entry.relative_path}: server sent more than {size} bytes")
            if written < size:
                raise RangeFetchInterrupted(
                    f"{entry.relative_path}: transfer stopped at {written}/{size} bytes",
                    bytes_on_disk=written,
                )
        return received
