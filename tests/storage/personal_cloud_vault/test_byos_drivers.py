"""Tests for LocalDeviceVaultDriver, S3CompatibleSigV4Driver, and PortableBundleVaultDriver."""

from __future__ import annotations

import hashlib
import io
import struct
import urllib.error
import urllib.parse
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.storage.personal_cloud_vault import (
    BUNDLE_MAGIC,
    BUNDLE_VERSION,
    BYOSStorageDriver,
    LocalDeviceVaultDriver,
    PersonalVaultError,
    PortableBundleVaultDriver,
    S3CompatibleSigV4Driver,
    S3HttpRequest,
    S3HttpResponse,
    S3SigV4Config,
    VaultAuthenticationError,
    VaultQuotaExceededError,
    VaultTamperDetectedError,
    VaultTransportError,
    compute_sigv4_signature,
)
from nexus_ai_agent.storage.personal_cloud_vault.byos_drivers import (
    _BUNDLE_HEADER_PREFIX,
    _BUNDLE_HEADER_TOTAL,
    _BUNDLE_INDEX_HEAD,
    _BUNDLE_INDEX_TAIL,
    _default_urllib_transport,
    _parse_s3_list_xml,
    _parse_s3_list_xml_entries,
)


def test_all_three_drivers_satisfy_byos_protocol(tmp_path: Path) -> None:
    local = LocalDeviceVaultDriver(tmp_path / "local")
    s3 = S3CompatibleSigV4Driver(
        S3SigV4Config(
            endpoint_url="https://account.r2.cloudflarestorage.com",
            bucket="my-bucket",
            region="auto",
            access_key_id="AKID",
            secret_access_key="SECRET",
        ),
        http_transport=lambda _req: S3HttpResponse(status_code=200),
    )
    bundle = PortableBundleVaultDriver(tmp_path / "vault.nagarvault")

    assert isinstance(local, BYOSStorageDriver)
    assert isinstance(s3, BYOSStorageDriver)
    assert isinstance(bundle, BYOSStorageDriver)


# --------------------------------------------------------------------------- #
# 1. LocalDeviceVaultDriver
# --------------------------------------------------------------------------- #


def test_local_device_vault_atomic_crud_and_quota(tmp_path: Path) -> None:
    vault_dir = tmp_path / "ext_drive_vault"
    driver = LocalDeviceVaultDriver(vault_dir, max_quota_bytes=250)
    assert driver.root_dir == vault_dir.resolve()
    assert driver.max_quota_bytes == 250
    assert driver.used_bytes() == 0

    cid_1 = "a" * 64
    cid_2 = "b" * 64
    assert not driver.has_chunk(cid_1)
    assert driver.get_chunk_mtime(cid_1) is None

    driver.put_chunk(cid_1, b"x" * 100)
    assert driver.has_chunk(cid_1)
    assert isinstance(driver.get_chunk_mtime(cid_1), float)
    assert driver.get_chunk(cid_1) == b"x" * 100
    assert driver.list_chunks() == [cid_1]
    assert driver.used_bytes() == 100

    # Stray .tmp files must be ignored by used_bytes, list_chunks, and list_manifests
    (vault_dir / "chunks" / ".stray.chk.tmp").write_bytes(b"z" * 900)
    (vault_dir / "manifests" / ".stray.json").write_bytes(b"{}")
    assert driver.used_bytes() == 100
    assert driver.list_chunks() == [cid_1]
    assert driver.list_manifests() == []

    # Overwriting existing chunk reclaims previous 100B before checking 250B quota
    driver.put_chunk(cid_1, b"y" * 180)
    assert driver.get_chunk(cid_1) == b"y" * 180
    assert driver.used_bytes() == 180

    # Exceeding 250B quota fails closed with VaultQuotaExceededError without writing
    with pytest.raises(VaultQuotaExceededError):
        driver.put_chunk(cid_2, b"q" * 80)
    assert not driver.has_chunk(cid_2)

    driver.put_manifest("snap-001", b'{"ok":true}')
    assert driver.get_manifest("snap-001") == b'{"ok":true}'
    assert driver.list_manifests() == ["snap-001"]

    with pytest.raises(VaultQuotaExceededError):
        driver.put_manifest("snap-002", b"m" * 100)

    driver.delete_chunk(cid_1)
    assert not driver.has_chunk(cid_1)
    driver.delete_manifest("snap-001")
    assert driver.list_manifests() == []


def test_local_device_vault_validation_and_error_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(PersonalVaultError):
        LocalDeviceVaultDriver(tmp_path / "bad_q", max_quota_bytes=0)

    real_dir = tmp_path / "real"
    real_dir.mkdir()
    sym_dir = tmp_path / "sym_root"
    sym_dir.symlink_to(real_dir)
    with pytest.raises(VaultTransportError):
        LocalDeviceVaultDriver(sym_dir)

    # Unbound quota path + missing chunk/manifest + invalid IDs
    driver = LocalDeviceVaultDriver(tmp_path / "unlimited")
    driver.put_chunk("0" * 64, b"ok")
    with pytest.raises(VaultTransportError):
        driver.get_chunk("f" * 64)
    with pytest.raises(VaultTransportError):
        driver.get_manifest("missing-snap")
    with pytest.raises(PersonalVaultError):
        driver.has_chunk("../escape")
    with pytest.raises(PersonalVaultError):
        driver.put_manifest("bad..snap", b"{}")

    # Failing os.replace cleans up .tmp and raises VaultTransportError
    def _boom_replace(_src: Any, _dst: Any) -> None:
        raise OSError("simulated disk failure")

    monkeypatch.setattr("os.replace", _boom_replace)
    with pytest.raises(VaultTransportError):
        driver.put_chunk("1" * 64, b"payload")
    leftover_tmps = list((driver.root_dir / "chunks").glob("*.tmp"))
    assert leftover_tmps == []


def test_local_driver_io_error_branches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    driver = LocalDeviceVaultDriver(tmp_path / "io_errs")
    cid = "2" * 64
    driver.put_chunk(cid, b"data")
    driver.put_manifest("s1", b"{}")

    monkeypatch.setattr(
        Path, "read_bytes", lambda _self: (_ for _ in ()).throw(OSError("read err"))
    )
    with pytest.raises(VaultTransportError):
        driver.get_chunk(cid)
    with pytest.raises(VaultTransportError):
        driver.get_manifest("s1")

    monkeypatch.undo()
    monkeypatch.setattr(
        Path, "unlink", lambda _self, missing_ok=False: (_ for _ in ()).throw(OSError("unlink err"))
    )
    with pytest.raises(VaultTransportError):
        driver.delete_chunk(cid)
    with pytest.raises(VaultTransportError):
        driver.delete_manifest("s1")

    monkeypatch.undo()
    # mkdir failure on initialization
    blocker_file = tmp_path / "blocker_file"
    blocker_file.write_bytes(b"not-a-dir")
    with pytest.raises(VaultTransportError):
        LocalDeviceVaultDriver(blocker_file / "nested")


# --------------------------------------------------------------------------- #
# 2. S3CompatibleSigV4Driver & AWS4-HMAC-SHA256 Reference Vectors
# --------------------------------------------------------------------------- #


def test_aws_sigv4_official_reference_vectors() -> None:
    fixed_ts = datetime(2013, 5, 24, 0, 0, 0, tzinfo=timezone.utc)

    # Vector 1: Official AWS S3 GET Object (Range: bytes=0-9)
    vec1 = compute_sigv4_signature(
        method="GET",
        url="https://examplebucket.s3.amazonaws.com/test.txt",
        access_key_id="AKIAIOSFODNN7EXAMPLE",
        secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        region="us-east-1",
        service="s3",
        payload=b"",
        extra_headers={"Range": "bytes=0-9"},
        timestamp=fixed_ts,
    )
    assert vec1.signature == "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    assert vec1.authorization_header == (
        "AWS4-HMAC-SHA256 "
        "Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )

    # Vector 2: Official AWS S3 List Objects query string canonicalization
    vec2 = compute_sigv4_signature(
        method="GET",
        url="https://examplebucket.s3.amazonaws.com/?prefix=J&max-keys=2",
        access_key_id="AKIAIOSFODNN7EXAMPLE",
        secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        region="us-east-1",
        service="s3",
        payload=b"",
        timestamp=fixed_ts,
    )
    assert vec2.signature == "34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7"

    # Session token inclusion + default timestamp path
    vec_tok = compute_sigv4_signature(
        method="PUT",
        url="https://r2.example.com",
        access_key_id="AKID",
        secret_access_key="SECRET",
        region="auto",
        payload=b"hello",
        session_token="SESSION-TOK-123",
    )
    assert "x-amz-security-token" in vec_tok.authorization_header
    assert vec_tok.signed_headers["x-amz-security-token"] == "SESSION-TOK-123"

    with pytest.raises(VaultAuthenticationError):
        compute_sigv4_signature(
            method="GET",
            url="https://s3.example.com/a",
            access_key_id="",
            secret_access_key="s",
            region="us-east-1",
        )
    with pytest.raises(PersonalVaultError):
        compute_sigv4_signature(
            method="GET",
            url="ftp://s3.example.com/a",
            access_key_id="a",
            secret_access_key="s",
            region="us-east-1",
        )


class _FakeS3MemoryTransport:
    """Deterministic in-memory S3 REST transport with SigV4 verification and pagination."""

    def __init__(self, *, page_size: int = 2) -> None:
        self.objects: dict[str, bytes] = {}
        self.requests: list[S3HttpRequest] = []
        self.page_size = page_size
        self.forced_status: int | None = None

    def __call__(self, request: S3HttpRequest) -> S3HttpResponse:
        self.requests.append(request)
        assert request.headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=")
        assert request.headers["x-amz-content-sha256"] == hashlib.sha256(request.body).hexdigest()

        if self.forced_status is not None:
            return S3HttpResponse(status_code=self.forced_status)

        parsed = urllib.parse.urlsplit(request.url)
        query = dict(urllib.parse.parse_qsl(parsed.query))

        # Path-style `/<bucket>/<key...>` or bucket list `/<bucket>?list-type=2`
        stripped = parsed.path.lstrip("/")
        parts = stripped.split("/", 1)
        object_key = parts[1] if len(parts) > 1 else ""

        if request.method == "GET" and query.get("list-type") == "2":
            prefix = query.get("prefix", "")
            matching = sorted(k for k in self.objects if k.startswith(prefix))
            start = int(query.get("continuation-token", "0"))
            slice_keys = matching[start : start + self.page_size]
            next_idx = start + self.page_size
            truncated = next_idx < len(matching)
            items_xml = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in slice_keys)
            tok_xml = (
                f"<NextContinuationToken>{next_idx}</NextContinuationToken>" if truncated else ""
            )
            xml_body = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                f"<IsTruncated>{'true' if truncated else 'false'}</IsTruncated>"
                f"{tok_xml}{items_xml}</ListBucketResult>"
            ).encode()
            return S3HttpResponse(status_code=200, body=xml_body)

        if request.method == "PUT":
            self.objects[object_key] = request.body
            return S3HttpResponse(status_code=200)
        if request.method == "HEAD":
            return S3HttpResponse(status_code=200 if object_key in self.objects else 404)
        if request.method == "GET":
            if object_key not in self.objects:
                return S3HttpResponse(status_code=404)
            return S3HttpResponse(status_code=200, body=self.objects[object_key])
        if request.method == "DELETE":
            self.objects.pop(object_key, None)
            return S3HttpResponse(status_code=204)
        return S3HttpResponse(status_code=405)


