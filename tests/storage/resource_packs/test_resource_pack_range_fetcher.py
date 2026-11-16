"""Resumable HTTP Range downloads against a real local ``http.server``."""

from __future__ import annotations

import hashlib
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
    default_blob_key,
)

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
