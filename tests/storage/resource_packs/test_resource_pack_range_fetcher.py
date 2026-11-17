"""Resumable HTTP Range downloads against a real local ``http.server``."""

from __future__ import annotations

import hashlib
import http.client
import io
import urllib.request
from pathlib import Path

import pytest
from resource_pack_testkit import PackMirror, blob, range_http_server, remote_entry

from nexus_ai_agent.storage.resource_packs import (
    HttpRangePackSource,
    IntegrityError,
    LocalContentStore,
    LocalDirectoryPackSource,
    RangeFetchError,
    RangeFetchInterrupted,
    RangeReply,
    ResourceBlobEntry,
    ResumableRangeFetcher,
    UnsafeSourceError,
    default_blob_key,
)
from nexus_ai_agent.storage.resource_packs import resumable_range_fetcher as rrf

SIZE = 200_000


def _fetcher(source: object, tmp_path: Path, **kwargs: int) -> ResumableRangeFetcher:
    store = LocalContentStore(tmp_path / "cas")
    return ResumableRangeFetcher(source, store, chunk_size=8192, **kwargs)  # type: ignore[arg-type]


def test_real_http_download_resumes_from_the_cut_byte(mirror: PackMirror, tmp_path: Path) -> None:
    data = blob("cinema-hd-texture", SIZE)
    entry = mirror.add_remote("textures/hero.ktx2", data)
    with range_http_server(mirror.root) as server:
        fetcher = _fetcher(HttpRangePackSource(server.base_url), tmp_path, max_attempts=1)
        server.cut_after_bytes = SIZE // 2
        with pytest.raises(RangeFetchInterrupted) as info:
            fetcher.fetch(entry)
        part = fetcher.store.part_path(entry.sha256)
        assert part.is_file() and part.stat().st_size == SIZE // 2
        assert info.value.bytes_on_disk == SIZE // 2
        assert not fetcher.store.has(entry.sha256)

        received: list[int] = []
        outcome = fetcher.fetch(entry, on_bytes=received.append)
    assert server.range_headers == [None, f"bytes={SIZE // 2}-"]
    assert sum(received) == outcome.bytes_transferred == SIZE - SIZE // 2
    assert outcome.resumed_from == SIZE // 2 and not outcome.already_present
    assert hashlib.sha256(outcome.path.read_bytes()).hexdigest() == entry.sha256
    assert not part.exists()


def test_in_call_retry_resumes_automatically(mirror: PackMirror, tmp_path: Path) -> None:
    data = blob("retry", SIZE)
    entry = mirror.add_remote("a.bin", data)
    with range_http_server(mirror.root) as server:
        server.cut_after_bytes = 12_345
        outcome = _fetcher(HttpRangePackSource(server.base_url), tmp_path).fetch(entry)
    assert server.range_headers == [None, "bytes=12345-"]
    assert outcome.attempts == 2 and outcome.path.read_bytes() == data


def test_server_ignoring_range_rewrites_the_part_from_zero(
    mirror: PackMirror, tmp_path: Path
) -> None:
    data = blob("no-range", 50_000)
    entry = mirror.add_remote("a.bin", data)
    fetcher_dir = tmp_path
    with range_http_server(mirror.root) as server:
        server.ignore_range = True
        fetcher = _fetcher(HttpRangePackSource(server.base_url), fetcher_dir)
        part = fetcher.store.part_path(entry.sha256)
        part.parent.mkdir(parents=True)
        part.write_bytes(b"\xff" * 1000)  # stale garbage that must not survive
        outcome = fetcher.fetch(entry)
    assert server.range_headers == ["bytes=1000-"]
    assert outcome.path.read_bytes() == data and outcome.bytes_transferred == 50_000


def test_hash_mismatch_rejects_the_blob_and_deletes_the_part(
    mirror: PackMirror, tmp_path: Path
) -> None:
    entry = mirror.add_remote("a.bin", blob("good", 4096))
    (mirror.root / default_blob_key(entry)).write_bytes(blob("evil", 4096))  # CDN poisoned
    fetcher = _fetcher(LocalDirectoryPackSource(mirror.root), tmp_path)
    with pytest.raises(IntegrityError, match="rejected blob"):
        fetcher.fetch(entry)
    assert not fetcher.store.has(entry.sha256)
    assert not fetcher.store.part_path(entry.sha256).exists()