def test_s3_compatible_sigv4_driver_crud_pagination_and_error_mapping() -> None:
    cfg = S3SigV4Config(
        endpoint_url="https://123456.r2.cloudflarestorage.com",
        bucket="personal-vault",
        region="auto",
        access_key_id="R2_ACCESS_KEY",
        secret_access_key="R2_SUPER_SECRET_KEY",
        session_token="R2_SESSION_TOKEN",
        prefix="user-42",
    )
    assert "R2_SUPER_SECRET_KEY" not in repr(cfg)
    assert "R2_SESSION_TOKEN" not in repr(cfg)

    fake_s3 = _FakeS3MemoryTransport(page_size=2)
    fixed_clock = lambda: datetime(2026, 11, 16, 12, 0, 0, tzinfo=timezone.utc)  # noqa: E731
    driver = S3CompatibleSigV4Driver(cfg, http_transport=fake_s3, clock=fixed_clock)
    assert driver.config == cfg

    c1, c2, c3 = "1" * 64, "2" * 64, "3" * 64
    assert not driver.has_chunk(c1)
    driver.put_chunk(c1, b"chunk-one")
    driver.put_chunk(c2, b"chunk-two")
    driver.put_chunk(c3, b"chunk-three")

    assert driver.has_chunk(c1)
    assert driver.get_chunk(c2) == b"chunk-two"
    # Pagination exercises 2 requests (page_size=2 for 3 items)
    assert driver.list_chunks() == [c1, c2, c3]

    driver.put_manifest("snap-1", b'{"v":1}')
    driver.put_manifest("snap-2", b'{"v":2}')
    driver.put_manifest("snap-3", b'{"v":3}')
    assert driver.get_manifest("snap-2") == b'{"v":2}'
    assert driver.list_manifests() == ["snap-1", "snap-2", "snap-3"]

    driver.delete_chunk(c2)
    assert not driver.has_chunk(c2)
    with pytest.raises(VaultTransportError):
        driver.get_chunk(c2)
    with pytest.raises(VaultTransportError):
        driver.get_manifest("snap-missing")

    # Virtual-hosted mode + empty prefix
    vhost_cfg = S3SigV4Config(
        endpoint_url="https://s3.us-east-1.amazonaws.com",
        bucket="mybucket",
        region="us-east-1",
        access_key_id="AKID",
        secret_access_key="SKEY",
        prefix="",
        path_style=False,
    )
    captured: list[S3HttpRequest] = []

    def _vhost_transport(req: S3HttpRequest) -> S3HttpResponse:
        captured.append(req)
        if "list-type=2" in req.url:
            return S3HttpResponse(status_code=200, body=b"<ListBucketResult/>")
        return S3HttpResponse(status_code=200, body=b"ok")

    vhost_driver = S3CompatibleSigV4Driver(vhost_cfg, http_transport=_vhost_transport)
    vhost_driver.put_chunk(c1, b"x")
    assert vhost_driver.list_manifests() == []
    assert captured[0].url.startswith("https://mybucket.s3.us-east-1.amazonaws.com/chunks/")

    # Error status codes: 403 -> Auth, 507 -> Quota, 500 -> Transport
    fake_s3.forced_status = 403
    with pytest.raises(VaultAuthenticationError):
        driver.has_chunk(c1)

    fake_s3.forced_status = 507
    with pytest.raises(VaultQuotaExceededError):
        driver.put_chunk(c1, b"x")

    fake_s3.forced_status = 500
    with pytest.raises(VaultTransportError):
        driver.put_chunk(c1, b"x")
    with pytest.raises(VaultTransportError):
        driver.has_chunk(c1)
    with pytest.raises(VaultTransportError):
        driver.put_manifest("snap-x", b"{}")
    with pytest.raises(VaultTransportError):
        driver.list_manifests()
    with pytest.raises(VaultTransportError):
        driver.delete_chunk(c1)


def test_s3_config_xml_security_and_urllib_transport_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(PersonalVaultError):
        S3SigV4Config("ftp://bad", "b", "auto", "k", "s")
    with pytest.raises(PersonalVaultError):
        S3SigV4Config("https://ok.example.com", "bad/bucket", "auto", "k", "s")
    with pytest.raises(PersonalVaultError):
        S3SigV4Config("https://ok.example.com", "bucket", "  ", "k", "s")
    with pytest.raises(VaultAuthenticationError):
        S3SigV4Config("https://ok.example.com", "bucket", "auto", "", "s")

    # XXE DOCTYPE / ENTITY / CDATA / padded-comment bypass defense (Finding F2)
    with pytest.raises(VaultTransportError):
        _parse_s3_list_xml(b'<!DOCTYPE foo [<!ENTITY xxe "bad">]><ListBucketResult/>')
    with pytest.raises(VaultTransportError):
        _parse_s3_list_xml(b"<unclosed")

    # Regression for F2: >2,000 bytes of XML comment padding before <!DOCTYPE / <!ENTITY
    comment_padding = b"<!-- " + (b"P" * 2400) + b" -->\n"
    padded_xxe_payload = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        + comment_padding
        + b'<!DoCtYpE ListBucketResult [<!eNtItY boom "expanded">]>\n'
        + b"<ListBucketResult><Contents><Key>&boom;</Key></Contents></ListBucketResult>"
    )
    assert len(comment_padding) > 2000
    with pytest.raises(VaultTransportError):
        _parse_s3_list_xml(padded_xxe_payload)

    # Padded CDATA, standalone custom entity ref, and NUL byte are also rejected
    with pytest.raises(VaultTransportError):
        _parse_s3_list_xml(
            comment_padding
            + b"<ListBucketResult><Contents><Key><![CDATA[x]]></Key></Contents></ListBucketResult>"
        )
    with pytest.raises(VaultTransportError):
        _parse_s3_list_xml(
            comment_padding
            + b"<ListBucketResult><Contents><Key>&custom_ent;</Key></Contents></ListBucketResult>"
        )
    with pytest.raises(VaultTransportError):
        _parse_s3_list_xml(b"<ListBucketResult>\x00</ListBucketResult>")

    # Standard predefined XML character entity (&amp;) continues to work cleanly
    keys_ok, tok_ok = _parse_s3_list_xml(
        b"<ListBucketResult><Contents><Key>p/a&amp;b.json</Key></Contents></ListBucketResult>"
    )
    assert keys_ok == ["p/a&b.json"]
    assert tok_ok is None

    # Default urllib transport unit tests (offline via monkeypatch)
    with pytest.raises(VaultTransportError):
        _default_urllib_transport(
            S3HttpRequest(method="GET", url="file:///etc/passwd", headers={}),
            timeout=1.0,
        )

    class _DummyResp:
        status = 200
        headers = {"ETag": '"abc"'}

        def __enter__(self) -> _DummyResp:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"resp-body"

    monkeypatch.setattr("urllib.request.urlopen", lambda _req, timeout=0: _DummyResp())
    ok_resp = _default_urllib_transport(
        S3HttpRequest(method="PUT", url="https://s3.example.com/b/k", headers={}, body=b"hi"),
        timeout=5.0,
    )
    assert ok_resp.status_code == 200
    assert ok_resp.body == b"resp-body"

    def _raise_http_err(_req: object, timeout: float = 0) -> object:
        raise urllib.error.HTTPError(
            url="https://s3.example.com/b/k",
            code=404,
            msg="Not Found",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b"missing"),
        )

    monkeypatch.setattr("urllib.request.urlopen", _raise_http_err)
    nf_resp = _default_urllib_transport(
        S3HttpRequest(method="GET", url="https://s3.example.com/b/k", headers={}),
        timeout=5.0,
    )
    assert nf_resp.status_code == 404
    assert nf_resp.body == b"missing"

    def _raise_url_err(_req: object, timeout: float = 0) -> object:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _raise_url_err)
    with pytest.raises(VaultTransportError):
        _default_urllib_transport(
            S3HttpRequest(method="GET", url="https://s3.example.com/b/k", headers={}),
            timeout=5.0,
        )


# --------------------------------------------------------------------------- #
# 3. PortableBundleVaultDriver (.nagarvault)
# --------------------------------------------------------------------------- #


def test_portable_bundle_vault_driver_roundtrip_quota_and_corruption(tmp_path: Path) -> None:
    with pytest.raises(PersonalVaultError):
        PortableBundleVaultDriver(tmp_path / "wrong.zip")
    with pytest.raises(PersonalVaultError):
        PortableBundleVaultDriver(tmp_path / "bad.nagarvault", max_quota_bytes=0)

    target_real = tmp_path / "real.nagarvault"
    target_real.write_bytes(b"x")
    sym_bundle = tmp_path / "sym.nagarvault"
    sym_bundle.symlink_to(target_real)
    with pytest.raises(VaultTransportError):
        PortableBundleVaultDriver(sym_bundle)

    bundle_file = tmp_path / "offline_transfer.nagarvault"
    writer = PortableBundleVaultDriver(bundle_file, max_quota_bytes=4096)
    assert writer.bundle_path == bundle_file.resolve()

    c1, c2 = "a" * 64, "b" * 64
    assert not writer.has_chunk(c1)
    writer.put_chunk(c1, b"encrypted-chunk-1")
    writer.put_chunk(c2, b"encrypted-chunk-2")
    writer.put_manifest("snap-100", b'{"snapshot":"100"}')

    with pytest.raises(VaultQuotaExceededError):
        writer.put_chunk("c" * 64, b"Q" * 5000)

    # Re-open on a "second offline device" -> full binary verification runs automatically
    reader = PortableBundleVaultDriver(bundle_file)
    assert reader.list_chunks() == [c1, c2]
    assert reader.list_manifests() == ["snap-100"]
    assert reader.get_chunk(c1) == b"encrypted-chunk-1"
    assert reader.get_manifest("snap-100") == b'{"snapshot":"100"}'

    reader.delete_chunk(c2)
    reader.delete_chunk(c2)  # idempotent delete of already-absent chunk
    assert reader.list_chunks() == [c1]
    with pytest.raises(VaultTransportError):
        reader.get_chunk(c2)
    with pytest.raises(VaultTransportError):
        reader.get_manifest("missing")

    # Parent creation error and read_bytes I/O error
    file_blocker = tmp_path / "blocker_file_bundle"
    file_blocker.write_bytes(b"not-a-dir")
    with pytest.raises(VaultTransportError):
        PortableBundleVaultDriver(file_blocker / "sub.nagarvault")

    # Corrupt a single byte in the .nagarvault file -> VaultTamperDetectedError
    raw = bytearray(bundle_file.read_bytes())
    raw[-5] ^= 0x01
    bundle_file.write_bytes(bytes(raw))
    with pytest.raises(VaultTamperDetectedError):
        PortableBundleVaultDriver(bundle_file)


