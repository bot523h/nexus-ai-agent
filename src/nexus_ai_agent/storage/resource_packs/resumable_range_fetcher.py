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
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import IO, BinaryIO, Protocol

from .content_store import LocalContentStore
from .errors import IntegrityError, RangeFetchError, RangeFetchInterrupted, UnsafeSourceError
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


def _normalise_host(host: str) -> str:
    return host.strip().rstrip(".").lower()


class _SourcePolicy:
    """Scheme + exact-hostname allowlist shared by requests and redirects."""

    def __init__(self, schemes: frozenset[str], hosts: frozenset[str], follow: bool) -> None:
        self.schemes = schemes
        self.hosts = hosts
        self.follow = follow

    def check(self, url: str) -> None:
        parsed = urllib.parse.urlsplit(url)
        host = _normalise_host(parsed.hostname or "")
        if parsed.scheme not in self.schemes:
            raise UnsafeSourceError(f"scheme {parsed.scheme!r} is not allowed for {url!r}")
        if host not in self.hosts:
            raise UnsafeSourceError(f"host {host!r} is not in allowed_hosts")


class _AllowlistRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect unless enabled *and* aimed at an allowed scheme/host.

    The check runs inside ``redirect_request`` — i.e. before urllib opens a
    connection to the new location — and the refused response is closed.
    """

    def __init__(self, policy: _SourcePolicy) -> None:
        self._policy = policy

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        try:
            if not self._policy.follow:
                raise UnsafeSourceError(f"HTTP {code} redirect refused (allow_redirects=False)")
            self._policy.check(newurl)
        except UnsafeSourceError:
            fp.close()
            raise
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER_DISPATCH = ("open", "_open", "error", "_call_chain", "add_handler")
_REDIRECT_HOOK_RE = re.compile(r"[a-z]+_error_3[0-9][0-9]")


def _policy_safe_opener(
    opener: urllib.request.OpenerDirector, policy: _SourcePolicy
) -> urllib.request.OpenerDirector:
    """Make a caller-supplied opener follow *policy*, or reject it.

    Accepted only when the opener's redirect behaviour is provably ours: it
    must be a plain :class:`urllib.request.OpenerDirector` (no overridden
    dispatch methods) carrying **no** redirect-capable handler of its own (no
    ``HTTPRedirectHandler`` and no ``*_error_3xx`` hook).  The policy redirect
    handler is then installed, so every 3xx it surfaces is checked against
    ``allowed_hosts`` and the allowed schemes before urllib connects anywhere.
    Openers from :func:`urllib.request.build_opener` include the stock
    redirect handler and are therefore rejected — pass ``extra_handlers``
    instead.
    """
    if not isinstance(opener, urllib.request.OpenerDirector):
        raise UnsafeSourceError("opener must be a urllib.request.OpenerDirector")
    for name in _OPENER_DISPATCH:
        if getattr(type(opener), name) is not getattr(urllib.request.OpenerDirector, name):
            raise UnsafeSourceError(
                f"custom opener overrides OpenerDirector.{name}; its redirects cannot be "
                "policy-checked"
            )
    handlers = getattr(opener, "handlers", None)  # stdlib attribute, absent from typeshed
    if not isinstance(handlers, list):
        raise UnsafeSourceError("opener exposes no handler list; it cannot be policy-checked")
    for handler in handlers:
        _reject_redirect_capable(handler)
    opener.add_handler(_AllowlistRedirectHandler(policy))
    return opener


def _reject_redirect_capable(handler: urllib.request.BaseHandler) -> None:
    if isinstance(handler, urllib.request.HTTPRedirectHandler) or any(
        _REDIRECT_HOOK_RE.fullmatch(name) for name in dir(handler)
    ):
        raise UnsafeSourceError(
            f"{type(handler).__name__} handles redirects itself; that would bypass the "
            "allowed_hosts policy"
        )


class HttpRangePackSource:
    """HTTP(S) source with ``Range`` support, on :mod:`urllib.request` only.

    Only ``http``/``https`` URLs are accepted (``file:`` and friends would turn
    a manifest into a local-file read primitive), and only towards
    ``allowed_hosts`` (exact, case-insensitive hostnames; ``None`` means "the
    base URL's host only").  An ``https`` base never accepts a downgrade to
    ``http``.  Redirects are refused unless ``allow_redirects=True``, and even
    then only to allowed hosts/schemes.  Every violation raises
    :class:`UnsafeSourceError` before a request is sent to the forbidden
    destination.

    The same policy binds a caller-supplied *opener* (see
    :func:`_policy_safe_opener`: openers with their own redirect logic are
    rejected) and ``extra_handlers`` (proxy/auth/... handlers added to the
    default opener; redirect-capable ones are rejected).  As defence in depth
    the final response URL is checked too, so a handler that silently
    followed a redirect elsewhere cannot hand back foreign bytes.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        headers: Mapping[str, str] | None = None,
        opener: urllib.request.OpenerDirector | None = None,
        allowed_hosts: Collection[str] | None = None,
        allow_redirects: bool = False,
        extra_handlers: Sequence[urllib.request.BaseHandler] = (),
    ) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"base_url must be an absolute http(s) URL: {base_url!r}")
        if isinstance(allowed_hosts, str):
            raise TypeError("allowed_hosts must be a collection of hostnames, not a str")
        if not isinstance(allow_redirects, bool):
            raise TypeError("allow_redirects must be a bool")
        hosts = frozenset(
            _normalise_host(h)
            for h in (allowed_hosts if allowed_hosts is not None else (parsed.hostname,))
            if h.strip()
        )
        schemes = frozenset({"https"} if parsed.scheme == "https" else {"http", "https"})
        self._policy = _SourcePolicy(schemes, hosts, allow_redirects)
        self._policy.check(base_url)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = dict(headers or {})
        for handler in extra_handlers:
            _reject_redirect_capable(handler)
        if opener is None:
            self._opener = urllib.request.build_opener(
                *extra_handlers, _AllowlistRedirectHandler(self._policy)
            )
        elif extra_handlers:
            raise ValueError("pass either opener or extra_handlers, not both")
        else:
            self._opener = _policy_safe_opener(opener, self._policy)

    @property
    def allowed_hosts(self) -> frozenset[str]:
        return self._policy.hosts

    def url_for(self, key: str) -> str:
        return f"{self.base_url}/{urllib.parse.quote(key.lstrip('/'), safe='/')}"

    def open_range(self, key: str, offset: int) -> RangeReply:
        headers = {"User-Agent": _USER_AGENT, "Accept-Encoding": "identity", **self.headers}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        url = self.url_for(key)
        self._policy.check(url)  # defence in depth: base_url is a public attribute
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            response = self._opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            error.close()
            raise RangeFetchError(f"HTTP {error.code} for {key!r}", status=error.code) from error
        except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
            raise RangeFetchInterrupted(
                f"transport failure for {key!r}: {error}", bytes_on_disk=offset
            ) from error
        try:
            self._policy.check(response.geturl())  # where the bytes actually came from
        except UnsafeSourceError:
            response.close()
            raise
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