def test_complete_part_on_disk_needs_no_request(mirror: PackMirror, tmp_path: Path) -> None:
    data = blob("done", 3000)
    entry = mirror.add_remote("a.bin", data)
    source = LocalDirectoryPackSource(mirror.root)
    fetcher = _fetcher(source, tmp_path)
    part = fetcher.store.part_path(entry.sha256)
    part.parent.mkdir(parents=True)
    part.write_bytes(data)
    outcome = fetcher.fetch(entry)
    assert source.requested_offsets == [] and outcome.bytes_transferred == 0
    again = fetcher.fetch(entry)  # now served straight from the CAS
    assert again.already_present and again.attempts == 0


def test_oversized_part_is_discarded(mirror: PackMirror, tmp_path: Path) -> None:
    data = blob("small", 100)
    entry = mirror.add_remote("a.bin", data)
    source = LocalDirectoryPackSource(mirror.root)
    fetcher = _fetcher(source, tmp_path)
    part = fetcher.store.part_path(entry.sha256)
    part.parent.mkdir(parents=True)
    part.write_bytes(b"x" * 500)
    assert fetcher.fetch(entry).path.read_bytes() == data
    assert source.requested_offsets == [(default_blob_key(entry), 0)]


def test_local_source_resume_and_honor_ranges_flag(mirror: PackMirror, tmp_path: Path) -> None:
    data = blob("local", 10_000)
    entry = mirror.add_remote("a.bin", data)
    for honor in (True, False):
        source = LocalDirectoryPackSource(mirror.root, honor_ranges=honor)
        fetcher = _fetcher(source, tmp_path / str(honor))
        part = fetcher.store.part_path(entry.sha256)
        part.parent.mkdir(parents=True)
        part.write_bytes(data[:4000])
        outcome = fetcher.fetch(entry)
        assert outcome.path.read_bytes() == data
        assert outcome.bytes_transferred == (6000 if honor else 10_000)


class _ScriptedSource:
    """A source whose replies are scripted per call (protocol edge cases)."""

    def __init__(self, replies: list[RangeReply | Exception]) -> None:
        self.replies = replies
        self.offsets: list[int] = []

    def open_range(self, key: str, offset: int) -> RangeReply:
        self.offsets.append(offset)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class _Exploding(io.BytesIO):
    def read(self, size: int | None = -1, /) -> bytes:
        raise ConnectionResetError("peer reset")


def _entry(data: bytes) -> ResourceBlobEntry:
    return remote_entry("x.bin", data)


def test_server_sending_extra_bytes_is_refused(tmp_path: Path) -> None:
    data = b"0123456789"
    source = _ScriptedSource([RangeReply(200, 0, None, io.BytesIO(data + b"EXTRA"))])
    fetcher = _fetcher(source, tmp_path)
    with pytest.raises(IntegrityError, match="more than 10 bytes"):
        fetcher.fetch(_entry(data))
    assert not fetcher.store.part_path(_entry(data).sha256).exists()


def test_declared_total_size_mismatch_is_refused(tmp_path: Path) -> None:
    source = _ScriptedSource([RangeReply(200, 0, 999, io.BytesIO(b"abc"))])
    with pytest.raises(IntegrityError, match="server reports 999"):
        _fetcher(source, tmp_path).fetch(_entry(b"abc"))


def test_misaligned_206_and_odd_statuses_are_refused(tmp_path: Path) -> None:
    data = b"abcdef"
    fetcher = _fetcher(_ScriptedSource([]), tmp_path)
    part = fetcher.store.part_path(_entry(data).sha256)
    part.parent.mkdir(parents=True)
    part.write_bytes(b"abc")
    fetcher.source = _ScriptedSource([RangeReply(206, 2, 6, io.BytesIO(b"cdef"))])
    with pytest.raises(RangeFetchError, match="refusing reply"):
        fetcher.fetch(_entry(data))
    fetcher.source = _ScriptedSource([RangeReply(204, 0, None, io.BytesIO(b""))])
    with pytest.raises(RangeFetchError, match="status=204"):
        fetcher.fetch(_entry(data))


def test_mid_stream_transport_errors_keep_the_part(tmp_path: Path) -> None:
    source = _ScriptedSource([RangeReply(200, 0, 4, _Exploding())])
    fetcher = _fetcher(source, tmp_path, max_attempts=1)
    with pytest.raises(RangeFetchInterrupted, match="interrupted"):
        fetcher.fetch(_entry(b"abcd"))
    assert fetcher.store.part_path(_entry(b"abcd").sha256).exists()