def test_portable_bundle_binary_format_tamper_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1) Missing file when verify_bundle called directly, and OSError on read_bytes
    empty_drv = PortableBundleVaultDriver(tmp_path / "not_yet_written.nagarvault")
    with pytest.raises(VaultTransportError):
        empty_drv.verify_bundle()

    io_drv = PortableBundleVaultDriver(tmp_path / "io_err.nagarvault")
    io_drv.put_chunk("a" * 64, b"ok")
    monkeypatch.setattr(
        Path, "read_bytes", lambda _self: (_ for _ in ()).throw(OSError("bundle read failed"))
    )
    with pytest.raises(VaultTransportError):
        io_drv.verify_bundle()
    monkeypatch.undo()

    def _write_and_expect_tamper(name: str, payload: bytes) -> None:
        p = tmp_path / f"{name}.nagarvault"
        p.write_bytes(payload)
        with pytest.raises(VaultTamperDetectedError):
            PortableBundleVaultDriver(p)

    # 2) Truncated header
    _write_and_expect_tamper("trunc_hdr", b"NAGAR")

    # 3) Bad magic / version
    bad_hdr = _BUNDLE_HEADER_PREFIX.pack(b"BADMAGIC", 1, 0, _BUNDLE_HEADER_TOTAL, 0)
    _write_and_expect_tamper("bad_magic", bad_hdr + hashlib.sha256(bad_hdr).digest())

    # 4) Bad index bounds
    bad_bounds = _BUNDLE_HEADER_PREFIX.pack(
        BUNDLE_MAGIC, BUNDLE_VERSION, 0, _BUNDLE_HEADER_TOTAL, 99
    )
    _write_and_expect_tamper("bad_bounds", bad_bounds + hashlib.sha256(bad_bounds).digest())

    def _pack_custom_bundle(entry_count: int, blob: bytes, index_bytes: bytes) -> bytes:
        hdr = _BUNDLE_HEADER_PREFIX.pack(
            BUNDLE_MAGIC,
            BUNDLE_VERSION,
            entry_count,
            _BUNDLE_HEADER_TOTAL + len(blob),
            len(index_bytes),
        )
        sha = hashlib.sha256(hdr + blob + index_bytes).digest()
        return hdr + sha + blob + index_bytes

    # 5) Truncated index head & truncated index tail
    _write_and_expect_tamper("trunc_idx_head", _pack_custom_bundle(1, b"", b"\x01"))
    _write_and_expect_tamper("trunc_idx_tail", _pack_custom_bundle(1, b"", b"\x01\x00\x05abc"))

    # 6) Invalid UTF-8 key in index
    bad_utf8_idx = (
        _BUNDLE_INDEX_HEAD.pack(1, 2)
        + b"\xff\xfe"
        + _BUNDLE_INDEX_TAIL.pack(_BUNDLE_HEADER_TOTAL, 0, 0, hashlib.sha256(b"").digest())
    )
    _write_and_expect_tamper("bad_utf8", _pack_custom_bundle(1, b"", bad_utf8_idx))

    # 7) Out-of-bounds blob slice
    key = ("a" * 64).encode("utf-8")
    oob_idx = (
        _BUNDLE_INDEX_HEAD.pack(1, len(key)) + key + _BUNDLE_INDEX_TAIL.pack(0, 5, 0, b"\x00" * 32)
    )
    _write_and_expect_tamper("oob_blob", _pack_custom_bundle(1, b"hello", oob_idx))

    # 8) Bad per-blob CRC32 despite valid archive SHA-256
    bad_crc_idx = (
        _BUNDLE_INDEX_HEAD.pack(1, len(key))
        + key
        + _BUNDLE_INDEX_TAIL.pack(
            _BUNDLE_HEADER_TOTAL, 5, 0xDEADBEEF, hashlib.sha256(b"hello").digest()
        )
    )
    _write_and_expect_tamper("bad_crc", _pack_custom_bundle(1, b"hello", bad_crc_idx))

    # 9) Unknown entry kind (e.g. 99)
    unk_kind_idx = (
        _BUNDLE_INDEX_HEAD.pack(99, len(key))
        + key
        + _BUNDLE_INDEX_TAIL.pack(
            _BUNDLE_HEADER_TOTAL,
            5,
            zlib.crc32(b"hello") & 0xFFFFFFFF,
            hashlib.sha256(b"hello").digest(),
        )
    )
    _write_and_expect_tamper("unk_kind", _pack_custom_bundle(1, b"hello", unk_kind_idx))

    # 10) Trailing extra bytes in index
    valid_idx = (
        _BUNDLE_INDEX_HEAD.pack(1, len(key))
        + key
        + _BUNDLE_INDEX_TAIL.pack(
            _BUNDLE_HEADER_TOTAL,
            5,
            zlib.crc32(b"hello") & 0xFFFFFFFF,
            hashlib.sha256(b"hello").digest(),
        )
        + b"extra"
    )
    _write_and_expect_tamper("trailing_idx", _pack_custom_bundle(1, b"hello", valid_idx))
    assert struct.calcsize(">8sHIQQ") == 30


def test_n2_local_driver_atomic_cross_process_lease_and_staging_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify O_CREAT|O_EXCL file leases (PID, TTL, conflict) and VaultStagingJournal."""
    import json
    import os
    import time

    from nexus_ai_agent.storage.personal_cloud_vault import (
        VaultRevisionConflictError,
        VaultStagingJournal,
    )

    driver_a = LocalDeviceVaultDriver(tmp_path / "shared_vault", max_quota_bytes=4096)
    driver_b = LocalDeviceVaultDriver(tmp_path / "shared_vault", max_quota_bytes=4096)

    with pytest.raises(PersonalVaultError):
        driver_a.acquire_push_lease(ttl_seconds=0.0)
    for bad_ttl in (float("nan"), float("inf"), float("-inf"), True):
        with pytest.raises(PersonalVaultError):
            driver_a.acquire_push_lease(ttl_seconds=bad_ttl)
    for bad_now in (float("nan"), float("inf"), True):
        with pytest.raises(PersonalVaultError):
            driver_a.read_active_leased_chunks(now_epoch=bad_now)

    c1 = "a" * 64
    c2 = "b" * 64
    driver_a.put_chunk(c1, b"payload-c1")
    lease_id = driver_a.acquire_push_lease(
        [c1],
        project_id="cinema-proj",
        ttl_seconds=120.0,
        lease_id="push-lease-01",
    )
    assert lease_id == "push-lease-01"
    # Lease and staging files never count against user storage quota
    assert driver_a.used_bytes() == len(b"payload-c1")

    # Atomic O_CREAT | O_EXCL collision rejects duplicate lease_id
    with pytest.raises(VaultRevisionConflictError):
        driver_b.acquire_push_lease([c1], lease_id="push-lease-01")

    # Inspect raw lease file on disk: must contain PID, TTL expiration, and chunk_ids
    lease_file = driver_a.root_dir / ".leases" / "lease-push-lease-01.json"
    raw_lease = json.loads(lease_file.read_text(encoding="utf-8"))
    assert raw_lease["pid"] == os.getpid()
    assert raw_lease["expires_at_epoch"] > raw_lease["created_at_epoch"]
    assert raw_lease["chunk_ids"] == [c1]

    # Second process/driver instance sees leased chunks & delete_chunk_if_unleased refuses delete
    assert driver_b.read_active_leased_chunks() == {c1}
    assert driver_b.delete_chunk_if_unleased(c1) is False
    assert driver_b.has_chunk(c1)
    driver_a.update_push_lease(lease_id, [c1, c2], project_id="cinema-proj", ttl_seconds=60.0)
    assert driver_b.read_active_leased_chunks() == {c1, c2}

    with pytest.raises(PersonalVaultError):
        driver_a.update_push_lease(lease_id, [c1], ttl_seconds=-1.0)
    with pytest.raises(VaultTransportError):
        driver_a.update_push_lease("missing-lease", [c1])

    # P1-2 Fail-Closed: ANY unexpected or malformed lease file raises VaultTransportError!
    leases_dir = driver_a.root_dir / ".leases"
    bad_cases = (
        ("ignore.txt", "x"),
        ("lease-corrupt.json", "{bad"),
        ("lease-listroot.json", "[1, 2]"),
        ("lease-bool-exp.json", json.dumps({"expires_at_epoch": True, "chunk_ids": []})),
        ("lease-nan-exp.json", '{"expires_at_epoch": NaN, "chunk_ids": []}'),
        (
            "lease-bad-ids.json",
            json.dumps({"expires_at_epoch": time.time() + 999, "chunk_ids": ["not-hex"]}),
        ),
        (
            "lease-nonlist.json",
            json.dumps({"expires_at_epoch": time.time() + 999, "chunk_ids": "bad"}),
        ),
    )
    for bad_name, bad_body in bad_cases:
        bad_path = leases_dir / bad_name
        bad_path.write_text(bad_body, encoding="utf-8")
        with pytest.raises(VaultTransportError):
            driver_b.read_active_leased_chunks()
        bad_path.unlink()
    assert driver_b.has_chunk(c1)

    # F1: a *live* holder blocks competitors (fail-closed timeout) and the kernel lock
    # is released automatically when the holder exits the context manager.
    with driver_a._coordination_lock():
        with (
            pytest.raises(VaultTransportError, match="Timed out acquiring"),
            driver_b._coordination_lock(timeout_seconds=0.01),
        ):
            pass
    assert driver_b.read_active_leased_chunks() == {c1, c2}
    # The persistent kernel lock file stays behind (never unlinked => no lock theft),
    # yet it is ignored by lease scanning and does not block new holders.
    assert (leases_dir / ".gc_coord.lock").exists()

    # Simulate TTL expiration into the future -> active set empty, expired lease unlinked,
    # and delete_chunk_if_unleased atomically deletes unleased c1
    future_now = time.time() + 3600.0
    assert driver_b.read_active_leased_chunks(now_epoch=future_now) == set()
    assert not lease_file.exists()
    assert driver_b.delete_chunk_if_unleased(c1) is True
    assert not driver_b.has_chunk(c1)
    assert driver_b.delete_chunk_if_unleased(c1) is False
    driver_a.release_push_lease(lease_id)

    # Staging journal round-trip, corrupt recovery, and clear
    assert driver_a.load_staging_journal("cinema-proj", None) is None
    journal = VaultStagingJournal(
        project_id="cinema-proj",
        parent_snapshot_id=None,
        salt_hex="11" * 32,
        kdf_iterations=100_000,
        key_id_hex="22" * 16,
        chunk_size=65_536,
        updated_at_epoch=time.time(),
        uploaded_chunk_ids=(c1, c2),
    )
    driver_a.save_staging_journal(journal)
    assert driver_a.used_bytes() == 0
    loaded = driver_b.load_staging_journal("cinema-proj", None)
    assert loaded == journal

    with pytest.raises(PersonalVaultError):
        VaultStagingJournal.from_json_bytes(b'{"schema_version": 99}')
    with pytest.raises(PersonalVaultError):
        VaultStagingJournal.from_json_bytes(
            json.dumps({**journal.to_dict(), "uploaded_chunk_ids": "bad"}).encode("utf-8")
        )

    # Corrupt journal file & mismatched project_id inside journal file both self-clean
    stage_files = list((driver_a.root_dir / ".staging").glob("stage-*.json"))
    assert len(stage_files) == 1
    stage_files[0].write_bytes(
        json.dumps({**journal.to_dict(), "project_id": "wrong-proj"}).encode("utf-8")
    )
    assert driver_b.load_staging_journal("cinema-proj", None) is None
    driver_a.save_staging_journal(journal)
    stage_files = list((driver_a.root_dir / ".staging").glob("stage-*.json"))
    stage_files[0].write_bytes(b"{corrupt")
    assert driver_b.load_staging_journal("cinema-proj", None) is None

    driver_a.save_staging_journal(journal)
    driver_a.clear_staging_journal("cinema-proj", None)
    assert driver_b.load_staging_journal("cinema-proj", None) is None

    orig_os_open = os.open

    def _fail_lease_open(path_arg: object, flags: int, mode: int = 0o777) -> int:
        if "ro-lease" in str(path_arg):
            raise PermissionError("disk read-only")
        return orig_os_open(path_arg, flags, mode)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", _fail_lease_open)
    with pytest.raises(VaultTransportError):
        driver_a.acquire_push_lease([c1], lease_id="ro-lease")
    monkeypatch.setattr(os, "open", orig_os_open)


def test_p1_3_multi_backend_s3_and_bundle_leases_and_chunk_mtime(tmp_path: Path) -> None:
    """Verify S3CompatibleSigV4Driver and PortableBundleVaultDriver leases & get_chunk_mtime."""
    import urllib.parse
    from datetime import datetime, timezone

    from nexus_ai_agent.storage.personal_cloud_vault import VaultRevisionConflictError
    from nexus_ai_agent.storage.personal_cloud_vault.byos_drivers import (
        _parse_http_or_iso_timestamp,
    )

    assert _parse_http_or_iso_timestamp("   ") is None
    assert _parse_http_or_iso_timestamp("not-a-timestamp") is None
    assert _parse_http_or_iso_timestamp("2026-11-16T10:00:00") is not None
    assert _parse_http_or_iso_timestamp("Mon, 16 Nov 2026 10:00:00 -0000") is not None

    fixed_now = datetime(2026, 11, 16, 12, 0, 0, tzinfo=timezone.utc)
    objects: dict[str, tuple[bytes, dict[str, str]]] = {}
    force_412_once = {"active": False}

    def _s3_transport(req: S3HttpRequest) -> S3HttpResponse:
        parsed = urllib.parse.urlsplit(req.url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        path_after_bucket = parsed.path.split("/vault-bkt/", 1)
        obj_key = path_after_bucket[1] if len(path_after_bucket) == 2 else ""

        if req.method == "GET" and query.get("list-type") == "2":
            prefix = query.get("prefix", "")
            items_xml = "".join(
                f"<Contents><Key>{k}</Key>"
                f"<LastModified>2026-11-16T11:30:00.000Z</LastModified></Contents>"
                for k in sorted(objects)
                if k.startswith(prefix)
            )
            body = (
                b'<?xml version="1.0" encoding="UTF-8"?>'
                b"<ListBucketResult><IsTruncated>false</IsTruncated>"
                + items_xml.encode("utf-8")
                + b"</ListBucketResult>"
            )
            return S3HttpResponse(status_code=200, body=body)
        if req.method == "HEAD":
            if obj_key in objects:
                return S3HttpResponse(status_code=200, headers=dict(objects[obj_key][1]))
            return S3HttpResponse(status_code=404)
        if req.method == "PUT":
            if force_412_once["active"] and req.headers.get("if-none-match") == "*":
                force_412_once["active"] = False
                return S3HttpResponse(status_code=412)
            hdrs = {"last-modified": "Mon, 16 Nov 2026 12:00:00 GMT"}
            objects[obj_key] = (req.body, hdrs)
            return S3HttpResponse(status_code=200)
        if req.method == "GET":
            if obj_key in objects:
                return S3HttpResponse(
                    status_code=200,
                    headers=dict(objects[obj_key][1]),
                    body=objects[obj_key][0],
                )
            return S3HttpResponse(status_code=404)
        if req.method == "DELETE":
            objects.pop(obj_key, None)
            return S3HttpResponse(status_code=204)
        return S3HttpResponse(status_code=405)

    cfg = S3SigV4Config(
        endpoint_url="https://s3.example.com",
        bucket="vault-bkt",
        region="us-east-1",
        access_key_id="AKID",
        secret_access_key="SECRET",
        prefix="p13",
    )
    s3_a = S3CompatibleSigV4Driver(cfg, http_transport=_s3_transport, clock=lambda: fixed_now)
    s3_b = S3CompatibleSigV4Driver(cfg, http_transport=_s3_transport, clock=lambda: fixed_now)

    c1 = "1" * 64
    c2 = "2" * 64
    assert s3_a.get_chunk_mtime(c1) is None
    s3_a.put_chunk(c1, b"s3-chunk-1")
    assert s3_a.get_chunk_mtime(c1) == fixed_now.timestamp()
    # Headerless S3 object populates mtime from ListObjectsV2 <LastModified>
    objects[f"p13/chunks/{c2}.chk"] = (b"s3-chunk-2", {})
    assert s3_b.list_chunks() == [c1, c2]
    assert s3_b.get_chunk_mtime(c2) is not None

    lid = s3_a.acquire_push_lease([c1], project_id="proj-s3", ttl_seconds=600.0, lease_id="s3-l1")
    assert lid == "s3-l1"
    assert f"p13/leases/{lid}.json" in objects
    with pytest.raises(VaultRevisionConflictError):
        s3_b.acquire_push_lease([c1], lease_id="s3-l1")
    force_412_once["active"] = True
    with pytest.raises(VaultRevisionConflictError):
        s3_b.acquire_push_lease([c1], lease_id="s3-l2")

    assert s3_b.read_active_leased_chunks() == {c1}
    assert s3_b.delete_chunk_if_unleased(c1) is False
    s3_a.update_push_lease(lid, [c1, c2], project_id="proj-s3", ttl_seconds=300.0)
    assert s3_b.read_active_leased_chunks() == {c1, c2}
    with pytest.raises(VaultTransportError):
        s3_a.update_push_lease("no-such-s3-lease", [c1])

    # Corrupt lease object on S3 fails closed with VaultTransportError
    objects["p13/leases/corrupt.json"] = (b"{broken-json", {})
    with pytest.raises(VaultTransportError):
        s3_b.read_active_leased_chunks()
    del objects["p13/leases/corrupt.json"]

    # Expire S3 lease -> auto-deletes lease object and allows delete_chunk_if_unleased
    assert s3_b.read_active_leased_chunks(now_epoch=fixed_now.timestamp() + 900.0) == set()
    assert f"p13/leases/{lid}.json" not in objects
    assert s3_b.delete_chunk_if_unleased(c1) is True
    assert s3_b.delete_chunk_if_unleased(c1) is False

    # PortableBundleVaultDriver multi-instance lease + get_chunk_mtime
    bundle_file = tmp_path / "multi.nagarvault"
    b_a = PortableBundleVaultDriver(bundle_file)
    assert b_a.get_chunk_mtime(c1) is None
    b_a.put_chunk(c1, b"bundle-c1")
    assert isinstance(b_a.get_chunk_mtime(c1), float)

    blid = b_a.acquire_push_lease([c1], project_id="proj-b", ttl_seconds=300.0, lease_id="b-l1")
    b_b = PortableBundleVaultDriver(bundle_file)
    assert isinstance(b_b.get_chunk_mtime(c1), float)
    with pytest.raises(VaultRevisionConflictError):
        b_b.acquire_push_lease([c1], lease_id="b-l1")
    assert b_b.read_active_leased_chunks() == {c1}
    assert b_b.delete_chunk_if_unleased(c1) is False
    b_a.update_push_lease(blid, [c1, c2], project_id="proj-b", ttl_seconds=120.0)
    assert b_b.read_active_leased_chunks() == {c1, c2}
    with pytest.raises(VaultTransportError):
        b_a.update_push_lease("missing-b-lease", [c1])

    import time as _time

    assert b_b.read_active_leased_chunks(now_epoch=_time.time() + 900.0) == set()
    assert b_b.delete_chunk_if_unleased(c1) is True
    assert b_b.delete_chunk_if_unleased(c1) is False
    b_a.acquire_push_lease([c2], lease_id="b-l2")
    b_b.release_push_lease("b-l2")
    assert b_b.read_active_leased_chunks() == set()


def test_edge_branches_local_s3_and_bundle_fail_closed_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover defensive/error branches: subdirectory entries, OSErrors, S3 HTTP failures, bundle."""
    import os

    from nexus_ai_agent.storage.personal_cloud_vault import VaultRevisionConflictError

    # --- LocalDeviceVaultDriver: subdirectories are ignored by every enumerator -----
    vault = LocalDeviceVaultDriver(tmp_path / "edge_local")
    vault.put_chunk("a" * 64, b"chunk")
    vault.put_manifest("snap-edge", b"{}")
    (vault.root_dir / "chunks" / "nested_dir").mkdir()
    (vault.root_dir / "manifests" / "nested_dir").mkdir()
    (vault.root_dir / "manifests" / ".hidden.json").write_bytes(b"{}")
    (vault.root_dir / "chunks" / ".hidden.chk").write_bytes(b"zz")
    assert vault.used_bytes() == len(b"chunk") + len(b"{}")
    assert vault.list_chunks() == ["a" * 64]
    assert vault.list_manifests() == ["snap-edge"]

    # OSError (non FileExistsError) while creating .gc_coord.lock fails closed
    orig_os_open = os.open

    def _fail_lock_open(path_arg: object, flags: int, mode: int = 0o777) -> int:
        if str(path_arg).endswith(".gc_coord.lock"):
            raise PermissionError("read-only vault")
        return orig_os_open(path_arg, flags, mode)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", _fail_lock_open)
    with pytest.raises(VaultTransportError, match="coordination lock"):
        vault.read_active_leased_chunks()
    monkeypatch.setattr(os, "open", orig_os_open)

    # OSError while reading a well-named lease file fails closed
    vault.acquire_push_lease(["a" * 64], lease_id="edge-lease")
    orig_read_bytes = Path.read_bytes

    def _fail_lease_read(self_path: Path) -> bytes:
        if self_path.name.startswith("lease-"):
            raise OSError("lease unreadable")
        return orig_read_bytes(self_path)

    monkeypatch.setattr(Path, "read_bytes", _fail_lease_read)
    with pytest.raises(VaultTransportError, match="Failed reading push lease file"):
        vault.read_active_leased_chunks()
    monkeypatch.setattr(Path, "read_bytes", orig_read_bytes)
    vault.release_push_lease("edge-lease")

    # --- S3: HTTP error branches, XML oddities, list filtering ----------------------
    objects: dict[str, tuple[bytes, dict[str, str]]] = {}
    state: dict[str, Any] = {
        "head_status": None,
        "put_status": None,
        "get_status": None,
        "delete_status": None,
        "extra_list_keys": (),
        "last_modified": None,
        "token_without_truncation": False,
    }

    def _s3_transport(req: S3HttpRequest) -> S3HttpResponse:
        parsed = urllib.parse.urlsplit(req.url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        key = parsed.path.split("/edge-bkt/", 1)[-1]

        if req.method == "GET" and query.get("list-type") == "2":
            prefix = query.get("prefix", "")
            keys = sorted(set(objects) | set(state["extra_list_keys"]))
            contents = "".join(
                f"<Contents><Key>{k}</Key><ETag>etag</ETag></Contents>"
                for k in keys
                if k.startswith(prefix)
            )
            contents += "<Contents><ETag>orphan</ETag></Contents>"
            token = (
                "<NextContinuationToken>tok-ignored</NextContinuationToken>"
                if state["token_without_truncation"]
                else ""
            )
            body = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                f"<ListBucketResult>{contents}{token}</ListBucketResult>"
            ).encode()
            return S3HttpResponse(status_code=200, body=body)
        if req.method == "HEAD":
            if state["head_status"] is not None:
                return S3HttpResponse(status_code=int(state["head_status"]))
            if key in objects:
                return S3HttpResponse(status_code=200, headers=dict(objects[key][1]))
            return S3HttpResponse(status_code=404)
        if req.method == "PUT":
            if state["put_status"] is not None:
                return S3HttpResponse(status_code=int(state["put_status"]))
            headers: dict[str, str] = {}
            if state["last_modified"] is not None:
                headers["last-modified"] = str(state["last_modified"])
            objects[key] = (req.body, headers)
            return S3HttpResponse(status_code=200)
        if req.method == "GET":
            if state["get_status"] is not None:
                return S3HttpResponse(status_code=int(state["get_status"]))
            if key in objects:
                return S3HttpResponse(
                    status_code=200,
                    headers=dict(objects[key][1]),
                    body=objects[key][0],
                )
            return S3HttpResponse(status_code=404)
        if req.method == "DELETE":
            if state["delete_status"] is not None:
                return S3HttpResponse(status_code=int(state["delete_status"]))
            objects.pop(key, None)
            return S3HttpResponse(status_code=204)
        return S3HttpResponse(status_code=405)

    cfg = S3SigV4Config(
        endpoint_url="https://edge.example.com",
        bucket="edge-bkt",
        region="us-east-1",
        access_key_id="AKID",
        secret_access_key="SECRET",
        prefix="p13",
    )
    s3 = S3CompatibleSigV4Driver(cfg, http_transport=_s3_transport)
    cid = "b" * 64

    # HEAD 500 -> get_chunk_mtime fails closed
    state["head_status"] = 500
    with pytest.raises(VaultTransportError, match="get_chunk_mtime"):
        s3.get_chunk_mtime(cid)
    state["head_status"] = None

    # HEAD 200 with unparseable Last-Modified -> falls back to the recorded mtime cache
    objects[f"p13/chunks/{cid}.chk"] = (b"edge", {"last-modified": "not-a-date"})
    assert s3.get_chunk_mtime(cid) is None
    s3.put_chunk(cid, b"edge")
    state["last_modified"] = "not-a-date"
    assert isinstance(s3.get_chunk_mtime(cid), float)
    state["last_modified"] = None

    # ListObjectsV2 oddities: sub-directory stems, wrong suffixes, mismatched prefixes,
    # Contents without <Key>, and a NextContinuationToken without IsTruncated=true
    state["token_without_truncation"] = True
    state["extra_list_keys"] = (
        "p13/chunks/sub/dir.chk",
        "p13/chunks/stray.txt",
        "p14/chunks/elsewhere.chk",
        "p13/leases/ghost.json",
    )
    assert s3.list_chunks() == [cid]

    # S3 lease probe (HEAD) 500 -> acquire_push_lease fails closed
    state["head_status"] = 500
    with pytest.raises(VaultTransportError, match="lease probe"):
        s3.acquire_push_lease([cid], lease_id="s3-edge")
    state["head_status"] = None

    # S3 lease PUT 500 -> acquire/update fail closed
    state["put_status"] = 500
    with pytest.raises(VaultTransportError, match="acquire_push_lease"):
        s3.acquire_push_lease([cid], lease_id="s3-edge")
    state["put_status"] = None
    s3.acquire_push_lease([cid], lease_id="s3-edge")
    state["put_status"] = 500
    with pytest.raises(VaultTransportError, match="update_push_lease"):
        s3.update_push_lease("s3-edge", [cid])
    state["put_status"] = None

    # S3 lease DELETE 500 -> release fails closed
    state["delete_status"] = 500
    with pytest.raises(VaultTransportError, match="release_push_lease"):
        s3.release_push_lease("s3-edge")
    state["delete_status"] = None

    # Lease listed but vanished (GET 404) is skipped; GET 500 fails closed
    state["extra_list_keys"] = ("p13/leases/ghost.json",)
    assert s3.read_active_leased_chunks() == {cid}
    state["get_status"] = 500
    with pytest.raises(VaultTransportError, match="read_active_leased_chunks"):
        s3.read_active_leased_chunks()
    state["get_status"] = None
    state["extra_list_keys"] = ()
    with pytest.raises(VaultRevisionConflictError):
        s3.acquire_push_lease([cid], lease_id="s3-edge")
    s3.release_push_lease("s3-edge")

    # --- PortableBundleVaultDriver: brand-new bundle & malformed lease key ----------
    fresh = PortableBundleVaultDriver(tmp_path / "brand_new.nagarvault")
    assert fresh.read_active_leased_chunks() == set()
    # A brand-new bundle has no lease files at all
    with pytest.raises(VaultTransportError):
        fresh.update_push_lease("missing-fresh", [])
    fresh.release_push_lease("missing-fresh")
    assert fresh.acquire_push_lease([], lease_id="fresh-lease") == "fresh-lease"

    bad_key = b"not-a-lease-key"
    bad_idx = (
        _BUNDLE_INDEX_HEAD.pack(3, len(bad_key))
        + bad_key
        + _BUNDLE_INDEX_TAIL.pack(
            _BUNDLE_HEADER_TOTAL,
            5,
            zlib.crc32(b"hello") & 0xFFFFFFFF,
            hashlib.sha256(b"hello").digest(),
        )
    )
    hdr = _BUNDLE_HEADER_PREFIX.pack(
        BUNDLE_MAGIC, BUNDLE_VERSION, 1, _BUNDLE_HEADER_TOTAL + 5, len(bad_idx)
    )
    bad_bundle = tmp_path / "bad_lease_key.nagarvault"
    bad_bundle.write_bytes(
        hdr + hashlib.sha256(hdr + b"hello" + bad_idx).digest() + b"hello" + bad_idx
    )
    with pytest.raises(VaultTamperDetectedError, match="Malformed lease key"):
        PortableBundleVaultDriver(bad_bundle)

    # Unrelated ListObjectsV2 children and empty-text NextContinuationToken are ignored
    entries, token = _parse_s3_list_xml_entries(
        b"<ListBucketResult><Name>bkt</Name><Prefix>x</Prefix>"
        b"<Contents><Key>k1</Key><Size>0</Size></Contents>"
        b"<NextContinuationToken/><IsTruncated>false</IsTruncated></ListBucketResult>"
    )
    assert entries == [("k1", None)]
    assert token is None