def test_416_discards_a_stale_part_and_restarts(tmp_path: Path) -> None:
    data = b"abcdef"
    source = _ScriptedSource(
        [
            RangeFetchError("range not satisfiable", status=416),
            RangeReply(200, 0, 6, io.BytesIO(data)),
        ]
    )
    fetcher = _fetcher(source, tmp_path)
    part = fetcher.store.part_path(_entry(data).sha256)
    part.parent.mkdir(parents=True)
    part.write_bytes(b"abc")
    assert fetcher.fetch(_entry(data)).path.read_bytes() == data
    assert source.offsets == [3, 0]
    exhausted = _fetcher(
        _ScriptedSource([RangeFetchError("gone", status=416)]), tmp_path / "b", max_attempts=1
    )
    with pytest.raises(RangeFetchError, match="gone"):
        exhausted.fetch(_entry(data))
    hard = _fetcher(_ScriptedSource([RangeFetchError("nope", status=404)]), tmp_path / "c")
    with pytest.raises(RangeFetchError, match="nope"):
        hard.fetch(_entry(data))


def test_fetcher_rejects_bad_configuration(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _fetcher(_ScriptedSource([]), tmp_path, max_attempts=0)


def test_local_source_guards(mirror: PackMirror) -> None:
    source = LocalDirectoryPackSource(mirror.root)
    with pytest.raises(RangeFetchError) as escape:
        source.open_range("../outside", 0)
    assert escape.value.status == 400
    with pytest.raises(RangeFetchError) as missing:
        source.open_range("blobs/none", 0)
    assert missing.value.status == 404
    entry = mirror.add_remote("a.bin", b"abc")
    with pytest.raises(RangeFetchError) as beyond:
        source.open_range(default_blob_key(entry), 10)
    assert beyond.value.status == 416


def test_http_source_status_handling(mirror: PackMirror) -> None:
    entry = mirror.add_remote("dir with space/a.bin", b"abcdef")
    with range_http_server(mirror.root) as server:
        source = HttpRangePackSource(server.base_url + "/", headers={"X-Pack": "1"})
        with source.open_range(default_blob_key(entry), 2) as reply:
            assert (reply.status, reply.start_offset, reply.total_size) == (206, 2, 6)
            assert reply.read(10) == b"cdef"
        with pytest.raises(RangeFetchError) as not_found:
            source.open_range("blobs/zz/missing", 0)
        assert not_found.value.status == 404
        with pytest.raises(RangeFetchError) as unsatisfiable:
            source.open_range(default_blob_key(entry), 6)
        assert unsatisfiable.value.status == 416
        server.bogus_content_range = True
        with pytest.raises(RangeFetchError, match="Content-Range"):
            source.open_range(default_blob_key(entry), 2)
    # the server is gone now: a transport failure is a resumable interruption
    with pytest.raises(RangeFetchInterrupted):
        HttpRangePackSource(server.base_url, timeout=2).open_range("x", 0)


def test_http_source_url_building_and_scheme_allowlist() -> None:
    source = HttpRangePackSource("https://cdn.example.com/packs/")
    assert source.url_for("/blobs/ab/x y") == "https://cdn.example.com/packs/blobs/ab/x%20y"
    for bad in ("file:///etc/passwd", "ftp://host/x", "https://", "relative/path"):
        with pytest.raises(ValueError):
            HttpRangePackSource(bad)


class _FakeResponse(io.BytesIO):
    def __init__(self, status: int, headers: dict[str, str]) -> None:
        super().__init__(b"")
        self.status = status
        self.headers = headers


class _FakeOpener(urllib.request.OpenerDirector):
    def __init__(self, response: _FakeResponse) -> None:
        super().__init__()
        self.response = response

    def open(self, fullurl: object, data: object = None, timeout: float | None = None) -> object:
        return self.response


def test_http_source_unexpected_status_and_unknown_total() -> None:
    odd = HttpRangePackSource("https://cdn.example", opener=_FakeOpener(_FakeResponse(204, {})))
    with pytest.raises(RangeFetchError, match="unexpected HTTP status 204"):
        odd.open_range("x", 0)
    star = _FakeResponse(206, {"Content-Range": "bytes 5-9/*"})
    reply = HttpRangePackSource("https://cdn.example", opener=_FakeOpener(star)).open_range("x", 5)
    assert reply.total_size is None
    plain = HttpRangePackSource("https://cdn.example", opener=_FakeOpener(_FakeResponse(200, {})))
    assert plain.open_range("x", 0).total_size is None


# -- F5: host allowlist & redirect policy ------------------------------------


class _RecordingOpener(urllib.request.OpenerDirector):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[object] = []

    def open(self, fullurl: object, data: object = None, timeout: float | None = None) -> object:
        self.calls.append(fullurl)
        raise AssertionError("no request may be sent to a forbidden host")


def test_disallowed_host_is_refused_before_any_request() -> None:
    with pytest.raises(UnsafeSourceError, match="evil.example"):
        HttpRangePackSource("https://evil.example/packs", allowed_hosts={"cdn.example"})
    with pytest.raises(UnsafeSourceError):  # an explicit empty allowlist allows nothing
        HttpRangePackSource("https://cdn.example", allowed_hosts=())
    with pytest.raises(UnsafeSourceError):  # userinfo cannot smuggle a host past the check
        HttpRangePackSource("https://cdn.example@evil.example/", allowed_hosts=["cdn.example"])
    default = HttpRangePackSource("https://CDN.Example./packs/")
    assert default.allowed_hosts == frozenset({"cdn.example"})
    explicit = HttpRangePackSource("https://cdn.example", allowed_hosts=["CDN.example", " "])
    assert explicit.allowed_hosts == frozenset({"cdn.example"})

    opener = _RecordingOpener()
    source = HttpRangePackSource("https://cdn.example", opener=opener)
    source.base_url = "https://evil.example"  # tampering after construction
    with pytest.raises(UnsafeSourceError):
        source.open_range("blobs/x", 0)
    assert opener.calls == []


def test_source_policy_arguments_are_strict() -> None:
    with pytest.raises(TypeError, match="not a str"):
        HttpRangePackSource("https://cdn.example", allowed_hosts="cdn.example")
    with pytest.raises(TypeError, match="allow_redirects"):
        HttpRangePackSource("https://cdn.example", allow_redirects=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="absolute"):
        HttpRangePackSource("http:///no-host")


def _redirect_handler(base: str, *, follow: bool) -> rrf._AllowlistRedirectHandler:
    source = HttpRangePackSource(base, allowed_hosts=["cdn.example"], allow_redirects=follow)
    return rrf._AllowlistRedirectHandler(source._policy)


def test_redirect_handler_blocks_scheme_downgrade_and_closes_the_reply() -> None:
    request = urllib.request.Request("https://cdn.example/blobs/x")
    headers = http.client.HTTPMessage()
    for target in ("http://cdn.example/blobs/x", "ftp://cdn.example/x", "https://evil.example/x"):
        body = io.BytesIO(b"")
        with pytest.raises(UnsafeSourceError):
            _redirect_handler("https://cdn.example", follow=True).redirect_request(
                request, body, 302, "Found", headers, target
            )
        assert body.closed
    handler = _redirect_handler("https://cdn.example", follow=True)
    target = "https://cdn.example/mirror/x"
    allowed = handler.redirect_request(request, io.BytesIO(b""), 302, "Found", headers, target)
    assert allowed is not None and allowed.full_url == "https://cdn.example/mirror/x"


@pytest.mark.parametrize("follow", [False, True])
def test_real_302_to_a_disallowed_host_is_blocked(
    mirror: PackMirror, tmp_path: Path, follow: bool
) -> None:
    entry = mirror.add_remote("textures/hero.ktx2", blob("redirect", 4096))
    with range_http_server(mirror.root) as server:
        source = HttpRangePackSource(server.base_url, allow_redirects=follow)
        fetcher = _fetcher(source, tmp_path, max_attempts=3)
        server.redirect_to = "http://evil.invalid"
        with pytest.raises(UnsafeSourceError):
            fetcher.fetch(entry)
    assert len(server.range_headers) == 1  # one 302, never retried, never followed
    assert not fetcher.store.has(entry.sha256)
    assert not fetcher.store.part_path(entry.sha256).exists()


def test_real_302_to_an_allowed_host_is_followed_when_enabled(
    mirror: PackMirror, tmp_path: Path
) -> None:
    data = blob("redirect-ok", 4096)
    entry = mirror.add_remote("textures/hero.ktx2", data)
    with range_http_server(mirror.root) as server:
        port = server.base_url.rsplit(":", 1)[1]
        source = HttpRangePackSource(
            server.base_url, allowed_hosts={"127.0.0.1", "localhost"}, allow_redirects=True
        )
        server.redirect_to = f"http://localhost:{port}"
        outcome = _fetcher(source, tmp_path).fetch(entry)
    assert outcome.path.read_bytes() == data
    assert server.request_hosts == [f"127.0.0.1:{port}", f"localhost:{port}"]


class _RedirectReturningOpener(urllib.request.OpenerDirector):
    def __init__(self, response: _FakeResponse) -> None:
        super().__init__()
        self.response = response

    def open(self, fullurl: object, data: object = None, timeout: float | None = None) -> object:
        return self.response


class _UrlResponse(io.BytesIO):
    def __init__(
        self, status: int, url: str | None = None, geturl_value: str | None = None
    ) -> None:
        super().__init__(b"ok")
        self.status = status
        self.headers: dict[str, str] = {}
        if url is not None:
            self.url = url
        self._geturl_value = geturl_value

    def geturl(self) -> str:
        return self._geturl_value or ""


def test_custom_opener_cannot_bypass_redirect_policy(mirror: PackMirror, tmp_path: Path) -> None:
    entry = mirror.add_remote("textures/hero.ktx2", blob("custom-opener", 2048))

    # 1. Real urllib.request.build_opener() passed as opener still gets SecurityRedirectHandler
    with range_http_server(mirror.root) as server:
        for follow in (False, True):
            server.redirect_to = "http://evil.invalid"
            custom = urllib.request.build_opener()
            source = HttpRangePackSource(server.base_url, opener=custom, allow_redirects=follow)
            with pytest.raises(UnsafeSourceError):
                _fetcher(source, tmp_path / f"std-{follow}").fetch(entry)

    # 1b. OpenerDirector subclass with pre-installed HTTPRedirectHandler also gets replaced
    class _SubclassWithRedirect(urllib.request.OpenerDirector):
        def __init__(self) -> None:
            super().__init__()
            self.add_handler(urllib.request.HTTPHandler())
            self.add_handler(urllib.request.HTTPErrorProcessor())
            self.add_handler(urllib.request.HTTPRedirectHandler())

    with range_http_server(mirror.root) as server:
        server.redirect_to = "http://evil.invalid"
        source_sub = HttpRangePackSource(
            server.base_url, opener=_SubclassWithRedirect(), allow_redirects=True
        )
        with pytest.raises(UnsafeSourceError):
            _fetcher(source_sub, tmp_path / "sub").fetch(entry)

    # 2. Custom OpenerDirector subclass returning raw 302 response
    for follow in (False, True):
        r302 = _FakeResponse(302, {"Location": "http://evil.invalid/x"})
        src = HttpRangePackSource(
            "https://cdn.example", opener=_RedirectReturningOpener(r302), allow_redirects=follow
        )
        with pytest.raises(UnsafeSourceError):
            src.open_range("blobs/x", 0)
        assert r302.closed

    allowed_302 = _FakeResponse(302, {"Location": "https://cdn.example/mirror/x"})
    src_allowed = HttpRangePackSource(
        "https://cdn.example",
        opener=_RedirectReturningOpener(allowed_302),
        allow_redirects=True,
    )
    with pytest.raises(RangeFetchError, match="unfollowed redirect HTTP 302"):
        src_allowed.open_range("blobs/x", 0)

    # 3. Custom OpenerDirector subclass silently following redirect (url / geturl differs)
    for follow, target in (
        (False, "https://cdn.example/other"),
        (True, "http://cdn.example/downgraded"),
        (True, "https://evil.invalid/steal"),
    ):
        resp_attr = _UrlResponse(200, url=target)
        src_attr = HttpRangePackSource(
            "https://cdn.example",
            opener=_RedirectReturningOpener(resp_attr),  # type: ignore[arg-type]
            allow_redirects=follow,
        )
        with pytest.raises(UnsafeSourceError):
            src_attr.open_range("blobs/x", 0)
        assert resp_attr.closed

    resp_geturl = _UrlResponse(200, geturl_value="http://evil.invalid/steal")
    src_geturl = HttpRangePackSource(
        "https://cdn.example",
        opener=_RedirectReturningOpener(resp_geturl),  # type: ignore[arg-type]
        allow_redirects=True,
    )
    with pytest.raises(UnsafeSourceError):
        src_geturl.open_range("blobs/x", 0)
    assert resp_geturl.closed