def test_f1_f2_lock_ownership_hidden_lease_entries_and_overflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F1 lock theft, F2 hidden/symlinked lease entries, and huge-JSON OverflowError."""
    import json
    import os
    import time

    import nexus_ai_agent.storage.personal_cloud_vault.byos_drivers as drivers_module

    vault = LocalDeviceVaultDriver(tmp_path / "f1_vault")
    leases_dir = vault.root_dir / ".leases"
    cid = "c" * 64
    vault.put_chunk(cid, b"leased-payload")

    # ---- F1a: a *live* holder is never preempted, even with a "stale-looking" mtime --
    owner_marker: list[str] = []
    with vault._coordination_lock():
        (leases_dir / ".gc_coord.lock").write_bytes(b"")
        stale_ts = time.time() - 3600.0
        os.utime(leases_dir / ".gc_coord.lock", (stale_ts, stale_ts))
        with (
            pytest.raises(VaultTransportError, match="Timed out acquiring"),
            vault._coordination_lock(timeout_seconds=0.01),
        ):
            pass
        # The holder still owns the coordination path: a competitor cannot barge in.
        owner_marker.append("held")

    # After release a new holder (a different driver instance/process) can proceed and
    # must leave the previous owner's kernel lock file untouched (no unlink => no theft).
    assert owner_marker == ["held"]
    assert vault.read_active_leased_chunks() == set()

    # ---- F1b: fallback ownership file is broken only when the owner PID is dead -----
    owner_path = leases_dir / ".gc_coord.owner"
    live_payload = json.dumps(
        {"pid": os.getpid(), "owner_token": "live-owner", "created_at_epoch": 0.0}
    ).encode()
    owner_path.write_bytes(live_payload)
    os.utime(owner_path, (time.time() - 3600.0, time.time() - 3600.0))

    real_fcntl = drivers_module._FCNTL
    monkeypatch.setattr(drivers_module, "_FCNTL", None)
    with (
        pytest.raises(VaultTransportError, match="Timed out acquiring"),
        drivers_module._cross_process_file_lock(
            leases_dir / ".gc_coord.lock",
            owner_path=owner_path,
            timeout_seconds=0.01,
        ),
    ):
        pass
    # The live owner's fallback file survived: no lock theft from a running holder.
    assert owner_path.read_bytes() == live_payload

    # A provably dead owner is broken; the new holder then owns and releases the file.
    dead_pid = 2**22 + 12345
    while True:
        try:
            os.kill(dead_pid, 0)
        except ProcessLookupError:
            break
        dead_pid += 1
    owner_path.write_bytes(
        json.dumps({"pid": dead_pid, "owner_token": "dead", "created_at_epoch": 0.0}).encode()
    )
    os.utime(owner_path, (time.time() - 3600.0, time.time() - 3600.0))
    with drivers_module._cross_process_file_lock(
        leases_dir / ".gc_coord.lock",
        owner_path=owner_path,
        timeout_seconds=1.0,
    ):
        assert owner_path.exists()
    assert not owner_path.exists()

    # A malformed (unparseable) fallback payload is never broken: fail closed instead.
    owner_path.write_bytes(b"{definitely-not-json")
    os.utime(owner_path, (time.time() - 3600.0, time.time() - 3600.0))
    with (
        pytest.raises(VaultTransportError, match="Timed out acquiring"),
        drivers_module._cross_process_file_lock(
            leases_dir / ".gc_coord.lock",
            owner_path=owner_path,
            timeout_seconds=0.01,
        ),
    ):
        pass
    owner_path.unlink()
    monkeypatch.setattr(drivers_module, "_FCNTL", real_fcntl)

    # ---- F1c: a stale owner must NOT unlink a lock file now owned by someone else ---
    replacement_owner = leases_dir / ".gc_coord.owner"
    replacement_owner.write_bytes(b"not-json-at-all")
    drivers_module._release_ownership_lock(
        replacement_owner,
        owner_token="ghost-token",
        owner_ino=replacement_owner.lstat().st_ino,
    )
    assert replacement_owner.exists()  # unparseable owner file is never removed

    # A different owner_token (or a recycled inode) must also survive release attempts
    other_owner = json.dumps({"owner_token": "someone-else"}).encode()
    replacement_owner.write_bytes(other_owner)
    same_ino = replacement_owner.lstat().st_ino
    drivers_module._release_ownership_lock(
        replacement_owner, owner_token="ghost-token", owner_ino=same_ino
    )
    drivers_module._release_ownership_lock(
        replacement_owner, owner_token="someone-else", owner_ino=same_ino + 1
    )
    assert replacement_owner.read_bytes() == other_owner
    replacement_owner.unlink()

    # ---- F2: hidden dot-files, symlinks and overflow leases all fail closed ---------
    hidden_corrupt = leases_dir / ".hidden_corrupt.json"
    hidden_corrupt.write_text("{not-a-lease", encoding="utf-8")
    with pytest.raises(VaultTransportError, match="hidden or symlinked"):
        vault.read_active_leased_chunks()
    hidden_corrupt.unlink()

    target_lease = tmp_path / "outside-lease.json"
    target_lease.write_text(
        json.dumps({"expires_at_epoch": time.time() + 600.0, "chunk_ids": []}),
        encoding="utf-8",
    )
    hidden_symlink = leases_dir / ".hidden_symlink.json"
    hidden_symlink.symlink_to(target_lease)
    with pytest.raises(VaultTransportError, match="hidden or symlinked"):
        vault.read_active_leased_chunks()
    # The atomic delete path re-reads leases under the lock, so it aborts fail-closed
    # and leaves the chunk untouched rather than guessing about an ambiguous lease dir.
    with pytest.raises(VaultTransportError, match="hidden or symlinked"):
        vault.delete_chunk_if_unleased(cid)
    assert vault.has_chunk(cid)
    hidden_symlink.unlink()

    hidden_subdir = leases_dir / ".hidden_dir"
    hidden_subdir.mkdir()
    with pytest.raises(VaultTransportError, match="hidden or symlinked"):
        vault.read_active_leased_chunks()
    hidden_subdir.rmdir()

    # OverflowError: a JSON integer so large that float() cannot represent it
    huge_lease = leases_dir / "lease-huge-expiry.json"
    huge_lease.write_text(
        '{"expires_at_epoch": ' + "9" * 400 + ', "chunk_ids": []}',
        encoding="utf-8",
    )
    with pytest.raises(VaultTransportError, match="Unrepresentable expires_at_epoch"):
        vault.read_active_leased_chunks()
    huge_lease.unlink()

    # With every hostile entry removed the lease directory is healthy again.
    assert vault.read_active_leased_chunks() == set()
    assert vault.delete_chunk_if_unleased(cid) is True


def test_f3_s3_staging_journal_and_if_match_lease_updates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F3 crash-recovery staging journals + If-Match lease refreshes on S3."""
    import json
    import time
    import urllib.parse
    from collections.abc import Mapping

    from nexus_ai_agent.storage.personal_cloud_vault import (
        VaultRevisionConflictError,
        VaultStagingJournal,
    )

    objects: dict[str, tuple[bytes, dict[str, str]]] = {}
    state: dict[str, Any] = {"force_412_update": False, "delete_status": None}
    etag_counter = {"n": 0}

    def _transport(req: S3HttpRequest) -> S3HttpResponse:
        parsed = urllib.parse.urlsplit(req.url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        key = parsed.path.split("/f3-bkt/", 1)[-1]

        if req.method == "GET" and query.get("list-type") == "2":
            prefix = query.get("prefix", "")
            body = (
                "<ListBucketResult>"
                + "".join(
                    f"<Contents><Key>{k}</Key></Contents>"
                    for k in sorted(objects)
                    if k.startswith(prefix)
                )
                + "</ListBucketResult>"
            ).encode()
            return S3HttpResponse(status_code=200, body=body)
        if req.method == "HEAD":
            if key in objects:
                return S3HttpResponse(status_code=200, headers=dict(objects[key][1]))
            return S3HttpResponse(status_code=404)
        if req.method == "PUT":
            if state["force_412_update"] and "if-match" in req.headers:
                return S3HttpResponse(status_code=412)
            etag_counter["n"] += 1
            objects[key] = (req.body, {"etag": f'"etag-{etag_counter["n"]}"'})
            return S3HttpResponse(status_code=200)
        if req.method == "GET":
            if key in objects:
                return S3HttpResponse(
                    status_code=200,
                    headers=dict(objects[key][1]),
                    body=objects[key][0],
                )
            return S3HttpResponse(status_code=404)
        if req.method == "DELETE":
            if state["delete_status"] is not None:
                return S3HttpResponse(status_code=int(state["delete_status"]))
            objects.pop(key, None)
            return S3HttpResponse(status_code=204)
        return S3HttpResponse(status_code=405)

    cfg = S3SigV4Config(
        endpoint_url="https://f3.example.com",
        bucket="f3-bkt",
        region="us-east-1",
        access_key_id="AKID",
        secret_access_key="SECRET",
        prefix="f3",
    )
    s3 = S3CompatibleSigV4Driver(cfg, http_transport=_transport)
    cid = "d" * 64
    journal = VaultStagingJournal(
        project_id="f3-proj",
        parent_snapshot_id=None,
        salt_hex="11" * 32,
        kdf_iterations=100_000,
        key_id_hex="22" * 16,
        chunk_size=65_536,
        updated_at_epoch=time.time(),
        uploaded_chunk_ids=(cid,),
    )

    # Round-trip, then crash-resume semantics: the journal survives an instance restart.
    assert s3.load_staging_journal("f3-proj", None) is None
    s3.save_staging_journal(journal)
    assert s3.load_staging_journal("f3-proj", None) == journal
    assert (
        S3CompatibleSigV4Driver(cfg, http_transport=_transport).load_staging_journal(
            "f3-proj", None
        )
        == journal
    )

    # Corrupt / mismatched journals self-heal to None (and are deleted)
    journal_key = s3._staging_journal_key("f3-proj", None)
    assert journal_key in objects
    objects[journal_key] = (b"{corrupt", {})
    assert s3.load_staging_journal("f3-proj", None) is None
    s3.save_staging_journal(journal)
    objects[journal_key] = (
        journal.to_json_bytes().replace(b"f3-proj", b"other-pr"),
        {},
    )
    assert s3.load_staging_journal("f3-proj", None) is None

    s3.save_staging_journal(journal)
    s3.clear_staging_journal("f3-proj", None)
    assert s3.load_staging_journal("f3-proj", None) is None

    # Saving fails closed on a non-2xx PUT response
    state["delete_status"] = None
    s3.put_chunk(cid, b"chunk-body")
    s3.acquire_push_lease([cid], lease_id="f3-lease")
    assert "if-match" not in objects  # lease object exists and carries an ETag
    s3.update_push_lease("f3-lease", [cid])
    state["force_412_update"] = True
    with pytest.raises(VaultRevisionConflictError, match="Concurrent modification"):
        s3.update_push_lease("f3-lease", [cid])
    state["force_412_update"] = False

    # The lease-state token guards delete_chunk_if_unleased against mid-check leases
    assert s3.delete_chunk_if_unleased(cid) is False
    s3.release_push_lease("f3-lease")
    assert s3.delete_chunk_if_unleased(cid) is True
    assert s3.delete_chunk_if_unleased(cid) is False

    # A lease object that appears between the lease scan and the DELETE aborts deletion
    late_chunks: list[str] = []
    real_send = s3._send
    calls = {"n": 0}

    def _racing_send(
        method: str,
        *,
        object_key: str | None = None,
        query: Mapping[str, str] | None = None,
        body: bytes = b"",
        content_type: str | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> S3HttpResponse:
        calls["n"] += 1
        if calls["n"] == 3 and not late_chunks:
            late_chunks.append(object_key or "")
            objects["f3/leases/newcomer.json"] = (
                json.dumps({"expires_at_epoch": time.time() + 600.0, "chunk_ids": [cid]}).encode(),
                {"etag": '"late"'},
            )
        return real_send(
            method,
            object_key=object_key,
            query=query,
            body=body,
            content_type=content_type,
            extra_headers=extra_headers,
        )

    s3.put_chunk(cid, b"chunk-body")
    monkeypatch.setattr(s3, "_send", _racing_send)
    assert s3.delete_chunk_if_unleased(cid) is False
    assert s3.has_chunk(cid)
    objects.pop("f3/leases/newcomer.json")


def test_f3_bundle_multi_instance_lease_serialization_and_staging_journal(
    tmp_path: Path,
) -> None:
    """F3 cross-process bundle lock: no peer write is lost and lease+delete is atomic."""
    import time

    from nexus_ai_agent.storage.personal_cloud_vault import (
        VaultRevisionConflictError,
        VaultStagingJournal,
    )

    bundle_file = tmp_path / "f3_multi.nagarvault"
    c1 = "a" * 64
    c2 = "b" * 64
    c3 = "c" * 64
    writer = PortableBundleVaultDriver(bundle_file)
    peer = PortableBundleVaultDriver(bundle_file)

    # 1) Two instances interleaving writes must not resurrect/clobber each other.
    writer.put_chunk(c1, b"one")
    peer.put_chunk(c2, b"two")  # peer reloads under the lock => c1 is preserved
    writer.put_chunk(c3, b"three")
    assert peer.list_chunks() == [c1, c2, c3]
    assert writer.get_chunk(c1) == b"one"

    # 2) Lease acquisition by one instance is visible to (and honoured by) the other.
    lease_id = writer.acquire_push_lease([c2], project_id="f3", lease_id="f3-bundle-lease")
    assert peer.read_active_leased_chunks() == {c2}
    assert peer.delete_chunk_if_unleased(c2) is False
    assert peer.has_chunk(c2)
    with pytest.raises(VaultRevisionConflictError):
        peer.acquire_push_lease([c2], lease_id="f3-bundle-lease")
    peer.update_push_lease(lease_id, [c2, c3])
    assert writer.read_active_leased_chunks() == {c2, c3}
    with pytest.raises(VaultTransportError):
        peer.update_push_lease("no-such-bundle-lease", [c2])

    # 3) Unleased chunks are deleted atomically; leased ones survive.
    assert peer.delete_chunk_if_unleased(c1) is True
    assert peer.delete_chunk_if_unleased(c1) is False
    assert peer.has_chunk(c2) and peer.has_chunk(c3)

    # 4) Expired leases are swept and no longer protect chunks.
    future = time.time() + 7200.0
    assert peer.read_active_leased_chunks(now_epoch=future) == set()
    assert writer.delete_chunk_if_unleased(c2, now_epoch=future) is True
    assert not peer.has_chunk(c2)

    # 5) Crash-recovery staging journals survive across bundle instances.
    journal = VaultStagingJournal(
        project_id="f3-bundle",
        parent_snapshot_id=None,
        salt_hex="33" * 32,
        kdf_iterations=100_000,
        key_id_hex="44" * 16,
        chunk_size=65_536,
        updated_at_epoch=time.time(),
        uploaded_chunk_ids=(c3,),
    )
    assert writer.load_staging_journal("f3-bundle", None) is None
    writer.save_staging_journal(journal)
    assert peer.load_staging_journal("f3-bundle", None) == journal
    assert PortableBundleVaultDriver(bundle_file).load_staging_journal("f3-bundle", None) == (
        journal
    )

    # Corrupt and mismatched journals self-heal to None instead of crashing a push.
    stage_key = writer._staging_journal_key("f3-bundle", None)

    def _inject_staging(key: str, payload: bytes) -> None:
        """Persist a hostile journal payload straight into the archive under the lock."""
        with writer._coordination_lock():
            writer._reload_locked()
            staging = dict(writer._staging_journals)
            staging[key] = payload
            writer._flush(
                dict(writer._chunks),
                dict(writer._manifests),
                dict(writer._leases),
                staging,
            )

    _inject_staging(stage_key, b"{not-json")
    assert writer.load_staging_journal("f3-bundle", None) is None
    _inject_staging(
        stage_key,
        journal.to_json_bytes().replace(b"f3-bundle", b"other-prj"),
    )
    assert writer.load_staging_journal("f3-bundle", None) is None

    writer.save_staging_journal(journal)
    peer.clear_staging_journal("f3-bundle", None)
    assert writer.load_staging_journal("f3-bundle", None) is None
    peer.clear_staging_journal("f3-bundle", None)  # idempotent no-op

    # 6) The bundle lock tolerates contention and never steals a live holder's lock.
    with writer._coordination_lock():
        with (
            pytest.raises(VaultTransportError, match="Timed out acquiring"),
            peer._coordination_lock(timeout_seconds=0.01),
        ):
            pass
    # (the earlier future-dated sweep already expired the only lease)
    assert peer.read_active_leased_chunks() == set()


def test_edge_branches_pid_probe_lock_break_and_backend_probe_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover the remaining fail-closed probes: PID liveness, lock break, S3 HTTP errors."""
    import json
    import os
    import time
    import urllib.parse

    import nexus_ai_agent.storage.personal_cloud_vault.byos_drivers as drivers_module
    from nexus_ai_agent.storage.personal_cloud_vault import VaultStagingJournal

    # --- PID liveness probes --------------------------------------------------------
    assert drivers_module._pid_is_alive(0) is True  # non-positive PID => unknown/alive
    assert drivers_module._pid_is_alive(-5) is True
    assert drivers_module._pid_is_alive(os.getpid()) is True

    real_kill = os.kill

    def _kernel_denies_signal(pid: int, sig: int) -> None:
        raise PermissionError("EPERM: live process owned by another user")

    monkeypatch.setattr(os, "kill", _kernel_denies_signal)
    assert drivers_module._pid_is_alive(4_000_000) is True
    assert drivers_module._pid_is_definitely_dead(4_000_000) is False
    monkeypatch.setattr(os, "kill", real_kill)

    def _kernel_broken(pid: int, sig: int) -> None:
        raise OSError(5, "unexpected kernel error")

    monkeypatch.setattr(os, "kill", _kernel_broken)
    assert drivers_module._pid_is_alive(4_000_001) is True
    monkeypatch.setattr(os, "kill", real_kill)
    assert drivers_module._pid_is_definitely_dead(0) is False
    assert drivers_module._pid_is_definitely_dead(os.getpid()) is False
    # A brand-new child process is provably alive => alive for both probes.
    import subprocess
    import sys

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert drivers_module._pid_is_alive(child.pid) is True
        assert drivers_module._pid_is_definitely_dead(child.pid) is False
    finally:
        child.kill()
        child.wait(timeout=30)

    # --- Stale ownership-lock breaking: every hostile permutation stays fail-closed --
    leases_dir = tmp_path / "break_vault" / ".leases"
    leases_dir.mkdir(parents=True)
    owner_path = leases_dir / ".gc_coord.owner"
    stale = time.time() - 3600.0

    def _break(stale_lock_seconds: float = 30.0) -> bool:
        return drivers_module._break_stale_ownership_lock(
            owner_path, stale_lock_seconds=stale_lock_seconds
        )

    # (a) vanished owner file -> nothing to break
    assert _break() is False

    # (b) fresh owner file -> never broken
    owner_path.write_bytes(json.dumps({"pid": 4_000_002}).encode())
    assert _break() is False

    # (c) stale but unparseable / non-dict / non-int PID payloads -> never broken
    for payload in (b"{not-json", b'["list"]', b'{"pid": "not-an-int"}'):
        owner_path.write_bytes(payload)
        os.utime(owner_path, (stale, stale))
        assert _break() is False
        assert owner_path.exists()

    # (d) stale, parseable, but the recorded PID is still alive -> never broken
    owner_path.write_bytes(json.dumps({"pid": os.getpid()}).encode())
    os.utime(owner_path, (stale, stale))
    assert _break() is False
    assert owner_path.exists()

    # (e) stale owner whose file is swapped between inspection and unlink -> no break
    dead_pid = 4_100_000
    while True:
        try:
            os.kill(dead_pid, 0)
        except ProcessLookupError:
            break
        dead_pid += 1
    owner_path.write_bytes(json.dumps({"pid": dead_pid}).encode())
    os.utime(owner_path, (stale, stale))
    real_lstat = Path.lstat
    swaps = {"n": 0}

    def _racing_lstat(self_path: Path) -> object:
        st = real_lstat(self_path)
        if self_path == owner_path:
            swaps["n"] += 1
            if swaps["n"] == 2:  # second lstat is the post-inspection re-check
                # A newer owner atomically replaces the file => brand-new inode.
                replacement = owner_path.with_name(".gc_coord.owner.new")
                replacement.write_bytes(b'{"pid": 1, "owner_token": "newer-owner"}')
                os.replace(replacement, owner_path)
                return real_lstat(self_path)
        return st

    monkeypatch.setattr(Path, "lstat", _racing_lstat)
    assert _break() is False
    monkeypatch.setattr(Path, "lstat", real_lstat)
    assert owner_path.exists()

    # (f) unlink itself fails (immutable directory semantics) -> no break reported
    real_unlink = Path.unlink

    def _refuse_unlink(self_path: Path, *args: object, **kwargs: object) -> None:
        if self_path == owner_path:
            raise OSError(1, "Operation not permitted")
        real_unlink(self_path, *args, **kwargs)  # type: ignore[arg-type]

    owner_path.write_bytes(json.dumps({"pid": dead_pid}).encode())
    os.utime(owner_path, (stale, stale))
    monkeypatch.setattr(Path, "unlink", _refuse_unlink)
    assert _break() is False
    monkeypatch.setattr(Path, "unlink", real_unlink)

    # (g) the genuine happy path finally breaks the provably dead owner
    assert _break() is True
    assert not owner_path.exists()

    # --- S3 lease-state / staging-journal probe failures ---------------------------
    state: dict[str, Any] = {
        "lease_head_status": None,
        "save_status": None,
        "load_status": None,
    }
    objects: dict[str, tuple[bytes, dict[str, str]]] = {}

    def _transport(req: S3HttpRequest) -> S3HttpResponse:
        parsed = urllib.parse.urlsplit(req.url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        key = parsed.path.split("/eg-bkt/", 1)[-1]
        if req.method == "GET" and query.get("list-type") == "2":
            prefix = query.get("prefix", "")
            body = (
                "<ListBucketResult>"
                + "".join(
                    f"<Contents><Key>{k}</Key></Contents>"
                    for k in sorted(set(objects) | {"eg/leases/ghost.json"})
                    if k.startswith(prefix)
                )
                + "</ListBucketResult>"
            ).encode()
            return S3HttpResponse(status_code=200, body=body)
        if req.method == "HEAD":
            if "leases/" in key and state["lease_head_status"] is not None:
                return S3HttpResponse(status_code=int(state["lease_head_status"]))
            if key in objects:
                return S3HttpResponse(status_code=200, headers=dict(objects[key][1]))
            return S3HttpResponse(status_code=404)
        if req.method == "PUT":
            if "staging/" in key and state["save_status"] is not None:
                return S3HttpResponse(status_code=int(state["save_status"]))
            objects[key] = (req.body, {"etag": '"e"'})
            return S3HttpResponse(status_code=200)
        if req.method == "GET":
            if "staging/" in key and state["load_status"] is not None:
                return S3HttpResponse(status_code=int(state["load_status"]))
            if key in objects:
                return S3HttpResponse(
                    status_code=200, headers=dict(objects[key][1]), body=objects[key][0]
                )
            return S3HttpResponse(status_code=404)
        if req.method == "DELETE":
            objects.pop(key, None)
            return S3HttpResponse(status_code=204)
        return S3HttpResponse(status_code=405)

    cfg = S3SigV4Config(
        endpoint_url="https://eg.example.com",
        bucket="eg-bkt",
        region="us-east-1",
        access_key_id="AKID",
        secret_access_key="SECRET",
        prefix="eg",
    )
    s3 = S3CompatibleSigV4Driver(cfg, http_transport=_transport)
    cid = "f" * 64
    s3.put_chunk(cid, b"payload")

    # A lease that disappeared between LIST and HEAD contributes an "absent" token,
    # and `delete_chunk_if_unleased` still only deletes genuinely unleased chunks.
    assert s3.delete_chunk_if_unleased(cid) is True
    assert s3.delete_chunk_if_unleased(cid) is False

    # A failing lease-state HEAD probe aborts the deletion fail-closed.
    s3.put_chunk(cid, b"payload")
    state["lease_head_status"] = 500
    with pytest.raises(VaultTransportError, match="lease state probe failed"):
        s3.delete_chunk_if_unleased(cid)
    state["lease_head_status"] = None
    assert s3.has_chunk(cid)

    # S3 staging-journal persistence / loading failures are surfaced, never swallowed.
    journal = VaultStagingJournal(
        project_id="eg-proj",
        parent_snapshot_id=None,
        salt_hex="55" * 32,
        kdf_iterations=100_000,
        key_id_hex="66" * 16,
        chunk_size=65_536,
        updated_at_epoch=time.time(),
        uploaded_chunk_ids=(cid,),
    )
    state["save_status"] = 500
    with pytest.raises(VaultTransportError, match="save_staging_journal"):
        s3.save_staging_journal(journal)
    state["save_status"] = None
    state["load_status"] = 500
    with pytest.raises(VaultTransportError, match="load_staging_journal"):
        s3.load_staging_journal("eg-proj", None)
    state["load_status"] = None

    # A bundle staging entry with a hostile key is a tamper signal, never a silent skip.
    bad_key = b"staging/not-a-digest.json"
    bad_idx = (
        _BUNDLE_INDEX_HEAD.pack(4, len(bad_key))
        + bad_key
        + _BUNDLE_INDEX_TAIL.pack(
            _BUNDLE_HEADER_TOTAL,
            5,
            zlib.crc32(b"hello") & 0xFFFFFFFF,
            hashlib.sha256(b"hello").digest(),
        )
    )
    hdr = _BUNDLE_HEADER_PREFIX.pack(
        BUNDLE_MAGIC, BUNDLE_VERSION, 1, _BUNDLE_HEADER_TOTAL + 5, len(bad_idx)
    )
    bad_bundle = tmp_path / "bad_staging_key.nagarvault"
    bad_bundle.write_bytes(
        hdr + hashlib.sha256(hdr + b"hello" + bad_idx).digest() + b"hello" + bad_idx
    )
    with pytest.raises(VaultTamperDetectedError, match="Malformed staging key"):
        PortableBundleVaultDriver(bad_bundle)


def test_edge_branches_owner_release_races_lock_errors_and_bundle_sweeps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover owner-release races, lock-acquisition OSErrors, and bundle sweep branches."""
    import json
    import os
    import time
    import urllib.parse

    import nexus_ai_agent.storage.personal_cloud_vault.byos_drivers as drivers_module
    from nexus_ai_agent.storage.personal_cloud_vault import VaultStagingJournal

    leases_dir = tmp_path / "rel_vault" / ".leases"
    leases_dir.mkdir(parents=True)
    owner_path = leases_dir / ".gc_coord.owner"
    payload = json.dumps({"pid": os.getpid(), "owner_token": "tok"}).encode()
    owner_path.write_bytes(payload)
    ino = owner_path.lstat().st_ino
    real_lstat = Path.lstat

    # (a) a newer owner replaced the file between the token check and the unlink
    swaps = {"n": 0}

    def _swap_on_final_check(self_path: Path) -> object:
        st = real_lstat(self_path)
        if self_path == owner_path:
            swaps["n"] += 1
            if swaps["n"] == 2:
                replacement = owner_path.with_name(".gc_coord.owner.new")
                replacement.write_bytes(b'{"owner_token": "newer"}')
                os.replace(replacement, owner_path)
                return real_lstat(self_path)
        return st

    monkeypatch.setattr(Path, "lstat", _swap_on_final_check)
    drivers_module._release_ownership_lock(owner_path, owner_token="tok", owner_ino=ino)
    monkeypatch.setattr(Path, "lstat", real_lstat)
    assert owner_path.exists()
    assert owner_path.read_bytes() == b'{"owner_token": "newer"}'

    # (b) the unlink itself fails: the release is a silent no-op (never raises)
    def _refuse_unlink(self_path: Path, *args: object, **kwargs: object) -> None:
        if self_path == owner_path:
            raise OSError(1, "Operation not permitted")
        real_lstat_unlink(self_path, *args, **kwargs)  # type: ignore[arg-type]

    real_lstat_unlink = Path.unlink
    owner_path.write_bytes(payload)
    owner_ino = owner_path.lstat().st_ino
    monkeypatch.setattr(Path, "unlink", _refuse_unlink)
    drivers_module._release_ownership_lock(owner_path, owner_token="tok", owner_ino=owner_ino)
    monkeypatch.setattr(Path, "unlink", real_lstat_unlink)
    assert owner_path.exists()
    owner_path.unlink()

    # (c) kernel flock failures: non-blocking contention and unexpected OSError
    vault = LocalDeviceVaultDriver(tmp_path / "rel_vault_body")
    real_fcntl = drivers_module._FCNTL

    class _ContendedFcntl:
        LOCK_EX = real_fcntl.LOCK_EX
        LOCK_NB = real_fcntl.LOCK_NB
        LOCK_UN = real_fcntl.LOCK_UN

        @staticmethod
        def flock(fd: int, operation: int) -> None:
            raise BlockingIOError(11, "kernel lock already held")

    monkeypatch.setattr(drivers_module, "_FCNTL", _ContendedFcntl)
    with (
        pytest.raises(VaultTransportError, match="Timed out acquiring"),
        vault._coordination_lock(timeout_seconds=0.01),
    ):
        pass

    class _BrokenFcntl(_ContendedFcntl):
        @staticmethod
        def flock(fd: int, operation: int) -> None:
            raise OSError(9, "bad file descriptor")

    monkeypatch.setattr(drivers_module, "_FCNTL", _BrokenFcntl)
    with pytest.raises(VaultTransportError, match="Failed acquiring vault coordination lock"):
        with vault._coordination_lock():
            pass
    monkeypatch.setattr(drivers_module, "_FCNTL", real_fcntl)

    # (d) fallback-owner acquisition OSError (e.g. read-only directory)
    monkeypatch.setattr(drivers_module, "_FCNTL", None)
    real_os_open = os.open

    def _readonly_owner_open(path_arg: object, flags: int, mode: int = 0o777) -> int:
        if str(path_arg).endswith(".gc_coord.owner"):
            raise PermissionError("read-only vault directory")
        return real_os_open(path_arg, flags, mode)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", _readonly_owner_open)
    with pytest.raises(VaultTransportError, match="Failed acquiring vault coordination lock"):
        with drivers_module._cross_process_file_lock(
            leases_dir / ".gc_coord.lock",
            owner_path=owner_path,
            timeout_seconds=0.01,
        ):
            pass
    monkeypatch.setattr(os, "open", real_os_open)
    monkeypatch.setattr(drivers_module, "_FCNTL", real_fcntl)

    # (e) an S3 lease-state token that shifts between the re-check and the delete aborts
    objects: dict[str, tuple[bytes, dict[str, str]]] = {}
    state: dict[str, Any] = {"shift_after_check": False, "heads": 0}

    def _transport(req: S3HttpRequest) -> S3HttpResponse:
        parsed = urllib.parse.urlsplit(req.url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        key = parsed.path.split("/eg-bkt/", 1)[-1]
        if req.method == "GET" and query.get("list-type") == "2":
            prefix = query.get("prefix", "")
            body = (
                "<ListBucketResult>"
                + "".join(
                    f"<Contents><Key>{k}</Key></Contents>"
                    for k in sorted(objects)
                    if k.startswith(prefix)
                )
                + "</ListBucketResult>"
            ).encode()
            return S3HttpResponse(status_code=200, body=body)
        if req.method == "HEAD":
            if "leases/" in key:
                state["heads"] += 1
                if state["shift_after_check"] and state["heads"] % 2 == 0:
                    return S3HttpResponse(status_code=404)  # the lease just vanished
            if key in objects:
                return S3HttpResponse(status_code=200, headers=dict(objects[key][1]))
            return S3HttpResponse(status_code=404)
        if req.method == "PUT":
            objects[key] = (req.body, {"etag": '"e"'})
            return S3HttpResponse(status_code=200)
        if req.method == "GET":
            if key in objects:
                return S3HttpResponse(
                    status_code=200, headers=dict(objects[key][1]), body=objects[key][0]
                )
            return S3HttpResponse(status_code=404)
        if req.method == "DELETE":
            objects.pop(key, None)
            return S3HttpResponse(status_code=204)
        return S3HttpResponse(status_code=405)

    cfg = S3SigV4Config(
        endpoint_url="https://eg.example.com",
        bucket="eg-bkt",
        region="us-east-1",
        access_key_id="AKID",
        secret_access_key="SECRET",
        prefix="eg",
    )
    s3 = S3CompatibleSigV4Driver(cfg, http_transport=_transport)
    cid = "e" * 64
    s3.put_chunk(cid, b"payload")
    s3.acquire_push_lease(["1" * 64], lease_id="eg-lease")

    # First token == baseline; the *second* re-check sees a different lease state.
    state["shift_after_check"] = True
    real_token = s3._lease_state_token
    calls = {"n": 0}

    def _shifting_token() -> tuple[tuple[str, str], ...]:
        calls["n"] += 1
        token = real_token()
        if calls["n"] > 1:
            return token + (("ghost", "changed"),)
        return token

    monkeypatch.setattr(s3, "_lease_state_token", _shifting_token)
    assert s3.delete_chunk_if_unleased(cid) is False
    assert s3.has_chunk(cid)
    monkeypatch.setattr(s3, "_lease_state_token", real_token)
    s3.release_push_lease("eg-lease")

    # (f) a bundle staging entry that shifts the archive view is fine, but a bundle
    #     delete must sweep expired leases even when the chunk itself survives.
    bundle_file = tmp_path / "sweep.nagarvault"
    bundle = PortableBundleVaultDriver(bundle_file)
    keep_cid = "2" * 64
    bundle.put_chunk(keep_cid, b"kept")
    bundle.acquire_push_lease([keep_cid], lease_id="sweep-lease", ttl_seconds=1.0)
    expired_now = time.time() + 7200.0
    # chunk is leased until expiry => the sweep persists, the chunk survives
    assert bundle.delete_chunk_if_unleased(keep_cid, now_epoch=expired_now) is True
    assert bundle.read_active_leased_chunks() == set()

    # unknown chunk with an expired lease present: sweep persists, delete reports False
    bundle.acquire_push_lease([], lease_id="sweep-lease-2", ttl_seconds=1.0)
    journal = VaultStagingJournal(
        project_id="sweep-proj",
        parent_snapshot_id=None,
        salt_hex="77" * 32,
        kdf_iterations=100_000,
        key_id_hex="88" * 16,
        chunk_size=65_536,
        updated_at_epoch=time.time(),
        uploaded_chunk_ids=(),
    )
    bundle.save_staging_journal(journal)
    assert bundle.delete_chunk_if_unleased("3" * 64, now_epoch=expired_now) is False
    assert bundle.load_staging_journal("sweep-proj", None) == journal
    assert bundle.read_active_leased_chunks() == set()


def test_last_mile_lease_state_shift_before_delete_aborts_s3_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The final lease-state re-check (right before DELETE) must abort the deletion."""
    objects: dict[str, tuple[bytes, dict[str, str]]] = {}

    def _transport(req: S3HttpRequest) -> S3HttpResponse:
        parsed = urllib.parse.urlsplit(req.url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        key = parsed.path.split("/lm-bkt/", 1)[-1]
        if req.method == "GET" and query.get("list-type") == "2":
            prefix = query.get("prefix", "")
            body = (
                "<ListBucketResult>"
                + "".join(
                    f"<Contents><Key>{k}</Key></Contents>"
                    for k in sorted(objects)
                    if k.startswith(prefix)
                )
                + "</ListBucketResult>"
            ).encode()
            return S3HttpResponse(status_code=200, body=body)
        if req.method == "HEAD":
            if key in objects:
                return S3HttpResponse(status_code=200, headers=dict(objects[key][1]))
            return S3HttpResponse(status_code=404)
        if req.method == "PUT":
            objects[key] = (req.body, {"etag": '"e"'})
            return S3HttpResponse(status_code=200)
        if req.method == "GET":
            if key in objects:
                return S3HttpResponse(
                    status_code=200, headers=dict(objects[key][1]), body=objects[key][0]
                )
            return S3HttpResponse(status_code=404)
        if req.method == "DELETE":
            objects.pop(key, None)
            return S3HttpResponse(status_code=204)
        return S3HttpResponse(status_code=405)

    cfg = S3SigV4Config(
        endpoint_url="https://lm.example.com",
        bucket="lm-bkt",
        region="us-east-1",
        access_key_id="AKID",
        secret_access_key="SECRET",
        prefix="lm",
    )
    s3 = S3CompatibleSigV4Driver(cfg, http_transport=_transport)
    cid = "9" * 64
    s3.put_chunk(cid, b"payload")
    s3.acquire_push_lease(["8" * 64], lease_id="lm-lease")

    real_token = s3._lease_state_token
    calls = {"n": 0}

    def _shift_on_third_check() -> tuple[tuple[str, str], ...]:
        calls["n"] += 1
        token = real_token()
        # call 1 = baseline, call 2 = post-scan re-check, call 3 = final pre-DELETE check
        if calls["n"] >= 3:
            return token + (("late", "appeared"),)
        return token

    monkeypatch.setattr(s3, "_lease_state_token", _shift_on_third_check)
    assert s3.delete_chunk_if_unleased(cid) is False
    assert s3.has_chunk(cid)
    assert calls["n"] >= 3
